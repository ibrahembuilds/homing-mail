"""Campaign operations shared by the web pages, the API and the AI agent."""
import datetime
import json
import math
import re

from . import contacts, events, leadtools, mailboxes, security, spamcheck, writer
from .db import now
from .render import TAG_RE, Renderer, UNSUB_TAG_RE, contact_values
from .sender import message_content, queue_single, timezone

EDITABLE = ('draft', 'pending_approval')
CAMPAIGN_FIELDS = ('name', 'subject', 'preheader', 'from_name', 'from_email', 'reply_to', 'html', 'text',
                   'list_id', 'rate_per_hour', 'delay_min', 'delay_max', 'window_start', 'window_end',
                   'window_days', 'daily_cap', 'track_opens', 'track_clicks', 'ai_mode', 'ai_instructions',
                   'ai_research', 'ai_news', 'mailbox_ids', 'subject_b', 'ab_percent', 'ab_wait_hours', 'skip_risky',
                   'offer_id')
LEAD_STATUSES = (('new', 'New'), ('contacted', 'Contacted'), ('replied', 'Replied'), ('interested', 'Interested'),
                 ('meeting', 'Meeting booked'), ('won', 'Won'), ('not_interested', 'Not interested'),
                 ('lost', 'Lost'))
SEND_IF = (('no_reply', 'if they have not replied'), ('no_reply_no_open', 'if they have not opened it'),
           ('opened_no_reply', 'if they opened it but have not replied'))
TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


class Problem(Exception):
    """A request that cannot be carried out; the message is shown to the user."""


def _int(value, default=None, low=None, high=None):
    if value is None or value == '':
        return default
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        raise Problem(f'"{value}" is not a number')
    if low is not None and number < low:
        number = low
    if high is not None and number > high:
        number = high
    return number


def clean_campaign(data, settings, existing=None):
    """Validates and normalises campaign fields from a form, the API or the agent."""
    out = {}

    def text(key, limit):
        if key in data and data[key] is not None:
            out[key] = str(data[key]).strip()[:limit]
    text('name', 200)
    text('subject', 300)
    text('subject_b', 300)
    text('preheader', 300)
    text('from_name', 120)
    text('reply_to', 254)
    if 'html' in data and data['html'] is not None:
        out['html'] = str(data['html'])[:2_000_000]
    if 'text' in data and data['text'] is not None:
        out['text'] = str(data['text'])[:500_000]
    if 'from_email' in data and data['from_email'] is not None:
        value = str(data['from_email']).strip()
        if value and not contacts.normalize_email(value):
            raise Problem(f'From address "{value}" is not a valid email address')
        out['from_email'] = (contacts.normalize_email(value) or '')
    if out.get('reply_to') and not contacts.normalize_email(out['reply_to']):
        raise Problem(f'Reply-to "{out["reply_to"]}" is not a valid email address')
    if 'list_id' in data:
        out['list_id'] = _int(data['list_id'])
    if 'rate_per_hour' in data:
        out['rate_per_hour'] = _int(data['rate_per_hour'], _int(settings.get('rate_per_hour'), 120), 1, 100000)
    for key in ('delay_min', 'delay_max'):
        if key in data:
            out[key] = _int(data[key], None, 0, 86400)
    if out.get('delay_min') is not None and out.get('delay_max') is not None and out['delay_max'] < out['delay_min']:
        out['delay_min'], out['delay_max'] = out['delay_max'], out['delay_min']
    for key in ('window_start', 'window_end'):
        if key in data:
            value = str(data[key] or '').strip()
            if value and not TIME_RE.match(value):
                raise Problem(f'Time "{value}" must look like 09:00')
            out[key] = value
    if 'window_days' in data:
        value = data['window_days']
        if isinstance(value, (list, tuple)):
            value = ''.join(str(v) for v in value)
        value = ''.join(sorted(set(c for c in str(value or '') if c in '0123456')))
        out['window_days'] = value or '0123456'
    if 'daily_cap' in data:
        out['daily_cap'] = _int(data['daily_cap'], 0, 0, 10_000_000)
    for key in ('track_opens', 'track_clicks', 'ai_research', 'ai_news', 'skip_risky'):
        if key in data:
            out[key] = 1 if str(data[key]).lower() in ('1', 'true', 'on', 'yes') else 0
    if 'ab_percent' in data:
        out['ab_percent'] = _int(data['ab_percent'], 20, 5, 50)
    if 'ab_wait_hours' in data:
        raw = data['ab_wait_hours']
        try:
            hours = 4.0 if raw is None or str(raw).strip() == '' else float(raw)
        except (TypeError, ValueError):
            hours = math.nan
        if not math.isfinite(hours):
            raise Problem('The test wait must be a number of hours')
        out['ab_wait_hours'] = max(0.0, min(hours, 72.0))
    if 'ai_mode' in data:
        mode = str(data['ai_mode'] or '').strip()
        if mode not in writer.MODES:
            raise Problem('AI writing must be off, "line" or "full"')
        out['ai_mode'] = mode
    if 'ai_instructions' in data and data['ai_instructions'] is not None:
        out['ai_instructions'] = str(data['ai_instructions']).strip()[:4000]
    if 'mailbox_ids' in data:
        out['mailbox_ids'] = mailboxes.format_ids(mailboxes.parse_ids(data['mailbox_ids']))
    if 'offer_id' in data:
        out['offer_id'] = _int(data['offer_id']) or None    # None: the default offer
    if existing is None and not out.get('name'):
        out['name'] = out.get('subject') or 'Untitled campaign'
    return out


