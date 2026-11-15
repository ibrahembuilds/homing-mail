"""The agent in the background, clean answers, human-sounding writing, leads
without an email, offers, and the inbox's mailbox profiles."""
import io
import json
import time

import pytest

from app import agent, agent_runs, ai, ai_openrouter, contacts, leadtools, news, offers, prospects, replies, textfmt, writer
from app.ai_common import ProviderError
from tests.test_e2e import env, make_campaign, make_list  # noqa: F401  (fixture)
from tests.test_providers import FakeAPI


@pytest.fixture(autouse=True)
def inline_runs(monkeypatch):
    monkeypatch.setattr(agent_runs, 'INLINE', True)


def answers(*replies_):
    """A stand-in model: each call returns the next scripted turn."""
    queue = list(replies_)

    def complete(messages):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    return complete


def turn(text='', calls=None):
    return {'text': text, 'tool_calls': calls or [], 'model': 'fake-1', 'usage': {}}


# -- formatting ----------------------------------------------------------------------------------
def test_answers_are_formatted_safely_and_plainly():
    html = str(textfmt.markdown('**Who they are:** tutors <script>x</script>\n- one\n- two\n\n'
                                '[site](https://a.test/x?a=1&b=2) and [bad](javascript:alert(1)) `{{first_name}}`'))
    assert '<strong>Who they are:</strong>' in html and '<ul><li>one</li><li>two</li></ul>' in html
    assert '&lt;script&gt;' in html and '<script>' not in html
    assert 'href="https://a.test/x?a=1&amp;b=2"' in html and 'href="javascript' not in html
    assert '<code>{{first_name}}</code>' in html
    assert textfmt.plain_punctuation('Short — nothing more. 3–4 days.') == 'Short, nothing more. 3-4 days.'
    tells = textfmt.ai_tells('Hi Sam, I came across your site — I hope this email finds you well!')
    assert 'i came across' in tells and any('dash' in t for t in tells)
    assert textfmt.ai_tells('Saw the new Leeds branch. Want me to send the redesign?') == []


# -- uploads: names and leads without an email -------------------------------------------------
def test_names_and_rows_without_email_are_kept():
    assert contacts.split_name('Stephen Edward Beach; Simon Charles Horsley') == ('Stephen and Simon', '')
    assert contacts.split_name('Helena (surname not found); co-founder Alice') == ('Helena and Alice', '')
    assert contacts.split_name('Dr Adefunke Osagie') == ('Adefunke', 'Osagie')
    rows, columns = contacts.parse_csv(b'business,current_website,decision_maker\nGold Star,goldstar.test,Jo Bloggs\n')
    assert columns['email'] is None and rows[0]['first_name'] == 'Jo' and rows[0]['fields']['decision_maker'] == 'Jo Bloggs'


def test_upload_keeps_leads_without_email_and_finds_them_on_their_website(env, monkeypatch):
    csv_text = ('email,business,current_website,decision_maker,preview_url\n'
                'info@known.test,Known Ltd,known.test,Ann Lee,https://preview.example/known\n'
                ',Gold Star Tutors,goldstar.test,,https://preview.example/gold\n'
                ',Lone Tutor,lone.test,Maria,\n'
                ',Ghost Co,,,\n')
    response = env.client.post('/campaigns/lists', data={
        'csrf': env.csrf, 'name': 'Tutors', 'consent': 'yes', 'file': (io.BytesIO(csv_text.encode()), 't.csv')},
        content_type='multipart/form-data')
    list_id = int(response.headers['Location'].rsplit('/', 1)[-1])
    assert contacts.list_counts(env.db, list_id)['total'] == 1
    waiting = prospects.waiting(env.db, list_id)
    assert [p['website'] for p in waiting] == ['goldstar.test', 'lone.test', '']   # not the preview link
    page = env.client.get(f'/campaigns/lists/{list_id}').text
    assert 'Leads without an email' in page and 'Find their emails' in page

    key = 0x21
    hidden = '%02x' % key + ''.join('%02x' % (ord(c) ^ key) for c in 'hello@goldstar.test')
    pages = {'https://goldstar.test/': '<a href="/contact-us">Contact</a><p>Welcome</p>',
             'https://goldstar.test/contact-us': f'<span data-cfemail="{hidden}">[email protected]</span>',
             'https://lone.test/': '<p>No address here</p>'}

    def fake_get(url, max_redirects=3):
        if url not in pages:
            raise leadtools.ToolError('Could not reach it.')
        return url, pages[url]
    monkeypatch.setattr(leadtools, '_safe_get', fake_get)
    response = env.client.post(f'/campaigns/lists/{list_id}/find-emails', data={'csrf': env.csrf})
    assert response.status_code == 302
    gold = env.db.one("SELECT * FROM prospects WHERE website = 'goldstar.test'")
    assert gold['status'] == 'added' and gold['email'] == 'hello@goldstar.test'
    assert 'contact-us' in gold['source']
    lone = env.db.one("SELECT * FROM prospects WHERE website = 'lone.test'")
    assert lone['status'] == 'guessed' and lone['email'] == 'maria@lone.test'     # a guess waits for the owner
    assert contacts.list_counts(env.db, list_id)['total'] == 2
    added = env.db.one("SELECT * FROM contacts WHERE email = 'hello@goldstar.test'")
    assert json.loads(added['fields'])['business'] == 'Gold Star Tutors'

    env.client.post(f'/campaigns/lists/{list_id}/prospects/{lone["id"]}',
                    data={'csrf': env.csrf, 'action': 'add', 'email': 'maria@lone.test'})
    assert contacts.list_counts(env.db, list_id)['total'] == 3
    ghost = env.db.one("SELECT * FROM prospects WHERE website = ''")
    env.client.post(f'/campaigns/lists/{list_id}/prospects/{ghost["id"]}', data={'csrf': env.csrf, 'action': 'dismiss'})
    assert prospects.waiting(env.db, list_id) == []


