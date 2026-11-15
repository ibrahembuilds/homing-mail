"""One email to one person, a different email for each lead in one campaign,
the simpler Settings, the offer assistant's live card and the folding sidebar."""
import io
import json

from app import agent, offers, perlead, service
from tests.test_e2e import body_of, env, make_campaign, make_list, parsed  # noqa: F401  (fixture)

LEADS = ('email,first_name,company,email_subject,email_body\n'
         'ana@example.com,Ana,Acme,"A homepage idea for Acme","Hi Ana,\n\nI rebuilt your homepage: https://preview.test/acme\n\nIbrahem"\n'
         'bob@example.com,Bob,Globex,,"Hi Bob,\n\nQuick one about Globex."\n'
         'carol@example.org,Carol,Initech,"Unused subject",\n')


def upload(env, csv_text=LEADS, name='Personal'):
    response = env.client.post('/campaigns/lists', data={
        'csrf': env.csrf, 'name': name, 'consent': 'yes', 'file': (io.BytesIO(csv_text.encode()), 'leads.csv')},
        content_type='multipart/form-data')
    return int(response.headers['Location'].rsplit('/', 1)[-1])


# -- one email to one person ----------------------------------------------------------------------
def test_write_one_email_to_one_person(env):
    make_list(env)
    page = env.client.get('/campaigns/compose?to=ana@example.com').text
    assert 'Write an email' in page and 'news@example.com' in page and 'Ana' in page and 'compose-preview' in page
    preview = env.client.post('/campaigns/compose/preview', json={'text': 'Hi\n\nSee https://a.test/x'},
                              headers={'X-CSRF-Token': env.csrf}).get_json()
    assert '<a href="https://a.test/x">' in preview['html'] and '<p>Hi</p>' in preview['html']

    response = env.post('/compose', to='ana@example.com', subject='Quick question', text='Hi Ana,\n\nAre you free Tuesday?',
                        sender='server')
    assert response.status_code == 302 and '/emails/' in response.headers['Location']
    row = env.db.one("SELECT * FROM messages WHERE kind = 'single'")
    assert row['to_email'] == 'ana@example.com' and row['status'] == 'queued'
    assert env.db.val("SELECT lead_status FROM contacts WHERE email = 'ana@example.com'") == 'contacted'
    env.drain()
    sent = [m for m in env.catcher.messages if m['to'] == ['ana@example.com']]
    assert len(sent) == 1
    message = parsed(sent[0])
    assert message['Subject'] == 'Quick question' and 'news@example.com' in message['From']
    assert '<p>Are you free Tuesday?</p>' in body_of(sent[0])
    assert '/t/' not in body_of(sent[0])          # a personal email: no open pixel, no rewritten links

    env.post('/suppressions', emails='bob@example.com')
    blocked = env.post('/compose', to='bob@example.com', subject='Hi', text='Hello', sender='server')
    assert blocked.status_code == 200 and 'asked not to be emailed' in blocked.text
    assert env.db.val("SELECT COUNT(*) FROM messages WHERE kind = 'single'") == 1
    assert 'Write an email' in env.client.get('/campaigns/inbox').text
    assert 'Written by you' in env.client.get('/campaigns/emails').text


