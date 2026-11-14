"""Developer API for sending individual emails from other applications.

Authenticate with `Authorization: Bearer <api key>`. Request and response
shapes follow the conventions popularised by transactional email services:

    POST   /emails            send one email            -> {"id": "..."}
    POST   /emails/batch      send up to 100            -> {"data": [{"id": "..."}]}
    GET    /emails/<id>       status and event history
    POST   /emails/<id>/cancel  cancel a scheduled email
    POST   /contacts          create or update a contact
    GET    /lists             contact lists
    GET    /campaigns/<id>    campaign status and results
"""
import collections
import datetime
import threading
import time
from email.utils import parseaddr

import flask

from .. import contacts, security, stats
from ..db import loads, now
from ..events import iso
from ..sender import queue_single

bp = flask.Blueprint('api', __name__)

RATE_LIMIT = 20          # requests per second per key
MAX_RECIPIENTS = 50
MAX_BATCH = 100
_hits = collections.defaultdict(collections.deque)
_hits_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, name, message):
        super().__init__(message)
        self.status, self.name, self.message = status, name, message


@bp.errorhandler(ApiError)
def handle_api_error(error):
    return {'statusCode': error.status, 'name': error.name, 'message': error.message}, error.status


@bp.errorhandler(404)
def handle_404(_):
    return {'statusCode': 404, 'name': 'not_found', 'message': 'Not found'}, 404


@bp.errorhandler(405)
def handle_405(_):
    return {'statusCode': 405, 'name': 'method_not_allowed', 'message': 'Method not allowed'}, 405


def _db():
    return flask.current_app.db


@bp.before_request
def authenticate():
    header = flask.request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        raise ApiError(401, 'missing_api_key', 'Send the API key as "Authorization: Bearer <key>".')
    row = _db().one('SELECT id FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL',
                    security.hash_key(header[7:].strip()))
    if row is None:
        raise ApiError(403, 'invalid_api_key', 'This API key is not valid.')
    stamp = time.monotonic()
    with _hits_lock:
        window = _hits[row['id']]
        while window and stamp - window[0] > 1.0:
            window.popleft()
        if len(window) >= RATE_LIMIT:
            raise ApiError(429, 'rate_limit_exceeded', f'More than {RATE_LIMIT} requests per second.')
        window.append(stamp)
    _db().run('UPDATE api_keys SET last_used_at = ? WHERE id = ?', now(), row['id'])


def _json():
    data = flask.request.get_json(silent=True)
    if data is None:
        raise ApiError(400, 'validation_error', 'The request body must be JSON.')
    return data


def _addresses(value, field, required=False):
    if value in (None, '', []):
        if required:
            raise ApiError(422, 'missing_required_field', f'"{field}" is required.')
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        address = contacts.normalize_email(parseaddr(str(item))[1])
        if not address:
            raise ApiError(422, 'validation_error', f'"{item}" in "{field}" is not a valid email address.')
        out.append(address)
    if len(out) > MAX_RECIPIENTS:
        raise ApiError(422, 'validation_error', f'"{field}" accepts at most {MAX_RECIPIENTS} addresses.')
    return out


def _parse_email(data, settings):
    if not isinstance(data, dict):
        raise ApiError(422, 'validation_error', 'Each email must be a JSON object.')
    sender = data.get('from') or ''
    name, address = parseaddr(str(sender))
    address = contacts.normalize_email(address)
    if not address:
        address = contacts.normalize_email(settings.get('from_email'))
        name = name or settings.get('from_name', '')
        if not address:
            raise ApiError(422, 'missing_required_field', '"from" is required (no default sender is configured).')
    subject = str(data.get('subject') or '').strip()
    if not subject:
        raise ApiError(422, 'missing_required_field', '"subject" is required.')
    html, text = data.get('html'), data.get('text')
    if not html and not text:
        raise ApiError(422, 'missing_required_field', 'Provide "html" or "text".')
    headers = data.get('headers') or {}
    if not isinstance(headers, dict):
        raise ApiError(422, 'validation_error', '"headers" must be an object.')
    tags = data.get('tags') or []
    if not isinstance(tags, list) or any(not isinstance(t, dict) or 'name' not in t for t in tags):
        raise ApiError(422, 'validation_error', '"tags" must be a list of {"name", "value"} objects.')
    scheduled_at = None
    if data.get('scheduled_at'):
        try:
            moment = datetime.datetime.fromisoformat(str(data['scheduled_at']).replace('Z', '+00:00'))
        except ValueError:
            raise ApiError(422, 'validation_error', '"scheduled_at" must be an ISO 8601 date-time.')
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        scheduled_at = moment.timestamp()
    return dict(
        from_email=address, from_name=name, to=_addresses(data.get('to'), 'to', required=True),
        cc=_addresses(data.get('cc'), 'cc'), bcc=_addresses(data.get('bcc'), 'bcc'),
        reply_to=_addresses(data.get('reply_to'), 'reply_to'), subject=subject[:300],
        html=str(html or ''), text=None if text is None else str(text),
        headers={str(k): str(v) for k, v in headers.items()}, tags=tags, scheduled_at=scheduled_at)


@bp.route('/emails', methods=['POST'])
def send_email():
    db = _db()
    fields = _parse_email(_json(), db.settings())
    key = flask.request.headers.get('Idempotency-Key')
    if key:
        key = key[:256]
        existing = db.one('SELECT id FROM messages WHERE idempotency_key = ?', key)
        if existing:
            return {'id': existing['id']}
    return {'id': queue_single(db, kind='api', idempotency_key=key, **fields)}