# -- the agent runs in the background ------------------------------------------------------------
def test_agent_turn_runs_in_the_background_and_reports_progress(env, monkeypatch):
    list_id = make_list(env)
    script = answers(turn('', [{'id': 'c1', 'name': 'list_leads', 'input': {'list_id': list_id}},
                               {'id': 'c2', 'name': 'get_lead', 'input': {'email': 'nobody@nowhere.test'}}]),
                     turn('**Four leads.** Ana — at Acme — looks best.'))
    steps = []
    monkeypatch.setattr(agent, 'run', (lambda real: lambda db, cfg, tid, complete=None, on_step=None, should_stop=None:
                                       real(db, cfg, tid, complete=script, on_step=lambda t: (steps.append(t), on_step(t)),
                                            should_stop=should_stop))(agent.run))
    thread = agent.new_thread(env.db)
    headers = {'X-CSRF-Token': env.csrf}
    response = env.client.post(f'/campaigns/agent/{thread}/send', json={'text': 'Who is on my list?'}, headers=headers)
    assert response.status_code == 200, response.text
    data = response.get_json()
    assert 'Who is on my list?' in data['html']
    assert steps == ['Thinking', 'Reading the leads', 'Opening the lead', 'Thinking']
    poll = env.client.get(f'/campaigns/agent/{thread}/poll?after={data["after"]}').get_json()
    assert poll['status'] == 'idle' and poll['error'] == ''
    assert '<strong>Four leads.</strong>' in poll['html'] and '—' not in poll['html']      # formatted, plain dashes
    assert '2 steps' in poll['html'] and '1 failed' in poll['html'] and 'agent-steps' in poll['html']
    page = env.client.get(f'/campaigns/agent/{thread}').text
    assert '<strong>Four leads.</strong>' in page and '**' not in page


def test_a_failed_ai_call_is_kept_visible_and_can_be_retried(env, monkeypatch):
    monkeypatch.setattr(agent, 'RETRY_DELAYS', (0, 0))
    calls = []

    def flaky(messages):
        calls.append(1)
        if len(calls) <= 3:
            raise ProviderError('OpenRouter returned an error (502): upstream timed out', retry=True)
        return turn('Done.')
    real = agent.run
    monkeypatch.setattr(agent, 'run', lambda db, cfg, tid, complete=None, **kw: real(db, cfg, tid, complete=flaky, **kw))
    thread = agent.new_thread(env.db)
    headers = {'X-CSRF-Token': env.csrf}
    env.client.post(f'/campaigns/agent/{thread}/send', json={'text': 'Hi'}, headers=headers)
    assert len(calls) == 3                                     # tried, then twice more
    state = agent_runs.state(env.db, thread)
    assert state['status'] == 'error' and 'upstream timed out' in state['error']
    assert agent.history(env.db, thread)[-1]['role'] == 'user'   # the message is kept
    page = env.client.get(f'/campaigns/agent/{thread}').text
    assert 'upstream timed out' in page and 'Try again' in page
    assert env.client.post(f'/campaigns/agent/{thread}/retry', headers=headers).status_code == 200
    assert agent_runs.state(env.db, thread)['status'] == 'idle'
    assert agent.history(env.db, thread)[-1]['content']['text'] == 'Done.'


