"""Event log and webhook fan-out."""
import datetime
import json
import time

import requests

from . import security
from .db import now

EVENT_TYPES = ('queued', 'sent', 'failed', 'opened', 'clicked', 'bounced',
               'complained', 'unsubscribed', 'suppressed')


def iso(ts):
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def record(db, message, type_, *, automated=False, link_id=None, ip='', user_agent='', detail='', extra=None):
    """Stores an event for a message and queues it for every matching webhook."""
    ts = now()
    db.run('INSERT INTO events (message_id, campaign_id, type, at, automated, link_id, ip, user_agent, detail) '
           'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
           message['id'], message['campaign_id'], type_, ts, 1 if automated else 0, link_id,
           (ip or '')[:64], (user_agent or '')[:300], (detail or '')[:1000])
    hooks = db.q('SELECT id, events FROM webhooks WHERE active = 1')
    if not hooks:
        return
    name = f'email.{type_}'
    data = {'email_id': message['id'], 'to': message['to_email'], 'from': message['from_email'],
            'subject': message['subject'], 'campaign_id': message['campaign_id'], 'kind': message['kind']}
    if automated:
        data['automated'] = True
    if detail:
        data['detail'] = detail[:1000]
    if extra:
        data.update(extra)
    body = json.dumps({'type': name, 'created_at': iso(ts), 'data': data})
    for hook in hooks:
        wanted = [e.strip() for e in hook['events'].split(',') if e.strip()]
        if '*' in wanted or name in wanted:
            db.run('INSERT INTO webhook_deliveries (webhook_id, event_type, body, created_at) VALUES (?, ?, ?, ?)',
                   hook['id'], name, body, ts)


RETRY_DELAYS = (30, 120, 600, 3600, 6 * 3600)


def deliver_webhooks(db, limit=20):
    """Sends pending webhook deliveries; called from the worker loop."""
    ts = now()
    rows = db.q("SELECT d.*, w.url, w.secret FROM webhook_deliveries d JOIN webhooks w ON w.id = d.webhook_id "
                "WHERE d.status = 'pending' AND d.next_attempt_at <= ? AND w.active = 1 "
                "ORDER BY d.id LIMIT ?", ts, limit)
    for row in rows:
        body = row['body'].encode()
        stamp = str(int(time.time()))
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'campaigns-webhook/1.0',
            'X-Webhook-Id': str(row['id']),
            'X-Webhook-Timestamp': stamp,
            'X-Webhook-Signature': 'sha256=' + security.webhook_signature(row['secret'], stamp, body),
        }
        ok, response = False, ''
        try:
            reply = requests.post(row['url'], data=body, headers=headers, timeout=10, allow_redirects=False)
            ok = 200 <= reply.status_code < 300
            response = f'HTTP {reply.status_code}'
        except requests.RequestException as exc:
            response = type(exc).__name__
        attempts = row['attempts'] + 1
        if ok:
            db.run("UPDATE webhook_deliveries SET status = 'delivered', attempts = ?, response = ? WHERE id = ?",
                   attempts, response, row['id'])
        elif attempts > len(RETRY_DELAYS):
            db.run("UPDATE webhook_deliveries SET status = 'failed', attempts = ?, response = ? WHERE id = ?",
                   attempts, response, row['id'])
        else:
            db.run('UPDATE webhook_deliveries SET attempts = ?, response = ?, next_attempt_at = ? WHERE id = ?',
                   attempts, response, now() + RETRY_DELAYS[attempts - 1], row['id'])
    # Keep the table from growing without bound.
    db.run("DELETE FROM webhook_deliveries WHERE status != 'pending' AND created_at < ?", ts - 14 * 86400)
