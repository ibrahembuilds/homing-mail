"""Email accounts that campaigns send from.

Each account is a real mailbox somewhere (Google Workspace, Gmail, Zoho, any
SMTP server, or this server). A campaign can rotate through several of them:
every new lead gets the account that has sent the least today, each account
stops at its own daily limit, and follow-ups always leave from the account
that sent the first email so the conversation stays in one thread. Replies
are read from every account that has IMAP details.
"""
import imaplib
import ssl

from . import contacts, replies
from .db import now

PROVIDERS = {
    'google': {
        'label': 'Google Workspace / Gmail', 'smtp_host': 'smtp.gmail.com', 'smtp_port': 587, 'smtp_tls': 'starttls',
        'imap_host': 'imap.gmail.com', 'imap_port': 993, 'daily_limit': 30,
        'help': 'Turn on 2-Step Verification, then create an app password at myaccount.google.com/apppasswords '
                'and paste it below (not your normal password). Gmail allows 500 emails a day on a free '
                'account and 2,000 on Workspace; stay far below that for cold email.'},
    'microsoft': {
        'label': 'Microsoft 365 / Outlook', 'smtp_host': 'smtp.office365.com', 'smtp_port': 587,
        'smtp_tls': 'starttls', 'imap_host': 'outlook.office365.com', 'imap_port': 993, 'daily_limit': 30,
        'help': 'Microsoft is switching off password sign-in for SMTP (including app passwords) by default from '
                'the end of December 2026, and Outlook.com already needs it. Until then an admin must enable '
                '"Authenticated SMTP" for the mailbox. For a long-term setup, prefer Google Workspace, Zoho or '
                'your own domain on this server.'},
    'zoho': {
        'label': 'Zoho Mail', 'smtp_host': 'smtp.zoho.com', 'smtp_port': 465, 'smtp_tls': 'ssl',
        'imap_host': 'imap.zoho.com', 'imap_port': 993, 'daily_limit': 30,
        'help': 'Enable IMAP access in Zoho Mail settings. With 2-factor sign-in, use an app-specific password. '
                'Accounts on zoho.eu or zoho.in use smtp.zoho.eu / smtp.zoho.in.'},
    'yahoo': {
        'label': 'Yahoo Mail', 'smtp_host': 'smtp.mail.yahoo.com', 'smtp_port': 465, 'smtp_tls': 'ssl',
        'imap_host': 'imap.mail.yahoo.com', 'imap_port': 993, 'daily_limit': 20,
        'help': 'Create an app password under Account security. Yahoo is strict with bulk mail: keep it small.'},
    'server': {
        'label': 'This mail server', 'smtp_host': '', 'smtp_port': 587, 'smtp_tls': 'starttls',
        'imap_host': '', 'imap_port': 993, 'daily_limit': 50,
        'help': 'A mailbox on your own domain hosted here. Mail leaves through this server, signed with your '
                'domain\'s DKIM key. Fill in IMAP only if you want replies read from this mailbox.'},
    'other': {
        'label': 'Other (SMTP / IMAP)', 'smtp_host': '', 'smtp_port': 587, 'smtp_tls': 'starttls',
        'imap_host': '', 'imap_port': 993, 'daily_limit': 30,
        'help': 'Any provider that offers SMTP: copy the server names and ports from its help pages.'},
}
TLS_MODES = (('starttls', 'STARTTLS (587)'), ('ssl', 'SSL/TLS (465)'), ('none', 'None'))
# Consumer mail services: fine for trying things out, poor for outreach at volume.
FREE_DOMAINS = {'gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com', 'live.com', 'msn.com', 'yahoo.com',
                'ymail.com', 'icloud.com', 'me.com', 'aol.com', 'gmx.com', 'gmx.net', 'mail.com', 'proton.me',
                'protonmail.com', 'zohomail.com', 'yandex.com', 'qq.com', '163.com'}
MAX_DAILY_LIMIT = 2000
RETRY_AFTER = 600   # seconds an account rests after its server could not be reached


class Problem(Exception):
    """Invalid account details; the message is shown to the user."""


def is_free(address):
    return address.rsplit('@', 1)[-1].lower() in FREE_DOMAINS


def _int(value, default, low, high):
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, number))