def create_campaign(db, settings, data, created_by='user'):
    fields = {
        'from_name': settings.get('from_name', ''), 'from_email': settings.get('from_email', ''),
        'reply_to': settings.get('reply_to', ''),
        'rate_per_hour': _int(settings.get('rate_per_hour'), 120, 1),
        'delay_min': _int(settings.get('delay_min')), 'delay_max': _int(settings.get('delay_max')),
        'window_start': settings.get('window_start', ''), 'window_end': settings.get('window_end', ''),
        'window_days': settings.get('window_days') or '0123456',
        'daily_cap': _int(settings.get('daily_cap'), 0),
        'track_opens': 1 if settings.get('track_opens') == '1' else 0,
        'track_clicks': 1 if settings.get('track_clicks') == '1' else 0,
    }
    cleaned = clean_campaign(data, settings)
    fields.update({k: v for k, v in cleaned.items() if v is not None or k in ('delay_min', 'delay_max', 'list_id')})
    if fields.get('list_id') and not db.one('SELECT id FROM lists WHERE id = ?', fields['list_id']):
        raise Problem(f'List {fields["list_id"]} does not exist')
    _check_offer(db, fields.get('offer_id'))
    _check_accounts(db, fields.get('mailbox_ids'))
    ts = now()
    fields.update({'created_by': created_by, 'created_at': ts, 'updated_at': ts})
    columns = ', '.join(fields)
    marks = ', '.join('?' for _ in fields)
    return db.run(f'INSERT INTO campaigns ({columns}) VALUES ({marks})', *fields.values()).lastrowid


def get_campaign(db, campaign_id):
    campaign = db.one('SELECT * FROM campaigns WHERE id = ?', campaign_id)
    if campaign is None:
        raise Problem(f'Campaign {campaign_id} does not exist')
    return campaign


def update_campaign(db, settings, campaign_id, data):
    campaign = get_campaign(db, campaign_id)
    cleaned = clean_campaign(data, settings, existing=campaign)
    if campaign['status'] not in EDITABLE:
        # Once sending has started only the pacing may change.
        allowed = ('rate_per_hour', 'delay_min', 'delay_max', 'window_start', 'window_end', 'window_days',
                   'daily_cap', 'name')
        blocked = [k for k in cleaned if k not in allowed and cleaned[k] != campaign[k]]
        if blocked:
            raise Problem(f'Campaign is {campaign["status"]}; only its name and pacing can still be changed')
        cleaned = {k: v for k, v in cleaned.items() if k in allowed}
    if cleaned.get('list_id') and not db.one('SELECT id FROM lists WHERE id = ?', cleaned['list_id']):
        raise Problem(f'List {cleaned["list_id"]} does not exist')
    _check_offer(db, cleaned.get('offer_id'))
    _check_accounts(db, cleaned.get('mailbox_ids'))
    if not cleaned:
        return campaign
    content_changed = any(k in cleaned and cleaned[k] != campaign[k]
                          for k in ('subject', 'subject_b', 'html', 'text', 'from_email', 'mailbox_ids'))
    ai_changed = any(k in cleaned and cleaned[k] != campaign[k]
                     for k in ('subject', 'html', 'ai_mode', 'ai_instructions', 'ai_research', 'ai_news', 'offer_id'))
    sets = ', '.join(f'{k} = ?' for k in cleaned)
    db.run(f'UPDATE campaigns SET {sets}, updated_at = ? WHERE id = ?', *cleaned.values(), now(), campaign_id)
    if 'ai_mode' in cleaned and cleaned['ai_mode'] != campaign['ai_mode']:
        # What was written for one mode (a line, a whole AI email, your own email) means nothing in another.
        db.run('DELETE FROM personalizations WHERE campaign_id = ?', campaign_id)
    if ai_changed:
        writer.invalidate(db, campaign_id)   # what the AI wrote was based on the old draft
    if content_changed:
        db.run("UPDATE campaigns SET spam_score = NULL, spam_report = '' WHERE id = ?", campaign_id)
        if campaign['status'] == 'pending_approval':
            # What was put up for approval is no longer what would be sent.
            db.run("UPDATE campaigns SET status = 'draft', scheduled_at = NULL WHERE id = ?", campaign_id)
    return get_campaign(db, campaign_id)


