"""Analytics queries. Rates are computed against delivered mail (sent minus
bounced), the convention inbox providers and most senders use."""
import datetime

from .db import now

_COUNTS = (
    "COUNT(*) AS total, "
    "SUM(CASE WHEN {p}status IN ('queued', 'sending') THEN 1 ELSE 0 END) AS queued, "
    # A message refused while sending bounced without a sent_at; it still counts as sent.
    "SUM(CASE WHEN {p}sent_at IS NOT NULL OR {p}bounced_at IS NOT NULL THEN 1 ELSE 0 END) AS sent, "
    "SUM(CASE WHEN {p}status = 'failed' THEN 1 ELSE 0 END) AS failed, "
    "SUM(CASE WHEN {p}status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled, "
    "SUM(CASE WHEN {p}bounced_at IS NOT NULL THEN 1 ELSE 0 END) AS bounced, "
    "SUM(CASE WHEN {p}bounce_type = 'hard' THEN 1 ELSE 0 END) AS hard_bounced, "
    "SUM(CASE WHEN {p}opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened, "
    "SUM(CASE WHEN {p}human_open = 1 THEN 1 ELSE 0 END) AS human_opened, "
    "SUM(CASE WHEN {p}clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked, "
    "SUM(CASE WHEN {p}human_click = 1 THEN 1 ELSE 0 END) AS human_clicked, "
    "SUM(CASE WHEN {p}complained_at IS NOT NULL THEN 1 ELSE 0 END) AS complained, "
    "SUM(CASE WHEN {p}unsubscribed_at IS NOT NULL THEN 1 ELSE 0 END) AS unsubscribed, "
    "SUM(CASE WHEN {p}replied_at IS NOT NULL THEN 1 ELSE 0 END) AS replied"
)


def counts(prefix=''):
    """Aggregate columns, optionally qualified with a table alias."""
    return _COUNTS.format(p=prefix)


COUNTS = counts()
M_COUNTS = counts('m.')


def _finish(row):
    data = {key: (row[key] or 0) for key in row.keys() if key not in ('label', 'key')}
    for key in ('label', 'key'):
        if key in row.keys():
            data[key] = row[key]
    delivered = max(data['sent'] - data['bounced'], 0)
    data['delivered'] = delivered

    def rate(count, base):
        return round(100.0 * count / base, 1) if base else 0.0
    data['delivery_rate'] = rate(delivered, data['sent'])
    data['open_rate'] = rate(data['opened'], delivered)
    data['human_open_rate'] = rate(data['human_opened'], delivered)
    data['click_rate'] = rate(data['clicked'], delivered)
    data['human_click_rate'] = rate(data['human_clicked'], delivered)
    data['click_to_open'] = rate(data['clicked'], data['opened'])
    data['bounce_rate'] = rate(data['bounced'], data['sent'])
    data['complaint_rate'] = round(100.0 * data['complained'] / delivered, 2) if delivered else 0.0
    data['unsubscribe_rate'] = rate(data['unsubscribed'], delivered)
    data['reply_rate'] = rate(data['replied'], delivered)
    return data


def by_step(db, campaign_id):
    """Results of the first email and each follow-up of a campaign."""
    rows = db.q(f"SELECT m.step AS key, m.step AS label, {M_COUNTS} FROM messages m WHERE m.campaign_id = ? "
                "GROUP BY m.step ORDER BY m.step", campaign_id)
    return [_finish(r) for r in rows]


def campaign(db, campaign_id):
    return _finish(db.one(f'SELECT {COUNTS} FROM messages WHERE campaign_id = ?', campaign_id))


def overall(db, since=0, kind=None):
    if kind:
        return _finish(db.one(f'SELECT {COUNTS} FROM messages WHERE created_at >= ? AND kind = ?', since, kind))
    return _finish(db.one(f'SELECT {COUNTS} FROM messages WHERE created_at >= ?', since))