def clean(data, existing=None):
    """Validates account fields from the form or the API."""
    out = {}
    if existing is None or 'email' in data:
        address = contacts.normalize_email(str(data.get('email') or '').strip())
        if not address:
            raise Problem('Enter the email address of the account')
        out['email'] = address
    provider = str(data.get('provider') or (existing['provider'] if existing else 'other'))
    if provider not in PROVIDERS:
        provider = 'other'
    out['provider'] = provider
    preset = PROVIDERS[provider]
    for key in ('from_name', 'smtp_host', 'smtp_user', 'imap_host', 'imap_user'):
        if key in data:
            out[key] = str(data[key] or '').strip()[:254]
    if existing is None:
        out.setdefault('smtp_host', preset['smtp_host'])
        out.setdefault('imap_host', preset['imap_host'])
    if 'smtp_port' in data or existing is None:
        out['smtp_port'] = _int(data.get('smtp_port'), preset['smtp_port'], 1, 65535)
    if 'imap_port' in data or existing is None:
        out['imap_port'] = _int(data.get('imap_port'), preset['imap_port'], 1, 65535)
    if 'smtp_tls' in data or existing is None:
        tls = str(data.get('smtp_tls') or preset['smtp_tls'])
        out['smtp_tls'] = tls if tls in dict(TLS_MODES) else 'starttls'
    if 'daily_limit' in data or existing is None:
        out['daily_limit'] = _int(data.get('daily_limit'), preset['daily_limit'], 1, MAX_DAILY_LIMIT)
    # A blank password field keeps the stored one, so it never has to be shown again.
    for key in ('smtp_password', 'imap_password'):
        value = str(data.get(key) or '')
        if value.strip():
            # Google shows app passwords in groups of four letters.
            out[key] = value.replace(' ', '') if provider == 'google' else value
    if provider == 'other' and 'smtp_host' in out and not out['smtp_host']:
        raise Problem('Enter the SMTP server of the provider')
    address = out.get('email') or (existing['email'] if existing else '')
    if out.get('smtp_host') or (existing and existing['smtp_host'] and 'smtp_host' not in out):
        if existing is None and not out.get('smtp_user'):
            out['smtp_user'] = address
        if existing is None and not out.get('smtp_password'):
            raise Problem('Enter the password (an app password for Gmail, Yahoo and Zoho with 2-factor sign-in)')
    if out.get('imap_host') and existing is None and not out.get('imap_user'):
        out['imap_user'] = address
    return out


def create(db, data):
    fields = clean(data)
    if db.one('SELECT id FROM mailboxes WHERE email = ?', fields['email']):
        raise Problem(f'{fields["email"]} is already connected')
    fields['created_at'] = now()
    columns = ', '.join(fields)
    return db.run(f'INSERT INTO mailboxes ({columns}) VALUES ({", ".join("?" for _ in fields)})',
                  *fields.values()).lastrowid


def get(db, mailbox_id):
    row = db.one('SELECT * FROM mailboxes WHERE id = ?', mailbox_id)
    if row is None:
        raise Problem(f'Email account {mailbox_id} does not exist')
    return row


def update(db, mailbox_id, data):
    row = get(db, mailbox_id)
    fields = clean(data, existing=row)
    if 'email' in fields and fields['email'].lower() != row['email'].lower() and \
            db.one('SELECT id FROM mailboxes WHERE email = ?', fields['email']):
        raise Problem(f'{fields["email"]} is already connected')
    if any(k.startswith(('smtp_', 'imap_')) for k in fields):
        fields.update(status='new', last_error='', retry_at=0)
    sets = ', '.join(f'{k} = ?' for k in fields)
    db.run(f'UPDATE mailboxes SET {sets} WHERE id = ?', *fields.values(), mailbox_id)
    return get(db, mailbox_id)


def set_active(db, mailbox_id, active):
    db.run('UPDATE mailboxes SET active = ?, retry_at = 0 WHERE id = ?', 1 if active else 0, mailbox_id)


def delete(db, mailbox_id):
    """Removes an account and takes it out of every campaign. Sent history stays."""
    with db.tx():
        for campaign in db.q("SELECT id, mailbox_ids FROM campaigns WHERE mailbox_ids != ''"):
            ids = [i for i in parse_ids(campaign['mailbox_ids']) if i != mailbox_id]
            db.run('UPDATE campaigns SET mailbox_ids = ? WHERE id = ?', format_ids(ids), campaign['id'])
        db.run('DELETE FROM mailboxes WHERE id = ?', mailbox_id)


# -- campaigns ----------------------------------------------------------------------
def parse_ids(value):
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value or '').replace(' ', '').split(',')
    out = []
    for item in items:
        text = str(item).strip()
        if text.isdigit() and int(text) not in out:
            out.append(int(text))
    return out


def format_ids(ids):
    return ','.join(str(i) for i in sorted(set(ids)))


def for_campaign(db, campaign):
    ids = parse_ids(campaign['mailbox_ids'])
    if not ids:
        return []
    return db.q(f'SELECT * FROM mailboxes WHERE id IN ({", ".join("?" for _ in ids)}) ORDER BY id', *ids)


def sent_since(db, since, ids=None):
    """Emails sent per account since a moment: {mailbox id: count}."""
    sql = 'SELECT mailbox_id, COUNT(*) AS n FROM messages WHERE mailbox_id IS NOT NULL AND sent_at >= ?'
    args = [since]
    if ids:
        sql += f' AND mailbox_id IN ({", ".join("?" for _ in ids)})'
        args += list(ids)
    return {row['mailbox_id']: row['n'] for row in db.q(sql + ' GROUP BY mailbox_id', *args)}