def _check_offer(db, offer_id):
    if offer_id and not db.one('SELECT id FROM offers WHERE id = ?', offer_id):
        raise Problem(f'Offer {offer_id} does not exist')


def _check_accounts(db, value):
    for mailbox_id in mailboxes.parse_ids(value):
        if not db.one('SELECT id FROM mailboxes WHERE id = ?', mailbox_id):
            raise Problem(f'Email account {mailbox_id} does not exist')


def sender_of(db, settings, campaign):
    """(from_email, from_name, mailbox_id) a test or check of this campaign uses:
    its first active email account, else its own From."""
    for account in mailboxes.for_campaign(db, campaign):
        if account['active']:
            return account['email'], account['from_name'] or campaign['from_name'], account['id']
    return (campaign['from_email'] or settings.get('from_email', ''),
            campaign['from_name'] or settings.get('from_name', ''), None)


def sample_contact(db, campaign):
    row = None
    if campaign['list_id']:
        row = db.one("SELECT c.* FROM list_members m JOIN contacts c ON c.id = m.contact_id "
                     "WHERE m.list_id = ? AND c.status = 'active' ORDER BY c.id LIMIT 1", campaign['list_id'])
    if row is not None:
        return row
    return {'id': 0, 'email': 'reader@example.com', 'first_name': 'Alex', 'last_name': 'Reader', 'fields': '{}'}


def readiness(db, settings, campaign):
    """Reasons this campaign cannot be sent yet (empty list = ready)."""
    problems = []
    # With a different email for every lead, the main email is never sent and need not be filled in.
    own_all, own_no_subject = False, 0
    if campaign['ai_mode'] == 'own' and campaign['list_id']:
        from . import perlead
        counts = perlead.counts(db, campaign)
        own_all = counts['total'] > 0 and counts['own'] >= counts['total']
        own_no_subject = db.val("SELECT COUNT(*) FROM personalizations WHERE campaign_id = ? AND status = 'edited' "
                                "AND subject = ''", campaign['id'], default=0)
    if not campaign['subject'].strip() and (not own_all or own_no_subject):
        problems.append('The subject is empty.' if not own_all else
                        f'{own_no_subject} lead{"s have" if own_no_subject != 1 else " has"} no subject of their own, '
                        'and the main subject is empty.')
    if not campaign['html'].strip() and not campaign['text'].strip() and not own_all:
        problems.append('The message body is empty.')
    if campaign['mailbox_ids']:
        if not [a for a in mailboxes.for_campaign(db, campaign) if a['active']]:
            problems.append('None of the email accounts chosen to send from is active.')
    elif not contacts.normalize_email(campaign['from_email'] or settings.get('from_email', '')):
        problems.append('No From address. Set one on the campaign or a default in Settings.')
    if not campaign['list_id']:
        problems.append('No contact list selected.')
    elif not contacts.list_counts(db, campaign['list_id'])['active']:
        problems.append('The selected list has no active contacts.')
    if campaign['ai_mode'] == 'line' and not writer.AI_LINE_RE.search(campaign['html'] or ''):
        problems.append('AI writing is set to "personal opening line": put {{ai_line}} in the message where '
                        'the line should go.')
    opened_before = False
    steps = db.q('SELECT * FROM campaign_steps WHERE campaign_id = ? ORDER BY position', campaign['id'])
    for step in steps:
        if step['send_if'] in ('opened_no_reply', 'no_reply_no_open') and not campaign['track_opens']:
            problems.append(f'Follow-up {step["position"]} depends on opens: turn on open tracking.')
        if step['send_if'] == 'no_reply_no_open' and opened_before:
            problems.append(f'Follow-up {step["position"]} only goes to leads who never opened, but it comes after '
                            'a follow-up sent only to leads who did, so nobody would get it.')
        opened_before = opened_before or step['send_if'] == 'opened_no_reply'
        if not (step['html'] or '').strip():
            problems.append(f'Follow-up {step["position"]} has no text.')
        if not step['as_reply'] and not (step['subject'] or '').strip():
            problems.append(f'Follow-up {step["position"]} needs a subject (or send it in the same thread).')
        if PLACEHOLDER_RE.search(step['html'] or ''):
            problems.append(f'Follow-up {step["position"]} still has a [placeholder] to fill in.')
    if PLACEHOLDER_RE.search(campaign['html'] or '') and not own_all:
        problems.append('The email still has a [placeholder] to fill in.')
    if campaign['ai_mode'] == 'own':
        from . import perlead
        leads, examples = perlead.placeholders(db, campaign['id'], BRACKETS_RE)
        if leads:
            problems.append(f'{leads} lead{"s" if leads != 1 else ""}\' own email{"s" if leads != 1 else ""} still '
                            f'{"have" if leads != 1 else "has"} placeholders to fill in: {", ".join(examples)}. '
                            'Use "Replace in every email" on the page with each lead\'s email.')
    for where, html in ([] if own_all else [('The email', campaign['html'])]) + [
            (f'Follow-up {s["position"]}', s['html']) for s in steps]:
        found = STARTER_TEXT_RE.search(html or '')
        if found:
            problems.append(f'{where} still has the example text "{found.group("text") or found.group("text2")}". Replace it with your own.')
    if campaign['list_id']:
        problems += _list_problems(db, campaign, steps)
    return problems


