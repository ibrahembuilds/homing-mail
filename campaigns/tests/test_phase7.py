"""The email look (branded or plain, signature, footer), emails written about the lead with real
case studies, the agent page and the menu that hides."""
from app import agent, ai, offers, service, textfmt, writer
from app.render import Renderer
from tests.test_e2e import body_of, env, make_campaign, make_list  # noqa: F401  (fixture)


def test_branded_and_plain_email_look(env):
    env.db.set_setting('email_signature', 'Ibrahem Ahmed\nFounder, Kanyou AI')
    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id, html='<p>Hi {{first_name}}, a short note.</p>')
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    env.drain()
    html = body_of(next(m for m in env.catcher.messages if m['to'] == ['ana@example.com']))
    assert 'background:#f3f3f7' in html and 'border-bottom:3px solid' in html          # the branded card
    assert 'Test Sender' in html and '<strong>Ibrahem Ahmed</strong>' in html          # header name, signature
    assert 'text-align:center' in html and 'Unsubscribe' in html and '1 Test Street' in html
    assert 'Ibrahem Ahmed' in body_of(next(m for m in env.catcher.messages if m['to'] == ['ana@example.com']), 'plain')

    env.db.set_setting('email_style', 'plain')
    campaign = service.get_campaign(env.db, campaign_id)
    contact = env.db.one("SELECT * FROM contacts WHERE email = 'ana@example.com'")
    _, plain, _ = Renderer(env.cfg, env.db).campaign_bodies(campaign, contact, '0' * 32, env.db.settings(), tracking=False)
    assert 'background:#f3f3f7' not in plain and 'Ibrahem Ahmed' in plain and '<!--footer-->' not in plain
    # A follow-up in the same thread always reads like a quick reply.
    env.db.set_setting('email_style', 'branded')
    _, reply_like, _ = Renderer(env.cfg, env.db).campaign_bodies(
        campaign, contact, '0' * 32, env.db.settings(), tracking=False,
        content={'subject': 'Re: x', 'html': '<p>Bumping this.</p>', 'text': '', 'preheader': '', 'look': 'plain'})
    assert 'background:#f3f3f7' not in reply_like

    page = env.client.get('/campaigns/settings').text
    assert 'How your emails look' in page and 'name="email_signature"' in page
    env.post('/settings', section='sender', from_name='Ibrahem', from_email='news@example.com', postal_address='1 Road',
             reply_to='', timezone='UTC', email_style='plain', email_signature='Ibrahem', email_logo_url='http://x.test/a.png')
    assert env.db.setting('email_style') == 'branded'               # refused: the logo must be https
    env.post('/settings', section='sender', from_name='Ibrahem', from_email='news@example.com', postal_address='1 Road',
             reply_to='', timezone='UTC', email_style='plain', email_signature='Ibrahem', email_logo_url='')
    assert env.db.setting('email_style') == 'plain'
    preview = env.client.post('/campaigns/compose/preview', json={'text': 'Hello'}, headers={'X-CSRF-Token': env.csrf})
    assert 'Ibrahem' in preview.get_json()['html']


def test_emails_are_about_the_lead_with_real_case_studies(env, monkeypatch):
    seen = []

    class Provider:
        @staticmethod
        def generate_json(api_key, model, system, prompt, schema, effort='low'):
            seen.append(system)
            return {'data': {'subject': 'Your homepage on a phone', 'html': '<p>Parents in Leeds book on their phones.</p>'},
                    'model': 'fake', 'usage': {}}
    monkeypatch.setattr(ai, 'resolve', lambda db, settings, purpose='agent': (Provider, 'k', 'fake'))
    offer_id = offers.save(env.db, {'name': 'Redesign', 'summary': 'A homepage that works on phones',
                                    'case_studies': 'Leeds tutoring centre: 2 enquiries a week, then 9 after the new homepage\n'
                                                    'Bradford tutor: bounce rate fell from 70% to 35%'})
    assert offers.case_studies(offers.get(env.db, offer_id))[1].startswith('Bradford')
    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id, html='<p>Hi {{first_name}}</p><p>See <a href="https://x.test">this</a>.</p>',
                                ai_mode='full')
    result = writer.personalize(env.db, env.db.settings(), service.get_campaign(env.db, campaign_id),
                                env.db.one("SELECT * FROM contacts WHERE email = 'ana@example.com'"))
    assert result['subject'] == 'Your homepage on a phone'
    system = seen[0]
    for part in ('Their situation', 'The problem', 'How it helps them', 'up to three', 'Never invent a client',
                 'Leeds tutoring centre: 2 enquiries a week, then 9'):
        assert part in system, part
    props = next(t for t in agent.TOOLS if t['name'] == 'save_offer')['input_schema']['properties']
    assert 'case_studies' in props
    assert 'special offer' in textfmt.ai_tells('Our special offer for you')
    assert textfmt.ai_tells('We have a slot on Tuesday.') == []
    page = env.client.get('/campaigns/offers').text
    assert 'Case studies' in page and 'Bradford tutor' in page


def test_agent_page_and_hideable_menu(env):
    first, second = agent.new_thread(env.db), agent.new_thread(env.db)
    agent.add_user_message(env.db, first, 'Hello')
    page = env.client.get(f'/campaigns/agent/{second}').text
    assert 'id="history-toggle"' in page and 'id="history" hidden' in page and 'New chat' in page
    assert 'id="nav-toggle"' in page and 'id="nav-open"' in page
    env.client.set_cookie('nav', 'collapsed', path='/')
    assert 'class="nav-collapsed"' in env.client.get('/campaigns/').text
