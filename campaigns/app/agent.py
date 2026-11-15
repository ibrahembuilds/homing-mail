"""The AI agents: a tool-calling loop over the platform's operations.

Two personas share the loop and the tools:
    outreach   the main assistant: leads, research, campaigns, replies
    offer      helps the owner write a clear offer that the others then use

Conversations are stored provider-neutrally:
    user       {"text": ..., "shown": what the owner typed (optional), "attachment": {...} (optional)}
    assistant  {"text": ..., "tool_calls": [{"id", "name", "input"}], "provider", "model", "raw", "note"}
    tool       {"results": [{"id", "name", "output", "is_error"}]}

A turn runs in the background (agent_runs.py); run() reports what it is doing
through on_step so the chat can show live progress.
"""
import concurrent.futures
import json
import logging
import re
import time

from . import (ai, ai_anthropic, ai_openrouter, contacts, leadtools, mailboxes, news, offers, perlead, planner,
               prospects, replies, service, stats, textfmt, writer)
from .ai_common import ProviderError
from .db import loads, now
from .sender import day_start, local_time, queue_single, warmup_cap

log = logging.getLogger('campaigns.agent')

PROVIDERS = ai.PROVIDERS
MAX_STEPS = 16
MAX_THREAD_MESSAGES = 200
MAX_OUTPUT = 20000
RETRY_DELAYS = (4, 12)   # seconds before trying a temporary AI failure again

REPLY_STYLE = """How to write your replies to the owner
- Write like a capable colleague in a chat: plain words, short sentences, the answer or the result first.
- Usually 2 to 6 short lines. Use a list only for 3 or more parallel items (steps, leads, options). No headings, \
no tables unless asked, and bold at most the one thing they must not miss.
- No filler ("Great question", "Certainly", "I'd be happy to", "Let me know if you need anything else"), no \
restating their request, no emoji, no long dashes (— or –).
- Do the obvious next step yourself instead of asking leave for it, unless it sends email, spends their money or \
deletes something.
- End with at most one question, or the next step you will take."""

SYSTEM = """You are the outreach assistant inside a self-hosted email platform. The person you are talking to owns \
the account. You help them understand and research their leads, find missing email addresses, build lead lists, \
write, check, schedule and review campaigns, and handle replies. You act through your tools.

How to work
- Start from facts. Call get_overview before your first action in a conversation: it gives the sender, the \
timezone, pacing, whether sends need approval, the lists and the owner's offers. Look things up instead of \
guessing ids.
- You can read everything in the account: every list and lead (list_leads, search_leads, get_lead), campaigns, \
results, replies and the calendar. Check the data before asking the owner something it already answers.
- What the owner sells comes from their offer (get_offers). Never invent facts about their business, prices, \
results or clients. Without an offer, ask for the two or three facts you need, or suggest the Offers page where an \
assistant helps write one.

Leads
- When the owner uploads leads you get a note saying which list they became and how many still need an email. \
Read a sample with list_leads, then say in two or three lines who they are and which columns you can use.
- Leads without an email: find_missing_emails looks on each company's own website (home, contact and about \
pages). Addresses published there are added to the list. Guessed addresses (name@company.com) go on a list only \
after the owner says yes (set_prospect_email), because bounces damage the sending domain.
- research_lead reads a lead's website and recent news in one go; web_search finds what is not there. Save what \
matters with save_lead_notes, with the source, so the per-lead writer can use it. Never present a guess as a \
finding, and say where facts came from.
- To build a lead list: create_list, then add_leads. Only add people the owner gave you or businesses whose \
details you found on public pages (put the page in "source"). The do-not-email list always wins.
- check_email tells whether addresses will work. find_email guesses one person's address.

Writing emails
- Short, plain and specific, with one clear ask. Personalise with merge tags such as {{first_name|there}}; any \
column of the list is a tag. An unsubscribe link and the postal address are added automatically, so do not write \
your own unless the owner asks, in which case use {{unsubscribe_url}}.
- When the owner wants each email written for the lead, use AI writing "full" with research on: the writer then \
writes every lead's email about their situation, why they need it, how it helps them, and up to three of the \
owner's case studies (from the offer). Ask for real case studies on the Offers page if there are none; never make \
them up. If the leads already carry a written email in their columns (for example {{email_body}}), offer to send \
that as it is with use_own_email_per_lead. Add one follow-up in the same thread a few days later.
- When you write the draft yourself, follow this structure:
""" + textfmt.EMAIL_STRUCTURE.replace('How a first email is built', '  The first email') + """
- set_ai_writing turns on per-lead writing; preview_ai_writing shows a few results so the owner can judge them \
before anything is sent. When each lead already has a written email in a column, use_own_email_per_lead sends \
each lead their own one in a single campaign.
- After creating or changing a campaign, run check_campaign and fix what it reports before you schedule.
- Subjects must describe the email: no fake "Re:" or "Fwd:", no false urgency, no misleading sender names.
- Mail only goes to the owner's lists. Unsubscribed, bounced and complained people are skipped and stay skipped.
- Keep the account's pacing unless the owner asks, and say so if a request would send much faster than usual.

Replies and follow-ups
- get_inbox shows replies from leads. Draft answers for the owner; send_reply follows the approval rule. \
create_task puts a call or email on the owner's calendar. set_reply_autopilot lets the AI answer replies.

Sending and approval
- schedule_campaign, send_email and send_reply say whether mail was scheduled or is waiting for approval. Repeat \
that exactly: if it waits for approval, say it is in the "Waiting for approval" box and never call it sent.
- A tool error means the action did not happen. Say in one line what failed and what you will do.

""" + textfmt.STYLE_RULES.replace('How the writing must sound', 'How emails you write must sound') + '\n\n' + REPLY_STYLE

