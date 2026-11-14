"""The people who can sign in to Homing, and what each may do.

Roles
    owner   everything, including adding and removing people
    admin   everything except changing owners
    member  daily work: leads, campaigns, inbox, calendar, agent, analytics;
            not the settings, the domain, sending accounts, API keys or the team

Where passwords live
    On a server with the bundled mail server, everyone signs in with their
    mailbox on that server (the mail server checks the password; the web proxy
    tells Homing who it is). Homing can create the mailbox through the mail
    server's user API.
    Without a mail server ("password" mode), Homing keeps its own password
    hashes. The owner can always sign in with CAMPAIGNS_ADMIN_PASSWORD.
"""
import re
import secrets

import requests
from werkzeug.security import check_password_hash, generate_password_hash

from .db import now

ROLES = (('owner', 'Owner'), ('admin', 'Admin'), ('member', 'Member'))
ROLE_NAMES = dict(ROLES)
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
SEEN_EVERY = 300   # seconds between last-seen updates

# Pages that change how the whole account works. Members do not get them.
ADMIN_ENDPOINTS = {
    'ui.settings', 'ui.settings_models', 'ui.settings_bounce_check', 'ui.settings_api_keys',
    'ui.settings_webhooks', 'ui.domains', 'ui.accounts', 'ui.account_add', 'ui.account_update',
    'ui.team', 'ui.team_add', 'ui.team_update',
}
# Pages members may look at but not change.
ADMIN_POSTS = {'ui.suppressions'}


class TeamError(Exception):
    """Shown to the operator as is."""


# -- people --------------------------------------------------------------------------
def normalize(email):
    email = str(email or '').strip().lower()
    return email if EMAIL_RE.match(email) or email == 'admin' else ''


def get(db, email):
    email = normalize(email)
    return db.one('SELECT * FROM team WHERE email = ?', email) if email else None


def everyone(db):
    return db.q("SELECT * FROM team ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, "
                "lower(name), email")


def ensure_owner(db, email):
    """The first owner comes from the installation (the mail server's first
    administrator, or "admin" without a mail server)."""
    email = normalize(email) or 'admin'
    if db.val("SELECT COUNT(*) FROM team WHERE role = 'owner'", default=0):
        return
    db.run("INSERT OR IGNORE INTO team (email, name, role, session_epoch, added_by, created_at) "
           "VALUES (?, '', 'owner', ?, 'install', ?)", email, secrets.token_urlsafe(12), now())
    db.run("UPDATE team SET role = 'owner', disabled = 0 WHERE email = ?", email)


def add(db, email, name='', role='member', added_by='', password=None):
    email = normalize(email)
    if not email or email == 'admin':
        raise TeamError('Enter a valid email address.')
    if role not in ROLE_NAMES:
        raise TeamError('Unknown role.')
    if get(db, email):
        raise TeamError(f'{email} is already on the team.')
    db.run('INSERT INTO team (email, name, role, password_hash, session_epoch, added_by, created_at) '
           'VALUES (?, ?, ?, ?, ?, ?, ?)', email, str(name or '').strip()[:120], role,
           generate_password_hash(password) if password else None, secrets.token_urlsafe(12),
           normalize(added_by), now())
    return get(db, email)


def owners(db):
    return db.val("SELECT COUNT(*) FROM team WHERE role = 'owner' AND disabled = 0", default=0)


def check_change(db, actor, target, new_role=None, removing=False):
    """Whether actor may change target's role or remove them."""
    if actor['role'] not in ('owner', 'admin'):
        raise TeamError('Only owners and admins can change the team.')
    touches_owner = target['role'] == 'owner' or new_role == 'owner'
    if touches_owner and actor['role'] != 'owner':
        raise TeamError('Only an owner can add, change or remove owners.')
    leaving_owner = target['role'] == 'owner' and (removing or (new_role and new_role != 'owner'))
    if leaving_owner and owners(db) <= 1:
        raise TeamError('There must always be at least one owner.')


