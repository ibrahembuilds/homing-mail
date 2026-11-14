"""Lead news, leads attached in the AI chat, the reply autopilot and sending
health (capacity, rates and the inbox placement test)."""
import io
import json
import time

from app import agent, health, news, replies, service, writer
from tests.test_accounts import add_account
from tests.test_e2e import env, make_campaign, make_list  # noqa: F401  (env is a fixture)
from tests.test_outreach import fake_ai  # noqa: F401  (fixture)

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Acme opens a new factory in Lyon - Les Echos</title><source>Les Echos</source>
  <pubDate>Tue, 10 Nov 2026 08:00:00 GMT</pubDate><link>https://news.example/acme-lyon</link></item>
<item><title>Acme raises 20 million in funding - TechNews</title><source>TechNews</source>
  <pubDate>Mon, 02 Nov 2026 08:00:00 GMT</pubDate><link>https://news.example/acme-funding</link></item>
<item><title>Stock markets fall on Monday - Daily</title><source>Daily</source>
  <pubDate>Wed, 11 Nov 2026 08:00:00 GMT</pubDate><link>https://news.example/markets</link></item>
</channel></rss>"""


class FeedReply:
    status_code = 200
    content = RSS.encode()


def fake_feed(monkeypatch):
    calls = []

    def get(url, params=None, **kwargs):
        calls.append(params['q'])
        return FeedReply()
    monkeypatch.setattr(news.requests, 'get', get)
    return calls


# -- news ------------------------------------------------------------------------------
def test_news_is_found_filtered_cached_and_used_in_writing(env, fake_ai, monkeypatch):
    calls = fake_feed(monkeypatch)
    items = news.search(env.db, 'Acme Inc')
    assert [i['title'] for i in items] == ['Acme opens a new factory in Lyon', 'Acme raises 20 million in funding']
    assert items[0]['source'] == 'Les Echos' and items[0]['url'] == 'https://news.example/acme-lyon'
    assert calls == ['"acme" when:60d']                                   # "Inc" is too generic to search for
    news.search(env.db, 'ACME inc.')
    assert len(calls) == 1                                                 # served from the cache

    list_id = make_list(env)
    ana = env.db.one("SELECT * FROM contacts WHERE email = 'ana@example.com'")
    assert news.company_of(ana) == 'Acme'
    page = env.client.get(f'/campaigns/leads/{ana["id"]}?news=1').text
    assert 'Acme opens a new factory in Lyon' in page and 'https://news.example/acme-lyon' in page

    campaign_id = make_campaign(env, list_id, html='<p>{{ai_line}}</p><p>Hello</p>', ai_mode='line', ai_news=True)
    assert service.get_campaign(env.db, campaign_id)['ai_news'] == 1
    writer.preview(env.db, env.db.settings(), service.get_campaign(env.db, campaign_id), 1)
    prompt = next(p for _, p in fake_ai.prompts if 'ana@example.com' in p)
    assert '<recent_news company="Acme">' in prompt and 'Acme opens a new factory in Lyon' in prompt

    out, is_error = agent.Toolbox(env.db, env.cfg).run('lead_news', {'email': 'ana@example.com'})
    assert not is_error and json.loads(out)['headlines'][0]['date'] == '2026-11-10'


def test_news_failure_does_not_stop_writing(env, fake_ai, monkeypatch):
    def down(*args, **kwargs):
        raise news.requests.ConnectionError('offline')
    monkeypatch.setattr(news.requests, 'get', down)
    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id, html='<p>{{ai_line}}</p>', ai_mode='line', ai_news=True)
    result = writer.preview(env.db, env.db.settings(), service.get_campaign(env.db, campaign_id), 1)
    assert result and not result[0].get('error')


# -- leads in the chat -------------------------------------------------------------------
def test_leads_attached_in_the_chat_become_a_list_the_agent_reads(env):
    thread = agent.new_thread(env.db)
    csv_text = 'Email,First Name,Company,Website\nana@acme.test,Ana,Acme,acme.test\nnope,Bad,,\n'
    upload = lambda consent: env.client.post(
        f'/campaigns/agent/{thread}/upload', headers={'X-CSRF-Token': env.csrf},
        data={'csrf': env.csrf, 'consent': consent, 'file': (io.BytesIO(csv_text.encode()), 'dental_clinics.csv')},
        content_type='multipart/form-data')
    assert upload('no').status_code == 400
    response = upload('yes')
    assert response.status_code == 200, response.text
    data = response.get_json()
    assert data['name'] == 'dental clinics' and data['added'] == 1 and data['invalid'] == 1
    assert data['columns'] == ['email', 'first_name', 'company', 'website']

    out, is_error = agent.Toolbox(env.db, env.cfg).run('list_leads', {'list_id': data['list_id']})
    leads = json.loads(out)
    assert not is_error and leads['total'] == 1
    assert leads['leads'][0]['email'] == 'ana@acme.test' and leads['leads'][0]['company'] == 'Acme'
    page = env.client.get(f'/campaigns/agent/{thread}').text
    assert 'data-upload' in page and 'id="attach"' in page


# -- reply autopilot ---------------------------------------------------------------------
def add_reply(e, email, category, body='Sounds good, can we talk?', age=0):
    contact = e.db.one('SELECT * FROM contacts WHERE email = ?', email)
    message_id = e.db.val('SELECT id FROM messages WHERE to_email = ? ORDER BY created_at LIMIT 1', email)
    ts = time.time() - age
    return e.db.run(
        'INSERT INTO replies (mailbox, uid, header_id, message_id, campaign_id, contact_id, from_email, from_name, '
        'subject, body, received_at, is_auto, category, summary, created_at) '
        'VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)',
        'news@example.com', f'1:{email}:{age}', f'<r-{email}-{age}@client.test>', message_id, contact['id'], email,
        contact['first_name'] or '', 'Re: Hi', body, ts, category, 'Wants to talk', ts).lastrowid


def test_reply_autopilot_drafts_or_answers(env, fake_ai):
    list_id = make_list(env)
    service.schedule_campaign(env.db, env.db.settings(), make_campaign(env, list_id))
    assert env.drain() == 4
    meeting = add_reply(env, 'ana@example.com', 'meeting')
    declined = add_reply(env, 'carol@example.org', 'not_interested', 'No thanks')
    add_reply(env, 'dave@example.net', 'question', age=5 * 86400)          # too old to answer by itself

    assert replies.auto_answer(env.db, env.cfg, env.db.settings()) == 0      # off by default
    env.db.set_setting('ai_reply_mode', 'draft')
    assert replies.auto_answer(env.db, env.cfg, env.db.settings()) == 1
    row = env.db.one('SELECT * FROM replies WHERE id = ?', meeting)
    assert row['draft'].startswith('Thanks Ana') and row['handled_at'] is None
    assert env.db.one('SELECT * FROM replies WHERE id = ?', declined)['drafted_at'] is None
    assert env.db.val('SELECT COUNT(*) FROM tasks WHERE reply_id = ?', meeting) == 1
    assert 'AI answer ready' in env.client.get('/campaigns/inbox').text
    page = env.client.get(f'/campaigns/inbox/{meeting}').text
    assert 'The AI wrote this answer for you' in page and 'Thanks Ana, Thursday' in page
    assert replies.auto_answer(env.db, env.cfg, env.db.settings()) == 0      # each reply once

    # Answering by itself: an answer that needs the owner's calendar link stays a draft.
    env.db.set_setting('ai_reply_mode', 'send')
    env.db.set_setting('ai_require_approval', '0')
    bob = add_reply(env, 'bob@example.com', 'interested')
    replies.auto_answer(env.db, env.cfg, env.db.settings())
    row = env.db.one('SELECT * FROM replies WHERE id = ?', bob)
    assert '[your calendar link]' in row['draft'] and row['handled_at'] is None

    original = fake_ai.generate_json

    def clean_reply(api_key, model, system, prompt, schema, effort='low'):
        if set(schema['properties']) == {'reply'}:
            return {'data': {'reply': 'Thanks! Pricing starts at 49 EUR a month. Shall I send a short demo?'},
                    'model': 'fake-1', 'usage': {}}
        return original(api_key, model, system, prompt, schema, effort)
    fake_ai.generate_json = clean_reply
    env.db.run('UPDATE replies SET drafted_at = NULL, draft = \'\' WHERE id = ?', bob)
    replies.auto_answer(env.db, env.cfg, env.db.settings())
    row = env.db.one('SELECT * FROM replies WHERE id = ?', bob)
    assert row['handled_at'] is not None and row['draft'] == ''
    sent = env.db.one("SELECT * FROM messages WHERE kind = 'reply' AND to_email = 'bob@example.com'")
    assert sent['status'] == 'queued' and sent['subject'] == 'Re: Hi'

    # With approval on, the answer waits for the owner.
    env.db.set_setting('ai_require_approval', '1')
    carol_q = add_reply(env, 'carol@example.org', 'question', 'What does it cost?', age=60)
    replies.auto_answer(env.db, env.cfg, env.db.settings())
    assert env.db.val("SELECT status FROM messages WHERE kind = 'reply' AND to_email = 'carol@example.org'") \
        == 'pending_approval'
    assert env.db.one('SELECT handled_at FROM replies WHERE id = ?', carol_q)['handled_at'] is not None

    # The owner can switch it from the chat and in Settings.
    out, is_error = agent.Toolbox(env.db, env.cfg).run('set_reply_autopilot', {'mode': 'off'})
    assert not is_error and env.db.setting('ai_reply_mode') == 'off'
    env.post('/settings', section='replies', reply_imap_user='', reply_imap_host='', reply_imap_port='',
             ai_reply_mode='draft')
    assert env.db.setting('ai_reply_mode') == 'draft'
    assert 'The AI writes a draft for me' in env.client.get('/campaigns/settings').text


# -- model prices -----------------------------------------------------------------------------
def test_model_list_has_prices_and_cheap_picks(env, monkeypatch):
    from app import ai_openrouter

    class Models:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            tools = ['tools', 'structured_outputs']
            return {'data': [
                {'id': 'deepseek/deepseek-v4.1-flash', 'name': 'DeepSeek: V4.1 Flash', 'created': 3,
                 'pricing': {'prompt': '0.0000000507', 'completion': '0.0000002028'}, 'supported_parameters': tools},
                {'id': 'anthropic/claude-haiku-5.5', 'name': 'Anthropic: Claude Haiku 5.5', 'created': 2,
                 'pricing': {'prompt': '0.0000001', 'completion': '0.0000005'}, 'supported_parameters': tools},
                {'id': 'openrouter/auto', 'name': 'Auto', 'created': 1,
                 'pricing': {'prompt': '-1', 'completion': '-1'}, 'supported_parameters': tools},
                {'id': 'x/free:free', 'name': 'Free', 'created': 0,
                 'pricing': {'prompt': '0', 'completion': '0'}, 'supported_parameters': tools},
                {'id': 'y/no-tools', 'name': 'No tools', 'created': 5, 'pricing': {}, 'supported_parameters': []}]}
    monkeypatch.setattr(ai_openrouter.requests, 'get', lambda *a, **k: Models())
    env.db.set_setting('ai_key_openrouter', 'sk-or-test')
    data = env.client.post('/campaigns/settings/models', json={'provider': 'openrouter'},
                           headers={'X-CSRF-Token': env.csrf}).get_json()
    by_id = {m['id']: m for m in data['catalog']}
    assert 'y/no-tools' not in by_id
    assert by_id['anthropic/claude-haiku-5.5']['input'] == 0.1 and by_id['anthropic/claude-haiku-5.5']['output'] == 0.5
    assert by_id['openrouter/auto']['input'] is None and by_id['x/free:free']['free']
    assert [m['id'] for m in data['recommended']['writer']] == ['deepseek/deepseek-v4.1-flash']
    assert [m['id'] for m in data['recommended']['agent']] == ['anthropic/claude-haiku-5.5']
    assert 'Load models and prices' in env.client.get('/campaigns/settings').text


# -- sending health ------------------------------------------------------------------------
def test_health_shows_capacity_and_rates(env):
    one = add_account(env, 'sam@send-one.test', 'pw', limit=20)
    add_account(env, 'kim@send-two.test', 'pw', limit=10)
    env.db.set_setting('daily_cap', '25')
    cap = health.capacity(env.db, env.db.settings())
    assert cap['account_total'] == 30 and cap['total'] == 25 and cap['limited_by'] == 'your daily cap'

    list_id = make_list(env)
    service.schedule_campaign(env.db, env.db.settings(), make_campaign(env, list_id, mailbox_ids=[one]))
    assert env.drain() == 4
    rows = env.db.q("SELECT id FROM messages WHERE kind = 'campaign' ORDER BY id")
    env.db.run("UPDATE messages SET opened_at = ?, human_open = 1 WHERE id = ?", time.time(), rows[0]['id'])
    env.db.run("UPDATE messages SET status = 'bounced', bounced_at = ?, bounce_type = 'hard' WHERE id = ?",
               time.time(), rows[1]['id'])
    stats = health.stats(env.db)
    t = stats['total']
    assert (t['sent'], t['opened'], t['bounced'], t['hard']) == (4, 1, 1, 1)
    assert t['open_rate'] == 25.0 and t['bounce_rate'] == 25.0 and t['levels']['bounce'] == 'few'
    assert stats['per_account'][0]['account'] == 'sam@send-one.test'
    assert health.capacity(env.db, env.db.settings())['left_today'] == 21

    page = env.client.get('/campaigns/sending-health').text
    assert 'Can still send today' in page and 'Marked as spam' in page and 'sam@send-one.test' in page
    assert 'Sending health' in env.client.get('/campaigns/settings').text


class PlacementIMAP:
    """Two mailboxes: kim gets everything in the inbox, sam finds it in spam."""
    sent = {}          # message id -> recipient

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.user, self.folder = None, None

    def login(self, user, password):
        self.user = user

    def list(self):
        return 'OK', [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
                      b'(\\HasNoChildren) "/" "Sent"']

    def select(self, folder, readonly=False):
        assert readonly
        self.folder = folder.strip('"')
        return 'OK', [b'1']

    def uid(self, command, *args):
        assert command == 'SEARCH' and args[1:3] == ('HEADER', 'Message-ID')
        to = PlacementIMAP.sent.get(args[3])
        where = {'kim@send-two.test': 'INBOX', 'sam@send-one.test': '[Gmail]/Spam'}.get(to)
        return 'OK', [b'7' if to == self.user and where == self.folder else b'']

    def logout(self):
        pass


def test_placement_test_finds_inbox_and_spam(env, monkeypatch):
    assert 'Connect an email account with its inbox' in env.client.get('/campaigns/sending-health').text
    add_account(env, 'sam@send-one.test', 'pw', imap_host='imap.send-one.test')
    add_account(env, 'kim@send-two.test', 'pw', imap_host='imap.send-two.test')
    monkeypatch.setattr(replies.imaplib, 'IMAP4_SSL', PlacementIMAP)
    response = env.post('/sending-health/placement', action='start')
    assert response.status_code == 302
    test = health.latest_test(env.db)
    assert test['total'] == 2 and test['counts']['waiting'] == 2
    assert {(r['from'], r['to']) for r in test['results']} == {('sam@send-one.test', 'kim@send-two.test'),
                                                               ('kim@send-two.test', 'sam@send-one.test')}
    for _ in range(5):
        env.engine.tick()
    assert len(env.catcher.messages) == 2
    assert all(m['user'] == {'kim@send-two.test': 'sam@send-one.test',
                             'sam@send-one.test': 'kim@send-two.test'}[m['to'][0]] for m in env.catcher.messages)
    PlacementIMAP.sent = {r['message_id']: r['to'] for r in test['results']}
    env.post('/sending-health/placement', action='check', test_id=test['id'])
    test = health.latest_test(env.db)
    assert test['finished'] and test['counts'] == {'inbox': 1, 'spam': 1, 'missing': 0, 'failed': 0, 'waiting': 0}
    assert test['inbox_rate'] == 50
    page = env.client.get('/campaigns/sending-health').text
    assert '50% reached the inbox' in page and 'Spam · [Gmail]/Spam' in page and 'Some emails landed in spam' in page
    # Test emails do not count in the delivery numbers.
    assert health.stats(env.db)['total']['sent'] == 0