OFFER_SYSTEM = """You help the owner of a small business shape a clear offer for their outreach emails: what they \
give, who it is for, the problem it fixes, the result, why they can be trusted, and the one small next step a lead \
should take. The outreach assistant and the per-lead writer use the saved offer whenever they write.

How to work
- Start with get_offers to see what is saved, and get_overview for the account. If the owner has a website, read \
it with research_website before asking what it already answers. You may look at their lead lists (list_leads) to \
fit the offer to the people they are emailing.
- Ask one or two short questions at a time, in plain words. When you can, propose a concrete answer they can \
accept or change instead of an open question, for example: Something like "a free homepage redesign, yours to \
keep, no strings". Close?
- Push for specifics: a named audience ("independent tutoring businesses in the UK with dated websites"), a result \
they can see or count, real proof. Never invent proof, clients, numbers, prices or guarantees; leave a part empty \
rather than guess.
- A good call to action is small and easy: reply "yes", a 10-minute call, look at a preview.
- Ask for two or three real case studies: who the client was, the problem they had, what changed (with numbers if \
the owner has them). Save them in case_studies, one per line. The emails quote them, so they must be true.
- When you have enough, show the offer as a short draft, one line per part, and ask if it is right. Save it with \
save_offer once they agree or ask you to save. Then say that the outreach assistant will use it.
- You can search the web (web_search) for how others in their market describe similar offers, but the offer must \
stay true to what the owner told you.

""" + REPLY_STYLE

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
    'offer_id': {'type': 'integer', 'description': 'Offer the AI writing should use (from get_offers). Omit for the '
                 'default offer.'},
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
_STAGES = [key for key, _ in service.LEAD_STATUSES]
_OFFER_PROPS = {key: {'type': 'string', 'description': hint} for key, _, hint, _ in offers.FIELDS}

