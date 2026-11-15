"""Reads replies from leads.

The mailbox people reply to (the From or Reply-To address) is read over IMAP
in read-only mode, so nothing changes in it: messages stay unread in webmail.
Each new message is matched to the email it answers (by its In-Reply-To and
References headers, or else by the sender's address) and stored. Messages from
people who are not leads are ignored.
"""
import email
import email.policy
import email.utils
import imaplib
import logging
import re
import ssl
import time

from . import bounces, contacts, events, writer
from .db import now
from .render import html_to_text

log = logging.getLogger('campaigns.replies')

OUR_ID_RE = re.compile(r'<([0-9a-f]{32})@', re.IGNORECASE)
FIRST_SCAN_DAYS = 14
MATCH_WINDOW_DAYS = 120
OOO_SUBJECT_RE = re.compile(
    r'(?i)(out of (the )?office|automatic reply|auto[- ]?reply|autoreply|away from|on vacation|on holiday|'
    r'abwesenheit|absence|réponse automatique|respuesta automática|risposta automatica|afwezig)')
UNSUBSCRIBE_RE = re.compile(
    r"(?i)\b(unsubscribe|remove me|take me off|opt[ -]?out|stop (e-?mailing|emailing|sending|contacting)|"
    r"do not (e-?mail|email|contact)|don'?t (e-?mail|email|contact)|no more emails)\b")
QUOTE_START_RE = re.compile(
    r'(?im)^(on .{0,200}wrote:\s*$|-{2,} ?original message ?-{2,}|_{10,}\s*$|from: .+$|'
    r'le .{0,200}a écrit\s*:|am .{0,200}schrieb .{0,80}:|el .{0,200}escribió:)')
LEAD_STATUS_FROM_CATEGORY = {'interested': 'interested', 'meeting': 'meeting', 'question': 'replied',
                             'not_interested': 'not_interested', 'unsubscribe': 'not_interested'}


# -- parsing ----------------------------------------------------------------------
def _decode_part(part):
    try:
        return part.get_content()
    except (LookupError, UnicodeError, KeyError):
        payload = part.get_payload(decode=True) or b''
        return payload.decode('utf-8', 'replace')


def body_text(msg):
    """The new text of a reply, without the quoted earlier message."""
    plain, html = None, None
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart' or part.get_filename():
            continue
        ctype = part.get_content_type()
        if ctype == 'text/plain' and plain is None:
            plain = _decode_part(part)
        elif ctype == 'text/html' and html is None:
            html = _decode_part(part)
    text = plain if plain is not None else html_to_text(html or '')
    text = text.replace('\r\n', '\n')
    match = QUOTE_START_RE.search(text)
    fresh = text[:match.start()] if match else text
    fresh = '\n'.join(line for line in fresh.splitlines() if not line.lstrip().startswith('>'))
    fresh = re.sub(r'\n{3,}', '\n\n', fresh).strip()
    return (fresh or text.strip())[:20000]


def is_automatic(msg):
    auto = (msg.get('Auto-Submitted') or '').strip().lower()
    if auto and auto != 'no':
        return True
    if msg.get('X-Autoreply') or msg.get('X-Autorespond') or msg.get('X-Auto-Response-Suppress', '').lower() == 'all':
        return True
    if (msg.get('Precedence') or '').strip().lower() in ('auto_reply', 'bulk', 'junk', 'list'):
        return True
    return bool(OOO_SUBJECT_RE.search(msg.get('Subject') or ''))


def _received_at(msg):
    try:
        moment = email.utils.parsedate_to_datetime(msg.get('Date'))
        stamp = moment.timestamp()
        return stamp if stamp <= now() + 3600 else now()
    except (TypeError, ValueError, IndexError):
        return now()


def _own_addresses(db, settings, mailbox_user):
    own = {mailbox_user.lower()}
    for key in ('from_email', 'reply_to', 'bounce_address', 'imap_user'):
        if settings.get(key):
            own.add(settings[key].strip().lower())
    own.update(row['email'].lower() for row in db.q('SELECT email FROM mailboxes'))
    return own


def _find_message(db, msg, sender):
    """Our email this answers: by its headers, else the latest email to the sender."""
    refs = ' '.join(str(msg.get(h) or '') for h in ('In-Reply-To', 'References'))
    for mid in reversed(OUR_ID_RE.findall(refs)):
        row = db.one('SELECT * FROM messages WHERE id = ?', mid.lower())
        if row is not None:
            return row
    return db.one("SELECT * FROM messages WHERE to_email = ? AND sent_at IS NOT NULL AND sent_at >= ? "
                  "AND kind IN ('campaign', 'reply', 'agent', 'api', 'single') ORDER BY sent_at DESC LIMIT 1",
                  sender, now() - MATCH_WINDOW_DAYS * 86400)