def set_role(db, actor, email, role):
    target = get(db, email)
    if target is None:
        raise TeamError('That person is not on the team.')
    if role not in ROLE_NAMES:
        raise TeamError('Unknown role.')
    check_change(db, actor, target, new_role=role)
    db.run('UPDATE team SET role = ? WHERE email = ?', role, target['email'])


def remove(db, actor, email):
    target = get(db, email)
    if target is None:
        raise TeamError('That person is not on the team.')
    if target['email'] == actor['email']:
        raise TeamError('You cannot remove yourself. Ask another owner.')
    check_change(db, actor, target, removing=True)
    db.run('DELETE FROM team WHERE email = ?', target['email'])


def set_password(db, email, password):
    """Homing's own password (sign-in without a mail server). Signs them out everywhere."""
    db.run('UPDATE team SET password_hash = ?, session_epoch = ? WHERE email = ?',
           generate_password_hash(password), secrets.token_urlsafe(12), normalize(email))


def end_sessions(db, email):
    db.run('UPDATE team SET session_epoch = ? WHERE email = ?', secrets.token_urlsafe(12), normalize(email))


def check_password(row, password):
    return bool(row and row['password_hash'] and password and check_password_hash(row['password_hash'], password))


def seen(db, row):
    if row['last_seen_at'] is None or now() - row['last_seen_at'] > SEEN_EVERY:
        db.run('UPDATE team SET last_seen_at = ? WHERE email = ?', now(), row['email'])


def new_password():
    """Twelve characters that survive copy-paste and every mail client."""
    alphabet = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(12))


def may(role, endpoint, method='GET'):
    """Whether someone with this role may use a page."""
    if role in ('owner', 'admin'):
        return True
    if endpoint in ADMIN_ENDPOINTS:
        return False
    return not (method == 'POST' and endpoint in ADMIN_POSTS)


# -- mailboxes on the bundled mail server -----------------------------------------------
def mail_api_ready(cfg):
    return bool(cfg.MAIL_SERVER and cfg.MAIL_API_URL and cfg.MAIL_API_TOKEN)


def _mail_api(cfg, method, path, body=None):
    try:
        reply = requests.request(method, f'{cfg.MAIL_API_URL.rstrip("/")}{path}', json=body, timeout=20,
                                 headers={'Authorization': f'Bearer {cfg.MAIL_API_TOKEN}'})
    except requests.RequestException:
        raise TeamError('Could not reach the mail server to manage the mailbox.')
    try:
        data = reply.json()
    except ValueError:
        data = {}
    return reply.status_code, (data.get('message') if isinstance(data, dict) else '') or ''


def create_mailbox(cfg, email, password, name=''):
    """Creates the mailbox. Returns True when created, False when it already existed."""
    status, message = _mail_api(cfg, 'POST', '/user', {
        'email': email, 'raw_password': password, 'displayed_name': name or '', 'enabled': True,
        'enable_imap': True, 'comment': 'Created from Homing → Team'})
    if status == 200:
        return True
    if status == 409 and 'already exists' in message:
        return False
    if status == 404:
        raise TeamError(f'{email.split("@")[-1]} is not a mail domain on this server. Add it in Mail admin first, '
                        'or use an address on your main domain.')
    if status in (401, 403):
        raise TeamError('The mail server refused Homing\'s API key. Run ./install.sh again on the server.')
    raise TeamError(f'The mail server could not create the mailbox ({status}): {message or "no details"}')


def set_mailbox_password(cfg, email, password):
    status, message = _mail_api(cfg, 'PATCH', f'/user/{email}', {'raw_password': password})
    if status == 200:
        return
    if status == 404:
        raise TeamError(f'{email} has no mailbox on this server.')
    raise TeamError(f'The mail server could not change the password ({status}): {message or "no details"}')