TOOLS = [
    {'name': 'get_overview',
     'description': 'Account facts: brand, default sender, timezone and current local time, pacing defaults, '
                    'warm-up status, whether sends need approval, offers, lists, and counts of leads and campaigns.',
     'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'list_contact_lists',
     'description': 'All contact lists with their id, name, number of active contacts, leads still needing an email '
                    'and merge-tag fields.',
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
    {'name': 'use_own_email_per_lead',
     'description': 'Give each lead of a draft campaign their own email, taken from two columns of its list (for '
                    'example email_subject and email_body). Leads with an empty column get the main email. Use it '
                    'when the uploaded file already holds a written email per lead.',
     'input_schema': {'type': 'object', 'properties': {
         'campaign_id': {'type': 'integer'}, 'body_column': {'type': 'string'},
         'subject_column': {'type': 'string', 'description': 'Empty = the campaign subject.'}},
         'required': ['campaign_id', 'body_column']}},
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
         'email': {'type': 'string'}, 'status': {'type': 'string', 'enum': _STAGES}},
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
     'description': 'The leads in a list with every uploaded column (company, role, website...), lead stage, saved '
                    'research and whether they replied, plus how many still need an email. Up to 50 at a time; '
                    'use offset for more.',
     'input_schema': {'type': 'object', 'properties': {
         'list_id': {'type': 'integer'}, 'limit': {'type': 'integer'}, 'offset': {'type': 'integer'}},
         'required': ['list_id']}},
    {'name': 'search_leads',
     'description': 'Find leads across all lists by name, email, company or any column text, and/or by stage.',
     'input_schema': {'type': 'object', 'properties': {
         'query': {'type': 'string'}, 'stage': {'type': 'string', 'enum': _STAGES},
         'list_id': {'type': 'integer'}, 'limit': {'type': 'integer', 'description': 'Default 25, at most 50.'}}}},
    {'name': 'get_lead',
     'description': 'Everything about one lead: every column, stage, notes, saved research, the lists they are in, '
                    'every email sent to them (opened, clicked) and every reply, and open tasks.',
     'input_schema': {'type': 'object', 'properties': {'email': {'type': 'string'}}, 'required': ['email']}},
    {'name': 'research_lead',
     'description': 'Research one lead\'s company now: their website (what they do and the email addresses '
                    'published on it) and recent news. Give the lead\'s email, or a prospect_id for a lead without '
                    'an email, or a website.',
     'input_schema': {'type': 'object', 'properties': {
         'email': {'type': 'string'}, 'prospect_id': {'type': 'integer'}, 'website': {'type': 'string'}}}},
    {'name': 'save_lead_notes',
     'description': 'Save facts about a lead (what they do, recent news, what to mention), each with where it came '
                    'from. The per-lead writer uses these notes. Adds to earlier notes unless replace is true.',
     'input_schema': {'type': 'object', 'properties': {
         'email': {'type': 'string'}, 'notes': {'type': 'string'}, 'replace': {'type': 'boolean'}},
         'required': ['email', 'notes']}},
    {'name': 'update_lead',
     'description': 'Correct a lead\'s first or last name, or set columns (company, website, role, city...). Columns '
                    'become merge tags. An empty value removes a column.',
     'input_schema': {'type': 'object', 'properties': {
         'email': {'type': 'string'}, 'first_name': {'type': 'string'}, 'last_name': {'type': 'string'},
         'fields': {'type': 'object', 'additionalProperties': {'type': 'string'}}},
         'required': ['email']}},
    {'name': 'list_prospects',
     'description': 'Leads in a list that have no email address yet, with what the search found for each.',
     'input_schema': {'type': 'object', 'properties': {'list_id': {'type': 'integer'}}, 'required': ['list_id']}},
    {'name': 'find_missing_emails',
     'description': 'Look for the addresses of a list\'s leads that have none, on each company\'s own website. '
                    'Addresses published there are added to the list; guesses wait for the owner\'s OK. About a '
                    'minute for 40 leads.',
     'input_schema': {'type': 'object', 'properties': {
         'list_id': {'type': 'integer'},
         'prospect_ids': {'type': 'array', 'items': {'type': 'integer'}, 'description': 'Only these leads.'}},
         'required': ['list_id']}},
    {'name': 'set_prospect_email',
     'description': 'Add a lead that had no email to its list with this address. Only with an address the owner '
                    'gave or approved.',
     'input_schema': {'type': 'object', 'properties': {'prospect_id': {'type': 'integer'}, 'email': {'type': 'string'}},
                      'required': ['prospect_id', 'email']}},
    {'name': 'create_list',
     'description': 'Create an empty lead list.',
     'input_schema': {'type': 'object', 'properties': {'name': {'type': 'string'}, 'description': {'type': 'string'}},
                      'required': ['name']}},
    {'name': 'add_leads',
     'description': 'Add leads to a list. Each needs an email, or a website so the email can be found later with '
                    'find_missing_emails. Other keys (company, role, city, notes...) become columns. Put where you '
                    'found each lead in "source".',
     'input_schema': {'type': 'object', 'properties': {
         'list_id': {'type': 'integer'},
         'leads': {'type': 'array', 'items': {'type': 'object', 'properties': {
             'email': {'type': 'string'}, 'first_name': {'type': 'string'}, 'last_name': {'type': 'string'},
             'company': {'type': 'string'}, 'website': {'type': 'string'}, 'role': {'type': 'string'},
             'city': {'type': 'string'}, 'source': {'type': 'string'}}}}},
         'required': ['list_id', 'leads']}},
    {'name': 'lead_news',
     'description': 'Recent news headlines (last 60 days, Google News) about a company, or about the company of a '
                    'lead given by email. Use it to personalise a message; mention a headline only if it is clearly '
                    'about that company.',
     'input_schema': {'type': 'object', 'properties': {'company': {'type': 'string'}, 'email': {'type': 'string'}}}},
    {'name': 'get_offers',
     'description': 'The owner\'s saved offers: what they give, to whom, the problem, the result, proof and the '
                    'call to action. The default one applies unless a campaign picks another.',
     'input_schema': {'type': 'object', 'properties': {}}},
    {'name': 'save_offer',
     'description': 'Create an offer, or change one (give offer_id). Only parts the owner told you or agreed to.',
     'input_schema': {'type': 'object', 'properties': dict({'offer_id': {'type': 'integer'},
                                                            'make_default': {'type': 'boolean'}}, **_OFFER_PROPS)}},
]

