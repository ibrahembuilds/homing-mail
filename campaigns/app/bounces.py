"""Bounce and spam-complaint processing.

Reads a mailbox over IMAP, understands delivery status notifications
(RFC 3464) and abuse feedback reports (RFC 5965), and updates the message,
the contact and the suppression list.
"""
import email
import email.policy
import imaplib
import logging
import re
import ssl

from . import contacts, events
from .db import now

log = logging.getLogger('campaigns.bounces')

REF_RE = re.compile(r'X-Message-Ref:\s*([0-9a-f]{32})', re.IGNORECASE)
VERP_RE = re.compile(r'\+([0-9a-f]{32})@', re.IGNORECASE)
MSGID_RE = re.compile(r'Message-ID:\s*<([0-9a-f]{32})@', re.IGNORECASE)
ADDR_RE = re.compile(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
SOFT_BOUNCE_LIMIT = 3


def parse_report(raw):
    """Classifies a mailbox message.

    Returns None for ordinary mail, otherwise a dict with
    kind ('bounce' | 'complaint'), message_id, recipient, hard, detail.
    """
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    text = raw.decode('utf-8', 'replace')
    report_type = (msg.get_param('report-type') or '').lower() if msg.get_content_type() == 'multipart/report' else ''

    message_id = None
    for pattern in (REF_RE, MSGID_RE):
        found = pattern.search(text)
        if found:
            message_id = found.group(1).lower()
            break
    if message_id is None:
        for header in ('Delivered-To', 'X-Original-To', 'To'):
            found = VERP_RE.search(str(msg.get(header, '')))
            if found:
                message_id = found.group(1).lower()
                break

    status = action = diagnostic = recipient = ''
    feedback = False
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype in ('message/delivery-status', 'message/feedback-report'):
            feedback = feedback or ctype == 'message/feedback-report'
            body = part.as_string()
            status = status or _field(body, 'Status')
            action = action or _field(body, 'Action')
            diagnostic = diagnostic or _field(body, 'Diagnostic-Code')
            recipient = recipient or _address(_field(body, 'Original-Rcpt-To') or _field(body, 'Final-Recipient')
                                              or _field(body, 'Original-Recipient'))

    if report_type == 'feedback-report' or feedback:
        return {'kind': 'complaint', 'message_id': message_id, 'recipient': recipient, 'hard': True,
                'detail': 'Recipient marked the message as spam'}

    sender = str(msg.get('From', '')).lower()
    looks_like_dsn = (report_type == 'delivery-status' or status
                      or 'mailer-daemon' in sender or 'postmaster' in sender)
    if not looks_like_dsn:
        return None
    if action.lower() in ('delayed', 'delivered', 'relayed', 'expanded'):
        return None
    if not status:
        found = re.search(r'\b([245]\.\d{1,3}\.\d{1,3})\b', text)
        status = found.group(1) if found else ''
        if not status:
            found = re.search(r'\b(5\d\d|4\d\d)[ -]', text)
            status = (found.group(1)[0] + '.0.0') if found else ''
    if not status and not message_id:
        return None
    hard = status.startswith('5') or (not status and action.lower() == 'failed')
    # Full mailbox and similar conditions are reported as 5.2.x but are temporary.
    if status.startswith(('5.2.2', '5.2.1', '5.3.')) or status.startswith('4'):
        hard = False
    return {'kind': 'bounce', 'message_id': message_id, 'recipient': recipient, 'hard': hard,
            'detail': ' '.join(f'{status} {diagnostic}'.split())[:500]}


def _field(body, name):
    found = re.search(rf'(?im)^{re.escape(name)}:\s*(.+(?:\n[ \t].+)*)', body)
    return ' '.join(found.group(1).split()) if found else ''


def _address(value):
    found = ADDR_RE.search(value or '')
    return found.group(0).lower() if found else ''


def apply_report(db, report):
    """Applies a parsed report. Returns True when it matched a message."""
    message = None
    if report.get('message_id'):
        message = db.one('SELECT * FROM messages WHERE id = ?', report['message_id'])
    if message is None and report.get('recipient'):
        message = db.one('SELECT * FROM messages WHERE to_email = ? AND sent_at IS NOT NULL '
                         'ORDER BY sent_at DESC LIMIT 1', report['recipient'])
    if message is None:
        return False
    ts = now()
    address = message['to_email']
    with db.tx():
        if report['kind'] == 'complaint':
            if message['complained_at'] is None:
                db.run("UPDATE messages SET status = 'complained', complained_at = ? WHERE id = ?", ts, message['id'])
                events.record(db, message, 'complained', detail=report['detail'])
            contacts.suppress(db, address, 'complained', report['detail'])
            return True
        if message['bounced_at'] is not None:
            return True
        kind = 'hard' if report['hard'] else 'soft'
        db.run("UPDATE messages SET status = 'bounced', bounced_at = ?, bounce_type = ?, error = ? WHERE id = ?",
               ts, kind, report['detail'], message['id'])
        events.record(db, message, 'bounced', detail=report['detail'], extra={'bounce': {'type': kind}})
        if report['hard']:
            contacts.suppress(db, address, 'bounced', report['detail'])
        else:
            db.run('UPDATE contacts SET soft_bounces = soft_bounces + 1 WHERE email = ?', address)
            count = db.val('SELECT soft_bounces FROM contacts WHERE email = ?', address, default=0)
            if count >= SOFT_BOUNCE_LIMIT:
                contacts.suppress(db, address, 'bounced', f'{count} soft bounces in a row: {report["detail"]}')
    return True


def poll(db, cfg, settings):
    """Processes unread messages in the bounce mailbox. Returns a result dict."""
    user = (settings.get('imap_user') or '').strip()
    password = settings.get('imap_password') or ''
    if not user or not password:
        return {'configured': False}
    host = settings.get('imap_host') or cfg.IMAP_HOST
    port = int(settings.get('imap_port') or cfg.IMAP_PORT)
    context = ssl.create_default_context()
    if settings.get('imap_verify_tls') != '1':
        # Inside the compose network the server is reached by its service
        # name, which never matches the public certificate.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    result = {'configured': True, 'seen': 0, 'bounces': 0, 'complaints': 0, 'unmatched': 0}
    conn = None
    try:
        if port == 143:
            conn = imaplib.IMAP4(host, port, timeout=30)
            conn.starttls(ssl_context=context)
        else:
            conn = imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=30)
        conn.login(user, password)
        conn.select('INBOX')
        status, data = conn.search(None, 'UNSEEN')
        numbers = data[0].split() if status == 'OK' and data and data[0] else []
        for number in numbers[:200]:
            status, parts = conn.fetch(number, '(RFC822)')
            if status != 'OK' or not parts or not isinstance(parts[0], tuple):
                continue
            result['seen'] += 1
            try:
                report = parse_report(parts[0][1])
                if report is None:
                    continue
                if apply_report(db, report):
                    result['bounces' if report['kind'] == 'bounce' else 'complaints'] += 1
                else:
                    result['unmatched'] += 1
            except Exception:  # one unreadable report must not stop the rest
                log.exception('could not process report %s', number)
            finally:
                conn.store(number, '+FLAGS', '\\Seen')
        result['ok'] = True
    except (OSError, imaplib.IMAP4.error) as exc:
        result['ok'] = False
        result['error'] = f'{type(exc).__name__}: {exc}'[:300]
    finally:
        if conn is not None:
            try:
                conn.logout()
            except (OSError, imaplib.IMAP4.error):
                pass
    return result
