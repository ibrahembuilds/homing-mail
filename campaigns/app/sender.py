"""Sending engine.

Campaign mail is paced: a rate per hour (or a random delay range), an optional
sending window, a daily cap per campaign, a per-recipient-domain hourly cap and
a server-wide warm-up ceiling. Single messages (API, tests, agent) go out at
once. Every message is claimed before the SMTP conversation so that a crash can
never produce a duplicate.
"""
import datetime
import json
import logging
import random
import re
import smtplib
import ssl
from zoneinfo import ZoneInfo

from . import contacts, events, leadtools, mailboxes, security, writer
from .db import loads, now
from .render import Renderer

log = logging.getLogger('campaigns.sender')

MAX_ATTEMPTS = 3
MAX_PER_TICK = 10
FOLLOW_UP_INTERVAL = 20   # seconds between scans for follow-ups that have become due
# Lead statuses that end a sequence: the lead answered, or the operator moved them on.
ACTIVE_LEAD_STATUSES = ('new', 'contacted')
CATCH_UP = 2.0   # seconds of missed sends a campaign may make up for
OPEN_WAIT_DAYS = 14   # how long an "only if they opened it" follow-up waits for an open
REJECT_STREAK_LIMIT = 5
# Reputation guards: pause a campaign that is clearly hurting the sender.
GUARD_MIN_SENT = 50
GUARD_BOUNCE_RATE = 0.08
GUARD_COMPLAINT_RATE = 0.005


# -- time helpers -------------------------------------------------------------
def timezone(settings):
    try:
        return ZoneInfo(settings.get('timezone') or 'UTC')
    except Exception:
        return datetime.timezone.utc


def local_time(settings, ts=None):
    return datetime.datetime.fromtimestamp(ts if ts is not None else now(), timezone(settings))


def day_start(settings, ts=None):
    local = local_time(settings, ts)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def in_window(campaign, local):
    """True when the campaign may send at this local time."""
    days = campaign['window_days'] or '0123456'
    if str(local.weekday()) not in days:
        return False
    start, end = campaign['window_start'], campaign['window_end']
    if not start or not end or start == end:
        return True
    current = local.strftime('%H:%M')
    if start < end:
        return start <= current < end
    return current >= start or current < end  # window that crosses midnight


def warmup_cap(settings, ts=None):
    """Server-wide ceiling for campaign mail today, or None when warm-up is off."""
    if settings.get('warmup_enabled') != '1':
        return None
    ts = ts if ts is not None else now()
    try:
        started = float(settings.get('warmup_started_at') or ts)
        first = max(1, int(settings.get('warmup_start') or 50))
        growth = max(1.0, float(settings.get('warmup_growth') or 1.5))
        ceiling = max(first, int(settings.get('warmup_max') or 5000))
    except ValueError:
        return None
    days = max(0, round((day_start(settings, ts) - day_start(settings, started)) / 86400))
    return int(min(ceiling, first * growth ** min(days, 60)))


def campaign_delay(campaign):
    """Seconds to wait before this campaign's next message."""
    low, high = campaign['delay_min'], campaign['delay_max']
    if low is not None and high is not None and high >= low and high > 0:
        return random.uniform(low, high)
    return 3600.0 / max(1, campaign['rate_per_hour'] or 1)


# -- SMTP ---------------------------------------------------------------------
class ServerDown(Exception):
    """The mail server could not be reached or refused the session."""


class Mailer:
    def __init__(self, cfg, settings, account=None):
        """account: a mailboxes row with its own SMTP server (smtp_host '' = this server)."""
        if account is not None and account['smtp_host']:
            self.host, self.port = account['smtp_host'], int(account['smtp_port'] or 587)
            self.user = account['smtp_user'] or account['email']
            self.password = account['smtp_password']
            self.tls = account['smtp_tls'] or 'starttls'
        elif settings.get('smtp_host'):
            self.host = settings['smtp_host']
            self.port = int(settings.get('smtp_port') or 25)
            self.user = settings.get('smtp_user', '')
            self.password = settings.get('smtp_password', '')
            self.tls = settings.get('smtp_tls') or 'none'
        else:
            self.host, self.port = cfg.SMTP_HOST, cfg.SMTP_PORT
            self.user, self.password, self.tls = cfg.SMTP_USER, cfg.SMTP_PASSWORD, cfg.SMTP_TLS
        self.helo = cfg.HOSTNAME or None
        self.conn = None

    def _connect(self):
        context = ssl.create_default_context()
        try:
            if self.tls == 'ssl':
                conn = smtplib.SMTP_SSL(self.host, self.port, timeout=30, local_hostname=self.helo, context=context)
            else:
                conn = smtplib.SMTP(self.host, self.port, timeout=30, local_hostname=self.helo)
                if self.tls == 'starttls':
                    conn.starttls(context=context)
            if self.user:
                conn.login(self.user, self.password)
        except (OSError, smtplib.SMTPException) as exc:
            raise ServerDown(f'{self.host}:{self.port}: {_short(exc)}') from exc
        self.conn = conn

    def _ready(self):
        if self.conn is not None:
            try:
                if self.conn.noop()[0] == 250:
                    return
            except (OSError, smtplib.SMTPException):
                pass
            self.close()
        self._connect()

    def send(self, envelope_from, recipients, msg):
        self._ready()
        return self.conn.send_message(msg, from_addr=envelope_from, to_addrs=recipients)

    def close(self):
        if self.conn is not None:
            try:
                self.conn.quit()
            except (OSError, smtplib.SMTPException):
                pass
            self.conn = None


