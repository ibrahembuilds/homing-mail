"""Uploaded leads that have no usable email address yet, and finding one.

An address the company publishes on its own website is added to the list at
once: it is where they ask to be contacted. A guess such as
first.last@company.com waits for the owner's OK, because guesses that bounce hurt
the sending domain.
"""
import concurrent.futures
import json
import logging
import re

from . import contacts, leadtools
from .db import loads, now

log = logging.getLogger('campaigns.prospects')

STATUSES = {'needs_email': 'Not searched yet', 'searching': 'Searching', 'guessed': 'Guess, needs your OK',
            'not_found': 'No email found', 'added': 'Added to the list', 'dismissed': 'Skipped'}
GENERIC = ('info', 'hello', 'enquiries', 'enquiry', 'contact', 'office', 'admin', 'mail', 'team', 'bookings',
           'booking', 'support', 'sales', 'study', 'tuition', 'learn')
WORKERS = 6
COMPANY_KEYS = ('company', 'company_name', 'business', 'organisation', 'organization', 'employer')


def add_missing(db, list_id, rows):
    """Rows of an upload without a valid email are kept as prospects. Returns how many."""
    kept, ts = 0, now()
    with db.tx():
        for row in rows:
            if contacts.normalize_email(row.get('email')):
                continue
            fields = {k: v for k, v in (row.get('fields') or {}).items() if v}
            website = leadtools.website_from_fields(fields)
            if not (website or any(fields.get(k) for k in COMPANY_KEYS)):
                continue      # nothing to search with
            if website and db.one('SELECT 1 FROM prospects WHERE list_id = ? AND website = ?', list_id, website):
                continue
            db.run('INSERT INTO prospects (list_id, first_name, last_name, fields, website, status, detail, '
                   'created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   list_id, row.get('first_name', '')[:100], row.get('last_name', '')[:100],
                   json.dumps(fields, ensure_ascii=False), website, 'needs_email' if website else 'not_found',
                   '' if website else 'No website in the file. Add one, or ask the agent to look the company up.',
                   ts, ts)
            kept += 1
    return kept


def import_upload(db, list_id, rows):
    """An uploaded file into a list: leads with an email join the list, the others wait for one."""
    summary = contacts.import_rows(db, list_id, rows)
    kept = add_missing(db, list_id, rows)
    summary['needs_email'] = kept
    summary['invalid'] = max(0, summary['invalid'] - kept)
    return summary


def label(prospect):
    fields = loads(prospect['fields'])
    company = next((fields[k] for k in COMPANY_KEYS if fields.get(k)), '')
    name = f'{prospect["first_name"]} {prospect["last_name"]}'.strip()
    return company or name or prospect['website'] or f'Lead {prospect["id"]}'


def _pick(found, first_name):
    """The best published address for this lead: one with their name, then a general inbox,
    then anything on their own domain or a personal mailbox they list."""
    usable = [e for e in found if e['own_domain'] or e['email'].split('@', 1)[1] in leadtools.FREE_PROVIDERS]
    if not usable:
        return None
    first = leadtools._ascii(first_name.split(' and ')[0]) if first_name else ''

    def rank(item):
        local = item['email'].split('@', 1)[0]
        if first and len(first) > 2 and first in leadtools._ascii(local):
            return 0
        if local in GENERIC:
            return 1
        return 2 if item['own_domain'] else 3
    return sorted(usable, key=rank)[0]


def find_one(db, settings, prospect):
    """Looks for this lead's address. Returns {'status', 'email', 'source', 'detail', 'others'}."""
    website = prospect['website']
    if not website:
        return {'status': 'not_found', 'email': '', 'source': '',
                'detail': 'No website to look on. Add one, or ask the agent to look the company up.'}
    try:
        site = leadtools.site_emails(db, website)
    except leadtools.ToolError as exc:
        site = {'emails': [], 'pages': [], 'error': str(exc)}
    best = _pick(site['emails'], prospect['first_name'])
    others = [e['email'] for e in site['emails'] if not best or e['email'] != best['email']][:4]
    if best and leadtools.check_domain(db, best['email'])['mail'] is not False:
        return {'status': 'found', 'email': best['email'], 'source': best['page'], 'others': others,
                'detail': f'Published on {best["page"]}'}
    first = prospect['first_name']
    if first and re.fullmatch(r"[A-Za-z][A-Za-z'-]+", first):
        try:
            result = leadtools.find_email(db, settings, first, prospect['last_name'], website)
        except leadtools.ToolError:
            result = {'candidates': []}
        if result['candidates']:
            top = result['candidates'][0]
            verified = top.get('source') == 'Hunter.io' and top.get('confidence') == 'high'
            return {'status': 'found' if verified else 'guessed', 'email': top['email'], 'source': top['source'],
                    'others': others, 'detail': ('Verified by Hunter.io' if verified else
                                                 f'Guessed ({top["source"]}); not on their website')}
    if site.get('error'):
        detail = f'Could not read {website}: {site["error"]}'
    else:
        detail = f'No email address on {website} (checked {len(site["pages"])} page{"s" if len(site["pages"]) != 1 else ""}).'
    return {'status': 'not_found', 'email': '', 'source': '', 'others': others, 'detail': detail}


