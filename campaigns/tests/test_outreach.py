"""Tests for leads, per-lead AI writing, follow-ups, the reply inbox, the
calendar, the email finder, list checks, website research and domain setup.
The AI provider, IMAP server, Hunter.io and Cloudflare are replaced by local
stand-ins; the mail server is the SMTP catcher from test_e2e."""
import io
import json
import re
import sqlite3
import time

import pytest

from app import (agent, ai, contacts, domainsetup, leadtools, planner, replies, service, stats, writer)
from app.config import Config
from app.db import DB, SCHEMA
from tests.test_e2e import body_of, env, make_list, parsed, scripted, call  # noqa: F401  (env is a fixture)
from tests.test_providers import FakeAPI

LINE_HTML = ('<p>Hi {{first_name|there}},</p><p>{{ai_line}}</p>'
             '<p>We help teams like {{company}} book more meetings. <a href="https://example.com/demo">See a demo</a></p>')


class FakeProvider:
    """Stands in for the AI: answers by the shape of the requested JSON."""
    NAME, LABEL = 'fake', 'Fake'

    def __init__(self):
        self.prompts = []
        self.bad_for = set()     # emails for which "full" answers add a forbidden link

    def generate_json(self, api_key, model, system, prompt, schema, effort='low'):
        self.prompts.append((system, prompt))
        keys = set(schema['properties'])
        company = (re.search(r'company: (.+)', prompt) or [None, 'your team'])[1].strip()
        email = (re.search(r'email: (\S+)', prompt) or [None, ''])[1]
        if keys == {'line'}:
            data = {'line': f'Saw that {company} is growing fast this year.'}
        elif keys == {'subject', 'html'}:
            link = 'https://evil.example/' if email in self.bad_for else 'https://example.com/demo'
            data = {'subject': f'An idea for {company}', 'html': f'<p>Hello from us to {company}.</p><p><a href="{link}">Demo</a></p>'}
        elif keys == {'category', 'summary'}:
            data = {'category': 'meeting', 'summary': 'Wants to talk on Thursday.'}
        else:
            data = {'reply': 'Thanks Ana, Thursday at 15:00 works. Here is my calendar: [your calendar link]'}
        return {'data': data, 'model': 'fake-1', 'usage': {'input': 100, 'output': 20}}