def _list_problems(db, campaign, steps):
    problems = []
    quality = leadtools.list_quality(db, campaign['list_id'])
    unusable = quality['invalid'] + (quality['risky'] if campaign['skip_risky'] else 0)
    if quality['total'] and unusable >= quality['total']:
        problems.append(f'None of the {quality["total"]} addresses on the list can be emailed: '
                        f'{quality["invalid"]} would bounce' +
                        (f' and {quality["risky"]} risky ones are skipped' if campaign['skip_risky'] and quality['risky']
                         else '') + '.')
    texts = [campaign['subject'], campaign['subject_b'], campaign['html']]
    texts += [t for s in steps for t in (s['subject'], s['html'])]
    tags = {m.group(1).lower() for t in texts for m in TAG_RE.finditer(t or '') if not m.group(2)}
    if not tags:
        return problems
    rows = db.q("SELECT c.* FROM list_members m JOIN contacts c ON c.id = m.contact_id "
                "WHERE m.list_id = ? AND c.status = 'active' LIMIT 5000", campaign['list_id'])
    values = [contact_values(r) for r in rows]
    known = {key for v in values for key in v}
    for tag in sorted(tags & known):
        empty = sum(1 for v in values if not str(v.get(tag) or '').strip())
        if empty:
            problems.append(f'{{{{{tag}}}}} is empty for {empty} of {len(values)} leads, so they would see a gap. '
                            f'Add a fallback, like {{{{{tag}|{TAG_FALLBACKS.get(tag, "there")}}}}}.')
    return problems


TAG_FALLBACKS = {'company': 'your team', 'first_name': 'there', 'name': 'there'}
STARTER_TEXT_RE = re.compile(r'(?P<text>Write your message here)|<br\s*/?>\s*(?P<text2>Your name)\s*</p>',
                             re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r'\[\s*(?:(?:your|insert|add|todo|tbd)\b[^\]\[]{0,120}'
                            r'|(?:company|first|last|full)?\s*name|company)\s*\]', re.IGNORECASE)


# [Your name], [your calendar link], [insert date]: text left for the sender to fill in.
BRACKETS_RE = re.compile(r'\[\s*(?:your|insert|add|todo|tbd|name|company|phone|link)\b[^\]\[\n]{0,60}\]', re.IGNORECASE)