# -- a different email for each lead ---------------------------------------------------------------
def test_one_campaign_with_a_different_email_for_each_lead(env):
    list_id = upload(env)
    campaign_id = make_campaign(env, list_id, subject='Main subject', html='<p>Main email for {{first_name}}</p>')
    page = env.client.get(f'/campaigns/campaigns/{campaign_id}').text
    assert 'A different email for each lead' in page

    response = env.post(f'/campaigns/{campaign_id}/save', ai_mode='own', action='own_columns',
                        own_subject_col='email_subject', own_body_col='email_body', name='Personal',
                        list_id=str(list_id), subject='Main subject', html='<p>Main email for {{first_name}}</p>')
    assert response.status_code == 302
    campaign = service.get_campaign(env.db, campaign_id)
    assert campaign['ai_mode'] == 'own'
    assert perlead.counts(env.db, campaign) == {'total': 3, 'own': 2}          # carol has no message column
    page = env.client.get(f'/campaigns/campaigns/{campaign_id}').text
    assert '2</strong> of <span class="num">3</span> leads have their own email' in page

    each = env.client.get(f'/campaigns/campaigns/{campaign_id}/each').text
    assert 'I rebuilt your homepage' in each and 'main email' in each
    carol = env.db.val("SELECT id FROM contacts WHERE email = 'carol@example.org'")
    env.post(f'/campaigns/{campaign_id}/each', contact_id=str(carol), action='save', subject='For Carol',
             text='Hi Carol,\n\nWritten just for you.')
    bob = env.db.val("SELECT id FROM contacts WHERE email = 'bob@example.com'")
    preview = env.client.get(f'/campaigns/campaigns/{campaign_id}/preview?contact={bob}&framed=1').text
    assert 'Quick one about Globex.' in preview and 'Main subject' in preview          # no subject column: main subject
    env.post(f'/campaigns/{campaign_id}/each', contact_id=str(bob), action='main')
    assert perlead.counts(env.db, service.get_campaign(env.db, campaign_id))['own'] == 2

    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    assert env.drain() == 3
    by_to = {m['to'][0]: m for m in env.catcher.messages}
    assert parsed(by_to['ana@example.com'])['Subject'] == 'A homepage idea for Acme'
    ana_html = body_of(by_to['ana@example.com'])
    assert 'I rebuilt your homepage' in ana_html and 'preview.test/acme' in ana_html and 'Unsubscribe' in ana_html
    assert parsed(by_to['carol@example.org'])['Subject'] == 'For Carol'
    assert 'Written just for you.' in body_of(by_to['carol@example.org'])
    assert parsed(by_to['bob@example.com'])['Subject'] == 'Main subject'           # back to the main email
    assert 'Main email for Bob' in body_of(by_to['bob@example.com'])
    assert env.client.post(f'/campaigns/campaigns/{campaign_id}/each', data={
        'csrf': env.csrf, 'contact_id': str(bob), 'text': 'late'}).status_code == 302   # refused after the start
    assert perlead.counts(env.db, service.get_campaign(env.db, campaign_id))['own'] == 2


def test_starting_each_lead_from_the_main_email_and_switching_modes(env):
    list_id = upload(env)
    campaign_id = make_campaign(env, list_id, subject='Hi {{first_name}}', html='<p>Hello {{first_name}} at {{company}}</p>')
    env.post(f'/campaigns/{campaign_id}/save', ai_mode='own', action='own_draft', name='x', list_id=str(list_id),
             subject='Hi {{first_name}}', html='<p>Hello {{first_name}} at {{company}}</p>')
    rows = {r['contact_id']: r for r in env.db.q('SELECT * FROM personalizations WHERE campaign_id = ?', campaign_id)}
    assert len(rows) == 3 and any(r['subject'] == 'Hi Ana' and 'Hello Ana at Acme' in r['body'] for r in rows.values())
    service.update_campaign(env.db, env.db.settings(), campaign_id, {'ai_mode': 'line'})
    assert env.db.val('SELECT COUNT(*) FROM personalizations WHERE campaign_id = ?', campaign_id) == 0   # no leftovers


def test_agent_can_give_each_lead_their_own_email(env):
    list_id = upload(env)
    campaign_id = make_campaign(env, list_id)
    out, is_error = agent.Toolbox(env.db, env.cfg).run('use_own_email_per_lead', {
        'campaign_id': campaign_id, 'body_column': 'email_body', 'subject_column': 'email_subject'})
    assert not is_error and json.loads(out)['leads_with_own_email'] == 2
    out, is_error = agent.Toolbox(env.db, env.cfg).run('use_own_email_per_lead', {
        'campaign_id': campaign_id, 'body_column': 'nope'})
    assert is_error and 'no column "nope"' in json.loads(out)['error']


