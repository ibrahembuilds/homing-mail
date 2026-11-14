"""Follow-up tasks and the calendar: what is due, what is scheduled, and
when automatic follow-ups will go out. Also builds the iCalendar feed that
Google Calendar, Outlook and Apple Calendar can subscribe to."""
import calendar as cal
import datetime
import secrets

from .db import now
from .sender import ACTIVE_LEAD_STATUSES, follow_up_candidates, local_time, timezone

QUICK_DUE = (('tomorrow', 'Tomorrow', 1), ('3days', 'In 3 days', 3), ('week', 'Next week', 7),
             ('2weeks', 'In 2 weeks', 14))


# -- tasks ------------------------------------------------------------------------
def due_at_from(settings, quick=None, date=None, hour='09:00'):
    """A due time from a quick choice ('tomorrow', '3days', ...) or a local date."""
    tz = timezone(settings)
    if quick:
        days = {key: d for key, _, d in QUICK_DUE}.get(quick)
        if days is None:
            raise ValueError('Unknown choice')
        base = local_time(settings).date() + datetime.timedelta(days=days)
    else:
        base = datetime.date.fromisoformat(str(date))
    hh, mm = (hour or '09:00').split(':')[:2]
    moment = datetime.datetime(base.year, base.month, base.day, int(hh), int(mm), tzinfo=tz)
    return moment.timestamp()