def check_campaign(db, cfg, settings, campaign):
    """Runs the content checks, stores and returns the report."""
    renderer = Renderer(cfg, db)
    contact = sample_contact(db, campaign)
    mid = '0' * 32
    from_email, from_name, _ = sender_of(db, settings, campaign)
    view = dict(campaign, from_email=from_email, from_name=from_name)
    content = None
    if campaign['ai_mode'] == 'own' and contact['id']:
        own = db.one("SELECT * FROM personalizations WHERE campaign_id = ? AND contact_id = ? AND status = 'edited'",
                     campaign['id'], contact['id'])
        if own is not None:   # check what this lead really gets
            from . import perlead
            content = perlead.content_for(campaign, own)
    msg = renderer.campaign_message(view, contact, mid, settings, tracking=False, content=content)
    if content is not None:
        from .render import text_to_html
        findings = spamcheck.lint(content['subject'], text_to_html(content['text']), content['text'], from_email)
    else:
        findings = spamcheck.lint(campaign['subject'], campaign['html'], campaign['text'], from_email)
    if (campaign['subject_b'] or '').strip():
        seen = {f['title'] for f in findings}
        for finding in spamcheck.lint(campaign['subject_b'], campaign['html'], campaign['text'], from_email):
            if finding['level'] != 'ok' and finding['title'] not in seen:
                findings.append(dict(finding, title=f'Subject B: {finding["title"]}'))
    for step in db.q('SELECT * FROM campaign_steps WHERE campaign_id = ? ORDER BY position', campaign['id']):
        subject = 'Follow-up' if step['as_reply'] else step['subject']
        for finding in spamcheck.lint(subject, step['html'], '', ''):
            if finding['level'] != 'ok' and 'no-reply' not in finding['title']:
                findings.append(dict(finding, title=f'Follow-up {step["position"]}: {finding["title"]}'))
    if len(findings) > 1:
        findings = [f for f in findings if f['level'] != 'ok']
    rspamd = spamcheck.rspamd_check(cfg.RSPAMD_URL, msg.as_bytes(), from_email, contact['email'], timeout=6)
    report = {'findings': findings, 'rspamd': rspamd, 'verdict': spamcheck.verdict(findings, rspamd),
              'checked_at': now(), 'custom_unsubscribe': bool(UNSUB_TAG_RE.search(campaign['html'] or ''))}
    db.run('UPDATE campaigns SET spam_score = ?, spam_report = ? WHERE id = ?',
           rspamd['score'] if rspamd else None, json.dumps(report), campaign['id'])
    return report


def parse_when(value, settings):
    """Accepts a timestamp, 'now', or local 'YYYY-MM-DD HH:MM' / ISO 8601 text."""
    if value in (None, '', 'now'):
        return now()
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace('Z', '+00:00')
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise Problem(f'Could not understand the send time "{value}". Use YYYY-MM-DD HH:MM.')
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone(settings))
    return moment.timestamp()


def schedule_campaign(db, settings, campaign_id, when=None, need_approval=False):
    """Schedules a campaign, or parks it for approval. Returns the new status."""
    campaign = get_campaign(db, campaign_id)
    if campaign['status'] not in EDITABLE:
        raise Problem(f'Campaign is already {campaign["status"]}')
    problems = readiness(db, settings, campaign)
    if problems:
        raise Problem('Not ready to send: ' + ' '.join(problems))
    moment = parse_when(when, settings)
    writer.ensure_rows(db, campaign)   # the AI starts writing now, so emails are ready on time
    status = 'pending_approval' if need_approval else 'scheduled'
    db.run('UPDATE campaigns SET status = ?, scheduled_at = ?, hold_reason = ?, updated_at = ? WHERE id = ?',
           status, moment, '', now(), campaign_id)
    return status


def approve_campaign(db, settings, campaign_id):
    campaign = get_campaign(db, campaign_id)
    if campaign['status'] != 'pending_approval':
        raise Problem('This campaign is not waiting for approval')
    problems = readiness(db, settings, campaign)
    if problems:
        raise Problem('Not ready to send: ' + ' '.join(problems))
    db.run("UPDATE campaigns SET status = 'scheduled', scheduled_at = ?, updated_at = ? WHERE id = ?",
           max(campaign['scheduled_at'] or 0, 0) or now(), now(), campaign_id)


