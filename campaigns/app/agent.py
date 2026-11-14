"""The AI agent: a tool-calling loop over the campaign operations.

Conversations are stored provider-neutrally:
    user       {"text": ...}
    assistant  {"text": ..., "tool_calls": [{"id", "name", "input"}], "provider", "model", "raw", "note"}
    tool       {"results": [{"id", "name", "output", "is_error"}]}
"""
import json
import time

from . import (ai, ai_anthropic, ai_openrouter, contacts, leadtools, mailboxes, news, planner, replies, service,
               stats, writer)
from .ai_common import ProviderError
from .db import loads, now
from .sender import day_start, local_time, queue_single, warmup_cap

PROVIDERS = ai.PROVIDERS
MAX_STEPS = 12
MAX_THREAD_MESSAGES = 150
MAX_OUTPUT = 20000

SYSTEM = """You are the email marketing assistant inside a self-hosted mail platform. The person you are \
talking to owns the account. You help them plan, write, check, schedule and review email campaigns, and you \
act through the tools you have been given.

How to work
- Start from facts. Call get_overview before your first action in a conversation so you know the sender \
identity, the timezone, the pacing defaults and whether sends need the owner's approval. Look up lists and \
campaigns instead of guessing ids.
- Write emails as clean, simple HTML with inline styles: a single column, real text, a clear call to action, \
and alt text on any image. Personalise with merge tags such as {{first_name|there}}; any column from the \
uploaded list is available as a tag. An unsubscribe link and the postal address are added automatically, so \
do not write your own unless the owner asks, in which case use {{unsubscribe_url}}.
- After creating or changing a campaign, run check_campaign and fix what it reports before you schedule. The \
check exists to make the message better for readers, which is also what keeps it out of the junk folder.
- Only state things about the owner's business, products, prices or offers that they told you. Ask when a \
fact you need is missing rather than inventing it.
- Subjects must describe what is in the email. No fake "Re:" or "Fwd:", no false urgency, no misleading \
sender names: these are deceptive, illegal in many countries, and they damage the sender's reputation.
- Mail only goes to the owner's uploaded lists. People who unsubscribed, bounced or complained are skipped \
automatically and must stay skipped.
- Pacing protects the sending domain. Keep the account's rate, window and warm-up settings unless the owner \
asks for a change, and say so if a request would send much faster than the account has been sending.

Leads, follow-ups and replies
- For cold outreach to business leads, prefer short plain emails, a personal opening ({{ai_line}} with AI \
writing set to "line") and one or two follow-ups in the same thread a few days apart. Follow-ups stop by \
themselves when a lead replies or the owner changes the lead's status.
- set_ai_writing turns on per-lead writing; preview_ai_writing shows a few results so the owner can judge \
them before anything is sent. With research on, the AI reads each lead's company website first; with news on, \
it also reads recent headlines about the company.
- When the owner uploads leads in the chat, you get a message saying which list they became. Read a sample with \
list_leads, say in one or two lines who they are (roles, companies, which columns can be used), then offer a \
campaign: short email with {{ai_line}}, AI writing "line" with research and news on, and one follow-up. Use \
lead_news for a few leads when the owner wants to see what is happening at their companies.
- check_email tells whether addresses will work before they go on a list. find_email guesses addresses; candidates marked unverified must not be added to a list without the \
owner's say-so, because bounces damage the sending domain. research_website reads a company's own site.
- get_inbox shows replies from leads. Draft answers for the owner; send_reply follows the same approval rule \
as other sends. Use create_task to put a follow-up call or email on the owner's calendar. When the owner \
wants the AI to read and answer replies by itself, use set_reply_autopilot.
- You may search the web when it helps (current facts about a company, an industry, a public event). Say \
where facts came from, and never present a guess as a finding.

Sending and approval
- schedule_campaign and send_email report back whether the mail was scheduled or is waiting for the owner's \
approval. Repeat that status accurately: if it is waiting for approval, say so and tell them it is in the \
"Waiting for approval" box; never describe it as sent.
- A tool error means the action did not happen. Say what failed and what you will do about it.

Keep replies short and concrete: what you did, what you found, and what you need from the owner next."""