def create_task(db, title, due_at, contact_id=None, reply_id=None, note=''):
    title = ' '.join(str(title or '').split())[:200] or 'Follow up'
    return db.run('INSERT INTO tasks (contact_id, reply_id, title, note, due_at, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                  contact_id, reply_id, title, str(note or '')[:2000], float(due_at), now()).lastrowid


def open_tasks(db, until=None, contact_id=None, limit=200):
    sql = ('SELECT t.*, c.email, c.first_name, c.last_name FROM tasks t LEFT JOIN contacts c ON c.id = t.contact_id '
           'WHERE t.done_at IS NULL')
    args = []
    if until is not None:
        sql += ' AND t.due_at <= ?'
        args.append(until)
    if contact_id is not None:
        sql += ' AND t.contact_id = ?'
        args.append(contact_id)
    sql += ' ORDER BY t.due_at LIMIT ?'
    args.append(limit)
    return db.q(sql, *args)


def end_of_today(settings):
    local = local_time(settings)
    return local.replace(hour=23, minute=59, second=59, microsecond=0).timestamp()


# -- calendar ---------------------------------------------------------------------
def _lead_name(row):
    name = f'{row["first_name"] or ""} {row["last_name"] or ""}'.strip()
    return name or row['email'] or 'lead'


def projected_follow_ups(db, settings, start, end):
    """Automatic follow-ups expected between start and end, grouped per
    campaign, step and local day: [{'at', 'campaign_id', 'campaign', 'step', 'count'}]."""
    out = {}
    tz = timezone(settings)
    for campaign in db.q("SELECT * FROM campaigns WHERE status IN ('sending', 'scheduled', 'pending_approval')"):
        steps = db.q('SELECT * FROM campaign_steps WHERE campaign_id = ? ORDER BY position', campaign['id'])
        if not steps:
            continue
        if campaign['status'] == 'sending':
            for step in steps:
                for row in follow_up_candidates(db, campaign['id'], step):
                    due = max(row['sent_at'] + step['delay_days'] * 86400, now())
                    _add(out, tz, due, campaign, step['position'], start, end)
        else:
            # Not started: estimate from the start time and the list size.
            size = db.val("SELECT COUNT(*) FROM list_members m JOIN contacts c ON c.id = m.contact_id "
                          f"WHERE m.list_id = ? AND c.status = 'active' AND c.lead_status IN "
                          f"({', '.join('?' for _ in ACTIVE_LEAD_STATUSES)})",
                          campaign['list_id'], *ACTIVE_LEAD_STATUSES, default=0)
            due = campaign['scheduled_at'] or now()
            for step in steps:
                due += step['delay_days'] * 86400
                _add(out, tz, due, campaign, step['position'], start, end, count=size)
    return sorted(out.values(), key=lambda item: item['at'])


def _add(out, tz, due, campaign, step, start, end, count=1):
    if not (start <= due < end):
        return
    day = datetime.datetime.fromtimestamp(due, tz).date()
    key = (campaign['id'], step, day)
    item = out.setdefault(key, {'at': due, 'campaign_id': campaign['id'], 'campaign': campaign['name'],
                                'step': step, 'count': 0})
    item['count'] += count
    item['at'] = min(item['at'], due)


def month_view(db, settings, year, month):
    """Weeks of days for one month, each day with its items."""
    tz = timezone(settings)
    first = datetime.datetime(year, month, 1, tzinfo=tz)
    last_day = cal.monthrange(year, month)[1]
    end = (first + datetime.timedelta(days=last_day)).timestamp()
    start = first.timestamp()
    days = {}

    def add(ts, item):
        day = datetime.datetime.fromtimestamp(ts, tz).date()
        days.setdefault(day, []).append(dict(item, at=ts))

    for task in db.q('SELECT t.*, c.email, c.first_name, c.last_name FROM tasks t '
                     'LEFT JOIN contacts c ON c.id = t.contact_id WHERE t.due_at >= ? AND t.due_at < ? '
                     'ORDER BY t.due_at', start, end):
        who = f' ({_lead_name(task)})' if task['contact_id'] else ''
        add(task['due_at'], {'kind': 'task', 'id': task['id'], 'title': task['title'] + who,
                             'done': bool(task['done_at']), 'contact_id': task['contact_id']})
    for campaign in db.q("SELECT * FROM campaigns WHERE scheduled_at >= ? AND scheduled_at < ? "
                         "AND status IN ('scheduled', 'pending_approval', 'sending', 'sent')", start, end):
        label = 'Starts' if campaign['status'] in ('scheduled', 'pending_approval') else 'Started'
        add(campaign['scheduled_at'], {'kind': 'campaign', 'id': campaign['id'],
                                       'title': f'{label}: {campaign["name"]}', 'status': campaign['status']})
    for item in projected_follow_ups(db, settings, start, end):
        add(item['at'], {'kind': 'followup', 'id': item['campaign_id'],
                         'title': f'Follow-up {item["step"]} of {item["campaign"]}: {item["count"]} '
                                  f'lead{"s" if item["count"] != 1 else ""}'})
    sent = db.q("SELECT sent_at FROM messages WHERE kind = 'campaign' AND sent_at >= ? AND sent_at < ?", start, end)
    per_day = {}
    for row in sent:
        day = datetime.datetime.fromtimestamp(row['sent_at'], tz).date()
        per_day[day] = per_day.get(day, 0) + 1
    replies = {}
    for row in db.q('SELECT received_at FROM replies WHERE is_auto = 0 AND received_at >= ? AND received_at < ?',
                    start, end):
        day = datetime.datetime.fromtimestamp(row['received_at'], tz).date()
        replies[day] = replies.get(day, 0) + 1

    today = local_time(settings).date()
    weeks = []
    for week in cal.Calendar(firstweekday=0).monthdatescalendar(year, month):
        weeks.append([{'date': day, 'in_month': day.month == month, 'today': day == today,
                       'items': sorted(days.get(day, []), key=lambda i: i['at']),
                       'sent': per_day.get(day, 0), 'replies': replies.get(day, 0)} for day in week])
    return weeks


# -- calendar feed (iCalendar) ------------------------------------------------------
def feed_token(db):
    token = db.setting('calendar_token')
    if not token:
        token = secrets.token_urlsafe(24)
        db.set_setting('calendar_token', token)
    return token


def _ics_text(value):
    return (str(value).replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,')
            .replace('\r\n', '\\n').replace('\n', '\\n'))


def _ics_fold(line):
    raw = line.encode('utf-8')
    if len(raw) <= 75:
        return line
    parts, current = [], b''
    for char in line:
        encoded = char.encode('utf-8')
        if len(current) + len(encoded) > (75 if not parts else 74):
            parts.append(current.decode('utf-8'))
            current = b''
        current += encoded
    parts.append(current.decode('utf-8'))
    return '\r\n '.join(parts)


def _ics_time(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def ics_feed(db, settings, base_url, brand):
    """Open tasks, campaign starts and expected follow-ups for the next 90 days."""
    start = now() - 30 * 86400
    end = now() + 90 * 86400
    stamp = _ics_time(now())
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', f'PRODID:-//{_ics_text(brand)}//Campaigns//EN',
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', f'X-WR-CALNAME:{_ics_text(brand)} follow-ups',
             'REFRESH-INTERVAL;VALUE=DURATION:PT1H', 'X-PUBLISHED-TTL:PT1H']

    def event(uid, ts, minutes, summary, description='', url=''):
        lines.extend(['BEGIN:VEVENT', f'UID:{uid}', f'DTSTAMP:{stamp}', f'DTSTART:{_ics_time(ts)}',
                      f'DTEND:{_ics_time(ts + minutes * 60)}', f'SUMMARY:{_ics_text(summary)}'])
        if description:
            lines.append(f'DESCRIPTION:{_ics_text(description)}')
        if url:
            lines.append(f'URL:{url}')
        lines.append('END:VEVENT')

    for task in db.q('SELECT t.*, c.email, c.first_name, c.last_name FROM tasks t '
                     'LEFT JOIN contacts c ON c.id = t.contact_id WHERE t.done_at IS NULL AND t.due_at < ?', end):
        who = f' ({_lead_name(task)})' if task['contact_id'] else ''
        link = f'{base_url}/leads/{task["contact_id"]}' if task['contact_id'] else f'{base_url}/calendar'
        event(f'task-{task["id"]}@campaigns', task['due_at'], 30, task['title'] + who,
              task['note'] or (task['email'] or ''), link)
    for campaign in db.q("SELECT * FROM campaigns WHERE status IN ('scheduled', 'pending_approval') "
                         "AND scheduled_at >= ? AND scheduled_at < ?", start, end):
        event(f'campaign-{campaign["id"]}@campaigns', campaign['scheduled_at'], 30,
              f'Campaign starts: {campaign["name"]}', campaign['subject'],
              f'{base_url}/campaigns/{campaign["id"]}')
    for item in projected_follow_ups(db, settings, now(), end):
        day = datetime.datetime.fromtimestamp(item['at'], timezone(settings)).strftime('%Y%m%d')
        event(f'followup-{item["campaign_id"]}-{item["step"]}-{day}@campaigns', item['at'], 30,
              f'Follow-up {item["step"]} of {item["campaign"]}: {item["count"]} leads', '',
              f'{base_url}/campaigns/{item["campaign_id"]}')
    lines.append('END:VCALENDAR')
    return '\r\n'.join(_ics_fold(line) for line in lines) + '\r\n'