def _short(exc):
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        parts = [f'{addr}: {code} {_text(reply)}' for addr, (code, reply) in exc.recipients.items()]
        return '; '.join(parts)[:500]
    if isinstance(exc, smtplib.SMTPResponseException):
        return f'{exc.smtp_code} {_text(exc.smtp_error)}'[:500]
    return f'{type(exc).__name__}: {exc}'[:500]


def _text(value):
    return value.decode('utf-8', 'replace') if isinstance(value, bytes) else str(value)


def envelope_sender(settings, mid, from_email):
    """Return-path. With a bounce mailbox configured every message gets its own
    address (bounces+<id>@domain) so a bounce identifies the exact message."""
    bounce = (settings.get('bounce_address') or '').strip()
    if '@' in bounce:
        local, domain = bounce.rsplit('@', 1)
        return f'{local}+{mid}@{domain}'
    return from_email


# -- queueing -----------------------------------------------------------------
def queue_single(db, *, kind, from_email, to, subject, html='', text=None, from_name='', cc=None,
                 bcc=None, reply_to=None, headers=None, tags=None, scheduled_at=None,
                 idempotency_key=None, track=True, mid=None, status='queued', contact_id=None, mailbox_id=None):
    """Queues one message outside any campaign. Returns its id.

    status='pending_approval' parks the message until the operator approves it.
    mailbox_id sends it through that email account instead of this server.
    """
    mid = mid or security.new_id()
    payload = {'from_email': from_email, 'from_name': from_name, 'to': list(to), 'cc': list(cc or []),
               'bcc': list(bcc or []), 'reply_to': list(reply_to or []), 'headers': headers or {},
               'tags': tags or [], 'html': html or '', 'text': text, 'track': track}
    first = to[0]
    with db.tx():
        db.run('INSERT INTO messages (id, kind, contact_id, to_email, to_domain, from_email, subject, status, '
               'scheduled_at, payload, idempotency_key, created_at, mailbox_id) '
               'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
               mid, kind, contact_id, first, first.rsplit('@', 1)[-1].lower(), from_email, subject, status,
               scheduled_at or 0, json.dumps(payload), idempotency_key, now(), mailbox_id)
        if status == 'queued':
            events.record(db, db.one('SELECT * FROM messages WHERE id = ?', mid), 'queued')
    return mid


class MissingStep(Exception):
    """A queued follow-up whose step was deleted."""


def message_content(db, campaign, row):
    """What one campaign message says, beyond the campaign's own draft:
    the follow-up step's text, the lead's AI-written version, and the earlier
    email a follow-up answers. Returns keyword arguments for the renderer."""
    out = {'content': None, 'ai_line': None, 'parent_id': None}
    if campaign['ai_mode']:
        mine = db.one("SELECT * FROM personalizations WHERE campaign_id = ? AND contact_id = ? "
                      "AND status IN ('done', 'edited')", campaign['id'], row['contact_id'])
        if mine is not None:
            if campaign['ai_mode'] == 'line':
                out['ai_line'] = mine['body']
            elif row['step'] == 0:
                out['content'] = {'subject': mine['subject'], 'html': mine['body'], 'text': '',
                                  'preheader': campaign['preheader']}
    if row['step']:
        step = db.one('SELECT * FROM campaign_steps WHERE campaign_id = ? AND position = ?',
                      campaign['id'], row['step'])
        if step is None:
            raise MissingStep()
        if step['as_reply']:
            parent = db.one('SELECT subject FROM messages WHERE id = ?', row['parent_id']) if row['parent_id'] else None
            base = re.sub(r'(?i)^\s*((re|aw|sv)\s*:\s*)+', '', (parent['subject'] if parent else '') or campaign['subject'])
            subject = f'Re: {base}'
            out['parent_id'] = row['parent_id']
        else:
            subject = step['subject'] or campaign['subject']
        out['content'] = {'subject': subject, 'html': step['html'], 'text': '', 'preheader': ''}
    return out


class Scan:
    """What a follow-up scan found for one campaign."""

    def __init__(self):
        self.pending, self.next_due, self.next_step = 0, None, None
        # "opened_no_reply" steps: leads who have not opened yet but still may.
        self.open_waiting, self.open_until, self.open_step = 0, None, None