_CAMPAIGN_PROPS = {
    'name': {'type': 'string', 'description': 'Internal name, not shown to recipients.'},
    'subject': {'type': 'string', 'description': 'Subject line. Merge tags allowed.'},
    'subject_b': {'type': 'string', 'description': 'Optional second subject to test. ab_percent of the list gets A '
                  'or B; after ab_wait_hours the one with more replies (then opens) goes to everyone else.'},
    'ab_percent': {'type': 'integer', 'description': 'Share of the list in the subject test, 5-50. Default 20.'},
    'ab_wait_hours': {'type': 'number', 'description': 'Hours to wait for results before picking the winner. Default 4.'},
    'skip_risky': {'type': 'boolean', 'description': 'Also skip system mailboxes and likely typos. Addresses that '
                   'would bounce are always skipped.'},
    'preheader': {'type': 'string', 'description': 'Preview text shown after the subject in the inbox.'},
    'html': {'type': 'string', 'description': 'HTML body. A fragment is fine; it is wrapped in a document.'},
    'text': {'type': 'string', 'description': 'Plain-text body. Omit to generate it from the HTML.'},
    'list_id': {'type': 'integer', 'description': 'Contact list to send to (from list_contact_lists).'},
    'from_name': {'type': 'string'},
    'from_email': {'type': 'string', 'description': 'Must be on a domain this server sends for.'},
    'reply_to': {'type': 'string'},
    'mailbox_ids': {'type': 'array', 'items': {'type': 'integer'},
                    'description': 'Email accounts (ids from get_overview) to send from in rotation instead of '
                                   'from_email. Empty list = from_email through this server.'},
    'rate_per_hour': {'type': 'integer', 'description': 'Maximum emails per hour for this campaign.'},
    'window_start': {'type': 'string', 'description': 'Earliest local time of day to send, HH:MM.'},
    'window_end': {'type': 'string', 'description': 'Latest local time of day to send, HH:MM.'},
    'daily_cap': {'type': 'integer', 'description': 'Maximum emails per day for this campaign, 0 = no cap.'},
}