def by_campaign(db, since=0, limit=100):
    rows = db.q(
        f"SELECT c.id AS key, c.name AS label, {M_COUNTS} FROM messages m JOIN campaigns c ON c.id = m.campaign_id "
        "WHERE m.created_at >= ? GROUP BY c.id ORDER BY c.id DESC LIMIT ?", since, limit)
    return [_finish(r) for r in rows]


def by_list(db, since=0):
    rows = db.q(
        f"SELECT l.id AS key, l.name AS label, {M_COUNTS} FROM messages m JOIN campaigns c ON c.id = m.campaign_id "
        "JOIN lists l ON l.id = c.list_id WHERE m.created_at >= ? GROUP BY l.id ORDER BY l.name", since)
    return [_finish(r) for r in rows]


def by_domain(db, since=0, campaign_id=None, limit=12):
    where, args = 'm.created_at >= ? AND m.sent_at IS NOT NULL', [since]
    if campaign_id:
        where += ' AND m.campaign_id = ?'
        args.append(campaign_id)
    rows = db.q(f"SELECT m.to_domain AS key, m.to_domain AS label, {M_COUNTS} FROM messages m WHERE {where} "
                "GROUP BY m.to_domain ORDER BY COUNT(*) DESC LIMIT ?", *args, limit)
    return [_finish(r) for r in rows]


def by_account(db, since=0):
    """Campaign results per email account sent from; mail through this server is one row."""
    rows = db.q(
        f"SELECT COALESCE(m.mailbox_id, 0) AS key, COALESCE(b.email, 'This server') AS label, {M_COUNTS} "
        "FROM messages m LEFT JOIN mailboxes b ON b.id = m.mailbox_id "
        "WHERE m.kind = 'campaign' AND m.created_at >= ? AND m.sent_at IS NOT NULL "
        "GROUP BY COALESCE(m.mailbox_id, 0) ORDER BY COUNT(*) DESC", since)
    return [_finish(r) for r in rows]


FUNNEL_STAGES = (('contacted', 'Reached'), ('opened', 'Opened'), ('replied', 'Replied'),
                 ('interested', 'Interested'), ('meeting', 'Meeting booked'))


def funnel_counts(db, since=0, per_campaign=False, campaign_id=None):
    """Lead counts per funnel stage in one pass over the messages: {campaign id
    (0 for all campaigns together): {'contacted', 'opened', ...}}. Every stage
    includes the later ones (a lead who replied also read the email, even if no
    open was seen)."""
    per_campaign = per_campaign or bool(campaign_id)
    scope, args = "m.kind = 'campaign' AND m.created_at >= ? AND m.contact_id IS NOT NULL", [since]
    reply_scope, reply_args = '', []
    if campaign_id:
        scope += ' AND m.campaign_id = ?'
        args.append(campaign_id)
        reply_scope, reply_args = 'AND r.campaign_id = ?', [campaign_id]
    group, reply_group = ('m.campaign_id', 'r.campaign_id') if per_campaign else ('0', '0')
    # Per campaign, a lead's status counts only when they replied to that
    # campaign; otherwise a meeting won elsewhere would show up there too.
    status_counts = 'p.replied' if per_campaign else '1'
    rows = db.q(
        f"WITH per AS (SELECT {group} AS g, m.contact_id AS id, "
        "  MAX(m.step = 0 AND m.sent_at IS NOT NULL) AS reached, MAX(m.opened_at IS NOT NULL) AS opened, "
        f"  MAX(m.replied_at IS NOT NULL) AS replied FROM messages m WHERE {scope} GROUP BY 1, 2), "
        f"said AS (SELECT {reply_group} AS g, r.contact_id AS id, MAX(r.category = 'meeting') AS meeting "
        f"  FROM replies r WHERE r.category IN ('interested', 'meeting') {reply_scope} GROUP BY 1, 2), "
        "flags AS (SELECT p.g, p.opened, p.replied, "
        f"  s.id IS NOT NULL OR ({status_counts} AND c.lead_status IN ('interested', 'meeting', 'won')) AS interested, "
        f"  COALESCE(s.meeting, 0) OR ({status_counts} AND c.lead_status IN ('meeting', 'won')) AS meeting "
        "  FROM per p JOIN contacts c ON c.id = p.id LEFT JOIN said s ON s.g = p.g AND s.id = p.id WHERE p.reached) "
        "SELECT g, COUNT(*) AS contacted, "
        "SUM(CASE WHEN meeting THEN 1 ELSE 0 END) AS meeting, "
        "SUM(CASE WHEN meeting OR interested THEN 1 ELSE 0 END) AS interested, "
        "SUM(CASE WHEN meeting OR interested OR replied THEN 1 ELSE 0 END) AS replied, "
        "SUM(CASE WHEN meeting OR interested OR replied OR opened THEN 1 ELSE 0 END) AS opened FROM flags GROUP BY g",
        *args, *reply_args)
    return {r['g']: {key: r[key] or 0 for key, _ in FUNNEL_STAGES} for r in rows}