def test_busy_conversation_and_stop(env, monkeypatch):
    thread = agent.new_thread(env.db)
    env.db.run("UPDATE agent_threads SET status = 'running', heartbeat_at = ? WHERE id = ?", time.time(),
               thread)
    response = env.client.post(f'/campaigns/agent/{thread}/send', json={'text': 'again'},
                               headers={'X-CSRF-Token': env.csrf})
    assert response.status_code == 409
    env.client.post(f'/campaigns/agent/{thread}/stop', headers={'X-CSRF-Token': env.csrf})
    assert env.db.val('SELECT stop_requested FROM agent_threads WHERE id = ?', thread) == 1
    env.db.run("UPDATE agent_threads SET heartbeat_at = 1 WHERE id = ?", thread)     # a dead run
    assert agent_runs.state(env.db, thread)['status'] == 'error'
    out = agent.run(env.db, env.cfg, thread, complete=answers(), should_stop=lambda: True)
    assert out[-1]['content']['note'] == 'Stopped.'


def test_attached_leads_are_described_to_the_agent_from_the_database(env, monkeypatch):
    seen = []
    real = agent.run

    def capture(messages):
        seen.append(messages[-1]['content']['text'])
        return turn('Got them.')
    monkeypatch.setattr(agent, 'run', lambda db, cfg, tid, complete=None, **kw: real(db, cfg, tid, complete=capture, **kw))
    thread = agent.new_thread(env.db)
    headers = {'X-CSRF-Token': env.csrf}
    csv_text = 'email,business,website\nana@acme.test,Acme,acme.test\n,Beta,beta.test\n'
    up = env.client.post(f'/campaigns/agent/{thread}/upload', headers=headers, content_type='multipart/form-data',
                         data={'consent': 'yes', 'file': (io.BytesIO(csv_text.encode()), 'leads.csv')}).get_json()
    assert up['added'] == 1 and up['needs_email'] == 1
    sent = env.client.post(f'/campaigns/agent/{thread}/send', headers=headers,
                           json={'text': '', 'attachment': {'list_id': up['list_id'], 'file': 'leads.csv'}}).get_json()
    assert 'leads.csv' in sent['html'] and '[Attached' not in sent['html']        # a chip, not the raw note
    assert f'list_id {up["list_id"]}' in seen[0] and '1 without one' in seen[0] and 'business' in seen[0]
    assert env.db.val('SELECT title FROM agent_threads WHERE id = ?', thread).startswith('Read these leads')


# -- the agent can read, research and build lists ---------------------------------------------------
def test_agent_tools_for_leads(env, monkeypatch):
    list_id = make_list(env)
    box = agent.Toolbox(env.db, env.cfg, search=lambda q: {'query': q, 'results': [{'url': 'https://x.test'}],
                                                           'usage': {}})

    def call(tool, **args):
        out, is_error = box.run(tool, args)
        assert not is_error, out
        return json.loads(out)
    found = call('search_leads', query='Acme')
    assert [l['email'] for l in found['leads']] == ['ana@example.com']
    call('save_lead_notes', email='ana@example.com', notes='Opened a Leeds branch in October (acme.com/news).')
    call('update_lead', email='ana@example.com', first_name='Ana', fields={'city': 'Leeds'})
    lead = call('get_lead', email='ana@example.com')
    assert 'Leeds branch' in lead['research'] and lead['columns']['city'] == 'Leeds'
    assert lead['lists'][0]['list_id'] == list_id

    monkeypatch.setattr(leadtools, 'fetch_site', lambda db, site: {'title': 'Acme', 'description': 'Widgets',
                                                                   'headings': [], 'text': 'We make widgets'})
    monkeypatch.setattr(leadtools, 'site_emails', lambda db, site: {'emails': [
        {'email': 'hello@acme.test', 'page': 'https://acme.test/contact', 'own_domain': True}], 'pages': []})
    monkeypatch.setattr(news, 'search', lambda db, company, days=60: [])
    research = call('research_lead', email='ana@example.com')
    assert research['site']['text'] == 'We make widgets' and research['emails_on_their_site'][0]['email'] == 'hello@acme.test'

    new = call('create_list', name='Found on the web')
    added = call('add_leads', list_id=new['list_id'], leads=[
        {'email': 'sam@shop.test', 'first_name': 'Sam', 'company': 'Shop', 'source': 'https://shop.test/about'},
        {'company': 'NoMail Ltd', 'website': 'nomailyet.test'},
        {'email': 'x@nomail-domain.test', 'company': 'Gone', 'website': 'gone.test'}])
    assert added['added'] == 1 and added['need_an_email'] == 2
    assert added['domains_that_receive_no_mail'] == ['x@nomail-domain.test']
    assert call('web_search', query='tutors in Leeds')['results'][0]['url'] == 'https://x.test'