TOOLS = [
    {'name': 'get_overview',
     'description': 'Account facts: brand, default sender, timezone and current local time, pacing defaults, '
                    'warm-up status, whether sends need approval, and counts of lists and campaigns.',
     'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'list_contact_lists',
     'description': 'All contact lists with their id, name, number of active contacts and merge-tag fields.',
     'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'list_campaigns',
     'description': 'Recent campaigns with status and headline results. Optionally filter by status.',
     'input_schema': {'type': 'object', 'properties': {
         'status': {'type': 'string', 'enum': ['draft', 'pending_approval', 'scheduled', 'sending', 'paused',
                                                'sent', 'cancelled']}}}},
    {'name': 'get_campaign',
     'description': 'Full details of one campaign: content, pacing, status, any hold reason and its results.',
     'input_schema': {'type': 'object', 'properties': {'campaign_id': {'type': 'integer'}},
                      'required': ['campaign_id']}},
    {'name': 'create_campaign',
     'description': 'Create a draft campaign. Nothing is sent. Returns the new id and the content check.',
     'input_schema': {'type': 'object', 'properties': _CAMPAIGN_PROPS, 'required': ['name', 'subject', 'html']}},
    {'name': 'update_campaign',
     'description': 'Change fields of a draft campaign (or the pacing of one that is already sending).',
     'input_schema': {'type': 'object',
                      'properties': dict({'campaign_id': {'type': 'integer'}}, **_CAMPAIGN_PROPS),
                      'required': ['campaign_id']}},
    {'name': 'check_campaign',
     'description': 'Run the content and spam-filter check on a campaign and whether it is ready to send.',
     'input_schema': {'type': 'object', 'properties': {'campaign_id': {'type': 'integer'}},
                      'required': ['campaign_id']}},
    {'name': 'send_test_email',
     'description': 'Send one copy of a campaign to an address the owner gave, so they can see it in an inbox.',
     'input_schema': {'type': 'object', 'properties': {'campaign_id': {'type': 'integer'},
                                                       'to_email': {'type': 'string'}},
                      'required': ['campaign_id', 'to_email']}},
    {'name': 'schedule_campaign',
     'description': 'Schedule a campaign to start sending to its list. Depending on account settings it is '
                    'scheduled or held for the owner\'s approval; the result says which.',
     'input_schema': {'type': 'object', 'properties': {
         'campaign_id': {'type': 'integer'},
         'send_at': {'type': 'string', 'description': 'Local start time "YYYY-MM-DD HH:MM" in the account '
                                                      'timezone. Omit to start as soon as possible.'}},
         'required': ['campaign_id']}},
    {'name': 'change_campaign_state',
     'description': 'Pause or resume a sending campaign, cancel one, or move a scheduled one back to draft.',
     'input_schema': {'type': 'object', 'properties': {
         'campaign_id': {'type': 'integer'},
         'action': {'type': 'string', 'enum': ['pause', 'resume', 'cancel', 'unschedule']}},
         'required': ['campaign_id', 'action']}},
    {'name': 'get_statistics',
     'description': 'Results for one campaign, or for the whole account over the last N days: delivered, '
                    'opens, clicks, bounces, spam complaints, unsubscribes, top links and results by mailbox '
                    'provider.',
     'input_schema': {'type': 'object', 'properties': {
         'campaign_id': {'type': 'integer'},
         'days': {'type': 'integer', 'description': 'Account-wide period, default 30.'}}}},
    {'name': 'send_email',
     'description': 'Send one individual email to one address (not a campaign). Held for approval when the '
                    'account requires it.',
     'input_schema': {'type': 'object', 'properties': {
         'to': {'type': 'string'}, 'subject': {'type': 'string'}, 'html': {'type': 'string'},
         'from_email': {'type': 'string', 'description': 'Omit to use the account default.'}},
         'required': ['to', 'subject', 'html']}},
    {'name': 'set_ai_writing',
     'description': 'Turn per-lead AI writing on or off for a draft campaign. mode "line": the AI writes a personal '
                    'opening for each lead where {{ai_line}} appears in the email (recommended). mode "full": it '
                    'rewrites the whole email per lead. mode "": off. research: read each lead\'s company website '
                    'first. instructions: what the owner wants the personal part to focus on.',
     'input_schema': {'type': 'object', 'properties': {
         'campaign_id': {'type': 'integer'}, 'mode': {'type': 'string', 'enum': ['line', 'full', '']},
         'instructions': {'type': 'string'}, 'research': {'type': 'boolean'},
         'news': {'type': 'boolean', 'description': 'Also use recent news headlines about each lead\'s company.'}},
         'required': ['campaign_id', 'mode']}},
    {'name': 'preview_ai_writing',
     'description': 'Write the AI version for the first few leads now (up to 3) and return them for review. '
                    'Costs one AI call per lead.',
     'input_schema': {'type': 'object', 'properties': {'campaign_id': {'type': 'integer'},
                                                       'count': {'type': 'integer'}},
                      'required': ['campaign_id']}},
    {'name': 'add_follow_up',
     'description': 'Add an automatic follow-up to a campaign. It goes to leads who got the previous email '
                    'delay_days earlier and have not replied (send_if no_reply_no_open: and did not open; '
                    'opened_no_reply: opened but did not reply). '
                    'as_reply true sends it in the same thread with "Re:" and the previous subject.',
     'input_schema': {'type': 'object', 'properties': {
         'campaign_id': {'type': 'integer'}, 'delay_days': {'type': 'number'},
         'html': {'type': 'string', 'description': 'Short HTML body; merge tags allowed.'},
         'as_reply': {'type': 'boolean'}, 'subject': {'type': 'string', 'description': 'Only when as_reply is false.'},
         'send_if': {'type': 'string', 'enum': ['no_reply', 'no_reply_no_open', 'opened_no_reply']}},
         'required': ['campaign_id', 'delay_days', 'html']}},
    {'name': 'get_inbox',
     'description': 'Replies from leads, newest first, with the AI category and what they wrote.',
     'input_schema': {'type': 'object', 'properties': {
         'filter': {'type': 'string', 'enum': ['needs_answer', 'interested', 'all']}}}},
    {'name': 'send_reply',
     'description': 'Answer a lead\'s reply in the same email thread (plain text). Held for the owner\'s '
                    'approval when the account requires it.',
     'input_schema': {'type': 'object', 'properties': {'reply_id': {'type': 'integer'}, 'text': {'type': 'string'}},
                      'required': ['reply_id', 'text']}},
    {'name': 'set_reply_autopilot',
     'description': 'How the AI handles new replies from leads that are interested, ask a question or want a '
                    'meeting. "off": the owner answers. "draft": the AI writes an answer the owner checks and '
                    'sends from the Inbox. "send": the AI answers by itself (still held for approval when sends '
                    'need approval). Interested leads also get a follow-up task.',
     'input_schema': {'type': 'object', 'properties': {'mode': {'type': 'string', 'enum': ['off', 'draft', 'send']}},
                      'required': ['mode']}},
    {'name': 'set_lead_status',
     'description': 'Move a lead in the pipeline. Any status other than new or contacted stops automatic follow-ups.',
     'input_schema': {'type': 'object', 'properties': {
         'email': {'type': 'string'},
         'status': {'type': 'string', 'enum': ['new', 'contacted', 'replied', 'interested', 'meeting', 'won',
                                               'not_interested', 'lost']}},
         'required': ['email', 'status']}},
    {'name': 'create_task',
     'description': 'Put a follow-up on the owner\'s calendar, optionally for one lead.',
     'input_schema': {'type': 'object', 'properties': {
         'title': {'type': 'string'}, 'due': {'type': 'string', 'description': 'Local "YYYY-MM-DD HH:MM".'},
         'email': {'type': 'string', 'description': 'The lead this is about, if any.'},
         'note': {'type': 'string'}},
         'required': ['title', 'due']}},
    {'name': 'list_tasks',
     'description': 'Open follow-up tasks due within the next N days (overdue ones included).',
     'input_schema': {'type': 'object', 'properties': {'days': {'type': 'integer'}}}},
    {'name': 'find_email',
     'description': 'Likely email addresses for a person at a company, with confidence. Checks that the '
                    'domain receives mail; verified only when the owner has added a Hunter.io key.',
     'input_schema': {'type': 'object', 'properties': {
         'first_name': {'type': 'string'}, 'last_name': {'type': 'string'},
         'company_domain': {'type': 'string', 'description': 'Website or domain, e.g. acme.com'}},
         'required': ['first_name', 'company_domain']}},
    {'name': 'check_email',
     'description': 'Check whether email addresses will work without emailing them: format, whether the domain '
                    'receives email, throwaway or system mailbox, likely typos; confirmed with Hunter.io when the '
                    'owner added a key.',
     'input_schema': {'type': 'object', 'properties': {
         'emails': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Up to 20 addresses.'}},
         'required': ['emails']}},
    {'name': 'research_website',
     'description': 'Read a company\'s public home page: title, description, headings and main text.',
     'input_schema': {'type': 'object', 'properties': {'url': {'type': 'string'}}, 'required': ['url']}},
    {'name': 'list_leads',
     'description': 'The leads in a list with every uploaded column (company, role, website...), lead stage and '
                    'whether they replied. Up to 50 at a time; use offset for more.',
     'input_schema': {'type': 'object', 'properties': {
         'list_id': {'type': 'integer'}, 'limit': {'type': 'integer'}, 'offset': {'type': 'integer'}},
         'required': ['list_id']}},
    {'name': 'lead_news',
     'description': 'Recent news headlines (last 60 days, Google News) about a company, or about the company of a '
                    'lead given by email. Use it to personalise a message; mention a headline only if it is clearly '
                    'about that company.',
     'input_schema': {'type': 'object', 'properties': {'company': {'type': 'string'}, 'email': {'type': 'string'}}}},
]

_STAT_KEYS = ('total', 'queued', 'sent', 'delivered', 'failed', 'bounced', 'hard_bounced', 'opened',
              'human_opened', 'clicked', 'complained', 'unsubscribed', 'delivery_rate', 'open_rate',
              'human_open_rate', 'click_rate', 'click_to_open', 'bounce_rate', 'complaint_rate',
              'unsubscribe_rate')


def _stats(data):
    return {key: data[key] for key in _STAT_KEYS}


def _campaign_summary(db, row):
    return {'id': row['id'], 'name': row['name'], 'subject': row['subject'], 'status': row['status'],
            'list_id': row['list_id'], 'hold_reason': row['hold_reason'] or None,
            'scheduled_at_local': _local(db, row['scheduled_at']), 'created_by': row['created_by']}


def _local(db, ts):
    if not ts:
        return None
    return local_time(db.settings(), ts).strftime('%Y-%m-%d %H:%M')


def _report_summary(report, problems):
    rspamd = report.get('rspamd')
    return {
        'verdict': report['verdict'],
        'findings': [{'level': f['level'], 'title': f['title'], 'advice': f['advice']}
                     for f in report['findings'] if f['level'] != 'ok'],
        'spam_filter': ({'score': rspamd['score'], 'reject_at': rspamd['required'], 'action': rspamd['action'],
                         'top_rules': [f'{s["name"]} ({s["score"]:+})' for s in rspamd['symbols'][:8]]}
                        if rspamd else 'not available on this server'),
        'not_ready_because': problems,
    }


class Toolbox:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg

    def run(self, name, args):
        """Executes one tool call. Returns (output text, is_error)."""
        handler = getattr(self, f'tool_{name}', None)
        if handler is None:
            return json.dumps({'error': f'Unknown tool {name}'}), True
        try:
            result = handler(**(args or {}))
        except service.Problem as exc:
            return json.dumps({'error': str(exc)}), True
        except TypeError as exc:
            return json.dumps({'error': f'Wrong arguments for {name}: {exc}'}), True
        except Exception as exc:  # a tool bug must not end the conversation
            return json.dumps({'error': f'{name} failed: {type(exc).__name__}: {exc}'[:500]}), True
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + '... [truncated]'
        return text, False

    # -- read --------------------------------------------------------------
    def tool_get_overview(self):
        db, settings = self.db, self.db.settings()
        local = local_time(settings)
        return {
            'brand': self.cfg.BRAND_NAME,
            'default_sender': {'name': settings['from_name'], 'email': settings['from_email'] or None,
                               'reply_to': settings['reply_to'] or None},
            'postal_address_set': bool(settings['postal_address'].strip()),
            'owner_notes': settings['ai_brand_voice'].strip() or None,
            'timezone': settings['timezone'], 'local_time': local.strftime('%Y-%m-%d %H:%M (%A)'),
            'sends_need_owner_approval': settings['ai_require_approval'] == '1',
            'pacing_defaults': {'rate_per_hour': settings['rate_per_hour'],
                                'window': f'{settings["window_start"]}-{settings["window_end"]}'
                                if settings['window_start'] else 'any time',
                                'daily_cap': settings['daily_cap'],
                                'per_domain_hourly_cap': settings['domain_hourly_cap']},
            'warmup_limit_today': warmup_cap(settings),
            'email_accounts': [{'id': a['row']['id'], 'email': a['row']['email'], 'active': bool(a['row']['active']),
                                'daily_limit': a['row']['daily_limit'], 'sent_today': a['sent_today'],
                                'sign_in': a['row']['status']}
                               for a in mailboxes.summary(db, day_start(settings))],
            'lists': db.val('SELECT COUNT(*) FROM lists', default=0),
            'active_contacts': db.val("SELECT COUNT(*) FROM contacts WHERE status = 'active'", default=0),
            'campaigns': db.val('SELECT COUNT(*) FROM campaigns', default=0),
            'last_30_days': _stats(stats.overall(db, now() - 30 * 86400, kind='campaign')),
        }

    def tool_list_contact_lists(self):
        out = []
        for row in self.db.q('SELECT * FROM lists ORDER BY id'):
            counts = contacts.list_counts(self.db, row['id'])
            fields = set()
            for member in self.db.q('SELECT c.fields FROM list_members m JOIN contacts c ON c.id = m.contact_id '
                                    'WHERE m.list_id = ? LIMIT 50', row['id']):
                fields.update(loads(member['fields']).keys())
            out.append({'id': row['id'], 'name': row['name'], 'description': row['description'],
                        'active_contacts': counts['active'], 'total_contacts': counts['total'],
                        'merge_tags': ['first_name', 'last_name', 'email'] + sorted(fields)})
        return out

    def tool_list_campaigns(self, status=None):
        if status:
            rows = self.db.q('SELECT * FROM campaigns WHERE status = ? ORDER BY id DESC LIMIT 25', status)
        else:
            rows = self.db.q('SELECT * FROM campaigns ORDER BY id DESC LIMIT 25')
        out = []
        for row in rows:
            item = _campaign_summary(self.db, row)
            if row['started_at']:
                data = stats.campaign(self.db, row['id'])
                item['results'] = {k: data[k] for k in ('sent', 'delivered', 'queued', 'open_rate', 'click_rate',
                                                        'bounce_rate', 'complaint_rate')}
            out.append(item)
        return out

    def tool_get_campaign(self, campaign_id):
        row = service.get_campaign(self.db, campaign_id)
        item = _campaign_summary(self.db, row)
        item.update({'subject_b': row['subject_b'] or None, 'ab_winner': row['ab_winner'] or None,
                     'skip_risky': bool(row['skip_risky']), 'preheader': row['preheader'], 'from_name': row['from_name'], 'from_email': row['from_email'],
                     'mailbox_ids': mailboxes.parse_ids(row['mailbox_ids']), 'reply_to': row['reply_to'], 'html': row['html'][:12000], 'text': row['text'][:4000],
                     'rate_per_hour': row['rate_per_hour'], 'window_start': row['window_start'],
                     'window_end': row['window_end'], 'daily_cap': row['daily_cap']})
        if row['started_at']:
            item['results'] = _stats(stats.campaign(self.db, campaign_id))
            item['hours_until_done'] = stats.hours_until_done(self.db, row)
        return item

    def tool_get_statistics(self, campaign_id=None, days=30):
        db = self.db
        if campaign_id:
            service.get_campaign(db, campaign_id)
            return {'campaign_id': campaign_id, 'results': _stats(stats.campaign(db, campaign_id)),
                    'top_links': [dict(r) for r in stats.top_links(db, campaign_id, limit=8)],
                    'by_mailbox_provider': [dict(domain=d['label'], sent=d['sent'], open_rate=d['open_rate'],
                                                 bounce_rate=d['bounce_rate'])
                                            for d in stats.by_domain(db, 0, campaign_id, limit=8)]}
        since = now() - max(1, min(int(days or 30), 365)) * 86400
        return {'days': days, 'results': _stats(stats.overall(db, since, kind='campaign')),
                'campaigns': [dict(id=c['key'], name=c['label'], sent=c['sent'], open_rate=c['open_rate'],
                                   click_rate=c['click_rate'], bounce_rate=c['bounce_rate'],
                                   complaint_rate=c['complaint_rate'])
                              for c in stats.by_campaign(db, since, limit=15)],
                'note': 'Open rates include automatic opens by mail apps (Apple Mail privacy protection); '
                        'human_open_rate is the conservative figure. Clicks are the more reliable signal.'}

    # -- write -------------------------------------------------------------
    def tool_create_campaign(self, **fields):
        db, settings = self.db, self.db.settings()
        campaign_id = service.create_campaign(db, settings, fields, created_by='agent')
        return self._checked(campaign_id, settings, created=True)

    def tool_update_campaign(self, campaign_id, **fields):
        db, settings = self.db, self.db.settings()
        row = service.update_campaign(db, settings, campaign_id, fields)
        if row['status'] not in service.EDITABLE:
            return {'campaign_id': campaign_id, 'status': row['status'], 'updated': sorted(fields)}
        return self._checked(campaign_id, settings)

    def tool_check_campaign(self, campaign_id):
        return self._checked(campaign_id, self.db.settings())

    def _checked(self, campaign_id, settings, created=False):
        row = service.get_campaign(self.db, campaign_id)
        report = service.check_campaign(self.db, self.cfg, settings, row)
        out = {'campaign_id': campaign_id, 'status': row['status'],
               'check': _report_summary(report, service.readiness(self.db, settings, row))}
        if created:
            out['created'] = True
        return out

    def tool_send_test_email(self, campaign_id, to_email):
        message_id = service.send_test(self.db, self.cfg, self.db.settings(), campaign_id, to_email)
        return {'queued': True, 'email_id': message_id, 'to': to_email,
                'note': 'Handed to the mail server; it should arrive within a minute.'}

    def tool_schedule_campaign(self, campaign_id, send_at=None):
        db, settings = self.db, self.db.settings()
        row = service.get_campaign(db, campaign_id)
        report = service.check_campaign(db, self.cfg, settings, row)
        if report['verdict'] == 'fix':
            bad = [f'{f["title"]} ({f["advice"]})' for f in report['findings'] if f['level'] == 'bad']
            raise service.Problem('The content check found problems that must be fixed first: '
                                  + ('; '.join(bad) or 'the spam filter score is over the limit'))
        need_approval = settings['ai_require_approval'] == '1'
        status = service.schedule_campaign(db, settings, campaign_id, send_at, need_approval=need_approval)
        row = service.get_campaign(db, campaign_id)
        recipients = contacts.list_counts(db, row['list_id'])['active']
        out = {'campaign_id': campaign_id, 'status': status, 'recipients': recipients,
               'start_local': _local(db, row['scheduled_at']), 'rate_per_hour': row['rate_per_hour']}
        if need_approval:
            out['note'] = ('NOT scheduled yet. It is waiting for the owner to approve it in the '
                           '"Waiting for approval" box. Tell them that.')
        return out

    def tool_change_campaign_state(self, campaign_id, action):
        if action == 'resume' and self.db.settings()['ai_require_approval'] == '1':
            row = service.get_campaign(self.db, campaign_id)
            if row['hold_reason'].startswith('Paused automatically'):
                raise service.Problem('This campaign was paused automatically to protect the sender reputation '
                                      f'({row["hold_reason"]}). Only the owner can resume it, from the campaign page.')
        return {'campaign_id': campaign_id, 'status': service.set_state(self.db, campaign_id, action)}

    def tool_send_email(self, to, subject, html, from_email=None):
        db, settings = self.db, self.db.settings()
        address = contacts.normalize_email(to)
        if not address:
            raise service.Problem(f'"{to}" is not a valid email address')
        sender = contacts.normalize_email(from_email or settings['from_email'])
        if not sender:
            raise service.Problem('No From address. The owner needs to set a default sender in Settings.')
        suppressed = contacts.is_suppressed(db, address)
        if suppressed:
            raise service.Problem(f'{address} is on the suppression list ({suppressed["reason"]}) and cannot be mailed.')
        if not subject.strip() or not html.strip():
            raise service.Problem('Subject and body are required.')
        need_approval = settings['ai_require_approval'] == '1'
        message_id = queue_single(db, kind='agent', from_email=sender, from_name=settings['from_name'],
                                  to=[address], subject=subject.strip()[:300], html=html,
                                  reply_to=[r for r in [settings['reply_to']] if r],
                                  status='pending_approval' if need_approval else 'queued')
        out = {'email_id': message_id, 'to': address, 'status': 'pending_approval' if need_approval else 'queued'}
        if need_approval:
            out['note'] = ('NOT sent yet. It is waiting for the owner to approve it in the '
                           '"Waiting for approval" box. Tell them that.')
        return out

    # -- leads, follow-ups, inbox ------------------------------------------
    def tool_set_ai_writing(self, campaign_id, mode, instructions=None, research=None, news=None):
        data = {'ai_mode': mode}
        if instructions is not None:
            data['ai_instructions'] = instructions
        if research is not None:
            data['ai_research'] = '1' if research else '0'
        if news is not None:
            data['ai_news'] = '1' if news else '0'
        row = service.update_campaign(self.db, self.db.settings(), campaign_id, data)
        out = {'campaign_id': campaign_id, 'ai_mode': row['ai_mode'], 'research': bool(row['ai_research']),
               'news': bool(row['ai_news'])}
        if row['ai_mode'] == 'line' and not writer.AI_LINE_RE.search(row['html'] or ''):
            out['note'] = 'Add {{ai_line}} to the email where the personal line should go (update_campaign).'
        return out

    def tool_preview_ai_writing(self, campaign_id, count=2):
        db, settings = self.db, self.db.settings()
        campaign = service.get_campaign(db, campaign_id)
        if not campaign['ai_mode']:
            raise service.Problem('AI writing is off for this campaign. Turn it on with set_ai_writing first.')
        try:
            results = writer.preview(db, settings, campaign, max(1, min(int(count or 2), 3)))
        except ai.NotConfigured as exc:
            raise service.Problem(str(exc))
        return [{'lead': r['contact']['email'], 'subject': r['subject'] or None,
                 'written': r['body'][:3000], 'error': r['error'] or None} for r in results]

    def tool_add_follow_up(self, campaign_id, delay_days, html, as_reply=True, subject='', send_if='no_reply'):
        position = service.add_step(self.db, campaign_id, {
            'delay_days': delay_days, 'html': html, 'as_reply': '1' if as_reply else '0', 'subject': subject,
            'send_if': send_if})
        return {'campaign_id': campaign_id, 'follow_up': position, 'delay_days': delay_days}

    def tool_get_inbox(self, filter='needs_answer'):
        where = {'needs_answer': 'r.handled_at IS NULL AND r.is_auto = 0',
                 'interested': "r.category IN ('interested', 'meeting', 'question')",
                 'all': '1 = 1'}.get(filter, 'r.handled_at IS NULL AND r.is_auto = 0')
        rows = self.db.q(f'SELECT r.*, c.first_name, c.last_name, c.lead_status FROM replies r '
                         f'LEFT JOIN contacts c ON c.id = r.contact_id WHERE {where} '
                         'ORDER BY r.received_at DESC LIMIT 20')
        return [{'reply_id': r['id'], 'from': r['from_email'],
                 'name': f'{r["first_name"] or ""} {r["last_name"] or ""}'.strip() or r['from_name'],
                 'subject': r['subject'], 'received_local': _local(self.db, r['received_at']),
                 'category': r['category'], 'summary': r['summary'], 'text': r['body'][:1500],
                 'lead_status': r['lead_status'], 'answered': bool(r['handled_at']),
                 'ai_draft': r['draft'] or None} for r in rows]

    def tool_set_reply_autopilot(self, mode):
        if mode not in ('off', 'draft', 'send'):
            raise service.Problem('mode must be off, draft or send')
        self.db.set_setting('ai_reply_mode', mode)
        return {'reply_autopilot': mode,
                'sends_need_owner_approval': self.db.settings()['ai_require_approval'] == '1'}

    def tool_send_reply(self, reply_id, text):
        db, settings = self.db, self.db.settings()
        row = db.one('SELECT * FROM replies WHERE id = ?', reply_id)
        if row is None:
            raise service.Problem(f'Reply {reply_id} does not exist')
        need_approval = settings['ai_require_approval'] == '1'
        message_id = service.reply_to_lead(db, self.cfg, settings, row, text, need_approval=need_approval)
        out = {'email_id': message_id, 'to': row['from_email'],
               'status': 'pending_approval' if need_approval else 'queued'}
        if need_approval:
            out['note'] = ('NOT sent yet. It is waiting for the owner to approve it in the '
                           '"Waiting for approval" box. Tell them that.')
        return out

    def _contact_by_email(self, address):
        address = contacts.normalize_email(address)
        row = self.db.one('SELECT * FROM contacts WHERE email = ?', address) if address else None
        if row is None:
            raise service.Problem(f'No lead with the address {address!r}')
        return row

    def tool_set_lead_status(self, email, status):
        contact = self._contact_by_email(email)
        service.set_lead_status(self.db, contact['id'], status)
        return {'lead': contact['email'], 'status': status}

    def tool_create_task(self, title, due, email=None, note=''):
        settings = self.db.settings()
        contact = self._contact_by_email(email) if email else None
        moment = service.parse_when(due, settings)
        task_id = planner.create_task(self.db, title, moment, contact['id'] if contact else None, note=note)
        return {'task_id': task_id, 'due_local': _local(self.db, moment), 'lead': contact['email'] if contact else None}

    def tool_list_tasks(self, days=7):
        until = now() + max(1, min(int(days or 7), 90)) * 86400
        return [{'task_id': t['id'], 'title': t['title'], 'due_local': _local(self.db, t['due_at']),
                 'lead': t['email'], 'overdue': t['due_at'] < now()} for t in planner.open_tasks(self.db, until)]

    def tool_find_email(self, first_name, company_domain, last_name=''):
        try:
            return leadtools.find_email(self.db, self.db.settings(), first_name, last_name, company_domain)
        except leadtools.ToolError as exc:
            raise service.Problem(str(exc))

    def tool_check_email(self, emails):
        if isinstance(emails, str):
            emails = [emails]
        try:
            results = leadtools.check_many(self.db, self.db.settings(), ' '.join(str(e) for e in emails[:20]), limit=20)
        except leadtools.ToolError as exc:
            raise service.Problem(str(exc))
        return [{'email': r['email'], 'verdict': r['verdict'], 'did_you_mean': r['suggestion'] or None,
                 'problems': [c['detail'] for c in r['checks'] if c['state'] != 'ok']} for r in results]

    def tool_research_website(self, url):
        try:
            return leadtools.fetch_site(self.db, url)
        except leadtools.ToolError as exc:
            raise service.Problem(str(exc))

    def tool_list_leads(self, list_id, limit=25, offset=0):
        db = self.db
        if not db.one('SELECT id FROM lists WHERE id = ?', list_id):
            raise service.Problem(f'List {list_id} does not exist')
        limit = max(1, min(int(limit or 25), 50))
        rows = db.q('SELECT c.* FROM list_members m JOIN contacts c ON c.id = m.contact_id WHERE m.list_id = ? '
                    'ORDER BY m.added_at, c.id LIMIT ? OFFSET ?', list_id, limit, max(0, int(offset or 0)))
        total = contacts.list_counts(db, list_id)['total']
        return {'list_id': list_id, 'total': total, 'showing': f'{offset + 1}-{offset + len(rows)}' if rows else '0',
                'leads': [{'email': r['email'], 'first_name': r['first_name'], 'last_name': r['last_name'],
                           'stage': r['lead_status'], 'subscribed': r['status'] == 'active',
                           **{k: str(v)[:200] for k, v in loads(r['fields']).items()}} for r in rows]}

    def tool_lead_news(self, company=None, email=None):
        if email:
            contact = self.db.one('SELECT * FROM contacts WHERE email = ?', email.strip().lower())
            if contact is None:
                raise service.Problem(f'No lead with the address {email}')
            company = company or news.company_of(contact)
        if not company:
            raise service.Problem('Give a company name (the lead has no company column or website).')
        try:
            items = news.search(self.db, company)
        except news.NewsError as exc:
            raise service.Problem(str(exc))
        return {'company': company, 'headlines': [
            {'title': i['title'], 'source': i['source'],
             'date': time.strftime('%Y-%m-%d', time.gmtime(i['date'])) if i['date'] else None, 'url': i['url']}
            for i in items] or 'No recent news found.'}


# -- conversation storage -------------------------------------------------------
def new_thread(db):
    ts = now()
    return db.run('INSERT INTO agent_threads (title, created_at, updated_at) VALUES (?, ?, ?)',
                  'New conversation', ts, ts).lastrowid


def history(db, thread_id):
    rows = db.q('SELECT id, role, content, created_at FROM agent_messages WHERE thread_id = ? ORDER BY id', thread_id)
    return [{'id': r['id'], 'role': r['role'], 'content': loads(r['content']), 'created_at': r['created_at']}
            for r in rows]


def _save(db, thread_id, role, content):
    ts = now()
    message_id = db.run('INSERT INTO agent_messages (thread_id, role, content, created_at) VALUES (?, ?, ?, ?)',
                        thread_id, role, json.dumps(content, ensure_ascii=False), ts).lastrowid
    db.run('UPDATE agent_threads SET updated_at = ? WHERE id = ?', ts, thread_id)
    return {'id': message_id, 'role': role, 'content': content, 'created_at': ts}


def _repair(db, thread_id, messages):
    """An assistant turn with tool calls must be followed by their results."""
    if messages and messages[-1]['role'] == 'assistant' and messages[-1]['content'].get('tool_calls'):
        results = [{'id': c['id'], 'name': c['name'], 'is_error': True,
                    'output': json.dumps({'error': 'Interrupted before this tool ran.'})}
                   for c in messages[-1]['content']['tool_calls']]
        messages.append(_save(db, thread_id, 'tool', {'results': results}))


def configured(settings):
    return ai.configured(settings)


def reply(db, cfg, thread_id, user_text, complete=None):
    """Adds the user's message, runs the agent and returns the new messages.

    `complete` can replace the provider call (used by tests).
    """
    settings = db.settings()
    provider_name = settings.get('ai_provider') or ai_openrouter.NAME
    if complete is None:
        try:
            provider, api_key, model = ai.resolve(db, settings, 'agent')
        except ai.NotConfigured as exc:
            raise service.Problem(str(exc))
        web_search = settings.get('ai_web_search') == '1'

        def complete(messages):
            return provider.complete(api_key, model, SYSTEM, messages, TOOLS, web_search=web_search)

    messages = history(db, thread_id)
    if len(messages) >= MAX_THREAD_MESSAGES:
        raise service.Problem('This conversation has become very long. Start a new one to continue.')
    _repair(db, thread_id, messages)
    start = len(messages)
    messages.append(_save(db, thread_id, 'user', {'text': user_text}))
    if start == 0:
        db.run('UPDATE agent_threads SET title = ? WHERE id = ?', ' '.join(user_text.split())[:60], thread_id)

    toolbox = Toolbox(db, cfg)
    for _ in range(MAX_STEPS):
        result = complete(messages)
        db.track_ai('agent', result.get('usage'))
        content = {'text': result.get('text', ''), 'tool_calls': result.get('tool_calls') or [],
                   'provider': provider_name, 'model': result.get('model', ''), 'raw': result.get('raw'),
                   'note': result.get('note', '')}
        messages.append(_save(db, thread_id, 'assistant', content))
        if result.get('paused') and not content['tool_calls']:
            continue   # a server-side web search paused the turn; resume it
        if not content['tool_calls']:
            break
        results = []
        for call in content['tool_calls']:
            output, is_error = toolbox.run(call['name'], call['input'])
            results.append({'id': call['id'], 'name': call['name'], 'output': output, 'is_error': is_error})
        messages.append(_save(db, thread_id, 'tool', {'results': results}))
    else:
        messages.append(_save(db, thread_id, 'assistant', {
            'text': '', 'tool_calls': [], 'provider': provider_name, 'raw': None,
            'note': f'Stopped after {MAX_STEPS} steps. Say "continue" to carry on.'}))
    return messages[start:]


__all__ = ['PROVIDERS', 'ProviderError', 'TOOLS', 'SYSTEM', 'Toolbox', 'configured', 'history', 'new_thread',
           'reply']