def set_state(db, campaign_id, action):
    """pause | resume | cancel | unschedule"""
    campaign = get_campaign(db, campaign_id)
    status = campaign['status']
    ts = now()
    if action == 'pause' and status == 'sending':
        db.run("UPDATE campaigns SET status = 'paused', hold_reason = 'Paused by you', updated_at = ? WHERE id = ?",
               ts, campaign_id)
    elif action == 'resume' and status == 'paused':
        db.run("UPDATE campaigns SET status = 'sending', hold_reason = '', next_send_at = ?, updated_at = ? "
               "WHERE id = ?", ts, ts, campaign_id)
    elif action == 'unschedule' and status in ('scheduled', 'pending_approval'):
        db.run("UPDATE campaigns SET status = 'draft', scheduled_at = NULL, updated_at = ? WHERE id = ?",
               ts, campaign_id)
    elif action == 'cancel' and status in ('sending', 'paused', 'scheduled', 'pending_approval'):
        with db.tx():
            db.run("UPDATE messages SET status = 'cancelled', error = 'Campaign cancelled' "
                   "WHERE campaign_id = ? AND status = 'queued'", campaign_id)
            db.run("UPDATE campaigns SET status = 'cancelled', finished_at = ?, hold_reason = '', updated_at = ? "
                   "WHERE id = ?", ts, ts, campaign_id)
    else:
        raise Problem(f'Cannot {action} a campaign that is {status}')
    return get_campaign(db, campaign_id)['status']


def duplicate_campaign(db, campaign_id):
    campaign = get_campaign(db, campaign_id)
    ts = now()
    fields = {key: campaign[key] for key in CAMPAIGN_FIELDS}
    fields['name'] = (campaign['name'] + ' (copy)')[:200]
    fields.update({'created_by': 'user', 'created_at': ts, 'updated_at': ts})
    columns = ', '.join(fields)
    marks = ', '.join('?' for _ in fields)
    with db.tx():
        new_id = db.run(f'INSERT INTO campaigns ({columns}) VALUES ({marks})', *fields.values()).lastrowid
        db.run('INSERT INTO campaign_steps (campaign_id, position, delay_days, send_if, as_reply, subject, html, '
               'created_at) SELECT ?, position, delay_days, send_if, as_reply, subject, html, ? '
               'FROM campaign_steps WHERE campaign_id = ?', new_id, ts, campaign_id)
    return new_id


# -- follow-up steps -------------------------------------------------------------
STEP_STARTER = ('<p>Hi {{first_name|there}},</p>\n<p>Just bringing this back to the top of your inbox in case it '
                'got buried. Would it be worth a quick chat?</p>\n<p>Thanks,<br>Your name</p>')
STEP_TEMPLATES = {
    'bump': ('Short reminder', 3, 'no_reply', STEP_STARTER),
    'value': ('Something useful', 4, 'no_reply',
              '<p>Hi {{first_name|there}},</p>\n<p>One more idea for {{company|your team}}: [your tip, example or '
              'result from a similar customer].</p>\n<p>Happy to share more if it is useful.</p>\n<p>Thanks,<br>Your name</p>'),
    'opened': ('They opened it', 2, 'opened_no_reply',
               '<p>Hi {{first_name|there}},</p>\n<p>I saw this might be on your radar. Is it worth 15 minutes this '
               'week, or should I check back later?</p>\n<p>Thanks,<br>Your name</p>'),
    'breakup': ('Last email', 6, 'no_reply',
                '<p>Hi {{first_name|there}},</p>\n<p>I have not heard back, so I will stop here. If timing changes, '
                'just reply to this email and I will pick it up.</p>\n<p>All the best,<br>Your name</p>'),
}


def clean_step(data):
    out = {}
    if 'delay_days' in data:
        try:
            delay = float(data['delay_days'])
        except (TypeError, ValueError):
            raise Problem('The wait must be a number of days')
        out['delay_days'] = max(0.01, min(delay, 365))
    if 'send_if' in data:
        if data['send_if'] not in dict(SEND_IF):
            raise Problem('Unknown follow-up condition')
        out['send_if'] = data['send_if']
    if 'as_reply' in data:
        out['as_reply'] = 1 if str(data['as_reply']).lower() in ('1', 'true', 'on', 'yes') else 0
    if 'subject' in data and data['subject'] is not None:
        out['subject'] = str(data['subject']).strip()[:300]
    if 'html' in data and data['html'] is not None:
        out['html'] = str(data['html'])[:500_000]
    return out


def steps_editable(campaign):
    return campaign['status'] not in ('sent', 'cancelled')