def follow_up_candidates(db, campaign_id, step, due_before=None, limit=2000, unopened_since=None):
    """Leads who got the previous email and should get this follow-up: no
    reply, still subscribed, not moved on by the operator, and (for
    "no_reply_no_open") never opened. With due_before, only those whose wait
    is over by then. With unopened_since, for an "opened_no_reply" step: the
    leads sent the previous email since then who have not opened it yet."""
    sql = ("SELECT m.id, m.contact_id, m.to_email, m.to_domain, m.sent_at FROM messages m "
           "JOIN contacts c ON c.id = m.contact_id "
           "WHERE m.campaign_id = ? AND m.kind = 'campaign' AND m.step = ? AND m.status = 'sent' "
           "AND m.sent_at IS NOT NULL AND c.status = 'active' "
           f"AND c.lead_status IN ({', '.join('?' for _ in ACTIVE_LEAD_STATUSES)}) "
           "AND NOT EXISTS (SELECT 1 FROM suppressions s WHERE s.email = c.email) "
           "AND NOT EXISTS (SELECT 1 FROM messages x WHERE x.campaign_id = m.campaign_id "
           "  AND x.contact_id = m.contact_id AND x.step = ?) "
           "AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.campaign_id = m.campaign_id "
           "  AND r.contact_id = m.contact_id AND r.replied_at IS NOT NULL)")
    args = [campaign_id, step['position'] - 1, *ACTIVE_LEAD_STATUSES, step['position']]
    if step['send_if'] == 'no_reply_no_open' or unopened_since is not None:
        sql += (" AND NOT EXISTS (SELECT 1 FROM messages o WHERE o.campaign_id = m.campaign_id "
                "AND o.contact_id = m.contact_id AND o.opened_at IS NOT NULL)")
        if unopened_since is not None:
            sql += ' AND m.sent_at >= ?'
            args.append(unopened_since)
    elif step['send_if'] == 'opened_no_reply':
        sql += (" AND EXISTS (SELECT 1 FROM messages o WHERE o.campaign_id = m.campaign_id "
                "AND o.contact_id = m.contact_id AND o.opened_at IS NOT NULL)")
    if due_before is not None:
        sql += ' AND m.sent_at <= ?'
        args.append(due_before - step['delay_days'] * 86400)
    sql += ' ORDER BY m.sent_at, m.rowid LIMIT ?'
    args.append(limit)
    return db.q(sql, *args)


def ab_active(campaign):
    """True when the campaign tests a second subject line. In "full" AI mode
    the AI writes each subject, so there is nothing to test."""
    return bool((campaign['subject_b'] or '').strip()) and campaign['ai_mode'] != 'full'


