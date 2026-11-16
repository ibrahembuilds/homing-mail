"""Reading the connected mailboxes: the "profiles" on the Inbox page.

Each profile is one mailbox Homing can sign in to: the reply mailbox from
Settings, and every email account with IMAP details. Lists are read live over
IMAP; opening a message marks it read, as any mail program does.
"""
import email
import email.header
import email.policy
import email.utils
import html as html_lib
import imaplib
import re
import ssl

from . import contacts, mailboxes, replies
from .render import html_to_text

FOLDERS = (
    ('inbox', 'Inbox', ('INBOX',), None),
    ('spam', 'Spam', ('Junk', 'Spam', 'INBOX.Junk', 'INBOX.Spam', '[Gmail]/Spam', 'Junk E-mail', 'Junk Email',
                      'Bulk Mail'), '\\Junk'),
    ('sent', 'Sent', ('Sent', 'INBOX.Sent', '[Gmail]/Sent Mail', 'Sent Items', 'Sent Messages', 'Sent Mail'), '\\Sent'),
)
FOLDER_LABELS = {key: label for key, label, _, _ in FOLDERS}
ERRORS = (OSError, imaplib.IMAP4.error, ssl.SSLError)


class MailError(Exception):
    """Shown to the user as is."""


def profiles(db, cfg, settings):
    """Every mailbox that can be read: [{'key', 'email', 'name', 'box', 'account_id'}]."""
    out, seen = [], set()
    main = replies.mailbox_settings(cfg, settings)
    if main['user'] and main['password']:
        out.append({'key': 'main', 'email': main['user'], 'name': settings.get('from_name') or '', 'box': main,
                    'account_id': None})
        seen.add(main['user'].lower())
    for account in db.q('SELECT * FROM mailboxes ORDER BY active DESC, email'):
        box = mailboxes.imap_box(cfg, settings, account)
        if box is None or account['email'].lower() in seen:
            continue
        seen.add(account['email'].lower())
        out.append({'key': str(account['id']), 'email': account['email'], 'name': account['from_name'] or '',
                    'box': box, 'account_id': account['id']})
    return out


def profile(db, cfg, settings, key):
    for item in profiles(db, cfg, settings):
        if item['key'] == key:
            return item
    raise MailError('That mailbox is not connected any more.')


def _decode(value):
    if not value:
        return ''
    try:
        return str(email.header.make_header(email.header.decode_header(str(value))))
    except (UnicodeError, LookupError, email.errors.HeaderParseError):
        return str(value)


def _folder_name(conn, key):
    """The server's name for inbox / spam / sent, or None when it has none."""
    if key == 'inbox':
        return 'INBOX'
    spec = next((f for f in FOLDERS if f[0] == key), None)
    if spec is None:
        return None
    _, _, names, flag = spec
    status, listing = conn.list()
    if status != 'OK':
        return None
    found = {}
    for raw in listing or []:
        line = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
        match = re.match(r'\((?P<flags>[^)]*)\)\s+(?P<sep>"[^"]*"|NIL)\s+(?P<name>.+)$', line)
        if not match:
            continue
        name = match.group('name').strip()
        name = name[1:-1] if name.startswith('"') and name.endswith('"') else name
        if flag and flag.lower() in match.group('flags').lower():
            return name
        found[name.lower()] = name
    for candidate in names:
        if candidate.lower() in found:
            return found[candidate.lower()]
    return None


def _quote(name):
    return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _open(box, folder, readonly=True):
    try:
        conn = replies.connect(box)
    except ERRORS as exc:
        raise MailError(f'Could not sign in to {box["user"]}: {exc}')
    name = _folder_name(conn, folder)
    if name is None:
        conn.logout()
        raise MailError(f'This mailbox has no {FOLDER_LABELS.get(folder, folder).lower()} folder.')
    status, _ = conn.select(_quote(name), readonly=readonly)
    if status != 'OK':
        conn.logout()
        raise MailError(f'Could not open the {FOLDER_LABELS.get(folder, folder)} folder.')
    return conn


def _close(conn):
    try:
        conn.logout()
    except ERRORS:
        pass