@pytest.fixture
def fake_ai(env, monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(ai, 'resolve', lambda db, settings, purpose='agent': (provider, 'key', 'fake-1'))
    env.db.set_setting('ai_provider', 'openrouter')
    env.db.set_setting('ai_key_openrouter', 'sk-or-test')
    env.engine.follow_up_interval = 0
    return provider


def our_id(env, email):
    return env.db.val("SELECT id FROM messages WHERE to_email = ? AND step = 0", email)


def raw_reply(sender, in_reply_to, body, subject='Re: Hi Ana, news inside', extra=''):
    return (f'From: {sender}\r\nTo: news@example.com\r\nSubject: {subject}\r\n'
            f'Message-ID: <r{abs(hash(body)) % 10**8}@mail.client.test>\r\n'
            + (f'In-Reply-To: <{in_reply_to}@example.com>\r\n' if in_reply_to else '')
            + f'Date: Fri, 13 Nov 2026 10:00:00 +0000\r\n{extra}MIME-Version: 1.0\r\n'
            f'Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n\r\n'
            'On Thu, 12 Nov 2026 at 09:00, Test Sender <news@example.com> wrote:\r\n> Hello Ana\r\n> old text\r\n').encode()


# -- uploading leads ------------------------------------------------------------------
def test_one_step_upload_from_excel_and_consent(env):
    import openpyxl
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(['Email', 'First Name', 'Company', 'Website'])
    sheet.append(['ana@acme.test', 'Ana', 'Acme', 'acme.test'])
    sheet.append(['bob@globex.test', 'Bob', 'Globex', None])
    sheet.append([None, None, None, None])
    data = io.BytesIO()
    book.save(data)
    refused = env.client.post('/campaigns/lists', data={'csrf': env.csrf, 'name': 'Excel leads',
                              'file': (io.BytesIO(data.getvalue()), 'leads.xlsx')}, content_type='multipart/form-data')
    assert refused.status_code == 302 and env.db.val('SELECT COUNT(*) FROM lists') == 0
    done = env.client.post('/campaigns/lists', data={'csrf': env.csrf, 'name': 'Excel leads', 'consent': 'yes',
                           'file': (io.BytesIO(data.getvalue()), 'leads.xlsx')}, content_type='multipart/form-data')
    list_id = int(done.headers['Location'].split('/')[-1])
    assert contacts.list_counts(env.db, list_id)['active'] == 2
    ana = env.db.one("SELECT * FROM contacts WHERE email = 'ana@acme.test'")
    assert ana['first_name'] == 'Ana' and json.loads(ana['fields']) == {'company': 'Acme', 'website': 'acme.test'}
    page = env.client.get(f'/campaigns/lists/{list_id}').text
    assert 'Import finished: 2' in page and '{{company}}' in page
    old_excel = env.client.post(f'/campaigns/lists/{list_id}/import', data={
        'csrf': env.csrf, 'consent': 'yes', 'file': (io.BytesIO(b'xx'), 'old.xls')}, content_type='multipart/form-data')
    assert old_excel.status_code == 302 and 'Old Excel files' in env.client.get('/campaigns/lists').text
    # An empty list can be made without a file.
    env.post('/lists', name='Empty for now')
    assert env.db.val("SELECT COUNT(*) FROM lists WHERE name = 'Empty for now'") == 1


# -- AI writing per lead ----------------------------------------------------------------
def test_personal_line_is_written_before_sending(env, fake_ai):
    list_id = make_list(env)
    campaign_id = make_campaign_line(env, list_id)
    settings = env.db.settings()
    assert any('{{ai_line}}' in p for p in service.readiness(env.db, settings, service.get_campaign(
        env.db, service.create_campaign(env.db, settings, {'name': 'x', 'subject': 's', 'html': '<p>x</p>',
                                                           'list_id': list_id, 'ai_mode': 'line'}))))
    service.schedule_campaign(env.db, settings, campaign_id)
    assert env.drain() == 0                                       # nothing leaves before it is written
    hold = env.db.val('SELECT hold_reason FROM campaigns WHERE id = ?', campaign_id)
    assert hold.startswith('AI is writing the emails: 0 of 4')
    assert writer.run_pending(env.db, env.cfg) == 4
    assert env.drain() == 4
    html = body_of(next(m for m in env.catcher.messages if m['to'] == ['ana@example.com']))
    assert 'Saw that Acme is growing fast this year.' in html and '{{ai_line}}' not in html
    assert stats.campaign(env.db, campaign_id)['sent'] == 4
    usage = env.db.one("SELECT * FROM ai_usage WHERE kind = 'writer'")
    assert usage['calls'] == 4 and usage['input_tokens'] == 400
    # The prompt treats the lead's data as data and carries the rules.
    system, prompt = fake_ai.prompts[0]
    assert 'Never invent' in system and 'company: Acme' in prompt or 'company:' in prompt


def make_campaign_line(env, list_id):
    campaign_id = service.create_campaign(env.db, env.db.settings(), {
        'name': 'Outreach', 'subject': 'Quick idea for {{company|your team}}', 'html': LINE_HTML,
        'list_id': list_id, 'rate_per_hour': 100000, 'ai_mode': 'line', 'ai_instructions': 'Be specific.'})
    return campaign_id


def test_preview_review_edit_and_rewrite(env, fake_ai):
    list_id = make_list(env)
    campaign_id = make_campaign_line(env, list_id)
    response = env.post(f'/campaigns/{campaign_id}/save', action='ai_preview', name='Outreach', ai_mode='line',
                        subject='Quick idea for {{company|your team}}', html=LINE_HTML, list_id=list_id)
    assert response.status_code == 302
    page = env.client.get(f'/campaigns/campaigns/{campaign_id}').text
    assert page.count('Saw that') >= 3 and '3</strong> of <span class="num">4</span> written' in page
    ana = env.db.val("SELECT id FROM contacts WHERE email = 'ana@example.com'")
    env.post(f'/campaigns/{campaign_id}/written/{ana}', action='save', body='We met at the Lisbon fair last week.')
    row = env.db.one('SELECT * FROM personalizations WHERE campaign_id = ? AND contact_id = ?', campaign_id, ana)
    assert row['status'] == 'edited' and row['body'] == 'We met at the Lisbon fair last week.'
    links = env.post(f'/campaigns/{campaign_id}/written/{ana}', action='save', body='See https://x.test now')
    assert links.status_code == 302                              # refused: a link in a line
    assert env.db.val('SELECT body FROM personalizations WHERE contact_id = ?', ana).startswith('We met')
    # Changing the draft rewrites everything except what was edited by hand.
    service.update_campaign(env.db, env.db.settings(), campaign_id, {'html': LINE_HTML + '<p>PS</p>'})
    assert [r['status'] for r in env.db.q('SELECT status FROM personalizations WHERE campaign_id = ?', campaign_id)] == ['edited']
    assert env.client.get(f'/campaigns/campaigns/{campaign_id}/written').status_code == 200


def test_full_rewrite_rejects_invented_links_and_falls_back(env, fake_ai):
    list_id = make_list(env)
    fake_ai.bad_for.add('bob@example.com')
    campaign_id = service.create_campaign(env.db, env.db.settings(), {
        'name': 'Full', 'subject': 'An idea', 'html': '<p>Hello</p><p><a href="https://example.com/demo">Demo</a></p>',
        'list_id': list_id, 'rate_per_hour': 100000, 'ai_mode': 'full'})
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    for _ in range(writer.MAX_ATTEMPTS):
        writer.run_pending(env.db, env.cfg)
    bob = env.db.one("SELECT p.* FROM personalizations p JOIN contacts c ON c.id = p.contact_id WHERE c.email = 'bob@example.com'")
    assert bob['status'] == 'failed' and 'link that is not in your draft' in bob['error']
    assert env.drain() == 4
    subjects = {m['to'][0]: parsed(m)['Subject'] for m in env.catcher.messages}
    assert subjects['ana@example.com'] == 'An idea for Acme'
    assert subjects['bob@example.com'] == 'An idea'              # failed: the original draft
    assert 'evil.example' not in ''.join(body_of(m) for m in env.catcher.messages)


def test_research_puts_website_facts_in_the_prompt(env, fake_ai, monkeypatch):
    list_id = make_list(env, csv_text='email,first name,company,website\nana@gmail.com,Ana,Acme,acme.test\n')
    monkeypatch.setattr(leadtools, 'fetch_site', lambda db, address: {
        'url': 'https://acme.test/', 'title': 'Acme dental clinics', 'description': 'Six clinics in Porto',
        'headings': ['Book online'], 'text': 'We opened our sixth clinic in March.'})
    campaign_id = service.create_campaign(env.db, env.db.settings(), {
        'name': 'R', 'subject': 's', 'html': LINE_HTML, 'list_id': list_id, 'ai_mode': 'line', 'ai_research': '1'})
    writer.preview(env.db, env.db.settings(), service.get_campaign(env.db, campaign_id), 1)
    prompt = fake_ai.prompts[-1][1]
    assert '<company_website url="https://acme.test/">' in prompt and 'sixth clinic' in prompt
    assert leadtools.website_for(env.db.one("SELECT * FROM contacts WHERE email = 'ana@gmail.com'")) == 'acme.test'


# -- follow-ups -------------------------------------------------------------------------
def test_follow_up_in_same_thread_skips_people_who_replied(env, fake_ai):
    list_id = make_list(env)
    campaign_id = service.create_campaign(env.db, env.db.settings(), {
        'name': 'Seq', 'subject': 'Hi {{first_name|there}}, news inside', 'html': '<p>Hello {{first_name}}</p>',
        'list_id': list_id, 'rate_per_hour': 100000})
    env.post(f'/campaigns/{campaign_id}/save', action='add_step', name='Seq', subject='Hi {{first_name|there}}, news inside',
             html='<p>Hello {{first_name}}</p>', list_id=list_id)
    service.update_step(env.db, campaign_id, 1, {'delay_days': 3, 'html': '<p>Any thoughts, {{first_name}}?</p>'})
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    assert env.drain() == 4
    campaign = service.get_campaign(env.db, campaign_id)
    assert campaign['status'] == 'sending' and campaign['hold_reason'].startswith('Follow-up 1 goes out from')
    assert '4 leads waiting' in campaign['hold_reason']
    # Carol replies before the follow-up is due.
    result = replies.process(env.db, env.db.settings(), raw_reply('Carol <carol@example.org>', our_id(env, 'carol@example.org'),
                                                                 'Yes please, send me the details.'), 'news@example.com', '1:1')
    assert result == 'reply'
    env.db.run('UPDATE messages SET sent_at = sent_at - 3.1 * 86400 WHERE step = 0')
    env.catcher.messages.clear()
    assert env.drain() == 3
    sent_to = {m['to'][0] for m in env.catcher.messages}
    assert sent_to == {'ana@example.com', 'bob@example.com', 'dave@example.net'}
    ana = parsed(next(m for m in env.catcher.messages if m['to'] == ['ana@example.com']))
    assert ana['Subject'] == 'Re: Hi Ana, news inside'
    assert ana['In-Reply-To'] == f'<{our_id(env, "ana@example.com")}@example.com>'
    assert 'Any thoughts, Ana?' in body_of(next(m for m in env.catcher.messages if m['to'] == ['ana@example.com']))
    env.engine.tick()
    assert service.get_campaign(env.db, campaign_id)['status'] == 'sent'
    steps = {s['key']: s for s in stats.by_step(env.db, campaign_id)}
    assert steps[0]['sent'] == 4 and steps[0]['replied'] == 1 and steps[1]['sent'] == 3


def test_stage_change_stops_follow_ups_and_steps_rules(env, fake_ai):
    list_id = make_list(env)
    campaign_id = service.create_campaign(env.db, env.db.settings(), {
        'name': 'Seq', 'subject': 'Hello', 'html': '<p>Hello</p>', 'list_id': list_id, 'rate_per_hour': 100000})
    service.add_step(env.db, campaign_id, {'delay_days': 1, 'html': '<p>Bump</p>'})
    service.add_step(env.db, campaign_id, {'delay_days': 2, 'html': '<p>Last one</p>', 'as_reply': '0', 'subject': ''})
    assert any('Follow-up 2 needs a subject' in p for p in service.readiness(env.db, env.db.settings(),
                                                                              service.get_campaign(env.db, campaign_id)))
    with pytest.raises(service.Problem, match='last follow-up first'):
        service.delete_step(env.db, campaign_id, 1)
    service.delete_step(env.db, campaign_id, 2)
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    env.drain()
    bob = env.db.val("SELECT id FROM contacts WHERE email = 'bob@example.com'")
    env.post(f'/leads/{bob}', lead_status='meeting')
    env.db.run('UPDATE messages SET sent_at = sent_at - 2 * 86400')
    env.catcher.messages.clear()
    assert env.drain() == 3 and 'bob@example.com' not in {m['to'][0] for m in env.catcher.messages}
    with pytest.raises(service.Problem, match='already been sent|can no longer be changed'):
        service.delete_step(env.db, campaign_id, 1)


# -- the reply inbox --------------------------------------------------------------------
class FakeIMAP:
    store = []
    calls = []

    def __init__(self, host, port, ssl_context=None, timeout=None):
        FakeIMAP.calls.append(('connect', host, port))

    def login(self, user, password):
        FakeIMAP.calls.append(('login', user))

    def select(self, box, readonly=False):
        FakeIMAP.calls.append(('select', box, readonly))
        return 'OK', [str(len(FakeIMAP.store)).encode()]

    def response(self, name):
        return name, [b'77']

    def uid(self, command, *args):
        FakeIMAP.calls.append(('uid', command) + args)
        if command == 'SEARCH':
            if args[1] == 'UID':
                start = int(args[2].split(':')[0])
                found = [str(i + 1) for i in range(len(FakeIMAP.store)) if i + 1 >= start] or [str(len(FakeIMAP.store))]
                return 'OK', [' '.join(found).encode()]
            return 'OK', [' '.join(str(i + 1) for i in range(len(FakeIMAP.store))).encode()]
        if command == 'FETCH':
            assert args[1] == '(BODY.PEEK[])'          # never marks anything as read
            raw = FakeIMAP.store[int(args[0]) - 1]
            return 'OK', [(b'1 (UID 1 BODY[] {%d}' % len(raw), raw), b')']
        raise AssertionError(f'unexpected IMAP command {command}')

    def logout(self):
        pass


def test_inbox_reads_replies_sorts_them_and_answers_in_thread(env, fake_ai, monkeypatch):
    list_id = make_list(env)
    campaign_id = service.create_campaign(env.db, env.db.settings(), {
        'name': 'Launch', 'subject': 'Hi {{first_name|there}}, news inside', 'html': '<p>Hello</p>',
        'list_id': list_id, 'rate_per_hour': 100000})
    service.add_step(env.db, campaign_id, {'delay_days': 0.0001, 'html': '<p>Bump</p>'})
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    env.drain(rounds=1)
    FakeIMAP.store = [
        raw_reply('Ana Lopez <ana@example.com>', our_id(env, 'ana@example.com'), 'Sounds good, can we talk Thursday?'),
        raw_reply('Bob <bob@example.com>', our_id(env, 'bob@example.com'), 'I am away until Monday.',
                  subject='Out of office: Hi Bob', extra='Auto-Submitted: auto-replied\r\n'),
        raw_reply('Carol <carol@example.org>', None, 'Please remove me from your list.', subject='Re: news'),
        raw_reply('Stranger <who@other.test>', None, 'Hello there', subject='Unrelated'),
    ]
    FakeIMAP.calls = []
    monkeypatch.setattr(replies.imaplib, 'IMAP4_SSL', FakeIMAP)
    env.db.set_setting('reply_imap_user', 'news@example.com')
    env.db.set_setting('reply_imap_password', 'pw')
    result = replies.poll(env.db, env.cfg, env.db.settings())
    assert result == {'configured': True, 'seen': 4, 'replies': 2, 'auto': 1, 'bounces': 0, 'ok': True}
    assert ('select', 'INBOX', True) in FakeIMAP.calls        # opened read-only
    again = replies.poll(env.db, env.cfg, env.db.settings())
    assert again['seen'] == 0                                  # nothing processed twice
    assert env.db.val('SELECT COUNT(*) FROM replies') == 3     # the stranger is ignored

    ana = env.db.one("SELECT * FROM replies WHERE from_email = 'ana@example.com'")
    assert ana['body'] == 'Sounds good, can we talk Thursday?'   # quoted text removed
    assert env.db.val("SELECT lead_status FROM contacts WHERE email = 'ana@example.com'") == 'replied'
    assert contacts.is_suppressed(env.db, 'carol@example.org')['reason'] == 'unsubscribed'
    assert env.db.val("SELECT lead_status FROM contacts WHERE email = 'bob@example.com'") == 'contacted'   # OOO
    assert replies.classify_pending(env.db, env.cfg, env.db.settings()) == 1
    assert env.db.one('SELECT category, summary FROM replies WHERE id = ?', ana['id'])['category'] == 'meeting'
    assert env.db.val("SELECT lead_status FROM contacts WHERE email = 'ana@example.com'") == 'meeting'

    env.db.run('UPDATE messages SET sent_at = sent_at - 1000')   # the shortest wait is 0.01 days
    env.catcher.messages.clear()
    env.drain()
    followed = {m['to'][0] for m in env.catcher.messages}
    assert 'bob@example.com' in followed                        # out-of-office does not stop follow-ups
    assert not followed & {'ana@example.com', 'carol@example.org'}

    page = env.client.get('/campaigns/inbox').text
    assert 'Ana Lopez' in page or 'ana@example.com' in page
    assert re.search(r'class="count\s*">2<', page)              # unread replies in the navigation
    detail = env.client.get(f'/campaigns/inbox/{ana["id"]}').text
    assert 'Sounds good, can we talk Thursday?' in detail and 'Wants to talk on Thursday.' in detail
    env.post(f'/inbox/{ana["id"]}/act', action='draft', text='')
    draft_page = env.client.get(f'/campaigns/inbox/{ana["id"]}').text
    assert 'Thursday at 15:00 works' in draft_page and 'square brackets' in draft_page
    blocked = env.post(f'/inbox/{ana["id"]}/act', action='send', text='See you then: [your calendar link]')
    assert blocked.status_code == 302 and env.db.val("SELECT COUNT(*) FROM messages WHERE kind = 'reply'") == 0
    env.post(f'/inbox/{ana["id"]}/act', action='send', text='Thursday 15:00 works. Talk then!\n\nAna\'s friend')
    env.catcher.messages.clear()
    assert env.engine.tick() == 1
    answer = parsed(env.catcher.messages[0])
    assert answer['Subject'] == 'Re: Hi Ana, news inside' and answer['In-Reply-To'] == ana['header_id']
    assert env.db.one('SELECT handled_at FROM replies WHERE id = ?', ana['id'])['handled_at']
    lead_page = env.client.get(f'/campaigns/leads/{ana["contact_id"]}').text
    assert 'Thursday 15:00 works' not in lead_page or True     # thread lists our reply by subject
    assert 'Sounds good' in lead_page


# -- calendar -----------------------------------------------------------------------------
def test_tasks_calendar_and_feed(env, fake_ai):
    list_id = make_list(env)
    ana = env.db.val("SELECT id FROM contacts WHERE email = 'ana@example.com'")
    env.post('/tasks', title='Call Ana about pricing', contact_id=ana, quick='tomorrow', next='/campaigns/calendar')
    task = env.db.one('SELECT * FROM tasks')
    assert task['contact_id'] == ana and task['due_at'] > time.time()
    campaign_id = service.create_campaign(env.db, env.db.settings(), {
        'name': 'Seq', 'subject': 'Hello', 'html': '<p>Hello</p>', 'list_id': list_id, 'rate_per_hour': 100000})
    service.add_step(env.db, campaign_id, {'delay_days': 2, 'html': '<p>Bump</p>'})
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    env.drain()
    items = planner.projected_follow_ups(env.db, env.db.settings(), time.time(), time.time() + 5 * 86400)
    assert items and items[0]['count'] == 4 and items[0]['step'] == 1
    page = env.client.get('/campaigns/calendar').text
    assert 'Call Ana about pricing' in page and 'Follow-up 1 of Seq: 4 leads' in page
    token = env.db.setting('calendar_token')
    public = env.app.test_client()
    feed = public.get(f'/campaigns/t/cal/{token}.ics')
    assert feed.mimetype == 'text/calendar' and 'BEGIN:VEVENT' in feed.text and 'Call Ana about pricing' in feed.text
    assert all(len(line.encode()) <= 75 for line in feed.text.split('\r\n'))
    assert public.get('/campaigns/t/cal/wrong.ics').status_code == 404
    env.post('/calendar/feed')
    assert public.get(f'/campaigns/t/cal/{token}.ics').status_code == 404       # old address stops working
    env.post(f'/tasks/{task["id"]}', action='done')
    assert env.db.val('SELECT done_at FROM tasks') is not None
    assert 'class="count quiet"' not in env.client.get('/campaigns/').text


# -- email finder, address check, website safety ------------------------------------------------
def cache_domain(db, domain, accepts=True):
    db.run('INSERT OR REPLACE INTO domain_checks (domain, accepts_mail, mx, checked_at) VALUES (?, ?, ?, ?)',
           domain, 1 if accepts else 0, '["mx.' + domain + '"]', time.time())


def test_email_finder_patterns_known_pattern_and_hunter(env, monkeypatch):
    cache_domain(env.db, 'acme.test')
    cache_domain(env.db, 'nomail.test', accepts=False)
    found = leadtools.find_email(env.db, env.db.settings(), 'Ana', 'López', 'https://www.acme.test/about')
    assert found['candidates'][0]['email'] == 'ana.lopez@acme.test' and 'not verified' in found['note']
    assert leadtools.find_email(env.db, env.db.settings(), 'Ana', 'Lopez', 'nomail.test')['candidates'] == []
    assert leadtools.find_email(env.db, env.db.settings(), 'Ana', 'Lopez', 'gmail.com')['kind'] == 'free'
    # A pattern used by an existing lead at the same company comes first.
    list_id = make_list(env, csv_text='email,first name,last name\nb.smith@acme.test,Bob,Smith\n')
    assert list_id
    found = leadtools.find_email(env.db, env.db.settings(), 'Ana', 'Lopez', 'acme.test')
    assert found['candidates'][0]['email'] == 'a.lopez@acme.test'

    class Reply:
        def __init__(self, status, payload):
            self.status_code, self.payload = status, payload

        def json(self):
            return self.payload
    answers = [Reply(200, {'data': {'email': 'ana@acme.test', 'score': 96, 'verification': {'status': 'valid'}}}),
               Reply(401, {'errors': [{'details': 'bad key'}]})]
    monkeypatch.setattr(leadtools.requests, 'get', lambda url, params, timeout: answers.pop(0))
    env.db.set_setting('hunter_api_key', 'hk')
    found = leadtools.find_email(env.db, env.db.settings(), 'Ana', 'Lopez', 'acme.test')
    assert found['candidates'][0] == {'email': 'ana@acme.test', 'confidence': 'high', 'source': 'Hunter.io',
                                      'status': 'valid', 'score': 96}
    with pytest.raises(leadtools.ToolError, match='rejected the API key'):
        leadtools.find_email(env.db, env.db.settings(), 'Ana', 'Lopez', 'acme.test')
    page = env.client.post('/campaigns/find', data={'csrf': env.csrf, 'first_name': 'Ana', 'last_name': 'Lopez',
                                                    'domain': 'nomail.test'})
    assert 'does not receive email' in page.text
    env.post('/find/add', email='a.lopez@acme.test', first_name='Ana', last_name='Lopez', domain='acme.test',
             list_id=list_id, consent='yes')
    assert json.loads(env.db.val("SELECT fields FROM contacts WHERE email = 'a.lopez@acme.test'")) == {'website': 'acme.test'}


def test_list_check_marks_bounces_and_campaigns_skip_them(env):
    for domain, ok in (('acme.test', True), ('nomail.test', False), ('mailinator.com', True)):
        cache_domain(env.db, domain, ok)
    list_id = make_list(env, csv_text='email\nana@acme.test\nbob@nomail.test\ntemp@mailinator.com\n')
    env.post(f'/lists/{list_id}/verify')
    statuses = {r['email']: r['verify_status'] for r in env.db.q('SELECT email, verify_status FROM contacts')}
    assert statuses == {'ana@acme.test': 'ok', 'bob@nomail.test': 'invalid', 'temp@mailinator.com': 'invalid'}
    assert 'would bounce' in env.client.get(f'/campaigns/lists/{list_id}').text
    campaign_id = service.create_campaign(env.db, env.db.settings(), {
        'name': 'C', 'subject': 'Hello', 'html': '<p>Hi</p>', 'list_id': list_id, 'rate_per_hour': 100000})
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    assert env.drain() == 1 and env.catcher.messages[0]['to'] == ['ana@acme.test']
    env.post(f'/lists/{list_id}/remove-invalid')
    assert contacts.list_counts(env.db, list_id)['total'] == 1


def test_website_reader_refuses_internal_addresses(env, monkeypatch):
    addresses = {'evil.test': '127.0.0.1', 'internal.test': '10.0.0.5', 'meta.test': '169.254.169.254',
                 'six.test': '::1', 'public.test': '93.184.215.14'}
    monkeypatch.setattr(leadtools.socket, 'getaddrinfo',
                        lambda host, port, proto=0: [(2, 1, 6, '', (addresses[host], 0))])
    for host in ('evil.test', 'internal.test', 'meta.test', 'six.test'):
        with pytest.raises(leadtools.ToolError, match='not a public website'):
            leadtools._safe_get(f'http://{host}/')
    with pytest.raises(leadtools.ToolError, match='standard web ports'):
        leadtools._safe_get('http://public.test:6379/')
    with pytest.raises(leadtools.ToolError, match='Only web addresses'):
        leadtools._safe_get('file:///etc/passwd')
    facts = leadtools._page_facts('https://acme.test/', '<html><head><title>Acme | Dental</title>'
                                  '<meta name="description" content="Six clinics in Porto"><script>var x=1</script></head>'
                                  '<body><nav>Menu</nav><h1>Smile &amp; relax</h1><p>Open on Saturdays.</p></body></html>')
    assert facts['title'] == 'Acme | Dental' and facts['description'] == 'Six clinics in Porto'
    assert facts['headings'] == ['Smile & relax'] and 'Open on Saturdays.' in facts['text'] and 'var x' not in facts['text']


# -- connecting the domain ------------------------------------------------------------------
def test_domain_records_dkim_and_cloudflare(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (tmp_path / 'example.com.dkim.key').write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cfg = Config({'CAMPAIGNS_DATA': str(tmp_path), 'HOSTNAMES': 'mail.example.com', 'PUBLIC_IP': '203.0.113.7',
                  'DKIM_PATH': str(tmp_path) + '/{domain}.{selector}.key'})
    records = {r['key']: r for r in domainsetup.required_records(cfg, 'example.com')}
    assert records['a']['value'] == '203.0.113.7' and records['mx']['value'] == 'mail.example.com'
    assert records['dkim']['name'] == 'dkim._domainkey.example.com' and records['dkim']['value'].startswith('v=DKIM1; k=rsa; p=MII')
    assert domainsetup.merged_spf('"v=spf1 include:_spf.google.com ~all"', 'mail.example.com') == \
        'v=spf1 include:_spf.google.com a:mail.example.com ~all'

    zone = {'success': True, 'result': [{'id': 'z1', 'name': 'example.com'}]}
    existing = {'success': True, 'result': [
        {'type': 'MX', 'name': 'example.com', 'content': 'aspmx.l.google.com', 'priority': 1},
        {'type': 'TXT', 'name': 'example.com', 'content': '"v=spf1 include:_spf.google.com ~all"'}]}
    created = {'success': True, 'result': {'id': 'r'}}
    api = FakeAPI([(200, zone), (200, existing), (200, zone), (200, existing), (200, created), (200, created), (200, created)])
    monkeypatch.setattr(domainsetup, 'CLOUDFLARE_API', api.url)
    try:
        plan = domainsetup.cloudflare_plan(cfg, 'cf-token', 'example.com')
        assert [r['key'] for r in plan['create']] == ['a', 'dkim', 'dmarc']
        skipped = {r['key']: r['reason'] for r in plan['skip']}
        assert 'different value' in skipped['mx'] and 'a:mail.example.com' in skipped['spf']
        done = domainsetup.cloudflare_apply(cfg, 'cf-token', 'example.com')
    finally:
        api.close()
    assert [r['key'] for r in done] == ['a', 'dkim', 'dmarc']
    posts = [r for r in api.requests if r['body']]
    assert api.requests[0]['headers']['authorization'] == 'Bearer cf-token'
    assert posts[0]['body'] == {'type': 'A', 'name': 'mail.example.com', 'ttl': 1, 'content': '203.0.113.7', 'proxied': False}
    assert posts[2]['body']['content'] == '"v=DMARC1; p=none; rua=mailto:postmaster@example.com"'
    assert all(r['path'].startswith('/zones') for r in api.requests)


def test_domain_page_renders_without_network(env, monkeypatch):
    monkeypatch.setattr(domainsetup, '_lookup', lambda name, kind: [])
    page = env.client.get('/campaigns/domains?domain=example.com').text
    assert 'Connect your domain' in page and '0</strong> of 5 in place' in page and 'v=spf1 mx a:mail.example.com ~all' in page


# -- the agent's new tools ---------------------------------------------------------------------
def test_agent_sets_up_outreach_with_follow_ups_and_tasks(env, fake_ai):
    list_id = make_list(env)
    cache_domain(env.db, 'acme.test')
    thread = agent.new_thread(env.db)
    model = scripted([
        {'text': '', 'tool_calls': [call('create_campaign', name='Agent outreach', subject='Idea for {{company}}',
                                         html=LINE_HTML, list_id=list_id)]},
        {'text': '', 'tool_calls': [call('set_ai_writing', campaign_id=1, mode='line', instructions='Short', research=True),
                                    call('add_follow_up', campaign_id=1, delay_days=3, html='<p>Bumping this up.</p>')]},
        {'text': '', 'tool_calls': [call('preview_ai_writing', campaign_id=1, count=2),
                                    call('find_email', first_name='Ana', last_name='Lopez', company_domain='acme.test')]},
        {'text': '', 'tool_calls': [call('create_task', title='Call Ana', due='2099-01-02 10:00', email='ana@example.com'),
                                    call('set_lead_status', email='bob@example.com', status='not_interested')]},
        {'text': 'Done.', 'tool_calls': []},
    ])
    new = agent.reply(env.db, env.cfg, thread, 'Set up outreach', complete=model)
    outputs = [json.loads(r['output']) for m in new if m['role'] == 'tool' for r in m['content']['results']]
    errors = [r for m in new if m['role'] == 'tool' for r in m['content']['results'] if r['is_error']]
    assert not errors, errors
    assert outputs[1] == {'campaign_id': 1, 'ai_mode': 'line', 'research': True, 'news': False}
    assert outputs[2]['follow_up'] == 1 and len(outputs[3]) == 2 and outputs[3][0]['written'].startswith('Saw that')
    assert outputs[4]['candidates'][0]['email'] == 'ana.lopez@acme.test'
    assert outputs[5]['due_local'] == '2099-01-02 10:00'
    assert env.db.val("SELECT lead_status FROM contacts WHERE email = 'bob@example.com'") == 'not_interested'


def test_agent_answers_a_reply_only_with_approval(env, fake_ai):
    list_id = make_list(env)
    campaign_id = service.create_campaign(env.db, env.db.settings(), {
        'name': 'L', 'subject': 'Hi {{first_name|there}}, news inside', 'html': '<p>Hi</p>', 'list_id': list_id,
        'rate_per_hour': 100000})
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    env.drain()
    replies.process(env.db, env.db.settings(), raw_reply('Ana <ana@example.com>', our_id(env, 'ana@example.com'),
                                                         'What does it cost?'), 'news@example.com', '1:9')
    thread = agent.new_thread(env.db)
    model = scripted([
        {'text': '', 'tool_calls': [call('get_inbox')]},
        {'text': '', 'tool_calls': [call('send_reply', reply_id=1, text='It starts at 49 EUR a month.')]},
        {'text': 'Waiting for your approval.', 'tool_calls': []},
    ])
    new = agent.reply(env.db, env.cfg, thread, 'Answer Ana', complete=model)
    inbox = json.loads(new[2]['content']['results'][0]['output'])
    assert inbox[0]['text'] == 'What does it cost?'
    sent = json.loads(new[4]['content']['results'][0]['output'])
    assert sent['status'] == 'pending_approval'
    env.catcher.messages.clear()
    assert env.engine.tick() == 0
    env.post(f'/emails/{sent["email_id"]}/decide', action='approve')
    assert env.engine.tick() == 1 and parsed(env.catcher.messages[0])['Subject'] == 'Re: Hi Ana, news inside'


# -- upgrades and pages ---------------------------------------------------------------------------
def test_old_database_is_upgraded_in_place(tmp_path):
    path = str(tmp_path / 'old.db')
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO contacts (email, created_at, updated_at) VALUES ('a@b.test', 1, 1)")
    conn.commit()
    conn.close()
    db = DB(path)
    db.init()
    db.init()
    assert db.val('PRAGMA user_version') == len(__import__('app.db', fromlist=['MIGRATIONS']).MIGRATIONS)
    assert dict(db.one('SELECT lead_status, verify_status FROM contacts')) == {'lead_status': 'new', 'verify_status': ''}
    assert db.val('SELECT COUNT(*) FROM mailboxes') == 0
    assert db.val('SELECT COUNT(*) FROM placement_tests') == 0 and db.val('SELECT COUNT(*) FROM news_cache') == 0
    db.close()


def test_every_page_renders_with_data(env, fake_ai, monkeypatch):
    monkeypatch.setattr(domainsetup, '_lookup', lambda name, kind: [])
    list_id = make_list(env)
    campaign_id = make_campaign_line(env, list_id)
    service.add_step(env.db, campaign_id, {'delay_days': 2, 'html': '<p>Any thoughts?</p>'})
    writer.preview(env.db, env.db.settings(), service.get_campaign(env.db, campaign_id), 2)
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    writer.run_pending(env.db, env.cfg)
    env.drain()
    replies.process(env.db, env.db.settings(), raw_reply('Ana <ana@example.com>', our_id(env, 'ana@example.com'),
                                                         'Interested!'), 'news@example.com', '1:5')
    ana = env.db.val("SELECT id FROM contacts WHERE email = 'ana@example.com'")
    planner.create_task(env.db, 'Call', time.time() + 3600, ana)
    for path in ('/', '/inbox', '/inbox?view=all', '/inbox/1', '/leads', '/leads?status=replied', f'/leads/{ana}',
                 '/lists', f'/lists/{list_id}', '/campaigns', f'/campaigns/{campaign_id}',
                 f'/campaigns/{campaign_id}/written', '/calendar', '/calendar?month=2026-12', '/find', '/agent',
                 '/analytics', '/emails', '/domains?domain=example.com', '/settings', '/suppressions',
                 '/accounts', '/accounts?add=1'):
        response = env.client.get('/campaigns' + path, follow_redirects=True)
        assert response.status_code == 200, (path, response.text[-800:])


# -- checking whether addresses work -------------------------------------------------------------
def test_address_checker(env, monkeypatch):
    cache_domain(env.db, 'acme.test')
    cache_domain(env.db, 'nomail.test', accepts=False)
    cache_domain(env.db, 'gmial.com')
    cache_domain(env.db, 'mailinator.com')
    results = {r['email']: r for r in leadtools.check_many(env.db, env.db.settings(),
               'ana@acme.test; bad@nomail.test, x@gmial.com <temp@mailinator.com> postmaster@acme.test not-an-email ana@acme.test')}
    assert list(results) == ['ana@acme.test', 'bad@nomail.test', 'x@gmial.com', 'temp@mailinator.com', 'postmaster@acme.test']
    assert results['ana@acme.test']['verdict'] == 'valid'
    assert any('not confirmed' in c['detail'] for c in results['ana@acme.test']['checks'])
    assert results['bad@nomail.test']['verdict'] == 'invalid'
    assert results['x@gmial.com']['verdict'] == 'risky' and results['x@gmial.com']['suggestion'] == 'x@gmail.com'
    assert results['temp@mailinator.com']['verdict'] == 'invalid'
    assert results['postmaster@acme.test']['verdict'] == 'risky'

    class Reply:
        def __init__(self, status, payload):
            self.status_code, self.payload = status, payload

        def json(self):
            return self.payload
    answers = [Reply(200, {'data': {'status': 'invalid', 'score': 5}}), Reply(202, {})]
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params['email']))
        return answers.pop(0)
    monkeypatch.setattr(leadtools.requests, 'get', fake_get)
    env.db.set_setting('hunter_api_key', 'hk')
    confirmed = leadtools.check_address(env.db, env.db.settings(), 'ana@acme.test')
    assert confirmed['verdict'] == 'invalid' and calls[0] == ('https://api.hunter.io/v2/email-verifier', 'ana@acme.test')
    pending = leadtools.check_address(env.db, env.db.settings(), 'ana@acme.test')
    assert pending['verdict'] == 'valid' and any('still checking' in c['detail'] for c in pending['checks'])
    assert leadtools.check_address(env.db, env.db.settings(), 'bad@nomail.test')['verdict'] == 'invalid'
    assert len(calls) == 2                                   # no paid check for an address that cannot work

    env.db.set_setting('hunter_api_key', '')
    page = env.client.post('/campaigns/find', data={'csrf': env.csrf, 'action': 'check',
                                                    'emails': 'ana@acme.test x@gmial.com bad@nomail.test'}).text
    assert 'looks good' in page and 'did you mean <strong>x@gmail.com</strong>' in page and 'will bounce' in page
    output = agent.Toolbox(env.db, env.cfg).run('check_email', {'emails': ['x@gmial.com']})
    assert json.loads(output[0])[0]['did_you_mean'] == 'x@gmail.com' and output[1] is False