def funnel(db, since=0, campaign_id=None):
    """Leads at each stage from first email to meeting.
    Returns [{'key', 'label', 'count', 'of_total', 'of_previous'}]."""
    counts = funnel_counts(db, since, campaign_id=campaign_id)
    row = counts.get(campaign_id or 0, {})
    total = row.get('contacted', 0)
    out, previous = [], total
    for key, label in FUNNEL_STAGES:
        count = row.get(key, 0)
        out.append({'key': key, 'label': label, 'count': count,
                    'of_total': round(100.0 * count / total, 1) if total else 0.0,
                    'of_previous': round(100.0 * count / previous, 1) if previous else 0.0})
        previous = count
    return out


def top_links(db, campaign_id=None, since=0, limit=15):
    where, args = "e.type = 'clicked' AND e.at >= ?", [since]
    if campaign_id:
        where += ' AND e.campaign_id = ?'
        args.append(campaign_id)
    # Grouped by address, so the same link used in several campaigns is one row.
    return db.q(f"SELECT l.url, COUNT(*) AS clicks, COUNT(DISTINCT e.message_id) AS people "
                f"FROM events e JOIN links l ON l.id = e.link_id WHERE {where} "
                "GROUP BY l.url ORDER BY people DESC, clicks DESC LIMIT ?", *args, limit)


def daily(db, days=30, campaign_id=None, tz=datetime.timezone.utc):
    """Per-day series for charts: sent, opened, clicked, bounced."""
    end = datetime.datetime.fromtimestamp(now(), tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - datetime.timedelta(days=days - 1)
    series = {}
    for i in range(days):
        day = (start + datetime.timedelta(days=i)).strftime('%Y-%m-%d')
        series[day] = {'day': day, 'sent': 0, 'opened': 0, 'clicked': 0, 'bounced': 0}
    where, args = "at >= ? AND type IN ('sent', 'opened', 'clicked', 'bounced')", [start.timestamp()]
    if campaign_id:
        where += ' AND campaign_id = ?'
        args.append(campaign_id)
    seen = set()
    for row in db.q(f'SELECT type, at, message_id FROM events WHERE {where} ORDER BY at', *args):
        day = datetime.datetime.fromtimestamp(row['at'], tz).strftime('%Y-%m-%d')
        if day not in series:
            continue
        # Opens and clicks are counted once per message, on the first day.
        if row['type'] in ('opened', 'clicked'):
            key = (row['type'], row['message_id'])
            if key in seen:
                continue
            seen.add(key)
        series[day][row['type']] += 1
    return list(series.values())


def hours_until_done(db, campaign_row):
    """Rough time left for a sending campaign, in hours."""
    queued = db.val("SELECT COUNT(*) FROM messages WHERE campaign_id = ? AND status = 'queued'",
                    campaign_row['id'], default=0)
    if not queued:
        return 0.0
    low, high = campaign_row['delay_min'], campaign_row['delay_max']
    if low is not None and high is not None and high > 0:
        per_message = (low + high) / 2.0
    else:
        per_message = 3600.0 / max(1, campaign_row['rate_per_hour'] or 1)
    return round(queued * per_message / 3600.0, 1)
