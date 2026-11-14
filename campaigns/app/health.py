"""Sending health: how much can go out today, how it is received, and where it lands.

The inbox placement test sends the real email from each sending account to the
owner's own connected mailboxes (Gmail, Outlook, ...) and then looks over IMAP
whether it arrived in the inbox or in the spam folder. Only connected mailboxes
can be checked, so the test is as broad as the accounts the owner has added.
"""
import imaplib
import json
import re
import ssl

from . import mailboxes, replies, security, service
from .db import now
from .render import Renderer
from .sender import day_start, warmup_cap

WINDOW_DAYS = 30
# (warn from, bad from) in percent. Gmail and Yahoo require complaints under 0.3%.
THRESHOLDS = {'bounce': (2.0, 5.0), 'complaint': (0.1, 0.3), 'unsubscribe': (1.0, 2.0)}
SPAM_NAME_RE = re.compile(r'(?i)(spam|junk|bulk|indésirable|ind&AOk-sirable|no deseado|indesiderata|unerwünscht)')
LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+(?P<sep>"[^"]*"|NIL)\s+(?P<name>.+)$')
MAX_PAIRS = 12
MISSING_AFTER = 20 * 60   # sent this long ago and in no folder: counted as missing
CHECK_FOR = 2 * 3600      # the worker keeps checking a test this long


def _window_hours(settings):
    start, end = settings.get('window_start') or '', settings.get('window_end') or ''
    if not start or not end or start == end:
        return 24
    minutes = lambda t: int(t[:2]) * 60 + int(t[3:5])
    span = (minutes(end) - minutes(start)) % (24 * 60)
    return max(1, round(span / 60))


def capacity(db, settings):
    """How many emails can go out today and what limits that."""
    today = day_start(settings)
    accounts = [a for a in mailboxes.summary(db, today) if a['row']['active']]
    account_total = sum(a['row']['daily_limit'] for a in accounts) if accounts else None
    daily_cap = int(settings.get('daily_cap') or 0) or None
    warm = warmup_cap(settings)
    rate = max(1, int(settings.get('rate_per_hour') or 1))
    at_rate = rate * _window_hours(settings)
    limits = [(n, why) for n, why in ((account_total, 'the daily limits of your email accounts'),
                                       (daily_cap, 'your daily cap'), (warm, 'warm-up'),
                                       (at_rate, 'the sending speed and window')) if n]
    total, reason = min(limits) if limits else (None, '')
    sent_today = db.val("SELECT COUNT(*) FROM messages WHERE sent_at >= ? AND kind != 'test'", today, default=0)
    return {'accounts': accounts, 'account_total': account_total, 'daily_cap': daily_cap, 'warmup': warm,
            'rate_per_hour': rate, 'window_hours': _window_hours(settings), 'at_rate': at_rate,
            'total': total, 'limited_by': reason, 'sent_today': sent_today,
            'left_today': max(0, total - sent_today) if total else None}


def _level(metric, pct):
    warn, bad = THRESHOLDS[metric]
    return 'bad' if pct >= bad else 'warn' if pct >= warn else 'good'


_STATS_SQL = """SELECT {group} COUNT(*) AS sent,
    SUM(CASE WHEN human_open > 0 OR opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened,
    SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked,
    SUM(CASE WHEN bounced_at IS NOT NULL OR status = 'bounced' THEN 1 ELSE 0 END) AS bounced,
    SUM(CASE WHEN bounce_type = 'hard' THEN 1 ELSE 0 END) AS hard,
    SUM(CASE WHEN complained_at IS NOT NULL OR status = 'complained' THEN 1 ELSE 0 END) AS complained,
    SUM(CASE WHEN unsubscribed_at IS NOT NULL THEN 1 ELSE 0 END) AS unsubscribed
  FROM messages WHERE kind != 'test' AND status IN ('sent', 'bounced', 'complained')
  AND COALESCE(sent_at, created_at) >= ? {tail}"""


def _rates(row):
    sent = row['sent'] or 0
    pct = lambda n: round(100.0 * (n or 0) / sent, 2) if sent else 0.0
    out = {key: row[key] or 0 for key in ('sent', 'opened', 'clicked', 'bounced', 'hard', 'complained', 'unsubscribed')}
    out.update(open_rate=pct(row['opened']), click_rate=pct(row['clicked']), bounce_rate=pct(row['bounced']),
               complaint_rate=pct(row['complained']), unsubscribe_rate=pct(row['unsubscribed']))
    out['levels'] = {m: _level(m, out[f'{m}_rate']) if sent >= 20 else 'few' for m in THRESHOLDS}
    return out