@bp.route('/emails/batch', methods=['POST'])
def send_batch():
    db = _db()
    data = _json()
    if not isinstance(data, list) or not data:
        raise ApiError(422, 'validation_error', 'The body must be a non-empty JSON array of emails.')
    if len(data) > MAX_BATCH:
        raise ApiError(422, 'validation_error', f'A batch accepts at most {MAX_BATCH} emails.')
    settings = db.settings()
    parsed = [_parse_email(item, settings) for item in data]  # validate everything before queueing anything
    with db.tx():
        ids = [queue_single(db, kind='api', **fields) for fields in parsed]
    return {'data': [{'id': mid} for mid in ids]}


def _last_event(row):
    for column, name in (('complained_at', 'complained'), ('bounced_at', 'bounced'), ('clicked_at', 'clicked'),
                         ('opened_at', 'opened')):
        if row[column]:
            return name
    return {'queued': 'queued', 'sending': 'queued', 'pending_approval': 'scheduled', 'sent': 'sent',
            'failed': 'failed', 'cancelled': 'canceled'}.get(row['status'], row['status'])


@bp.route('/emails/<mid>', methods=['GET'])
def get_email(mid):
    db = _db()
    row = db.one('SELECT * FROM messages WHERE id = ?', mid)
    if row is None:
        raise ApiError(404, 'not_found', 'Email not found.')
    payload = loads(row['payload'])
    history = db.q('SELECT type, at, automated, detail FROM events WHERE message_id = ? ORDER BY id', mid)
    return {
        'object': 'email', 'id': row['id'], 'to': payload.get('to') or [row['to_email']],
        'from': row['from_email'], 'subject': row['subject'], 'created_at': iso(row['created_at']),
        'scheduled_at': iso(row['scheduled_at']) if row['scheduled_at'] else None,
        'sent_at': iso(row['sent_at']), 'last_event': _last_event(row), 'error': row['error'] or None,
        'tags': payload.get('tags') or [],
        'events': [{'type': f'email.{e["type"]}', 'created_at': iso(e['at']),
                    **({'automated': True} if e['automated'] else {}),
                    **({'detail': e['detail']} if e['detail'] else {})} for e in history],
    }


@bp.route('/emails/<mid>/cancel', methods=['POST'])
def cancel_email(mid):
    done = _db().run("UPDATE messages SET status = 'cancelled', error = 'Cancelled through the API' "
                     "WHERE id = ? AND status IN ('queued', 'pending_approval') AND kind != 'campaign'", mid).rowcount
    if not done:
        raise ApiError(409, 'not_cancellable', 'Only emails that have not been sent yet can be cancelled.')
    return {'object': 'email', 'id': mid}


@bp.route('/contacts', methods=['POST'])
def create_contact():
    db = _db()
    data = _json()
    if not isinstance(data, dict):
        raise ApiError(422, 'validation_error', 'The body must be a JSON object.')
    email = contacts.normalize_email(data.get('email'))
    if not email:
        raise ApiError(422, 'validation_error', '"email" is missing or not a valid address.')
    list_id = data.get('list_id')
    if list_id is not None and not db.one('SELECT id FROM lists WHERE id = ?', list_id):
        raise ApiError(404, 'not_found', f'List {list_id} does not exist.')
    properties = data.get('properties') or {}
    if not isinstance(properties, dict):
        raise ApiError(422, 'validation_error', '"properties" must be an object.')
    row = {'email': email, 'first_name': str(data.get('first_name') or ''),
           'last_name': str(data.get('last_name') or ''),
           'fields': {str(k): str(v) for k, v in properties.items()}}
    with db.tx():
        if list_id is not None:
            contacts.import_rows(db, list_id, [row])
        elif not db.one('SELECT id FROM contacts WHERE email = ?', email):
            ts = now()
            db.run('INSERT INTO contacts (email, first_name, last_name, fields, created_at, updated_at) '
                   'VALUES (?, ?, ?, ?, ?, ?)', email, row['first_name'], row['last_name'],
                   flask.json.dumps(row['fields']), ts, ts)
        if data.get('unsubscribed') is True:
            contacts.suppress(db, email, 'unsubscribed', 'Set through the API')
    contact_id = db.val('SELECT id FROM contacts WHERE email = ?', email)
    if contact_id is None:
        raise ApiError(422, 'validation_error', 'This address cannot be added (system mailbox).')
    return {'object': 'contact', 'id': contact_id}


@bp.route('/lists', methods=['GET'])
def get_lists():
    db = _db()
    data = []
    for row in db.q('SELECT * FROM lists ORDER BY id'):
        counts = contacts.list_counts(db, row['id'])
        data.append({'id': row['id'], 'name': row['name'], 'created_at': iso(row['created_at']),
                     'contacts': counts['total'], 'active': counts['active']})
    return {'object': 'list', 'data': data}


@bp.route('/campaigns/<int:campaign_id>', methods=['GET'])
def get_campaign(campaign_id):
    db = _db()
    row = db.one('SELECT * FROM campaigns WHERE id = ?', campaign_id)
    if row is None:
        raise ApiError(404, 'not_found', 'Campaign not found.')
    return {'object': 'campaign', 'id': row['id'], 'name': row['name'], 'subject': row['subject'],
            'status': row['status'], 'scheduled_at': iso(row['scheduled_at']), 'started_at': iso(row['started_at']),
            'finished_at': iso(row['finished_at']), 'stats': stats.campaign(db, campaign_id)}