# -- storing ----------------------------------------------------------------------
def process(db, settings, raw, mailbox, uid):
    """Handles one raw message from the reply mailbox. Returns what it was:
    'reply', 'auto', 'bounce', 'own', 'unknown' or 'duplicate'."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, sender = email.utils.parseaddr(str(msg.get('From') or ''))
    sender = (contacts.normalize_email(sender) or '').lower()
    if not sender or sender in _own_addresses(db, settings, mailbox):
        return 'own'
    report = bounces.parse_report(raw)
    if report is not None:
        bounces.apply_report(db, report)
        return 'bounce'
    if db.one('SELECT 1 FROM replies WHERE mailbox = ? AND uid = ?', mailbox, uid):
        return 'duplicate'

    message = _find_message(db, msg, sender)
    contact = None
    if message is not None and message['contact_id']:
        contact = db.one('SELECT * FROM contacts WHERE id = ?', message['contact_id'])
    if contact is None:
        contact = db.one('SELECT * FROM contacts WHERE email = ?', sender)
    if message is None and contact is None:
        return 'unknown'   # not a lead: leave it to webmail

    automatic = is_automatic(msg)
    text = body_text(msg)
    subject = ' '.join(str(msg.get('Subject') or '').split())[:300]
    category, summary = None, ''
    if automatic:
        category = 'out_of_office' if OOO_SUBJECT_RE.search(subject) else 'other'
        summary = 'Automatic reply'
    elif UNSUBSCRIBE_RE.search(subject) or UNSUBSCRIBE_RE.search(text[:400]):
        category, summary = 'unsubscribe', 'Asked to stop receiving emails'
    ts = now()
    with db.tx():
        reply_id = db.run(
            'INSERT OR IGNORE INTO replies (mailbox, uid, header_id, message_id, campaign_id, contact_id, from_email, '
            'from_name, subject, body, received_at, is_auto, category, summary, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            mailbox, uid, str(msg.get('Message-ID') or '').strip()[:300],
            message['id'] if message is not None else None,
            message['campaign_id'] if message is not None else None,
            contact['id'] if contact is not None else None,
            sender, name[:200], subject, text, _received_at(msg), 1 if automatic else 0, category, summary,
            ts).lastrowid
        if automatic:
            return 'auto'
        if message is not None:
            db.run('UPDATE messages SET replied_at = COALESCE(replied_at, ?) WHERE id = ?', ts, message['id'])
            events.record(db, message, 'replied', detail=subject, extra={'reply': {'id': reply_id}})
        if contact is not None:
            db.run("UPDATE contacts SET lead_status = 'replied', updated_at = ? WHERE id = ? "
                   "AND lead_status IN ('new', 'contacted')", ts, contact['id'])
            # The conversation has started: no more automatic follow-ups to this lead.
            db.run("UPDATE messages SET status = 'cancelled', error = 'Lead replied' WHERE contact_id = ? "
                   "AND kind = 'campaign' AND step > 0 AND status = 'queued'", contact['id'])
        if category == 'unsubscribe':
            unsubscribe_lead(db, sender, contact, message['id'] if message is not None else None, reply_id)
    return 'reply'


def unsubscribe_lead(db, address, contact=None, message_id=None, reply_id=None):
    contacts.suppress(db, address, 'unsubscribed', 'Asked in a reply')
    if message_id:
        db.run('UPDATE messages SET unsubscribed_at = COALESCE(unsubscribed_at, ?) WHERE id = ?', now(), message_id)
    if reply_id:
        db.run('UPDATE replies SET handled_at = COALESCE(handled_at, ?) WHERE id = ?', now(), reply_id)
    if contact is not None:
        db.run("UPDATE contacts SET lead_status = 'not_interested' WHERE id = ? "
               "AND lead_status IN ('new', 'contacted', 'replied')", contact['id'])


def apply_category(db, reply_id, category, summary=None):
    """Stores a category and moves the lead along the pipeline (never backwards
    from a status the operator set, such as won)."""
    reply = db.one('SELECT * FROM replies WHERE id = ?', reply_id)
    if reply is None:
        return
    db.run('UPDATE replies SET category = ?, summary = COALESCE(?, summary) WHERE id = ?', category, summary, reply_id)
    status = LEAD_STATUS_FROM_CATEGORY.get(category)
    if reply['contact_id'] and status:
        db.run("UPDATE contacts SET lead_status = ?, updated_at = ? WHERE id = ? "
               "AND lead_status IN ('new', 'contacted', 'replied')", status, now(), reply['contact_id'])
    if category == 'unsubscribe':
        contact = db.one('SELECT * FROM contacts WHERE id = ?', reply['contact_id']) if reply['contact_id'] else None
        unsubscribe_lead(db, reply['from_email'], contact, reply['message_id'], reply_id)


def classify_pending(db, cfg, settings, limit=10):
    """Lets the AI sort replies that have no category yet. Returns how many."""
    from . import ai
    if not ai.configured(settings):
        return 0
    done = 0
    for reply in db.q('SELECT * FROM replies WHERE category IS NULL AND is_auto = 0 ORDER BY id LIMIT ?', limit):
        try:
            result = writer.classify_reply(db, settings, reply, writer.sent_text_for(db, cfg, reply['message_id']))
        except (ai.ProviderError, ai.NotConfigured) as exc:
            db.set_state('inbox_error', str(exc))
            break
        apply_category(db, reply['id'], result['category'], result['summary'])
        done += 1
    else:
        db.set_state('inbox_error', '')
    return done


ANSWERABLE = ('interested', 'meeting', 'question')
REPLY_MODES = (('off', 'Off: I answer myself'),
               ('draft', 'Write a draft for me to check and send'),
               ('send', 'Answer by itself'))
PLACEHOLDER_RE = re.compile(r'\[[^\]]{2,80}\]')


def auto_answer(db, cfg, settings, limit=5):
    """The reply autopilot: for replies the AI sorted as interested, a meeting
    request or a question, write an answer; keep it as a draft, or send it
    (held for approval when the owner wants to approve sends). An answer that
    still needs a fact from the owner ([your calendar link]) stays a draft.
    Interested leads also get a follow-up task. Returns how many were handled."""
    from . import planner, service
    mode = settings.get('ai_reply_mode') or 'off'
    if mode not in ('draft', 'send'):
        return 0
    # Only fresh replies: turning the autopilot on must not answer old conversations.
    rows = db.q(f"SELECT * FROM replies WHERE drafted_at IS NULL AND handled_at IS NULL AND is_auto = 0 "
                f"AND received_at >= ? AND category IN ({', '.join('?' for _ in ANSWERABLE)}) "
                f"ORDER BY received_at LIMIT ?", now() - 3 * 86400, *ANSWERABLE, limit)
    done = 0
    for reply in rows:
        contact = db.one('SELECT * FROM contacts WHERE id = ?', reply['contact_id']) if reply['contact_id'] else None
        try:
            text = writer.draft_reply(db, settings, reply, contact, writer.sent_text_for(db, cfg, reply['message_id']))
        except Exception as exc:  # provider down or a bad answer: try again next pass
            db.set_state('inbox_error', f'Could not draft an answer: {exc}'[:300])
            break
        db.run('UPDATE replies SET draft = ?, drafted_at = ? WHERE id = ?', text, now(), reply['id'])
        if mode == 'send' and not PLACEHOLDER_RE.search(text):
            fresh = db.one('SELECT * FROM replies WHERE id = ?', reply['id'])
            try:
                service.reply_to_lead(db, cfg, settings, fresh, text,
                                      need_approval=settings.get('ai_require_approval') == '1')
                db.run("UPDATE replies SET draft = '' WHERE id = ?", reply['id'])
            except service.Problem as exc:
                db.set_state('inbox_error', f'The AI answer was kept as a draft: {exc}'[:300])
        if contact is not None and reply['category'] in ('interested', 'meeting') and not db.one(
                'SELECT 1 FROM tasks WHERE reply_id = ? AND done_at IS NULL', reply['id']):
            planner.create_task(db, f'Follow up with {reply["from_name"] or reply["from_email"]}', now() + 2 * 86400,
                                contact_id=contact['id'], reply_id=reply['id'],
                                note=f'They replied: {reply["summary"] or reply["subject"]}')
        done += 1
    return done


# -- the mailbox ------------------------------------------------------------------
def mailbox_settings(cfg, settings):
    user = (settings.get('reply_imap_user') or '').strip()
    return {
        'user': user,
        'password': settings.get('reply_imap_password') or '',
        'host': settings.get('reply_imap_host') or settings.get('imap_host') or cfg.IMAP_HOST,
        'port': int(settings.get('reply_imap_port') or settings.get('imap_port') or cfg.IMAP_PORT),
        'verify': settings.get('imap_verify_tls') == '1',
    }


def connect(box):
    context = ssl.create_default_context()
    if not box['verify']:
        # Inside the compose network the server is reached by its service name,
        # which never matches the public certificate.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if box['port'] == 143:
        conn = imaplib.IMAP4(box['host'], box['port'], timeout=30)
        conn.starttls(ssl_context=context)
    else:
        conn = imaplib.IMAP4_SSL(box['host'], box['port'], ssl_context=context, timeout=30)
    conn.login(box['user'], box['password'])
    return conn


def poll(db, cfg, settings, limit=200):
    """Reads new messages from the reply mailbox. Returns a result dict."""
    box = mailbox_settings(cfg, settings)
    if not box['user'] or not box['password']:
        return {'configured': False}
    return read_box(db, settings, box, limit)


def read_box(db, settings, box, limit=200):
    """Reads new messages from one inbox. Returns a result dict."""
    result = {'configured': True, 'seen': 0, 'replies': 0, 'auto': 0, 'bounces': 0}
    key = f'reply_uid:{box["user"]}'
    conn = None
    try:
        conn = connect(box)
        status, _ = conn.select('INBOX', readonly=True)
        if status != 'OK':
            raise imaplib.IMAP4.error('cannot open INBOX')
        validity = (conn.response('UIDVALIDITY')[1] or [b'0'])[0]
        validity = validity.decode() if isinstance(validity, bytes) else str(validity)
        known_validity, _, last = (db.state(key) or '').partition(':')
        last = int(last) if last.isdigit() and known_validity == validity else None
        if last is None:
            since = time.strftime('%d-%b-%Y', time.gmtime(now() - FIRST_SCAN_DAYS * 86400))
            status, data = conn.uid('SEARCH', None, 'SINCE', since)
            last = 0
        else:
            status, data = conn.uid('SEARCH', None, 'UID', f'{last + 1}:*')
        uids = sorted(int(u) for u in (data[0].split() if status == 'OK' and data and data[0] else []))
        uids = [u for u in uids if u > last][:limit]
        for uid in uids:
            status, parts = conn.uid('FETCH', str(uid), '(BODY.PEEK[])')
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None) if status == 'OK' else None
            if raw:
                result['seen'] += 1
                try:
                    kind = process(db, settings, raw, box['user'], f'{validity}:{uid}')
                except Exception:  # one odd message must not stop the rest
                    log.exception('could not process reply %s', uid)
                    kind = 'error'
                if kind == 'reply':
                    result['replies'] += 1
                elif kind == 'auto':
                    result['auto'] += 1
                elif kind == 'bounce':
                    result['bounces'] += 1
            db.set_state(key, f'{validity}:{uid}')
        if not uids and last == 0:
            db.set_state(key, f'{validity}:0')
        result['ok'] = True
    except (OSError, imaplib.IMAP4.error) as exc:
        result['ok'] = False
        result['error'] = f'{type(exc).__name__}: {exc}'[:300]
    finally:
        if conn is not None:
            try:
                conn.logout()
            except (OSError, imaplib.IMAP4.error):
                pass
    return result


def thread_for(db, contact_id, email_address, cfg=None):
    """Everything exchanged with one lead, oldest first: our emails and theirs.
    With cfg, our campaign emails carry the text the lead received."""
    items = []
    for row in db.q("SELECT m.*, c.name AS campaign_name FROM messages m LEFT JOIN campaigns c ON c.id = m.campaign_id "
                    "WHERE (m.contact_id = ? OR m.to_email = ?) AND m.kind != 'test' AND m.status != 'cancelled' "
                    "ORDER BY COALESCE(m.sent_at, m.created_at)", contact_id or -1, email_address):
        text = ''
        if cfg is not None:
            text = writer.sent_text_for(db, cfg, row['id'])
            if text.startswith('Subject:'):
                text = text.split('\n\n', 1)[-1]
            text = text.split('\n\n--\nUnsubscribe:')[0].strip()
        items.append({'kind': 'out', 'at': row['sent_at'] or row['created_at'], 'row': row, 'text': text})
    for row in db.q('SELECT * FROM replies WHERE contact_id = ? OR from_email = ? ORDER BY received_at',
                    contact_id or -1, email_address):
        items.append({'kind': 'in', 'at': row['received_at'], 'row': row})
    items.sort(key=lambda item: item['at'] or 0)
    return items
