"""The owner's offers: what they sell, to whom, and what a lead should do next.

The outreach agent and the per-lead writer read the campaign's offer (or the
default one) so emails say true, specific things instead of guessing. The offer
agent (agent persona "offer") helps write them.
"""
from . import service
from .db import now

FIELDS = (
    ('name', 'Name', 'For you only, e.g. "Free homepage redesign".', 120),
    ('summary', 'The offer in one line', 'What you give them, in their words.', 400),
    ('audience', 'Who it is for', 'Be specific: type of business, size, place, situation.', 600),
    ('problem', 'The problem it fixes', 'What it costs them today.', 800),
    ('outcome', 'The result they get', 'What changes for them, ideally something they can see or count.', 800),
    ('details', 'What they get', 'What is included, how it works, price or "free", any guarantee.', 1500),
    ('proof', 'Why they can trust you', 'Only real facts: clients, numbers, examples, years.', 1200),
    ('case_studies', 'Case studies', 'One per line: who the client was, their problem, what changed. Real ones only; '
                     'the emails quote up to three.', 2400),
    ('call_to_action', 'The next step you ask for', 'Small and easy: reply "yes", a 10-minute call, look at a preview.', 400),
    ('notes', 'Notes for the writer', 'Tone, words to avoid, anything else.', 1500),
)
KEYS = tuple(f[0] for f in FIELDS)
LABELS = {f[0]: f[1] for f in FIELDS}


def everything(db):
    return db.q('SELECT * FROM offers ORDER BY is_default DESC, updated_at DESC')


def get(db, offer_id):
    row = db.one('SELECT * FROM offers WHERE id = ?', offer_id)
    if row is None:
        raise service.Problem(f'Offer {offer_id} does not exist')
    return row


def default(db):
    return db.one('SELECT * FROM offers ORDER BY is_default DESC, updated_at DESC LIMIT 1')


def for_campaign(db, campaign):
    """The campaign's own offer, else the default one, else None."""
    offer_id = campaign['offer_id'] if 'offer_id' in campaign.keys() else None
    if offer_id:
        row = db.one('SELECT * FROM offers WHERE id = ?', offer_id)
        if row is not None:
            return row
    return default(db)


def clean(data, existing=None):
    out = {}
    for key, _, _, limit in FIELDS:
        if key in data and data[key] is not None:
            out[key] = ' '.join(str(data[key]).split()) if key in ('name', 'summary') else str(data[key]).strip()
            out[key] = out[key][:limit]
    if existing is None and not out.get('name'):
        out['name'] = (out.get('summary') or 'My offer')[:80]
    if 'name' in out and not out['name']:
        raise service.Problem('Give the offer a name.')
    return out


def save(db, data, offer_id=None, make_default=None):
    """Creates or updates an offer. Returns its id. The first offer becomes the default."""
    existing = get(db, offer_id) if offer_id else None
    fields = clean(data, existing)
    ts = now()
    with db.tx():
        if existing is None:
            first = db.val('SELECT COUNT(*) FROM offers', default=0) == 0
            fields.update({'created_at': ts, 'updated_at': ts, 'is_default': 1 if first else 0})
            columns, marks = ', '.join(fields), ', '.join('?' for _ in fields)
            offer_id = db.run(f'INSERT INTO offers ({columns}) VALUES ({marks})', *fields.values()).lastrowid
        elif fields:
            sets = ', '.join(f'{k} = ?' for k in fields)
            db.run(f'UPDATE offers SET {sets}, updated_at = ? WHERE id = ?', *fields.values(), ts, offer_id)
        if make_default:
            set_default(db, offer_id)
    return offer_id


def set_default(db, offer_id):
    get(db, offer_id)
    with db.tx():
        db.run('UPDATE offers SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END', offer_id)


def delete(db, offer_id):
    row = get(db, offer_id)
    with db.tx():
        db.run('DELETE FROM offers WHERE id = ?', offer_id)
        db.run('UPDATE campaigns SET offer_id = NULL WHERE offer_id = ?', offer_id)
        if row['is_default']:
            db.run('UPDATE offers SET is_default = 1 WHERE id = (SELECT id FROM offers ORDER BY updated_at DESC LIMIT 1)')


def as_dict(row):
    return {'offer_id': row['id'], 'default': bool(row['is_default']),
            **{key: row[key] for key in KEYS if row[key]}}


def as_text(row):
    """The offer as labelled lines, for AI prompts."""
    if row is None:
        return ''
    return '\n'.join(f'{LABELS[key]}: {row[key]}' for key in KEYS if key != 'name' and row[key])


def completeness(row):
    """Which of the important parts are still empty."""
    return [LABELS[key] for key in ('summary', 'audience', 'outcome', 'case_studies', 'call_to_action') if not row[key]]


def case_studies(row):
    """The offer's case studies, one per item (at most three)."""
    if row is None or not row['case_studies']:
        return []
    return [line.strip(' -•\t') for line in row['case_studies'].splitlines() if line.strip(' -•\t')][:3]


__all__ = ['FIELDS', 'KEYS', 'LABELS', 'as_dict', 'as_text', 'clean', 'completeness', 'default', 'delete',
           'everything', 'for_campaign', 'get', 'save', 'set_default']