def stats(db, days=WINDOW_DAYS):
    """Delivery numbers over the last days, overall and per sending account."""
    since = now() - days * 86400
    total = _rates(db.one(_STATS_SQL.format(group='', tail=''), since))
    total['refused'] = db.val("SELECT COUNT(*) FROM messages WHERE status = 'failed' AND kind != 'test' "
                              "AND created_at >= ?", since, default=0)
    total['replied'] = db.val('SELECT COUNT(*) FROM replies WHERE is_auto = 0 AND received_at >= ?', since, default=0)
    total['reply_rate'] = round(100.0 * total['replied'] / total['sent'], 2) if total['sent'] else 0.0
    names = {r['id']: r['email'] for r in db.q('SELECT id, email FROM mailboxes')}
    per_account = []
    for row in db.q(_STATS_SQL.format(group='mailbox_id,', tail='GROUP BY mailbox_id ORDER BY COUNT(*) DESC'), since):
        item = _rates(row)
        item['account'] = names.get(row['mailbox_id'], 'Server default sender' if row['mailbox_id'] is None
                                    else 'Removed account')
        per_account.append(item)
    advice = []
    if total['levels']['bounce'] in ('warn', 'bad'):
        advice.append('Too many emails bounce. Check addresses before sending (Leads → Check emails) and remove '
                      'old lists.')
    if total['levels']['complaint'] in ('warn', 'bad'):
        advice.append('People are marking your emails as spam. Send only to people who fit your offer, make the '
                      'first email shorter and more personal, and keep the unsubscribe link visible.')
    if total['levels']['unsubscribe'] == 'bad':
        advice.append('Many unsubscribes: the list or the message does not fit the readers well.')
    return {'days': days, 'total': total, 'per_account': per_account, 'advice': advice}


# -- inbox placement test ---------------------------------------------------------------
def seeds(db, cfg, settings):
    """Connected mailboxes the test can look into."""
    out = []
    for account in db.q('SELECT * FROM mailboxes WHERE active = 1 ORDER BY id'):
        box = mailboxes.imap_box(cfg, settings, account)
        if box is not None:
            out.append({'email': account['email'], 'provider': mailboxes.PROVIDERS.get(account['provider'], {})
                        .get('label', account['provider']), 'box': box})
    reply_box = replies.mailbox_settings(cfg, settings)
    if reply_box['user'] and reply_box['password'] and '@' in reply_box['user'] and \
            reply_box['user'].lower() not in {s['email'].lower() for s in out}:
        out.append({'email': reply_box['user'], 'provider': 'Reply mailbox', 'box': reply_box})
    return out


def _senders(db, settings, campaign):
    rows = mailboxes.for_campaign(db, campaign) if campaign is not None else []
    rows = [r for r in rows if r['active']] or db.q('SELECT * FROM mailboxes WHERE active = 1 ORDER BY id')
    if rows:
        return [(r['email'], r['from_name'] or settings.get('from_name', ''), r['id']) for r in rows]
    if settings.get('from_email'):
        return [(settings['from_email'], settings.get('from_name', ''), None)]
    return []


def _content(db, cfg, settings, campaign, mid):
    if campaign is not None and (campaign['html'].strip() or campaign['text'].strip()):
        return Renderer(cfg, db).campaign_bodies(campaign, service.sample_contact(db, campaign), mid, settings,
                                                 tracking=False)
    name = settings.get('from_name') or 'the team'
    text = (f'Hi Alex,\n\nI came across your company and had a quick question about how you handle new customer '
            f'enquiries today. Would a short call next week be useful?\n\nBest,\n{name}')
    html = ''.join(f'<p>{p.replace(chr(10), "<br>")}</p>' for p in text.split('\n\n'))
    return 'Quick question', html, text


def start_test(db, cfg, settings, campaign_id=None):
    """Queues the placement emails. Returns the test id."""
    targets = seeds(db, cfg, settings)
    if not targets:
        raise service.Problem('Connect at least one email account with its inbox (IMAP) first, for example a '
                              'Gmail or Outlook address in Email accounts. The test sends to your own mailboxes '
                              'and checks which folder the email lands in.')
    campaign = service.get_campaign(db, campaign_id) if campaign_id else db.one(
        "SELECT * FROM campaigns WHERE TRIM(html) != '' OR TRIM(text) != '' ORDER BY updated_at DESC LIMIT 1")
    senders = _senders(db, settings, campaign)
    if not senders:
        raise service.Problem('No From address. Add an email account or a default sender in Settings.')
    pairs = [(s, t) for s in senders for t in targets if s[0].lower() != t['email'].lower()]
    if not pairs:  # only one mailbox: it can still show whether its own mail is filtered
        pairs = [(s, t) for s in senders for t in targets]
    results = []
    for (from_email, from_name, mailbox_id), target in pairs[:MAX_PAIRS]:
        mid = security.new_id()
        subject, html, text = _content(db, cfg, settings, campaign, mid)
        service.queue_single(db, kind='test', mid=mid, from_email=from_email, from_name=from_name,
                             to=[target['email']], subject=subject, html=html, text=text, track=False,
                             mailbox_id=mailbox_id)
        results.append({'message_id': mid, 'from': from_email, 'to': target['email'],
                        'provider': target['provider'], 'state': 'waiting', 'folder': ''})
    return db.run('INSERT INTO placement_tests (campaign_id, token, results, created_at) VALUES (?, ?, ?, ?)',
                  campaign['id'] if campaign is not None else None, security.new_id()[:12],
                  json.dumps(results), now()).lastrowid