# -- simpler pages -----------------------------------------------------------------------------------
def test_settings_tabs_still_save_every_section(env):
    page = env.client.get('/campaigns/settings').text
    for section in ('id="sender"', 'id="ai"', 'id="replies"', 'id="pacing"', 'id="advanced"', 'data-settings-tabs',
                    'More options', 'id="leadtools"', 'id="webhooks"'):
        assert section in page, section
    env.post('/settings', section='sender', from_name='Ibrahem', from_email='hello@example.com',
             postal_address='1 Road', reply_to='', timezone='Europe/London')
    assert env.db.setting('from_name') == 'Ibrahem' and env.db.setting('timezone') == 'Europe/London'
    env.post('/settings', section='pacing', rate_per_hour='50', window_start='09:00', window_end='17:00',
             window_days=['0', '1'], daily_cap='0', domain_hourly_cap='0', delay_min='', delay_max='',
             warmup_start='20', warmup_growth='1.3', warmup_max='500', track_clicks='on')
    assert env.db.setting('rate_per_hour') == '50' and env.db.setting('track_opens') == '0'


def test_offer_assistant_page_and_folding_sidebar(env):
    thread = agent.new_thread(env.db, 'offer')
    page = env.client.get(f'/campaigns/agent/{thread}').text
    assert 'id="offer-live"' in page and 'Nothing saved yet' in page and 'class="card threads"' not in page
    offers.save(env.db, {'name': 'Redesign', 'summary': 'A free homepage redesign'})
    card = env.client.get('/campaigns/offers/card').text
    assert 'A free homepage redesign' in card and 'not yet' in card
    assert 'nav-collapsed' not in env.client.get('/campaigns/').text.split('>', 2)[1]
    env.client.set_cookie('nav', 'collapsed', path='/')
    assert 'class="nav-collapsed"' in env.client.get('/campaigns/').text
    assert 'id="nav-toggle"' in env.client.get('/campaigns/').text


def test_own_emails_need_no_main_email_but_block_placeholders(env):
    csv_text = ('email,first_name,email_subject,email_body\n'
                'ana@example.com,Ana,Hi Ana,"Hi Ana,\n\nHere it is.\n\nBest,\n[Your name]"\n'
                'bob@example.com,Bob,Hi Bob,"Hi Bob,\n\nHere it is.\n\nBest,\n[Your name]"\n')
    list_id = upload(env, csv_text)
    campaign_id = make_campaign(env, list_id, subject='', html='')
    service.update_campaign(env.db, env.db.settings(), campaign_id, {'ai_mode': 'own'})
    perlead.fill_from_columns(env.db, service.get_campaign(env.db, campaign_id), 'email_subject', 'email_body')
    problems = service.readiness(env.db, env.db.settings(), service.get_campaign(env.db, campaign_id))
    assert not any('subject is empty' in p or 'body is empty' in p for p in problems)    # every lead has their own
    assert any('[Your name]' in p for p in problems)
    page = env.client.get(f'/campaigns/campaigns/{campaign_id}/each').text
    assert 'still have text to fill in' in page and 'Replace in every email' in page

    env.post(f'/campaigns/{campaign_id}/each/replace', find='[Your name]', replace='Ibrahem')
    assert service.readiness(env.db, env.db.settings(), service.get_campaign(env.db, campaign_id)) == []
    assert 'Best,\nIbrahem' in env.db.val('SELECT body FROM personalizations WHERE campaign_id = ? LIMIT 1', campaign_id)

    service.send_test(env.db, env.cfg, env.db.settings(), campaign_id, 'me@example.com')   # the first lead's own email
    env.drain()
    test = next(m for m in env.catcher.messages if m['to'] == ['me@example.com'])
    assert parsed(test)['Subject'] == '[Test] Hi Ana' and 'Ibrahem' in body_of(test)
    empty = env.client.post('/campaigns/compose/preview', json={'text': ''}, headers={'X-CSRF-Token': env.csrf}).get_json()
    assert 'Your message appears here' in empty['html']