def test_offers_page_and_agent_use_them(env, fake_offer_writer):
    response = env.client.post('/campaigns/offers/save', data={
        'csrf': env.csrf, 'name': 'Free redesign', 'summary': 'A free homepage redesign, yours to keep',
        'audience': 'UK tutoring businesses with dated websites', 'call_to_action': 'Reply yes for the link'})
    assert response.status_code == 302
    row = offers.default(env.db)
    assert row['name'] == 'Free redesign' and row['is_default'] == 1
    page = env.client.get('/campaigns/offers').text
    assert 'A free homepage redesign' in page and 'Still empty' in page
    overview, _ = agent.Toolbox(env.db, env.cfg).run('get_overview', {})
    assert 'Free redesign' in overview
    out, _ = agent.Toolbox(env.db, env.cfg).run('save_offer', {'offer_id': row['id'], 'proof': '40 sites rebuilt'})
    assert json.loads(out)['proof'] == '40 sites rebuilt'

    assistant = env.client.post('/campaigns/offers/assistant', data={'csrf': env.csrf})
    thread_id = int(assistant.headers['Location'].rsplit('/', 1)[-1])
    assert env.db.val('SELECT kind FROM agent_threads WHERE id = ?', thread_id) == 'offer'
    assert 'Offer assistant' in env.client.get(f'/campaigns/agent/{thread_id}').text


@pytest.fixture
def fake_offer_writer(monkeypatch):
    calls = []

    class Provider:
        @staticmethod
        def generate_json(api_key, model, system, prompt, schema, effort='low'):
            calls.append({'system': system, 'prompt': prompt})
            line = ('I came across your site — really impressive!' if len(calls) == 1
                    else 'Your Leeds branch opened in October; the homepage still lists one site.')
            return {'data': {'line': line}, 'model': 'fake', 'usage': {}}
    monkeypatch.setattr(ai, 'resolve', lambda db, settings, purpose='agent': (Provider, 'k', 'fake'))
    return calls


def test_per_lead_writing_uses_offer_and_research_and_avoids_ai_phrases(env, fake_offer_writer):
    list_id = make_list(env)
    offers.save(env.db, {'name': 'Redesign', 'summary': 'A free homepage redesign'})
    env.db.run("UPDATE contacts SET research = 'Opened a Leeds branch in October' WHERE email = 'ana@example.com'")
    campaign_id = make_campaign(env, list_id, html='<p>Hi {{first_name}},</p><p>{{ai_line}}</p><p>Want it?</p>',
                                ai_mode='line')
    campaign = env.db.one('SELECT * FROM campaigns WHERE id = ?', campaign_id)
    contact = env.db.one("SELECT * FROM contacts WHERE email = 'ana@example.com'")
    result = writer.personalize(env.db, env.db.settings(), campaign, contact)
    assert result['body'].startswith('Your Leeds branch')
    assert len(fake_offer_writer) == 2                         # the first try sounded machine-written
    first, second = fake_offer_writer
    assert 'A free homepage redesign' in first['system'] and 'No long dashes' in first['system']
    assert 'Opened a Leeds branch' in first['prompt'] and 'i came across' in second['prompt']