def pick(db, campaign, today, follow=None):
    """The account the next email of this campaign leaves from.
    Returns (account, '') or (None, why nothing can send now).
    follow: the account that sent the first email to this lead."""
    accounts = for_campaign(db, campaign)
    ts = now()
    counts = sent_since(db, today, [a['id'] for a in accounts])
    if follow is not None:
        if not follow['active']:
            return None, f'{follow["email"]} is paused'
        if follow['retry_at'] > ts:
            return None, f'{follow["email"]} could not connect: {follow["last_error"]}'
        if counts.get(follow['id'], 0) >= follow['daily_limit']:
            return None, f'{follow["email"]} reached its daily limit of {follow["daily_limit"]}'
        return follow, ''
    usable = [a for a in accounts if a['active'] and a['retry_at'] <= ts
              and counts.get(a['id'], 0) < a['daily_limit']]
    if usable:
        return min(usable, key=lambda a: (counts.get(a['id'], 0) / a['daily_limit'], a['id'])), ''
    if not [a for a in accounts if a['active']]:
        return None, 'All email accounts of this campaign are paused'
    if all(counts.get(a['id'], 0) >= a['daily_limit'] for a in accounts if a['active']):
        total = sum(a['daily_limit'] for a in accounts if a['active'])
        return None, f'All email accounts reached their daily limits ({total} emails today); resumes tomorrow'
    errors = '; '.join(f'{a["email"]}: {a["last_error"]}' for a in accounts if a['active'] and a['retry_at'] > ts)
    return None, f'Email accounts could not connect, retrying shortly. {errors}'[:500]


def mark_down(db, account, error):
    db.run("UPDATE mailboxes SET status = 'error', last_error = ?, retry_at = ? WHERE id = ?",
           str(error)[:300], now() + RETRY_AFTER, account['id'])


def mark_ok(db, account):
    if account['status'] != 'ok' or account['last_error']:
        db.run("UPDATE mailboxes SET status = 'ok', last_error = '', retry_at = 0 WHERE id = ?", account['id'])


# -- checking and reading -----------------------------------------------------------
def imap_box(cfg, settings, account):
    """The account's inbox in the shape replies.read_box expects, or None."""
    user = account['imap_user'] or (account['email'] if account['imap_host'] else '')
    password = account['imap_password'] or account['smtp_password']
    if not account['imap_host'] or not user or not password:
        return None
    own = account['imap_host'] in (cfg.IMAP_HOST, settings.get('imap_host'))
    return {'user': user, 'password': password, 'host': account['imap_host'], 'port': account['imap_port'],
            'verify': settings.get('imap_verify_tls') == '1' if own else True}


def test(db, cfg, settings, account):
    """Signs in to SMTP (and IMAP when set) without sending anything.
    Stores and returns {'smtp': error or '', 'imap': error, '' or None}."""
    from .sender import Mailer, ServerDown
    result = {'smtp': '', 'imap': None}
    mailer = Mailer(cfg, settings, account)
    try:
        mailer._connect()
    except ServerDown as exc:
        result['smtp'] = str(exc)
    finally:
        mailer.close()
    box = imap_box(cfg, settings, account)
    if box is not None:
        try:
            conn = replies.connect(box)
            conn.logout()
            result['imap'] = ''
        except (OSError, imaplib.IMAP4.error, ssl.SSLError) as exc:
            result['imap'] = f'{box["host"]}:{box["port"]}: {type(exc).__name__}: {exc}'[:300]
    problems = [p for p in (result['smtp'], result['imap']) if p]
    db.run('UPDATE mailboxes SET status = ?, last_error = ?, retry_at = 0, checked_at = ? WHERE id = ?',
           'error' if problems else 'ok', ' | '.join(problems)[:300], now(), account['id'])
    return result


def poll(db, cfg, settings):
    """Reads replies from every active account with IMAP details."""
    total = {'accounts': 0, 'replies': 0, 'errors': []}
    for account in db.q('SELECT * FROM mailboxes WHERE active = 1 ORDER BY id'):
        box = imap_box(cfg, settings, account)
        if box is None:
            continue
        result = replies.read_box(db, settings, box)
        total['accounts'] += 1
        total['replies'] += result.get('replies', 0)
        if not result.get('ok'):
            total['errors'].append(f'{account["email"]}: {result.get("error", "")}')
    return total


def warnings(account):
    """Advice shown next to an account."""
    out = []
    if is_free(account['email']):
        out.append('Free address: providers watch these closely and limit them to a few hundred emails a day. '
                   'Use it for testing or warm contacts; for cold outreach use a domain you own.')
    if account['provider'] == 'microsoft':
        out.append('Microsoft turns off password sign-in for SMTP by default from the end of December 2026.')
    if account['daily_limit'] > 50:
        out.append('Over 50 a day from one mailbox raises the spam risk. Add more mailboxes instead.')
    return out


def summary(db, today):
    """Every account with what it sent since the start of today, for the pages."""
    rows = db.q('SELECT * FROM mailboxes ORDER BY active DESC, email')
    counts = sent_since(db, today)
    return [{'row': r, 'sent_today': counts.get(r['id'], 0), 'warnings': warnings(r), 'free': is_free(r['email'])}
            for r in rows]