def _add_to_list(db, prospect, email, source):
    fields = loads(prospect['fields'])
    if source:
        fields['email_source'] = source[:300]
    summary = contacts.import_rows(db, prospect['list_id'], [{
        'email': email, 'first_name': prospect['first_name'], 'last_name': prospect['last_name'], 'fields': fields}])
    contact = db.one('SELECT id FROM contacts WHERE email = ?', email)
    return summary, contact['id'] if contact else None


def _apply(db, prospect, result):
    ts = now()
    detail = result['detail'] + (f' Also on the site: {", ".join(result["others"])}.' if result.get('others') else '')
    if result['status'] == 'found':
        summary, contact_id = _add_to_list(db, prospect, result['email'], result['source'])
        if summary['suppressed'] or summary['role']:
            db.run("UPDATE prospects SET status = 'dismissed', email = ?, detail = ?, updated_at = ? WHERE id = ?",
                   result['email'], 'On your do-not-email list.', ts, prospect['id'])
            return 'dismissed'
        db.run("UPDATE prospects SET status = 'added', email = ?, source = ?, detail = ?, contact_id = ?, "
               "updated_at = ? WHERE id = ?", result['email'], result['source'][:300], detail[:600], contact_id, ts,
               prospect['id'])
        return 'added'
    db.run('UPDATE prospects SET status = ?, email = ?, source = ?, detail = ?, updated_at = ? WHERE id = ?',
           result['status'], result['email'], result['source'][:300], detail[:600], ts, prospect['id'])
    return result['status']


def find_emails(db, settings, list_id=None, ids=None, limit=40):
    """Searches for the addresses of waiting prospects (all of a list, or the given ids).
    Returns {'checked', 'added': [...], 'guessed': [...], 'not_found': [...]}."""
    where, args = ["status IN ('needs_email', 'searching')"], []
    if list_id:
        where.append('list_id = ?')
        args.append(list_id)
    if ids:
        where = [f'id IN ({",".join("?" for _ in ids)})', "status NOT IN ('added', 'dismissed')"]
        args = [int(i) for i in ids]
    rows = db.q(f'SELECT * FROM prospects WHERE {" AND ".join(where)} ORDER BY id LIMIT ?', *args, int(limit))
    if rows:
        db.run(f'UPDATE prospects SET status = \'searching\', updated_at = ? WHERE id IN ({",".join("?" for _ in rows)})',
               now(), *[r['id'] for r in rows])

    def job(prospect):
        try:
            return find_one(db, settings, prospect)
        finally:
            db.close()   # this thread's connection

    out = {'checked': len(rows), 'added': [], 'guessed': [], 'not_found': []}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [(row, pool.submit(job, row)) for row in rows]
        for row, future in futures:
            try:
                result = future.result()
            except Exception as exc:   # one bad site must not stop the rest
                log.exception('email search failed for prospect %s', row['id'])
                result = {'status': 'not_found', 'email': '', 'source': '', 'detail': f'Search failed: {exc}'[:300]}
            status = _apply(db, row, result)
            item = {'prospect_id': row['id'], 'lead': label(row), 'website': row['website'],
                    'email': result['email'] or None, 'detail': result['detail']}
            out.get(status if status in out else 'not_found', out['not_found']).append(item)
    return out


def set_email(db, prospect_id, email):
    """The owner (or the agent, on the owner's word) gives the address: the lead joins the list."""
    prospect = db.one('SELECT * FROM prospects WHERE id = ?', prospect_id)
    if prospect is None:
        raise ValueError(f'Lead {prospect_id} does not exist')
    address = contacts.normalize_email(email)
    if not address:
        raise ValueError(f'"{email}" is not a valid email address')
    summary, contact_id = _add_to_list(db, prospect, address, 'added by you')
    if summary['suppressed'] or summary['role']:
        raise ValueError(f'{address} is on the do-not-email list or is a system mailbox')
    db.run("UPDATE prospects SET status = 'added', email = ?, contact_id = ?, updated_at = ? WHERE id = ?",
           address, contact_id, now(), prospect_id)
    return contact_id


def dismiss(db, prospect_id):
    db.run("UPDATE prospects SET status = 'dismissed', updated_at = ? WHERE id = ?", now(), prospect_id)


def counts(db, list_id):
    return {r['status']: r['n'] for r in db.q('SELECT status, COUNT(*) AS n FROM prospects WHERE list_id = ? '
                                              'GROUP BY status', list_id)}


def waiting(db, list_id):
    """Prospects still without an address in the list (not added, not skipped)."""
    return db.q("SELECT * FROM prospects WHERE list_id = ? AND status NOT IN ('added', 'dismissed') "
                "ORDER BY CASE status WHEN 'guessed' THEN 0 WHEN 'searching' THEN 1 WHEN 'needs_email' THEN 2 "
                "ELSE 3 END, id", list_id)


def recover(db):
    """At start: a search cut short by a restart is done again."""
    db.run("UPDATE prospects SET status = 'needs_email' WHERE status = 'searching'")


def as_dict(row):
    return {'prospect_id': row['id'], 'lead': label(row), 'first_name': row['first_name'],
            'last_name': row['last_name'], 'website': row['website'] or None, 'status': row['status'],
            'email': row['email'] or None, 'detail': row['detail'] or None,
            **{k: str(v)[:200] for k, v in loads(row['fields']).items()}}


__all__ = ['STATUSES', 'add_missing', 'as_dict', 'counts', 'dismiss', 'find_emails', 'find_one', 'import_upload',
           'label', 'recover', 'set_email', 'waiting']
