"""Campaigns where each lead gets their own email (ai_mode "own").

Each lead's subject and message come from columns of the uploaded file (for
example email_subject and email_body), or start as the campaign's main email and
are edited by hand. They are stored in personalizations with status 'edited';
the message is plain text and becomes a simple email when sent. A lead without
their own email gets the main one.
"""
from .db import loads, now
from .render import contact_values, html_to_text, merge

MODE = 'own'
SUBJECT_HINTS = ('email_subject', 'subject', 'subject_line', 'email_title')
BODY_HINTS = ('email_body', 'body', 'message', 'email_text', 'email_message', 'text', 'pitch')


def columns(db, list_id):
    """The columns of a list's leads, in the order they first appear."""
    seen = []
    for row in db.q('SELECT c.fields FROM list_members m JOIN contacts c ON c.id = m.contact_id '
                    'WHERE m.list_id = ? LIMIT 200', list_id or 0):
        for key in loads(row['fields']):
            if key not in seen:
                seen.append(key)
    return seen


def guess(cols):
    """(subject column, message column) that look right, or ''."""
    subject = next((c for hint in SUBJECT_HINTS for c in cols if c == hint), '')
    body = next((c for hint in BODY_HINTS for c in cols if c == hint and c != subject), '')
    return subject, body


def _members(db, campaign):
    return db.q("SELECT c.* FROM list_members m JOIN contacts c ON c.id = m.contact_id "
                "WHERE m.list_id = ? AND c.status = 'active'", campaign['list_id'] or 0)


def _store(db, campaign_id, contact_id, subject, text):
    db.run("INSERT INTO personalizations (campaign_id, contact_id, status, subject, body, model, updated_at) "
           "VALUES (?, ?, 'edited', ?, ?, 'you', ?) ON CONFLICT(campaign_id, contact_id) DO UPDATE SET "
           "status = 'edited', subject = excluded.subject, body = excluded.body, error = '', model = 'you', "
           "updated_at = excluded.updated_at", campaign_id, contact_id, subject[:300], text[:50_000], now())


def fill_from_columns(db, campaign, subject_col, body_col):
    """Each lead's email from two columns. Leads with an empty message column keep what they had.
    Returns {'filled', 'empty'}."""
    filled = empty = 0
    with db.tx():
        for contact in _members(db, campaign):
            fields = loads(contact['fields'])
            text = str(fields.get(body_col) or '').strip()
            if not text:
                empty += 1
                continue
            subject = ' '.join(str(fields.get(subject_col) or '').split()) if subject_col else ''
            _store(db, campaign['id'], contact['id'], subject, text)
            filled += 1
    return {'filled': filled, 'empty': empty}


def fill_from_draft(db, campaign):
    """Leads without their own email get a copy of the main one, with their details filled in,
    ready to edit. Returns how many were added."""
    added = 0
    have = {r['contact_id'] for r in db.q("SELECT contact_id FROM personalizations WHERE campaign_id = ? "
                                          "AND status = 'edited'", campaign['id'])}
    with db.tx():
        for contact in _members(db, campaign):
            if contact['id'] in have:
                continue
            values = contact_values(contact)
            subject = ' '.join(merge(campaign['subject'] or '', values).split())
            text = campaign['text'] or html_to_text(campaign['html'] or '')
            _store(db, campaign['id'], contact['id'], subject, merge(text, values).strip())
            added += 1
    return added


def placeholders(db, campaign_id, pattern):
    """Leads whose own email still has a [placeholder]: (how many, examples found)."""
    found, leads = [], 0
    for row in db.q("SELECT subject, body FROM personalizations WHERE campaign_id = ? AND status = 'edited'",
                    campaign_id):
        hits = pattern.findall(f'{row["subject"]}\n{row["body"]}')
        if hits:
            leads += 1
            for hit in hits:
                text = hit if isinstance(hit, str) else hit[0]
                if text and text not in found:
                    found.append(text)
    return leads, found[:6]


def replace_all(db, campaign_id, find, replace):
    """Replaces text in every lead's own email. Returns how many emails changed."""
    if not find:
        return 0
    changed = 0
    with db.tx():
        for row in db.q("SELECT contact_id, subject, body FROM personalizations WHERE campaign_id = ? "
                        "AND status = 'edited'", campaign_id):
            subject, body = row['subject'].replace(find, replace), row['body'].replace(find, replace)
            if (subject, body) != (row['subject'], row['body']):
                db.run('UPDATE personalizations SET subject = ?, body = ?, updated_at = ? WHERE campaign_id = ? '
                       'AND contact_id = ?', subject, body, now(), campaign_id, row['contact_id'])
                changed += 1
    return changed


def save_one(db, campaign_id, contact_id, subject, text):
    text = (text or '').strip()
    if not text:
        raise ValueError('Write a message, or use "Main email" to send them the main one.')
    _store(db, campaign_id, contact_id, ' '.join((subject or '').split()), text)


def clear_one(db, campaign_id, contact_id):
    db.run('DELETE FROM personalizations WHERE campaign_id = ? AND contact_id = ?', campaign_id, contact_id)


def counts(db, campaign):
    total = db.val("SELECT COUNT(*) FROM list_members m JOIN contacts c ON c.id = m.contact_id "
                   "WHERE m.list_id = ? AND c.status = 'active'", campaign['list_id'] or 0, default=0)
    own = db.val("SELECT COUNT(*) FROM personalizations p JOIN list_members m ON m.contact_id = p.contact_id "
                 "AND m.list_id = ? WHERE p.campaign_id = ? AND p.status = 'edited'",
                 campaign['list_id'] or 0, campaign['id'], default=0)
    return {'total': total, 'own': own}


def members(db, campaign, query='', only='', page=1, per_page=20):
    """The list's leads with their own email (or None), for the editing page."""
    where, args = ["m.list_id = ?", "c.status = 'active'"], [campaign['list_id'] or 0]
    if query:
        where.append('(c.email LIKE ? OR c.first_name LIKE ? OR c.last_name LIKE ? OR c.fields LIKE ?)')
        args += [f'%{query}%'] * 4
    if only == 'own':
        where.append("p.status = 'edited'")
    elif only == 'main':
        where.append("(p.status IS NULL OR p.status != 'edited')")
    sql = (f"FROM list_members m JOIN contacts c ON c.id = m.contact_id LEFT JOIN personalizations p "
           f"ON p.contact_id = c.id AND p.campaign_id = ? WHERE {' AND '.join(where)}")
    total = db.val(f'SELECT COUNT(*) {sql}', campaign['id'], *args, default=0)
    rows = db.q(f"SELECT c.id, c.email, c.first_name, c.last_name, c.fields, p.subject AS own_subject, "
                f"p.body AS own_text, p.status AS own_status {sql} ORDER BY c.id LIMIT ? OFFSET ?",
                campaign['id'], *args, per_page, (max(1, page) - 1) * per_page)
    return rows, total


def content_for(campaign, row):
    """What the sender uses for one lead's first email."""
    return {'subject': row['subject'] or campaign['subject'], 'html': '', 'text': row['body'],
            'preheader': campaign['preheader']}


__all__ = ['MODE', 'clear_one', 'columns', 'content_for', 'counts', 'fill_from_columns', 'fill_from_draft', 'guess',
           'members', 'save_one']