def add_step(db, campaign_id, data=None, template=None):
    campaign = get_campaign(db, campaign_id)
    if template in STEP_TEMPLATES:
        _, delay, send_if, html = STEP_TEMPLATES[template]
        data = dict({'delay_days': delay, 'send_if': send_if, 'html': html}, **(data or {}))
    if not steps_editable(campaign):
        raise Problem(f'Campaign is {campaign["status"]}; follow-ups can no longer be added')
    position = db.val('SELECT COALESCE(MAX(position), 0) + 1 FROM campaign_steps WHERE campaign_id = ?',
                      campaign_id, default=1)
    if position > 10:
        raise Problem('A campaign can have at most 10 follow-ups')
    fields = {'delay_days': 3 if position == 1 else 4, 'send_if': 'no_reply', 'as_reply': 1, 'subject': '',
              'html': STEP_STARTER}
    fields.update(clean_step(data or {}))
    db.run('INSERT INTO campaign_steps (campaign_id, position, delay_days, send_if, as_reply, subject, html, '
           'created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', campaign_id, position, fields['delay_days'],
           fields['send_if'], fields['as_reply'], fields['subject'], fields['html'], now())
    db.run("UPDATE campaigns SET spam_score = NULL, spam_report = '' WHERE id = ?", campaign_id)
    return position


def update_step(db, campaign_id, position, data):
    campaign = get_campaign(db, campaign_id)
    if not steps_editable(campaign):
        raise Problem(f'Campaign is {campaign["status"]}; its follow-ups can no longer be changed')
    fields = clean_step(data)
    if not fields:
        return
    sets = ', '.join(f'{k} = ?' for k in fields)
    if not db.run(f'UPDATE campaign_steps SET {sets} WHERE campaign_id = ? AND position = ?',
                  *fields.values(), campaign_id, position).rowcount:
        raise Problem(f'Follow-up {position} does not exist')
    db.run("UPDATE campaigns SET spam_score = NULL, spam_report = '' WHERE id = ?", campaign_id)


def delete_step(db, campaign_id, position):
    campaign = get_campaign(db, campaign_id)
    if not steps_editable(campaign):
        raise Problem(f'Campaign is {campaign["status"]}; its follow-ups can no longer be changed')
    if not isinstance(position, int) or position < 1:
        raise Problem('Only follow-ups can be deleted, not the first email')
    last = db.val('SELECT MAX(position) FROM campaign_steps WHERE campaign_id = ?', campaign_id, default=0)
    if position != last:
        raise Problem('Delete the last follow-up first, so the order stays clear')
    if db.val("SELECT COUNT(*) FROM messages WHERE campaign_id = ? AND step = ? "
              "AND (sent_at IS NOT NULL OR bounced_at IS NOT NULL OR status IN ('sent', 'sending', 'failed', 'bounced'))",
              campaign_id, position, default=0):
        raise Problem(f'Follow-up {position} has already been sent to some leads; it cannot be deleted')
    with db.tx():
        db.run("UPDATE messages SET status = 'cancelled', error = 'This follow-up step was deleted' "
               "WHERE campaign_id = ? AND step = ? AND status = 'queued'", campaign_id, position)
        db.run('DELETE FROM campaign_steps WHERE campaign_id = ? AND position = ?', campaign_id, position)


# -- leads -------------------------------------------------------------------------
def set_lead_status(db, contact_id, status):
    if status not in dict(LEAD_STATUSES):
        raise Problem('Unknown lead status')
    if not db.run('UPDATE contacts SET lead_status = ?, updated_at = ? WHERE id = ?', status, now(), contact_id).rowcount:
        raise Problem('Lead not found')
    if status not in ('new', 'contacted'):
        # The operator moved this lead on: stop automatic follow-ups to them.
        db.run("UPDATE messages SET status = 'cancelled', error = ? WHERE contact_id = ? AND kind = 'campaign' "
               "AND step > 0 AND status = 'queued'", f'Follow-up stopped: lead marked {status}', contact_id)