WEB_SEARCH_TOOL = {
    'name': 'web_search',
    'description': 'Search the web. Returns the best results with their address and a short summary. Use it for '
                   'facts about a company, person or market that are not in the account, and say where facts '
                   'came from.',
    'input_schema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}

_OFFER_TOOL_NAMES = ('get_overview', 'get_offers', 'save_offer', 'research_website', 'list_contact_lists',
                     'list_leads', 'search_leads')
OFFER_TOOLS = [t for t in TOOLS if t['name'] in _OFFER_TOOL_NAMES]

PERSONAS = {
    'outreach': {'system': SYSTEM, 'tools': TOOLS},
    'offer': {'system': OFFER_SYSTEM, 'tools': OFFER_TOOLS},
}

# What a running turn is doing, for the live line under the chat.
ACTIVITY = {
    'get_overview': 'Looking at the account', 'list_contact_lists': 'Looking at your lists',
    'list_campaigns': 'Looking at the campaigns', 'get_campaign': 'Opening the campaign',
    'create_campaign': 'Creating the campaign', 'update_campaign': 'Editing the campaign',
    'check_campaign': 'Checking the email', 'send_test_email': 'Sending a test email',
    'schedule_campaign': 'Scheduling', 'change_campaign_state': 'Changing the campaign',
    'get_statistics': 'Reading the results', 'send_email': 'Preparing the email',
    'set_ai_writing': 'Setting up AI writing', 'preview_ai_writing': 'Writing samples for a few leads',
    'use_own_email_per_lead': 'Giving each lead their own email',
    'add_follow_up': 'Adding a follow-up', 'get_inbox': 'Reading the inbox', 'send_reply': 'Preparing the reply',
    'set_reply_autopilot': 'Changing how replies are handled', 'set_lead_status': 'Moving the lead',
    'create_task': 'Adding it to the calendar', 'list_tasks': 'Looking at the calendar',
    'find_email': 'Looking for an email address', 'check_email': 'Checking email addresses',
    'research_website': 'Reading their website', 'list_leads': 'Reading the leads',
    'search_leads': 'Searching your leads', 'get_lead': 'Opening the lead', 'research_lead': 'Researching the lead',
    'save_lead_notes': 'Saving notes on the lead', 'update_lead': 'Updating the lead',
    'list_prospects': 'Looking at leads without an email',
    'find_missing_emails': 'Looking for emails on their websites', 'set_prospect_email': 'Adding the lead',
    'create_list': 'Creating the list', 'add_leads': 'Adding the leads', 'lead_news': 'Reading the news',
    'get_offers': 'Reading your offers', 'save_offer': 'Saving the offer', 'web_search': 'Searching the web',
}

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


def _company(fields):
    return next((str(fields[k]) for k in news.COMPANY_FIELDS if fields.get(k)), '')


def _lead_brief(row):
    fields = loads(row['fields'])
    return {'email': row['email'], 'name': f'{row["first_name"]} {row["last_name"]}'.strip() or None,
            'company': _company(fields) or None, 'website': leadtools.website_from_fields(fields) or None,
            'stage': row['lead_status'], 'subscribed': row['status'] == 'active',
            'has_research': bool(row['research'])}


class Toolbox:
    def __init__(self, db, cfg, search=None):
        self.db = db
        self.cfg = cfg
        self.search = search   # web search for providers without a built-in one

    def run(self, name, args):
        """Executes one tool call. Returns (output text, is_error)."""
        handler = getattr(self, f'tool_{name}', None)
        if handler is None:
            return json.dumps({'error': f'Unknown tool {name}'}), True
        try:
            result = handler(**(args or {}))
        except (service.Problem, ValueError, leadtools.ToolError) as exc:
            return json.dumps({'error': str(exc)}), True
        except TypeError as exc:
            return json.dumps({'error': f'Wrong arguments for {name}: {exc}'}), True
        except ProviderError as exc:
            return json.dumps({'error': str(exc)}), True
        except Exception as exc:  # a tool bug must not end the conversation
            log.exception('tool %s failed', name)
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
            'offers': [{'offer_id': o['id'], 'name': o['name'], 'summary': o['summary'], 'default': bool(o['is_default'])}
                       for o in offers.everything(db)] or 'none yet (the Offers page helps write one)',
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
            'lists': [{'list_id': r['id'], 'name': r['name']} for r in db.q('SELECT id, name FROM lists ORDER BY id DESC LIMIT 20')],
            'active_contacts': db.val("SELECT COUNT(*) FROM contacts WHERE status = 'active'", default=0),
            'leads_needing_email': db.val("SELECT COUNT(*) FROM prospects WHERE status IN ('needs_email', 'guessed')",
                                          default=0),
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
            waiting = prospects.counts(self.db, row['id'])
            out.append({'id': row['id'], 'name': row['name'], 'description': row['description'],
                        'active_contacts': counts['active'], 'total_contacts': counts['total'],
                        'leads_needing_email': waiting.get('needs_email', 0) + waiting.get('guessed', 0),
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
                     'skip_risky': bool(row['skip_risky']), 'preheader': row['preheader'], 'from_name': row['from_name'],
                     'from_email': row['from_email'], 'offer_id': row['offer_id'],
                     'mailbox_ids': mailboxes.parse_ids(row['mailbox_ids']), 'reply_to': row['reply_to'],
                     'html': row['html'][:12000], 'text': row['text'][:4000],
                     'ai_writing': {'mode': row['ai_mode'] or 'off', 'research': bool(row['ai_research']),
                                    'news': bool(row['ai_news'])},
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

    def tool_use_own_email_per_lead(self, campaign_id, body_column, subject_column=''):
        db = self.db
        campaign = service.get_campaign(db, campaign_id)
        cols = perlead.columns(db, campaign['list_id'])
        for col in (body_column, subject_column):
            if col and col not in cols:
                raise service.Problem(f'The list has no column "{col}". Its columns: {", ".join(cols) or "none"}.')
        row = service.update_campaign(db, db.settings(), campaign_id, {'ai_mode': 'own'})
        result = perlead.fill_from_columns(db, row, subject_column, body_column)
        return {'campaign_id': campaign_id, 'leads_with_own_email': result['filled'],
                'leads_getting_the_main_email': result['empty'],
                'note': 'The owner can read and edit each one on the campaign page (Personal emails).'}

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
        offset = max(0, int(offset or 0))
        rows = db.q('SELECT c.* FROM list_members m JOIN contacts c ON c.id = m.contact_id WHERE m.list_id = ? '
                    'ORDER BY m.added_at, c.id LIMIT ? OFFSET ?', list_id, limit, offset)
        total = contacts.list_counts(db, list_id)['total']
        waiting = prospects.counts(db, list_id)
        return {'list_id': list_id, 'total': total, 'showing': f'{offset + 1}-{offset + len(rows)}' if rows else '0',
                'leads_needing_email': waiting.get('needs_email', 0) + waiting.get('guessed', 0),
                'leads': [{'email': r['email'], 'first_name': r['first_name'], 'last_name': r['last_name'],
                           'stage': r['lead_status'], 'subscribed': r['status'] == 'active',
                           'research': r['research'][-600:] or None,
                           **{k: str(v)[:300] for k, v in loads(r['fields']).items()}} for r in rows]}

    def tool_search_leads(self, query='', stage=None, list_id=None, limit=25):
        db = self.db
        where, args, join = ['1 = 1'], [], ''
        if list_id:
            join = 'JOIN list_members m ON m.contact_id = c.id AND m.list_id = ?'
            args.append(int(list_id))
        query = ' '.join(str(query or '').split())
        if query:
            like = f'%{query}%'
            where.append('(c.email LIKE ? OR c.first_name LIKE ? OR c.last_name LIKE ? OR c.fields LIKE ? '
                         'OR c.research LIKE ? OR c.notes LIKE ?)')
            args += [like] * 6
        if stage:
            where.append('c.lead_status = ?')
            args.append(stage)
        limit = max(1, min(int(limit or 25), 50))
        rows = db.q(f'SELECT c.* FROM contacts c {join} WHERE {" AND ".join(where)} ORDER BY c.updated_at DESC '
                    'LIMIT ?', *args, limit)
        return {'found': len(rows), 'leads': [_lead_brief(r) for r in rows]}

    def tool_get_lead(self, email):
        db = self.db
        contact = self._contact_by_email(email)
        exchanged = []
        for item in replies.thread_for(db, contact['id'], contact['email'])[-30:]:
            row = item['row']
            if item['kind'] == 'out':
                exchanged.append({'we_sent': row['subject'], 'when': _local(db, row['sent_at'] or row['created_at']),
                                  'status': row['status'], 'opened': bool(row['opened_at']),
                                  'clicked': bool(row['clicked_at'])})
            else:
                exchanged.append({'they_replied': row['subject'], 'when': _local(db, row['received_at']),
                                  'category': row['category'], 'summary': row['summary'], 'text': row['body'][:800]})
        return {
            'email': contact['email'], 'first_name': contact['first_name'], 'last_name': contact['last_name'],
            'stage': contact['lead_status'], 'subscribed': contact['status'] == 'active',
            'address_check': contact['verify_status'] or 'not checked',
            'columns': {k: str(v)[:2000] for k, v in loads(contact['fields']).items()},
            'owner_notes': contact['notes'] or None, 'research': contact['research'] or None,
            'lists': [dict(r) for r in db.q('SELECT l.id AS list_id, l.name FROM list_members m JOIN lists l '
                                            'ON l.id = m.list_id WHERE m.contact_id = ?', contact['id'])],
            'emails': exchanged or 'nothing sent or received yet',
            'open_tasks': [{'title': t['title'], 'due_local': _local(db, t['due_at'])} for t in db.q(
                'SELECT * FROM tasks WHERE contact_id = ? AND done_at IS NULL ORDER BY due_at', contact['id'])],
        }

    def tool_research_lead(self, email=None, prospect_id=None, website=None):
        db = self.db
        fields, label, contact = {}, '', None
        if email:
            contact = self._contact_by_email(email)
            fields = loads(contact['fields'])
            site = leadtools.website_for(contact)
            label = contact['email']
        elif prospect_id:
            row = db.one('SELECT * FROM prospects WHERE id = ?', prospect_id)
            if row is None:
                raise service.Problem(f'No lead {prospect_id} without an email')
            fields, site, label = loads(row['fields']), row['website'], prospects.label(row)
        else:
            site = leadtools.domain_from(website or '')
            label = site
        site = site or leadtools.domain_from(website or '')
        company = _company(fields) or (news.company_of(contact) if contact else '') or (site.split('.')[0] if site else '')
        if not site and not company:
            raise service.Problem('Nothing to research: no website or company name for this lead.')

        def facts():
            try:
                return leadtools.fetch_site(db, site) if site else None
            except leadtools.ToolError as exc:
                return {'error': str(exc)}
            finally:
                db.close()

        def published():
            try:
                return leadtools.site_emails(db, site)['emails'] if site else []
            except leadtools.ToolError:
                return []
            finally:
                db.close()

        def headlines():
            try:
                return news.search(db, company) if company else []
            except news.NewsError:
                return []
            finally:
                db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            jobs = [pool.submit(facts), pool.submit(published), pool.submit(headlines)]
            site_facts, emails, items = (job.result() for job in jobs)
        out = {'lead': label, 'company': company or None, 'website': site or None,
               'site': ({'title': site_facts.get('title'), 'description': site_facts.get('description'),
                         'headings': site_facts.get('headings'), 'text': (site_facts.get('text') or '')[:2500]}
                        if site_facts and not site_facts.get('error') else (site_facts or {}).get('error')),
               'emails_on_their_site': [{'email': e['email'], 'page': e['page']} for e in emails] or None,
               'recent_news': [{'title': i['title'], 'source': i['source'],
                                'date': time.strftime('%Y-%m-%d', time.gmtime(i['date'])) if i['date'] else None,
                                'url': i['url']} for i in items[:6]] or None}
        if contact is not None and contact['research']:
            out['already_saved_notes'] = contact['research'][-1500:]
        return out

    def tool_save_lead_notes(self, email, notes, replace=False):
        contact = self._contact_by_email(email)
        text = str(notes or '').strip()[:3000]
        if not text:
            raise service.Problem('Write the notes to save.')
        stamp = time.strftime('%Y-%m-%d')
        combined = text if replace else f'{contact["research"]}\n\n[{stamp}] {text}'.strip()
        combined = combined[-6000:]
        self.db.run('UPDATE contacts SET research = ?, updated_at = ? WHERE id = ?', combined, now(), contact['id'])
        return {'lead': contact['email'], 'saved': True, 'notes_length': len(combined)}

    def tool_update_lead(self, email, first_name=None, last_name=None, fields=None):
        contact = self._contact_by_email(email)
        merged = loads(contact['fields'])
        for key, value in (fields or {}).items():
            slug = re.sub(r'[^a-z0-9]+', '_', str(key).lower()).strip('_')[:40]
            if not slug or slug in ('email', 'first_name', 'last_name'):
                continue
            if value in (None, ''):
                merged.pop(slug, None)
            else:
                merged[slug] = str(value)[:1000]
        if len(merged) > 60:
            raise service.Problem('A lead can have at most 60 columns.')
        self.db.run('UPDATE contacts SET first_name = ?, last_name = ?, fields = ?, updated_at = ? WHERE id = ?',
                    contact['first_name'] if first_name is None else str(first_name).strip()[:100],
                    contact['last_name'] if last_name is None else str(last_name).strip()[:100],
                    json.dumps(merged, ensure_ascii=False), now(), contact['id'])
        return {'lead': contact['email'], 'updated': True}

    def tool_list_prospects(self, list_id):
        if not self.db.one('SELECT id FROM lists WHERE id = ?', list_id):
            raise service.Problem(f'List {list_id} does not exist')
        rows = prospects.waiting(self.db, list_id)
        return {'list_id': list_id, 'waiting': len(rows), 'leads': [prospects.as_dict(r) for r in rows[:60]]}

    def tool_find_missing_emails(self, list_id, prospect_ids=None):
        if not self.db.one('SELECT id FROM lists WHERE id = ?', list_id):
            raise service.Problem(f'List {list_id} does not exist')
        result = prospects.find_emails(self.db, self.db.settings(), list_id=list_id, ids=prospect_ids or None)
        if not result['checked']:
            return {'checked': 0, 'note': 'No leads in this list are waiting for an email search.'}
        result['note'] = ('Added leads are on the list now. Guessed addresses are not: ask the owner before '
                          'adding any with set_prospect_email.')
        return result

    def tool_set_prospect_email(self, prospect_id, email):
        contact_id = prospects.set_email(self.db, prospect_id, email)
        return {'prospect_id': prospect_id, 'email': contacts.normalize_email(email), 'added_to_list': True,
                'contact_id': contact_id}

    def tool_create_list(self, name, description=''):
        name = ' '.join(str(name or '').split())[:120]
        if not name:
            raise service.Problem('Give the list a name.')
        list_id = self.db.run('INSERT INTO lists (name, description, created_at) VALUES (?, ?, ?)',
                              name, str(description or '')[:500], now()).lastrowid
        return {'list_id': list_id, 'name': name}

    def tool_add_leads(self, list_id, leads):
        db = self.db
        if not db.one('SELECT id FROM lists WHERE id = ?', list_id):
            raise service.Problem(f'List {list_id} does not exist')
        if not isinstance(leads, list) or not leads:
            raise service.Problem('Give at least one lead.')
        rows, no_mail = [], []
        for lead in leads[:300]:
            if not isinstance(lead, dict):
                continue
            address = contacts.normalize_email(lead.get('email'))
            if address and leadtools.check_domain(db, address)['mail'] is False:
                no_mail.append(address)
                address = None
            extra = {re.sub(r'[^a-z0-9]+', '_', str(k).lower()).strip('_')[:40]: str(v)[:1000]
                     for k, v in lead.items() if k not in ('email', 'first_name', 'last_name') and v not in (None, '')}
            rows.append({'email': address or '', 'first_name': str(lead.get('first_name') or '')[:100],
                         'last_name': str(lead.get('last_name') or '')[:100], 'fields': extra})
        summary = prospects.import_upload(db, list_id, rows)
        return {'list_id': list_id, 'added': summary['added'] + summary['existing'],
                'already_in_list': summary['already_in_list'], 'need_an_email': summary['needs_email'],
                'skipped_do_not_email': summary['suppressed'], 'skipped_invalid': summary['invalid'],
                'domains_that_receive_no_mail': no_mail or None}

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

    # -- offers --------------------------------------------------------------
    def tool_get_offers(self):
        rows = offers.everything(self.db)
        if not rows:
            return {'offers': [], 'note': 'No offer saved yet.'}
        return {'offers': [dict(offers.as_dict(r), still_empty=offers.completeness(r) or None) for r in rows]}

    def tool_save_offer(self, offer_id=None, make_default=None, **fields):
        offer_id = offers.save(self.db, fields, offer_id=offer_id, make_default=make_default)
        row = offers.get(self.db, offer_id)
        return {'saved': True, **offers.as_dict(row), 'still_empty': offers.completeness(row) or None}

    # -- the web ---------------------------------------------------------------
    def tool_web_search(self, query):
        if self.search is None:
            raise service.Problem('Web search is off. It can be turned on in Settings → AI.')
        result = self.search(query)
        self.db.track_ai('agent', result.pop('usage', None))
        return result


# -- conversation storage -------------------------------------------------------
def new_thread(db, kind='outreach'):
    ts = now()
    return db.run('INSERT INTO agent_threads (title, kind, created_at, updated_at) VALUES (?, ?, ?, ?)',
                  'New conversation', kind if kind in PERSONAS else 'outreach', ts, ts).lastrowid


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


def add_user_message(db, thread_id, text, shown=None, attachment=None):
    """Stores what the owner sent. `text` is what the model reads; `shown` what the chat shows."""
    messages = history(db, thread_id)
    if len(messages) >= MAX_THREAD_MESSAGES:
        raise service.Problem('This conversation has become very long. Start a new one to continue.')
    _repair(db, thread_id, messages)
    content = {'text': text}
    if shown is not None:
        content['shown'] = shown
    if attachment:
        content['attachment'] = attachment
    saved = _save(db, thread_id, 'user', content)
    if not any(m['role'] == 'user' for m in messages):
        visible = shown if shown is not None else text
        title = ' '.join(visible.split())[:60] or (f'Leads: {attachment["name"]}' if attachment else 'New conversation')
        db.run('UPDATE agent_threads SET title = ? WHERE id = ?', title, thread_id)
    return saved


def _with_retries(call):
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return call()
        except ProviderError as exc:
            if not exc.retry or attempt == len(RETRY_DELAYS):
                raise
            log.warning('AI call failed (%s); trying again in %ss', exc, RETRY_DELAYS[attempt])
            time.sleep(RETRY_DELAYS[attempt])


def run(db, cfg, thread_id, complete=None, on_step=None, should_stop=None):
    """Runs the agent on the conversation until it answers. Returns the new messages.

    `complete` can replace the provider call (tests). on_step(text) hears what it is doing;
    should_stop() is asked between steps."""
    thread = db.one('SELECT kind FROM agent_threads WHERE id = ?', thread_id)
    persona = PERSONAS.get(thread['kind'] if thread else 'outreach', PERSONAS['outreach'])
    system, tools = persona['system'], list(persona['tools'])
    settings = db.settings()
    provider_name = settings.get('ai_provider') or ai_openrouter.NAME
    search = None
    if complete is None:
        try:
            provider, api_key, model = ai.resolve(db, settings, 'agent')
        except ai.NotConfigured as exc:
            raise service.Problem(str(exc))
        web = settings.get('ai_web_search') == '1'
        native = web and getattr(provider, 'NATIVE_WEB_SEARCH', False)
        if web and not native and hasattr(provider, 'search'):
            tools.append(WEB_SEARCH_TOOL)

            def search(query):
                return provider.search(api_key, model, query)

        def complete(messages):
            return provider.complete(api_key, model, system, messages, tools, web_search=native)

    def step(text):
        if on_step is not None:
            on_step(text)

    messages = history(db, thread_id)
    _repair(db, thread_id, messages)
    start = len(messages)
    toolbox = Toolbox(db, cfg, search=search)
    for _ in range(MAX_STEPS):
        if should_stop is not None and should_stop():
            messages.append(_save(db, thread_id, 'assistant', {
                'text': '', 'tool_calls': [], 'provider': provider_name, 'raw': None, 'note': 'Stopped.'}))
            break
        step('Thinking')
        result = _with_retries(lambda: complete(messages))
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
            step(ACTIVITY.get(call['name'], 'Working'))
            output, is_error = toolbox.run(call['name'], call['input'])
            results.append({'id': call['id'], 'name': call['name'], 'output': output, 'is_error': is_error})
        messages.append(_save(db, thread_id, 'tool', {'results': results}))
    else:
        messages.append(_save(db, thread_id, 'assistant', {
            'text': '', 'tool_calls': [], 'provider': provider_name, 'raw': None,
            'note': f'Stopped after {MAX_STEPS} steps. Say "continue" to carry on.'}))
    return messages[start:]


def reply(db, cfg, thread_id, user_text, complete=None):
    """Adds the owner's message and runs the agent in this thread. Returns the new messages, the
    owner's message first. (The chat page runs turns in the background; see agent_runs.)"""
    first = add_user_message(db, thread_id, user_text)
    return [first] + run(db, cfg, thread_id, complete=complete)


__all__ = ['ACTIVITY', 'OFFER_SYSTEM', 'OFFER_TOOLS', 'PERSONAS', 'PROVIDERS', 'ProviderError', 'SYSTEM', 'TOOLS',
           'Toolbox', 'add_user_message', 'configured', 'history', 'new_thread', 'reply', 'run']