def _folders(conn):
    """(inbox, [spam folders]) of a mailbox."""
    status, lines = conn.list()
    spam = []
    for line in lines if status == 'OK' else []:
        line = line.decode('utf-8', 'replace') if isinstance(line, bytes) else str(line)
        match = LIST_RE.match(line)
        if not match:
            continue
        name = match['name'].strip()
        bare = name.strip('"')
        if '\\Junk' in match['flags'] or SPAM_NAME_RE.search(bare.rsplit('/', 1)[-1].rsplit('.', 1)[-1]):
            spam.append(name if name.startswith('"') else f'"{bare}"')
    return 'INBOX', spam


def _find(conn, folder, mid, gmail):
    status, _ = conn.select(folder, readonly=True)
    if status != 'OK':
        return False
    status, data = conn.uid('SEARCH', None, 'HEADER', 'Message-ID', mid)
    if status == 'OK' and data and data[0]:
        return True
    if gmail:
        status, data = conn.uid('SEARCH', None, 'X-GM-RAW', f'"rfc822msgid:{mid}"')
        return status == 'OK' and bool(data and data[0])
    return False


def check_test(db, cfg, settings, test_id):
    """Looks for the test emails in each mailbox. Returns the test with results."""
    row = db.one('SELECT * FROM placement_tests WHERE id = ?', test_id)
    if row is None:
        raise service.Problem('That placement test does not exist.')
    results = json.loads(row['results'])
    boxes = {s['email'].lower(): s['box'] for s in seeds(db, cfg, settings)}
    waiting = [r for r in results if r['state'] == 'waiting']
    for address in {r['to'].lower() for r in waiting}:
        mine = [r for r in waiting if r['to'].lower() == address]
        for item in mine:
            msg = db.one('SELECT status, error, sent_at FROM messages WHERE id = ?', item['message_id'])
            if msg is None or msg['status'] in ('failed', 'bounced', 'cancelled'):
                item['state'], item['folder'] = 'failed', (msg['error'] if msg is not None else 'not found')[:200]
        mine = [r for r in mine if r['state'] == 'waiting']
        box = boxes.get(address)
        if not mine or box is None:
            continue
        conn = None
        try:
            conn = replies.connect(box)
            inbox, spam = _folders(conn)
            gmail = 'gmail' in box['host'] or 'googlemail' in box['host']
            for item in mine:
                msg = db.one('SELECT sent_at FROM messages WHERE id = ?', item['message_id'])
                if msg is None or not msg['sent_at']:
                    continue
                if _find(conn, inbox, item['message_id'], gmail):
                    item['state'], item['folder'] = 'inbox', 'Inbox'
                    continue
                folder = next((f for f in spam if _find(conn, f, item['message_id'], gmail)), None)
                if folder:
                    item['state'], item['folder'] = 'spam', folder.strip('"')
                elif now() - msg['sent_at'] > MISSING_AFTER:
                    item['state'], item['folder'] = 'missing', ''
        except (OSError, imaplib.IMAP4.error, ssl.SSLError) as exc:
            for item in mine:
                item['error'] = f'Could not open the mailbox: {exc}'[:200]
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass
    db.run('UPDATE placement_tests SET results = ?, checked_at = ? WHERE id = ?', json.dumps(results), now(), test_id)
    return describe(db.one('SELECT * FROM placement_tests WHERE id = ?', test_id))


def describe(row):
    if row is None:
        return None
    results = json.loads(row['results'])
    counts = {state: sum(1 for r in results if r['state'] == state)
              for state in ('inbox', 'spam', 'missing', 'failed', 'waiting')}
    done = len(results) - counts['waiting']
    return {'id': row['id'], 'created_at': row['created_at'], 'checked_at': row['checked_at'],
            'campaign_id': row['campaign_id'], 'results': results, 'counts': counts, 'total': len(results),
            'finished': counts['waiting'] == 0,
            'inbox_rate': round(100.0 * counts['inbox'] / done) if done else None}


def latest_test(db):
    return describe(db.one('SELECT * FROM placement_tests ORDER BY id DESC LIMIT 1'))


def check_pending(db, cfg, settings):
    """Worker step: keep checking recent tests until every email is found."""
    for row in db.q("SELECT id, results FROM placement_tests WHERE created_at >= ?", now() - CHECK_FOR):
        if any(r['state'] == 'waiting' for r in json.loads(row['results'])):
            check_test(db, cfg, settings, row['id'])