def reply_to_lead(db, cfg, settings, reply, text, need_approval=False):
    """Answers a lead's reply in the same email thread. Returns the message id."""
    text = (text or '').strip()
    if not text:
        raise Problem('Write a reply first.')
    if '[' in text and re.search(r'\[(your|insert|add)[^\]]*\]', text, re.IGNORECASE):
        raise Problem('The reply still has a [placeholder] to fill in.')
    from_email = settings.get('reply_to') or settings.get('from_email') or ''
    from_name = settings.get('from_name', '')
    original = db.one('SELECT * FROM messages WHERE id = ?', reply['message_id']) if reply['message_id'] else None
    if original is not None and original['from_email']:
        from_email = original['from_email']
    account = None
    if original is not None and original['mailbox_id']:
        # Answer from the same account the lead wrote back to.
        account = db.one('SELECT * FROM mailboxes WHERE id = ?', original['mailbox_id'])
        if account is not None:
            from_email, from_name = account['email'], account['from_name'] or from_name
    if not contacts.normalize_email(from_email):
        raise Problem('No From address. Set one in Settings.')
    subject = reply['subject'] or (original['subject'] if original is not None else '')
    subject = re.sub(r'(?i)^\s*((re|aw|sv)\s*:\s*)+', '', subject)
    headers = {}
    if reply['header_id']:
        headers = {'In-Reply-To': reply['header_id'], 'References': reply['header_id']}
    import html as html_lib
    body_html = ''.join(f'<p>{html_lib.escape(p).replace(chr(10), "<br>")}</p>' for p in text.split('\n\n'))
    message_id = queue_single(db, kind='reply', from_email=from_email, from_name=from_name,
                              to=[reply['from_email']], subject=f'Re: {subject}'[:300], html=body_html, text=text,
                              headers=headers, track=False, contact_id=reply['contact_id'],
                              status='pending_approval' if need_approval else 'queued',
                              mailbox_id=account['id'] if account is not None else None)
    db.run('UPDATE replies SET handled_at = ?, read_at = COALESCE(read_at, ?) WHERE id = ?', now(), now(), reply['id'])
    return message_id


def delete_campaign(db, campaign_id):
    campaign = get_campaign(db, campaign_id)
    if campaign['status'] in ('sending', 'scheduled'):
        raise Problem('Cancel the campaign before deleting it')
    with db.tx():
        db.run('DELETE FROM events WHERE campaign_id = ?', campaign_id)
        db.run('DELETE FROM links WHERE campaign_id = ?', campaign_id)
        db.run('DELETE FROM campaigns WHERE id = ?', campaign_id)


def send_test(db, cfg, settings, campaign_id, to_email):
    """Sends the campaign as one recipient would get it, to a chosen address."""
    campaign = get_campaign(db, campaign_id)
    address = contacts.normalize_email(to_email)
    if not address:
        raise Problem(f'"{to_email}" is not a valid email address')
    from_email, from_name, mailbox_id = sender_of(db, settings, campaign)
    if not contacts.normalize_email(from_email):
        raise Problem('No From address. Set one on the campaign or a default in Settings.')
    contact = sample_contact(db, campaign)
    extra = {}
    if campaign['ai_mode'] and contact['id']:
        # The test shows what the first lead really gets: their own email, or the AI's version.
        content = message_content(db, campaign, {'contact_id': contact['id'], 'step': 0, 'parent_id': None})
        extra = {'content': content['content'], 'ai_line': content['ai_line']}
    if not extra.get('content') and not campaign['html'].strip() and not campaign['text'].strip():
        raise Problem('The message body is empty.')
    mid = security.new_id()
    subject, html, text = Renderer(cfg, db).campaign_bodies(campaign, contact, mid, settings, tracking=False,
                                                           **extra)
    reply_to = campaign['reply_to'] or settings.get('reply_to', '')
    if mailbox_id and reply_to == settings.get('reply_to'):
        reply_to = ''
    return queue_single(db, kind='test', mid=mid, from_email=from_email, from_name=from_name,
                        to=[address], subject=f'[Test] {subject}', html=html, text=text,
                        reply_to=[reply_to] if reply_to else [], track=False, mailbox_id=mailbox_id)


def approve_message(db, message_id):
    row = db.one("SELECT * FROM messages WHERE id = ? AND status = 'pending_approval'", message_id)
    if row is None:
        raise Problem('This email is not waiting for approval')
    db.run("UPDATE messages SET status = 'queued', scheduled_at = 0 WHERE id = ?", message_id)
    events.record(db, row, 'queued', detail='Approved')


def discard_message(db, message_id):
    done = db.run("UPDATE messages SET status = 'cancelled', error = 'Not approved' "
                  "WHERE id = ? AND status = 'pending_approval'", message_id).rowcount
    if not done:
        raise Problem('This email is not waiting for approval')


def pending_approvals(db):
    return {
        'campaigns': db.q("SELECT c.*, l.name AS list_name FROM campaigns c LEFT JOIN lists l ON l.id = c.list_id "
                          "WHERE c.status = 'pending_approval' ORDER BY c.id"),
        'messages': db.q("SELECT * FROM messages WHERE status = 'pending_approval' ORDER BY created_at"),
    }