def test_offer_persona_gets_its_own_instructions_and_tools(env, monkeypatch):
    seen = {}

    def complete(api_key, model, system, history, tools, web_search=False):
        seen.update(system=system, tools=[t['name'] for t in tools], web_search=web_search)
        return turn('What do you sell?')
    monkeypatch.setattr(ai_openrouter, 'complete', complete)
    env.db.set_setting('ai_provider', 'openrouter')
    env.db.set_setting('ai_key_openrouter', 'sk-or-test')
    env.db.set_setting('ai_model_openrouter', 'anthropic/claude-test')
    thread = agent.new_thread(env.db, 'offer')
    agent.reply(env.db, env.cfg, thread, 'Help me')
    assert seen['system'] == agent.OFFER_SYSTEM and 'save_offer' in seen['tools'] and 'send_email' not in seen['tools']
    assert seen['tools'][-1] == 'web_search' and seen['web_search'] is False    # searched on demand, not every call


def test_openrouter_search_runs_only_when_asked():
    api = FakeAPI([
        (200, {'model': 'm', 'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'ok'}}]}),
        (200, {'model': 'm', 'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': '1. Leeds Tutors - leedstutors.test',
            'annotations': [{'type': 'url_citation', 'url_citation': {
                'url': 'https://leedstutors.test', 'title': 'Leeds Tutors', 'content': 'GCSE maths and English'}}]}}]}),
    ])
    original = ai_openrouter.API
    ai_openrouter.API = api.url
    try:
        ai_openrouter.complete('k', 'm', 'sys', [{'role': 'user', 'content': {'text': 'hi'}}], agent.TOOLS, web_search=True)
        found = ai_openrouter.search('k', 'm', 'tutors in Leeds')
    finally:
        ai_openrouter.API = original
        api.close()
    assert 'plugins' not in api.requests[0]['body']
    assert api.requests[1]['body']['plugins'][0]['id'] == 'web'
    assert found['results'] == [{'title': 'Leeds Tutors', 'url': 'https://leedstutors.test',
                                 'snippet': 'GCSE maths and English'}]


# -- inbox profiles -------------------------------------------------------------------------------
class FakeIMAP:
    RAW = (b'From: Ana Lima <ana@example.com>\r\nTo: hello@example.com\r\nSubject: Re: your redesign\r\n'
           b'Date: Fri, 13 Nov 2026 10:00:00 +0000\r\nMessage-ID: <abc@client.test>\r\n'
           b'Content-Type: text/plain; charset=utf-8\r\n\r\nLooks great, send it over.\r\n')

    def __init__(self):
        self.selected = None

    def list(self):
        return 'OK', [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Junk) "/" "Junk"',
                      b'(\\HasNoChildren \\Sent) "/" "Sent"']

    def select(self, name, readonly=True):
        self.selected = name
        return 'OK', [b'1']

    def uid(self, command, *args):
        if command == 'SEARCH':
            return 'OK', [b'7' if self.selected == '"INBOX"' else b'']
        if args[1].startswith('(FLAGS'):
            return 'OK', [(b'1 (UID 7 FLAGS () BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)] {120}', self.RAW), b')']
        return 'OK', [(b'1 (UID 7 BODY[] {200}', self.RAW), b')']

    def logout(self):
        pass


def test_inbox_profiles_read_each_mailbox(env, monkeypatch):
    monkeypatch.setattr(replies, 'connect', lambda box: FakeIMAP())
    env.db.set_setting('reply_imap_user', 'hello@example.com')
    env.db.set_setting('reply_imap_password', 'pw')
    page = env.client.get('/campaigns/inbox').text
    assert 'Lead replies' in page and 'hello@example.com' in page
    listing = env.client.get('/campaigns/inbox/mail/main').text
    assert 'Ana Lima' in listing and 'Re: your redesign' in listing
    assert 'Nothing here' in env.client.get('/campaigns/inbox/mail/main?folder=spam').text
    message = env.client.get('/campaigns/inbox/mail/main/inbox/7').text
    assert 'Looks great, send it over.' in message and 'Reply from hello@example.com' in message
    response = env.client.post('/campaigns/inbox/mail/main/inbox/7/reply', data={'csrf': env.csrf, 'text': 'Here it is.'})
    assert response.status_code == 302
    sent = env.db.one("SELECT * FROM messages WHERE kind = 'reply' ORDER BY created_at DESC LIMIT 1")
    payload = json.loads(sent['payload'])
    assert sent['to_email'] == 'ana@example.com' and sent['subject'] == 'Re: your redesign'
    assert payload['from_email'] == 'hello@example.com' and payload['headers']['In-Reply-To'] == '<abc@client.test>'
    assert env.client.get('/campaigns/inbox/mail/nope').status_code == 404