def assign_variants(db, campaign):
    """Splits the first emails into test groups A and B (ab_percent of the
    list, at least 2 each) and a rest that waits for the winner."""
    ids = [r['id'] for r in db.q("SELECT id FROM messages WHERE campaign_id = ? AND step = 0 AND status = 'queued' "
                                 "AND variant = '' ORDER BY rowid", campaign['id'])]
    if not ids:
        return
    test = max(4, -(-len(ids) * max(1, min(100, campaign['ab_percent'] or 20)) // 100))
    if len(ids) - test < 2:
        test = len(ids)   # too few to hold any back: everyone is in the test
    db.many('UPDATE messages SET variant = ? WHERE id = ?',
            [('a' if i % 2 == 0 else 'b', mid) for i, mid in enumerate(ids[:test])]
            + [('rest', mid) for mid in ids[test:]])


def ab_results(db, campaign_id):
    """Results of subjects A and B among the test emails (and the rest that got the winner)."""
    out = {}
    for variant in ('a', 'b'):
        # A bounce refused during the send has no sent_at, so "sent" counts it here.
        row = db.one("SELECT SUM(CASE WHEN sent_at IS NOT NULL OR bounced_at IS NOT NULL THEN 1 ELSE 0 END) AS sent, "
                     "SUM(CASE WHEN bounced_at IS NOT NULL THEN 1 ELSE 0 END) AS bounced, "
                     "SUM(CASE WHEN opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened, "
                     "SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked, "
                     "SUM(CASE WHEN EXISTS (SELECT 1 FROM messages r WHERE r.campaign_id = m.campaign_id "
                     "  AND r.contact_id = m.contact_id AND r.replied_at IS NOT NULL) THEN 1 ELSE 0 END) AS replied "
                     "FROM messages m WHERE m.campaign_id = ? AND m.step = 0 AND m.variant = ?", campaign_id, variant)
        sent = row['sent'] or 0
        delivered = max(sent - (row['bounced'] or 0), 0)

        def rate(n):
            return round(100.0 * (n or 0) / delivered, 1) if delivered else 0.0
        out[variant] = {'sent': sent, 'opened': row['opened'] or 0, 'replied': row['replied'] or 0,
                        'open_rate': rate(row['opened']), 'click_rate': rate(row['clicked']),
                        'reply_rate': rate(row['replied'])}
    return out


def ab_winner(db, campaign_id):
    """'a' or 'b': more replies wins, then more opens, then more clicks; a tie keeps A."""
    results = ab_results(db, campaign_id)

    def score(v):
        return (results[v]['reply_rate'], results[v]['open_rate'], results[v]['click_rate'])
    return 'b' if score('b') > score('a') else 'a'


def _insert_members(db, campaign, ts):
    """Queues a first email for every eligible list member that has none yet.
    Returns how many were added."""
    skipped = ('invalid', 'risky') if campaign['skip_risky'] else ('invalid',)
    return db.run(
        "INSERT OR IGNORE INTO messages (id, campaign_id, contact_id, kind, to_email, to_domain, from_email, "
        "subject, status, created_at) "
        "SELECT lower(hex(randomblob(16))), ?, c.id, 'campaign', lower(c.email), "
        "lower(substr(c.email, instr(c.email, '@') + 1)), ?, ?, 'queued', ? "
        "FROM list_members m JOIN contacts c ON c.id = m.contact_id "
        f"WHERE m.list_id = ? AND c.status = 'active' AND c.verify_status NOT IN ({', '.join('?' for _ in skipped)}) "
        "AND c.email NOT IN (SELECT email FROM suppressions) ORDER BY m.added_at, c.id",
        campaign['id'], campaign['from_email'], campaign['subject'], ts, campaign['list_id'], *skipped).rowcount


def add_late_members(db, campaign):
    """Leads added to the list while the campaign runs get the first email too.
    During a subject test they wait for the winner with the rest."""
    if not campaign['list_id']:
        return 0
    leadtools.verify_list(db, campaign['list_id'])
    with db.tx():
        added = _insert_members(db, campaign, now())
        if added and ab_active(campaign):
            db.run("UPDATE messages SET variant = 'rest' WHERE campaign_id = ? AND step = 0 AND variant = '' "
                   "AND status = 'queued'", campaign['id'])
    return added


def enqueue_campaign(db, campaign):
    """Creates one queued message per eligible list member. Returns the count."""
    if campaign['list_id']:
        leadtools.verify_list(db, campaign['list_id'])
    ts = now()
    writer.ensure_rows(db, campaign)
    with db.tx():
        _insert_members(db, campaign, ts)
        if ab_active(campaign):
            assign_variants(db, campaign)
        total = db.val("SELECT COUNT(*) FROM messages WHERE campaign_id = ? AND status = 'queued'",
                       campaign['id'], default=0)
        if total:
            db.run("UPDATE campaigns SET status = 'sending', started_at = COALESCE(started_at, ?), "
                   "next_send_at = ?, hold_reason = '', updated_at = ? WHERE id = ?", ts, ts, ts, campaign['id'])
        else:
            db.run("UPDATE campaigns SET status = 'sent', started_at = COALESCE(started_at, ?), finished_at = ?, "
                   "hold_reason = 'No eligible recipients in the list', updated_at = ? WHERE id = ?",
                   ts, ts, ts, campaign['id'])
    return total


# -- engine -------------------------------------------------------------------
class Engine:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.renderer = Renderer(cfg, db)
        self.follow_up_interval = FOLLOW_UP_INTERVAL
        self._scanned = {}   # campaign id -> (time of last scan, Scan)
        self._account_mailers = {}   # mailbox id -> Mailer, for one tick
        self._account_hold = ''

    def recover(self):
        """Run once at worker start: anything left mid-send is not retried."""
        self.db.run("UPDATE messages SET status = 'failed', "
                    "error = 'Interrupted during send; not retried to avoid a duplicate' "
                    "WHERE status = 'sending'")

    def tick(self):
        """One pass of the engine. Returns the number of messages handed to SMTP."""
        db = self.db
        settings = db.settings()
        for campaign in db.q("SELECT * FROM campaigns WHERE status = 'scheduled' AND scheduled_at <= ?", now()):
            enqueue_campaign(db, campaign)
        mailer = Mailer(self.cfg, settings)
        self._account_mailers = {}
        sent = 0
        try:
            sent += self._send_singles(settings, mailer)
            sent += self._send_campaigns(settings, mailer)
        except ServerDown as exc:
            log.warning('mail server unavailable: %s', exc)
            db.set_state('server_down', str(exc))
            db.set_state('server_down_until', now() + 30)
        else:
            if db.state('server_down'):
                db.set_state('server_down', '')
        finally:
            mailer.close()
            for account_mailer in self._account_mailers.values():
                account_mailer.close()
        return sent

    # -- single messages ---------------------------------------------------
    def _send_singles(self, settings, mailer):
        db = self.db
        if float(db.state('server_down_until', '0') or 0) > now():
            return 0
        rows = db.q("SELECT * FROM messages WHERE status = 'queued' AND kind != 'campaign' AND scheduled_at <= ? "
                    "ORDER BY scheduled_at, rowid LIMIT 20", now())
        sent = 0
        for row in rows:
            payload = loads(row['payload'])
            recipients = [a for a in payload.get('to', []) + payload.get('cc', []) + payload.get('bcc', [])]
            blocked = [a for a in recipients
                       if (contacts.is_suppressed(db, a) or {'reason': ''})['reason'] in ('bounced', 'complained')]
            recipients = [a for a in recipients if a not in blocked]
            if not recipients:
                db.run("UPDATE messages SET status = 'failed', error = ? WHERE id = ? AND status = 'queued'",
                       'Recipient is on the suppression list (earlier bounce or complaint)', row['id'])
                events.record(db, row, 'suppressed', detail=', '.join(blocked))
                continue
            try:
                msg = self.renderer.single_message(row, settings)
            except (ValueError, KeyError, TypeError) as exc:
                db.run("UPDATE messages SET status = 'failed', error = ? WHERE id = ?",
                       f'Could not build message: {exc}'[:500], row['id'])
                events.record(db, row, 'failed', detail=str(exc))
                continue
            account = db.one('SELECT * FROM mailboxes WHERE id = ?', row['mailbox_id']) if row['mailbox_id'] else None
            if account is not None and account['smtp_host']:
                via = self._account_mailers.get(account['id'])
                if via is None:
                    via = self._account_mailers[account['id']] = Mailer(self.cfg, settings, account)
                try:
                    outcome = self._deliver(row, msg, account['email'], recipients, via)
                except ServerDown as exc:
                    mailboxes.mark_down(db, account, exc)
                    db.run("UPDATE messages SET scheduled_at = ?, error = ? WHERE id = ? AND status = 'queued'",
                           now() + 300, str(exc)[:500], row['id'])
                    continue
            else:
                envelope = envelope_sender(settings, row['id'], row['from_email'])
                outcome = self._deliver(row, msg, envelope, recipients, mailer)
            if outcome == 'sent':
                sent += 1
        return sent

    # -- campaigns ---------------------------------------------------------
    def _send_campaigns(self, settings, mailer):
        db = self.db
        ts = now()
        if float(db.state('server_down_until', '0') or 0) > ts:
            return 0
        local = local_time(settings, ts)
        today = day_start(settings, ts)
        global_cap = warmup_cap(settings, ts)
        global_sent = db.val("SELECT COUNT(*) FROM messages WHERE kind = 'campaign' AND sent_at >= ?", today, default=0)
        domain_cap = _int(settings.get('domain_hourly_cap'))
        sent_total = 0

        for campaign in db.q("SELECT * FROM campaigns WHERE status = 'sending' ORDER BY started_at, id"):
            cid = campaign['id']
            self._follow_ups(campaign)
            queued = db.val("SELECT COUNT(*) FROM messages WHERE campaign_id = ? AND status = 'queued'", cid, default=0)
            if not queued:
                scan = self._follow_ups(campaign, force=True)
                queued = db.val("SELECT COUNT(*) FROM messages WHERE campaign_id = ? AND status = 'queued'",
                                cid, default=0)
            if not queued:
                if scan.pending:
                    when = local_time(settings, scan.next_due).strftime('%d %b %H:%M')
                    self._hold(campaign, f'Follow-up {scan.next_step} goes out from {when} '
                                         f'({scan.pending} lead{"s" if scan.pending != 1 else ""} waiting)')
                    continue
                if scan.open_waiting:
                    when = local_time(settings, scan.open_until).strftime('%d %b %H:%M')
                    self._hold(campaign, f'Follow-up {scan.open_step} goes to leads who open the earlier email; '
                                         f'waiting for {scan.open_waiting} lead{"s" if scan.open_waiting != 1 else ""} '
                                         f'until {when}')
                    continue
                db.run("UPDATE campaigns SET status = 'sent', finished_at = ?, hold_reason = '', updated_at = ? "
                       "WHERE id = ? AND status = 'sending'", now(), now(), cid)
                continue
            guard = self._reputation_guard(cid)
            if guard:
                db.run("UPDATE campaigns SET status = 'paused', hold_reason = ?, updated_at = ? WHERE id = ?",
                       guard, now(), cid)
                continue
            if not in_window(campaign, local):
                self._hold(campaign, 'Waiting for the sending window')
                continue
            campaign = self._ab_decide(campaign)
            campaign_sent_today = db.val('SELECT COUNT(*) FROM messages WHERE campaign_id = ? AND sent_at >= ?',
                                         cid, today, default=0)
            sent_here = 0
            hold = ''
            next_send_at = campaign['next_send_at']
            while sent_here < MAX_PER_TICK and next_send_at <= now():
                if campaign['daily_cap'] and campaign_sent_today >= campaign['daily_cap']:
                    hold = f'Daily limit of {campaign["daily_cap"]} reached; resumes tomorrow'
                    break
                if global_cap is not None and global_sent >= global_cap:
                    hold = f'Warm-up limit of {global_cap} emails today reached; resumes tomorrow'
                    break
                row = self._next_message(campaign, domain_cap)
                if row is None:
                    hold = self._why_waiting(campaign, domain_cap)
                    break
                outcome = self._send_campaign_message(campaign, row, settings, mailer, today)
                if outcome == 'skip':
                    continue
                if outcome == 'no_account':
                    hold = self._account_hold
                    break
                # Keep the cadence: when this send was due a moment ago, the next
                # one is timed from when it was due, so fast rates are really met.
                base = next_send_at if next_send_at > now() - CATCH_UP else now()
                next_send_at = base + campaign_delay(campaign)
                db.run('UPDATE campaigns SET next_send_at = ? WHERE id = ?', next_send_at, cid)
                if outcome == 'sent':
                    sent_here += 1
                    campaign_sent_today += 1
                    global_sent += 1
                    db.set_state(f'reject_streak:{cid}', 0)
                elif outcome == 'rejected':
                    streak = int(db.state(f'reject_streak:{cid}', '0') or 0) + 1
                    db.set_state(f'reject_streak:{cid}', streak)
                    if streak >= REJECT_STREAK_LIMIT:
                        last = db.val('SELECT error FROM messages WHERE id = ?', row['id'], default='')
                        db.run("UPDATE campaigns SET status = 'paused', hold_reason = ?, updated_at = ? WHERE id = ?",
                               f'Paused automatically: the mail server rejected {streak} messages in a row. '
                               f'Last reply: {last}'[:500], now(), cid)
                        db.set_state(f'reject_streak:{cid}', 0)
                        hold = None
                        break
            if hold is not None:
                self._hold(campaign, hold)
            sent_total += sent_here
        return sent_total

    def _hold(self, campaign, reason):
        if campaign['hold_reason'] != reason:
            self.db.run("UPDATE campaigns SET hold_reason = ? WHERE id = ? AND status = 'sending'",
                        reason, campaign['id'])

    def _reputation_guard(self, cid):
        row = self.db.one(
            "SELECT SUM(CASE WHEN sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent, "
            "SUM(CASE WHEN bounce_type = 'hard' THEN 1 ELSE 0 END) AS bounced, "
            "SUM(CASE WHEN complained_at IS NOT NULL THEN 1 ELSE 0 END) AS complained "
            "FROM messages WHERE campaign_id = ?", cid)
        sent = row['sent'] or 0
        if sent < GUARD_MIN_SENT:
            return ''
        if (row['bounced'] or 0) / sent > GUARD_BOUNCE_RATE:
            return (f'Paused automatically: {row["bounced"]} of {sent} messages hard-bounced '
                    f'(over {GUARD_BOUNCE_RATE:.0%}). Clean the list before resuming.')
        if (row['complained'] or 0) / sent > GUARD_COMPLAINT_RATE:
            return (f'Paused automatically: {row["complained"]} spam complaints on {sent} messages '
                    f'(over {GUARD_COMPLAINT_RATE:.1%}). Review the list and content before resuming.')
        return ''

    # Messages whose AI-written version is not ready yet wait (follow-ups never do).
    AI_READY = ("(m.step > 0 OR EXISTS (SELECT 1 FROM personalizations p WHERE p.campaign_id = m.campaign_id "
                "AND p.contact_id = m.contact_id AND p.status IN ('done', 'edited', 'failed')))")

    def _ab_decide(self, campaign):
        """Picks the winning subject once every test email is out and the wait
        is over. Returns the campaign row, refreshed when a winner was set."""
        db = self.db
        if not ab_active(campaign) or campaign['ab_winner']:
            return campaign
        row = db.one("SELECT SUM(CASE WHEN variant IN ('a', 'b') AND status = 'queued' THEN 1 ELSE 0 END) AS testing, "
                     "SUM(CASE WHEN variant = 'rest' AND status = 'queued' THEN 1 ELSE 0 END) AS waiting, "
                     "MAX(CASE WHEN variant IN ('a', 'b') THEN sent_at END) AS last_sent "
                     "FROM messages WHERE campaign_id = ? AND step = 0", campaign['id'])
        if row['testing'] or not row['waiting']:
            return campaign
        # No last_sent: every test email bounced or failed, so there is nothing to wait for.
        if row['last_sent'] and now() < row['last_sent'] + (campaign['ab_wait_hours'] or 0) * 3600:
            return campaign
        winner = ab_winner(db, campaign['id'])
        db.run('UPDATE campaigns SET ab_winner = ?, ab_decided_at = ? WHERE id = ?', winner, now(), campaign['id'])
        log.info('campaign %s: subject %s won the test', campaign['id'], winner.upper())
        return db.one('SELECT * FROM campaigns WHERE id = ?', campaign['id'])

    def _next_message(self, campaign, domain_cap):
        db = self.db
        ts = now()
        ready = f' AND {self.AI_READY}' if campaign['ai_mode'] else ''
        if ab_active(campaign) and not campaign['ab_winner']:
            ready += " AND m.variant != 'rest'"
        if domain_cap:
            return db.one(
                "SELECT m.* FROM messages m WHERE m.campaign_id = ? AND m.status = 'queued' AND m.scheduled_at <= ? "
                "AND m.to_domain NOT IN (SELECT to_domain FROM messages WHERE kind = 'campaign' AND sent_at >= ? "
                f"GROUP BY to_domain HAVING COUNT(*) >= ?){ready} ORDER BY m.step DESC, m.scheduled_at, m.rowid LIMIT 1",
                campaign['id'], ts, ts - 3600, domain_cap)
        return db.one("SELECT m.* FROM messages m WHERE m.campaign_id = ? AND m.status = 'queued' "
                      f"AND m.scheduled_at <= ?{ready} ORDER BY m.step DESC, m.scheduled_at, m.rowid LIMIT 1",
                      campaign['id'], ts)

    def _why_waiting(self, campaign, domain_cap):
        db = self.db
        due = db.val("SELECT COUNT(*) FROM messages WHERE campaign_id = ? AND status = 'queued' AND scheduled_at <= ?",
                     campaign['id'], now(), default=0)
        if not due:
            return ''
        if campaign['ai_mode']:
            blocked = db.val(f"SELECT COUNT(*) FROM messages m WHERE m.campaign_id = ? AND m.status = 'queued' "
                             f"AND NOT {self.AI_READY}", campaign['id'], default=0)
            if blocked:
                error = db.state('writer_error')
                if error:
                    return f'AI writing is stopped: {error}'[:300]
                done = writer.progress(db, campaign['id'])
                return f'AI is writing the emails: {done["ready"] + done["failed"]} of {done["total"]} ready'
        if ab_active(campaign) and not campaign['ab_winner']:
            row = db.one("SELECT SUM(CASE WHEN variant = 'rest' AND status = 'queued' THEN 1 ELSE 0 END) AS waiting, "
                         "MAX(CASE WHEN variant IN ('a', 'b') THEN sent_at END) AS last_sent "
                         "FROM messages WHERE campaign_id = ? AND step = 0", campaign['id'])
            if row['waiting']:
                when = local_time(db.settings(), (row['last_sent'] or now()) + (campaign['ab_wait_hours'] or 0) * 3600)
                return (f'Testing two subject lines. The other {row["waiting"]} leads get the winner '
                        f'from {when.strftime("%d %b %H:%M")}')
        return f'Per-domain limit of {domain_cap}/hour reached; continuing shortly'

    def _follow_ups(self, campaign, force=False):
        """Queues follow-ups that have become due, and first emails for leads
        added to the list since the start. Returns a Scan."""
        db = self.db
        cid = campaign['id']
        ts = now()
        last = self._scanned.get(cid)
        if last and not force and ts - last[0] < self.follow_up_interval:
            return last[1]
        steps = db.q('SELECT * FROM campaign_steps WHERE campaign_id = ? ORDER BY position', cid)
        add_late_members(db, campaign)
        if campaign['ai_mode']:
            writer.ensure_rows(db, campaign)  # leads added to the list after the start
        scan = Scan()
        for step in steps:
            if step['send_if'] == 'opened_no_reply':
                # Leads who have not opened yet still may; the campaign waits for them a while.
                unopened = follow_up_candidates(db, cid, step, unopened_since=ts - OPEN_WAIT_DAYS * 86400)
                if unopened:
                    scan.open_waiting += len(unopened)
                    until = unopened[-1]['sent_at'] + OPEN_WAIT_DAYS * 86400
                    if scan.open_until is None or until > scan.open_until:
                        scan.open_until, scan.open_step = until, step['position']
            for row in follow_up_candidates(db, cid, step, due_before=ts):
                db.run("INSERT OR IGNORE INTO messages (id, campaign_id, contact_id, kind, to_email, to_domain, "
                       "from_email, subject, status, step, parent_id, scheduled_at, created_at) "
                       "VALUES (?, ?, ?, 'campaign', ?, ?, ?, '', 'queued', ?, ?, 0, ?)",
                       security.new_id(), cid, row['contact_id'], row['to_email'], row['to_domain'],
                       campaign['from_email'], step['position'], row['id'], ts)
            waiting = follow_up_candidates(db, cid, step)
            if waiting:
                scan.pending += len(waiting)
                due = waiting[0]['sent_at'] + step['delay_days'] * 86400
                if scan.next_due is None or due < scan.next_due:
                    scan.next_due, scan.next_step = due, step['position']
        self._scanned[cid] = (ts, scan)
        return scan

    def _pick_account(self, campaign, row, today):
        """(account, outcome): the email account this message leaves from, or
        an outcome ('skip' / 'no_account') when it cannot go now."""
        db = self.db
        follow = None
        if row['step'] and row['parent_id']:
            first = db.one('SELECT m.mailbox_id FROM messages m WHERE m.id = ?', row['parent_id'])
            if first is not None and first['mailbox_id']:
                follow = db.one('SELECT * FROM mailboxes WHERE id = ?', first['mailbox_id'])
        account, why = mailboxes.pick(db, campaign, today, follow)
        if account is not None:
            return account, None
        if follow is not None:
            # The follow-up waits for the account that wrote to this lead, so the
            # thread stays in one inbox; the rest of the campaign carries on.
            full = mailboxes.sent_since(db, today, [follow['id']]).get(follow['id'], 0) >= follow['daily_limit']
            later = today + 86400 if full else max(now() + 600, follow['retry_at'])
            db.run("UPDATE messages SET scheduled_at = ?, error = ? WHERE id = ? AND status = 'queued'",
                   later, why[:300], row['id'])
            return None, 'skip'
        self._account_hold = why
        return None, 'no_account'

    def _send_campaign_message(self, campaign, row, settings, mailer, today=None):
        db = self.db
        contact = db.one('SELECT * FROM contacts WHERE id = ?', row['contact_id'])
        if contact is None or contact['status'] != 'active' or contacts.is_suppressed(db, row['to_email']):
            db.run("UPDATE messages SET status = 'cancelled', error = 'Recipient no longer subscribed' "
                   "WHERE id = ? AND status = 'queued'", row['id'])
            return 'skip'
        if row['step'] and contact['lead_status'] not in ACTIVE_LEAD_STATUSES:
            db.run("UPDATE messages SET status = 'cancelled', error = ? WHERE id = ? AND status = 'queued'",
                   f'Follow-up stopped: lead is "{contact["lead_status"]}"', row['id'])
            return 'skip'
        try:
            extra = message_content(db, campaign, row)
        except MissingStep:
            db.run("UPDATE messages SET status = 'cancelled', error = 'This follow-up step was deleted' "
                   "WHERE id = ? AND status = 'queued'", row['id'])
            return 'skip'
        account, view, view_settings = None, campaign, settings
        if campaign['mailbox_ids']:
            account, waiting = self._pick_account(campaign, row, today or day_start(settings))
            if account is None:
                return waiting
            view = dict(campaign, from_email=account['email'], from_name=account['from_name'] or campaign['from_name'])
            if campaign['reply_to'] == settings.get('reply_to'):
                # The server-wide default would send replies away from the account's own inbox.
                view['reply_to'] = ''
                view_settings = dict(settings, reply_to='')
        if row['variant'] == 'b' or (row['variant'] == 'rest' and campaign['ab_winner'] == 'b'):
            view = dict(view, subject=campaign['subject_b'])
        try:
            msg = self.renderer.campaign_message(view, contact, row['id'], view_settings, **extra)
        except (ValueError, KeyError, TypeError) as exc:
            db.run("UPDATE messages SET status = 'failed', error = ? WHERE id = ?",
                   f'Could not build message: {exc}'[:500], row['id'])
            events.record(db, row, 'failed', detail=str(exc))
            return 'rejected'
        from_email = view['from_email'] or settings.get('from_email', '')
        if row['subject'] != msg['Subject'] or row['from_email'] != from_email or account is not None:
            db.run('UPDATE messages SET subject = ?, from_email = ?, mailbox_id = ? WHERE id = ?', msg['Subject'],
                   from_email, account['id'] if account is not None else None, row['id'])
        if account is not None and account['smtp_host']:
            # The provider's server only accepts its own address as the sender;
            # its bounces arrive in the account's inbox, which is read for them.
            envelope = account['email']
            via = self._account_mailers.get(account['id'])
            if via is None:
                via = self._account_mailers[account['id']] = Mailer(self.cfg, settings, account)
        else:
            envelope, via = envelope_sender(settings, row['id'], from_email), mailer
        try:
            outcome = self._deliver(row, msg, envelope, [row['to_email']], via)
        except ServerDown as exc:
            if via is mailer:
                raise
            log.warning('email account %s unavailable: %s', account['email'], exc)
            mailboxes.mark_down(db, account, exc)
            return 'skip'
        if account is not None and outcome == 'sent':
            mailboxes.mark_ok(db, account)
        if outcome == 'sent':
            db.run("UPDATE contacts SET lead_status = 'contacted', updated_at = ? WHERE id = ? AND lead_status = 'new'",
                   now(), contact['id'])
        return outcome

    # -- the SMTP hand-off ---------------------------------------------------
    def _deliver(self, row, msg, envelope, recipients, mailer):
        """Returns sent | bounced | rejected | retry | skip. Raises ServerDown
        (leaving the message queued) when the server cannot be reached."""
        db = self.db
        mid = row['id']
        claimed = db.run("UPDATE messages SET status = 'sending', attempts = attempts + 1 "
                         "WHERE id = ? AND status = 'queued'", mid).rowcount
        if not claimed:
            return 'skip'
        attempts = row['attempts'] + 1
        try:
            mailer.send(envelope, recipients, msg)
        except ServerDown:
            db.run("UPDATE messages SET status = 'queued', attempts = attempts - 1 WHERE id = ?", mid)
            raise
        except smtplib.SMTPRecipientsRefused as exc:
            error = _short(exc)
            if all(code >= 500 for code, _ in exc.recipients.values()):
                ts = now()
                db.run("UPDATE messages SET status = 'bounced', bounced_at = ?, bounce_type = 'hard', error = ? "
                       "WHERE id = ?", ts, error, mid)
                events.record(db, row, 'bounced', detail=error, extra={'bounce': {'type': 'hard'}})
                for address in exc.recipients:
                    contacts.suppress(db, address, 'bounced', error)
                return 'bounced'
            return self._retry(row, attempts, error)
        except smtplib.SMTPServerDisconnected as exc:
            db.run("UPDATE messages SET status = 'failed', error = ? WHERE id = ?",
                   'Connection lost during send; not retried to avoid a duplicate', mid)
            events.record(db, row, 'failed', detail=_short(exc))
            mailer.close()
            return 'rejected'
        except smtplib.SMTPResponseException as exc:
            error = _short(exc)
            if exc.smtp_code >= 500:
                db.run("UPDATE messages SET status = 'failed', error = ? WHERE id = ?", error, mid)
                events.record(db, row, 'failed', detail=error)
                return 'rejected'
            return self._retry(row, attempts, error)
        except (OSError, smtplib.SMTPException) as exc:
            mailer.close()
            return self._retry(row, attempts, _short(exc))
        db.run("UPDATE messages SET status = 'sent', sent_at = ?, error = '' WHERE id = ?", now(), mid)
        events.record(db, row, 'sent')
        return 'sent'

    def _retry(self, row, attempts, error):
        db = self.db
        if attempts >= MAX_ATTEMPTS:
            db.run("UPDATE messages SET status = 'failed', error = ? WHERE id = ?", error, row['id'])
            events.record(db, row, 'failed', detail=error)
            return 'rejected'
        db.run("UPDATE messages SET status = 'queued', scheduled_at = ?, error = ? WHERE id = ?",
               now() + 300 * attempts, error, row['id'])
        return 'retry'


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