def list_messages(box, folder='inbox', limit=50, query=''):
    """The newest messages in a folder: [{'uid', 'from_name', 'from_email', 'to', 'subject', 'date', 'unread'}]
    plus the folder's total."""
    conn = _open(box, folder)
    try:
        query = ' '.join(str(query or '').split())[:100]
        if query:
            status, data = conn.uid('SEARCH', 'CHARSET', 'UTF-8', 'TEXT', _quote(query))
        else:
            status, data = conn.uid('SEARCH', None, 'ALL')
        uids = [u for u in (data[0].split() if status == 'OK' and data and data[0] else [])]
        total = len(uids)
        uids = uids[-limit:]
        out = []
        if uids:
            status, parts = conn.uid('FETCH', b','.join(uids).decode(),
                                     '(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])')
            for part in parts or []:
                if not isinstance(part, tuple):
                    continue
                meta = part[0].decode('utf-8', 'replace')
                uid = re.search(r'UID (\d+)', meta)
                if not uid:
                    continue
                head = email.message_from_bytes(part[1], policy=email.policy.compat32)
                name, address = email.utils.parseaddr(_decode(head.get('From')))
                try:
                    when = email.utils.parsedate_to_datetime(head.get('Date')).timestamp()
                except (TypeError, ValueError, IndexError):
                    when = None
                out.append({'uid': uid.group(1), 'from_name': name or address, 'from_email': address,
                            'to': _decode(head.get('To'))[:200], 'subject': _decode(head.get('Subject')) or '(no subject)',
                            'date': when, 'unread': '\\Seen' not in meta})
        out.sort(key=lambda m: int(m['uid']), reverse=True)
        return {'messages': out, 'total': total}
    except ERRORS as exc:
        raise MailError(f'Could not read the mailbox: {exc}')
    finally:
        _close(conn)


def read_message(box, folder, uid):
    """One message in full (marked read). Returns its headers, text and attachment names."""
    if not str(uid).isdigit():
        raise MailError('No such message.')
    conn = _open(box, folder, readonly=False)
    try:
        status, parts = conn.uid('FETCH', str(uid), '(BODY[])')
        raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None) if status == 'OK' else None
        if not raw:
            raise MailError('That message is no longer in this folder.')
    except ERRORS as exc:
        raise MailError(f'Could not read the message: {exc}')
    finally:
        _close(conn)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    plain, html, attachments = None, None, []
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        if part.get_filename():
            attachments.append(_decode(part.get_filename()))
            continue
        ctype = part.get_content_type()
        if ctype == 'text/plain' and plain is None:
            plain = replies._decode_part(part)
        elif ctype == 'text/html' and html is None:
            html = replies._decode_part(part)
    text = plain if plain and plain.strip() else html_to_text(html or '')
    name, address = email.utils.parseaddr(_decode(msg.get('From')))
    try:
        when = email.utils.parsedate_to_datetime(msg.get('Date')).timestamp()
    except (TypeError, ValueError, IndexError):
        when = None
    return {'uid': str(uid), 'from_name': name or address, 'from_email': address, 'to': _decode(msg.get('To')),
            'cc': _decode(msg.get('Cc')), 'subject': _decode(msg.get('Subject')) or '(no subject)', 'date': when,
            'text': text.replace('\r\n', '\n').strip()[:100_000], 'attachments': attachments[:20],
            'message_id': (msg.get('Message-ID') or '').strip(), 'references': (msg.get('References') or '').strip(),
            'reply_to': email.utils.parseaddr(_decode(msg.get('Reply-To')))[1] or address,
            'spam_header': (msg.get('X-Spam') or msg.get('X-Spam-Status') or '').strip()[:200]}


def reply(db, settings, prof, original, text, need_approval=False):
    """Answers a message from the profile's own address, in the same thread. Returns the message id."""
    from .sender import queue_single
    text = (text or '').strip()
    if not text:
        raise MailError('Write a reply first.')
    to = contacts.normalize_email(original['reply_to'] or original['from_email'])
    if not to:
        raise MailError('This message has no address to reply to.')
    subject = re.sub(r'(?i)^\s*((re|aw|sv)\s*:\s*)+', '', original['subject'] or '')
    headers = {}
    if original['message_id']:
        references = f'{original["references"]} {original["message_id"]}'.strip()
        headers = {'In-Reply-To': original['message_id'], 'References': references[-900:]}
    body_html = ''.join(f'<p>{html_lib.escape(p).replace(chr(10), "<br>")}</p>' for p in text.split('\n\n'))
    contact = db.one('SELECT id FROM contacts WHERE email = ?', to)
    return queue_single(db, kind='reply', from_email=prof['email'], from_name=prof['name'] or settings.get('from_name', ''),
                        to=[to], subject=f'Re: {subject}'[:300], html=body_html, text=text, headers=headers,
                        track=False, contact_id=contact['id'] if contact else None,
                        status='pending_approval' if need_approval else 'queued',
                        mailbox_id=prof['account_id'])


__all__ = ['FOLDERS', 'FOLDER_LABELS', 'MailError', 'list_messages', 'profile', 'profiles', 'read_message', 'reply']
