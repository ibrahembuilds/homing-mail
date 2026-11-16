"""Reviewing a campaign before it goes out: every lead's email, one click each, and
the choice of who gets it (leads left out are kept in campaign_skips)."""
from .db import loads, now
from .render import contact_values, html_to_text, merge


def _subject_and_start(campaign, contact, own):
    """What the lead sees in their inbox list: the subject and the first words."""
    values = contact_values(contact)
    subject, text = campaign['subject'] or '', campaign['text'] or html_to_text(campaign['html'] or '')
    if own is not None and own['status'] in ('done', 'edited'):
        if campaign['ai_mode'] == 'own':
            subject, text = own['subject'] or subject, own['body']
        elif campaign['ai_mode'] == 'full':
            subject, text = own['subject'] or subject, html_to_text(own['body'])
        elif campaign['ai_mode'] == 'line':
            text = own['body'] + ' ' + text
    elif campaign['ai_mode'] in ('line', 'full'):
        text = 'The AI has not written this one yet. ' + text
    subject = ' '.join(merge(subject, values).split())
    start = ' '.join(merge(text, values).replace('{{ai_line}}', '').split())
    return subject or '(no subject)', start[:140]


def leads(db, campaign, query='', show='', page=1, per_page=50):
    """The list's leads with what they will get. show: '' | 'in' | 'out'."""
    where, args = ['m.list_id = ?', "c.status = 'active'"], [campaign['list_id'] or 0]
    if query:
        where.append('(c.email LIKE ? OR c.first_name LIKE ? OR c.last_name LIKE ? OR c.fields LIKE ?)')
        args += [f'%{query}%'] * 4
    if show == 'out':
        where.append('s.contact_id IS NOT NULL')
    elif show == 'in':
        where.append('s.contact_id IS NULL')
    sql = (f'FROM list_members m JOIN contacts c ON c.id = m.contact_id '
           f'LEFT JOIN campaign_skips s ON s.contact_id = c.id AND s.campaign_id = ? WHERE {" AND ".join(where)}')
    total = db.val(f'SELECT COUNT(*) {sql}', campaign['id'], *args, default=0)
    rows = db.q(f'SELECT c.*, s.contact_id IS NOT NULL AS left_out {sql} ORDER BY m.added_at, c.id LIMIT ? OFFSET ?',
                campaign['id'], *args, per_page, (max(1, page) - 1) * per_page)
    own = {r['contact_id']: r for r in db.q('SELECT * FROM personalizations WHERE campaign_id = ?', campaign['id'])}
    sent = {r['contact_id']: r['status'] for r in db.q(
        "SELECT contact_id, status FROM messages WHERE campaign_id = ? AND step = 0", campaign['id'])}
    out = []
    for row in rows:
        subject, start = _subject_and_start(campaign, row, own.get(row['id']))
        name = f'{row["first_name"]} {row["last_name"]}'.strip()
        company = next((loads(row['fields']).get(k) for k in ('company', 'business', 'company_name')
                        if loads(row['fields']).get(k)), '')
        out.append({'id': row['id'], 'email': row['email'], 'name': name or row['email'], 'company': company,
                    'subject': subject, 'start': start, 'included': not row['left_out'],
                    'status': sent.get(row['id'], '')})
    return out, total


def counts(db, campaign):
    total = db.val("SELECT COUNT(*) FROM list_members m JOIN contacts c ON c.id = m.contact_id "
                   "WHERE m.list_id = ? AND c.status = 'active'", campaign['list_id'] or 0, default=0)
    out = db.val('SELECT COUNT(*) FROM campaign_skips s JOIN list_members m ON m.contact_id = s.contact_id '
                 'AND m.list_id = ? WHERE s.campaign_id = ?', campaign['list_id'] or 0, campaign['id'], default=0)
    return {'total': total, 'left_out': out, 'included': total - out}


def set_included(db, campaign, contact_ids, include):
    """Puts leads back in, or leaves them out. Emails already queued for a left-out lead are
    cancelled; emails already sent cannot be taken back. Returns how many changed."""
    changed = 0
    with db.tx():
        for contact_id in contact_ids:
            if include:
                changed += db.run('DELETE FROM campaign_skips WHERE campaign_id = ? AND contact_id = ?',
                                  campaign['id'], contact_id).rowcount
                db.run("UPDATE messages SET status = 'queued', error = '' WHERE campaign_id = ? AND contact_id = ? "
                       "AND status = 'cancelled' AND error = 'Left out by you'", campaign['id'], contact_id)
            else:
                changed += db.run('INSERT OR IGNORE INTO campaign_skips (campaign_id, contact_id, created_at) '
                                  'VALUES (?, ?, ?)', campaign['id'], contact_id, now()).rowcount
                db.run("UPDATE messages SET status = 'cancelled', error = 'Left out by you' WHERE campaign_id = ? "
                       "AND contact_id = ? AND status = 'queued'", campaign['id'], contact_id)
    return changed


__all__ = ['counts', 'leads', 'set_included']
