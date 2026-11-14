"""Problems found by the multi-agent review: stalled campaigns, miscounted
results, deletions that went too far, false typo warnings and missing headers."""
import io
import time

import pytest

from app import leadtools, service, stats
from tests.test_e2e import env, make_list  # noqa: F401  (env is a fixture)
from tests.test_improvements import campaign_for, sent_to


def test_subject_test_does_not_stall_when_no_test_email_went_out(env):
    rows = '\n'.join(f'lead{i}@acme.test' for i in range(10))
    campaign_id = campaign_for(env, make_list(env, csv_text='email\n' + rows + '\n'), subject_b='B', ab_percent=40)
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    env.engine.tick()
    env.db.run("UPDATE messages SET status = 'failed', sent_at = NULL WHERE campaign_id = ? AND variant IN ('a', 'b')",
               campaign_id)
    env.catcher.messages.clear()
    assert env.drain() == 6
    campaign = service.get_campaign(env.db, campaign_id)
    assert campaign['ab_winner'] == 'a' and campaign['status'] == 'sent'


def test_subject_test_settings_are_kept_and_checked(env):
    list_id = make_list(env, csv_text='email\nana@acme.test\n')
    assert service.get_campaign(env.db, campaign_for(env, list_id, ab_wait_hours=0))['ab_wait_hours'] == 0
    assert service.get_campaign(env.db, campaign_for(env, list_id, ab_wait_hours=''))['ab_wait_hours'] == 4
    for bad in ('nan', 'inf', 'soon'):
        with pytest.raises(service.Problem):
            campaign_for(env, list_id, ab_wait_hours=bad)


def test_bounces_refused_at_send_time_are_not_counted_twice(env):
    campaign_id = campaign_for(env, make_list(env, csv_text='email\na@acme.test\nb@acme.test\n'))
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    env.drain()
    env.db.run("UPDATE messages SET sent_at = NULL, bounced_at = ?, status = 'bounced' WHERE to_email = 'b@acme.test'",
               time.time())
    result = stats.campaign(env.db, campaign_id)
    assert (result['sent'], result['bounced'], result['delivered']) == (2, 1, 1)


def test_funnel_for_one_campaign_ignores_other_campaigns(env):
    list_id = make_list(env, csv_text='email\nana@acme.test\n')
    first, second = campaign_for(env, list_id), campaign_for(env, list_id, name='Second')
    for cid in (first, second):
        service.schedule_campaign(env.db, env.db.settings(), cid)
        env.drain()
    env.db.run('UPDATE messages SET replied_at = ? WHERE campaign_id = ?', time.time(), first)
    service.set_lead_status(env.db, env.db.val('SELECT id FROM contacts'), 'meeting')
    meetings = {cid: stats.funnel(env.db, campaign_id=cid)[-1]['count'] for cid in (first, second)}
    assert meetings == {first: 1, second: 0}
    assert stats.funnel(env.db)[-1]['count'] == 1


def test_follow_up_deletion_guards(env):
    campaign_id = campaign_for(env, make_list(env, csv_text='email\nana@acme.test\n'))
    service.add_step(env.db, campaign_id)
    with pytest.raises(service.Problem):
        service.delete_step(env.db, campaign_id, 0)               # would cancel every first email
    response = env.post(f'/campaigns/{campaign_id}/save', action='delete_step_abc', name='Outreach',
                        subject='Subject A', html='<p>Hi</p>')
    assert response.status_code in (302, 400)
    assert env.db.val('SELECT COUNT(*) FROM campaign_steps WHERE campaign_id = ?', campaign_id) == 1
    # A step that was only ever queued and then cancelled can still go.
    env.db.run("INSERT INTO messages (id, campaign_id, contact_id, kind, to_email, to_domain, from_email, subject, "
               "status, step, created_at) SELECT 'x1', ?, id, 'campaign', email, 'acme.test', '', '', 'cancelled', 1, 0 "
               "FROM contacts", campaign_id)
    service.delete_step(env.db, campaign_id, 1)
    assert not env.db.val('SELECT COUNT(*) FROM campaign_steps WHERE campaign_id = ?', campaign_id)


def test_leads_added_after_the_start_get_the_first_email(env):
    list_id = make_list(env, csv_text='email\nana@acme.test\n')
    campaign_id = campaign_for(env, list_id)
    service.add_step(env.db, campaign_id, {'html': '<p>Following up</p>', 'delay_days': 3})
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    env.drain()
    assert sent_to(env) == ['ana@acme.test']
    env.client.post(f'/campaigns/lists/{list_id}/import', data={
        'csrf': env.csrf, 'consent': 'yes', 'file': (io.BytesIO(b'email\nbob@acme.test\n'), 'more.csv')},
        content_type='multipart/form-data')
    env.drain()
    assert sent_to(env) == ['ana@acme.test', 'bob@acme.test']


def test_impossible_follow_up_order_is_flagged(env):
    campaign_id = campaign_for(env, make_list(env, csv_text='email\nana@acme.test\n'), track_opens=1)
    service.add_step(env.db, campaign_id, {'send_if': 'opened_no_reply'})
    service.add_step(env.db, campaign_id, {'send_if': 'no_reply_no_open'})
    problems = service.readiness(env.db, env.db.settings(), service.get_campaign(env.db, campaign_id))
    assert any('nobody would get it' in p for p in problems)


def test_a_list_nobody_can_be_emailed_on_is_not_sent(env):
    list_id = make_list(env, csv_text='email\na@nomail.test\nb@nomail.test\n')
    campaign_id = campaign_for(env, list_id)
    leadtools.verify_list(env.db, list_id)
    problems = service.readiness(env.db, env.db.settings(), service.get_campaign(env.db, campaign_id))
    assert 'None of the 2 addresses on the list can be emailed: 2 would bounce.' in problems


def test_merge_tags_empty_for_some_leads_need_a_fallback(env):
    list_id = make_list(env, csv_text='email,company\na@acme.test,Acme\nb@acme.test,\n')
    campaign_id = campaign_for(env, list_id, subject='Quick question about {{company}}',
                               html='<p>Hi {{first_name|there}}, {{company|your team}}</p>')
    problems = service.readiness(env.db, env.db.settings(), service.get_campaign(env.db, campaign_id))
    assert ('{{company}} is empty for 1 of 2 leads, so they would see a gap. '
            'Add a fallback, like {{company|your team}}.') in problems
    assert not any('first_name' in p for p in problems)


@pytest.mark.parametrize('domain, expected', [
    ('gmial.com', 'gmail.com'), ('gmai.com', 'gmail.com'), ('gmail.co', 'gmail.com'), ('gmail.cm', 'gmail.com'),
    ('hotmial.com', 'hotmail.com'), ('yahooo.com', 'yahoo.com'), ('outlok.com', 'outlook.com'),
    ('ymail.com', ''), ('gmx.net', ''), ('acme.com', ''), ('ge.com', ''), ('outlook.de', ''), ('hotmart.com', ''),
    ('email.com', ''), ('gmail.com', ''),
])
def test_typo_suggestions(domain, expected):
    assert leadtools.typo_suggestion(domain) == expected


@pytest.mark.parametrize('html, flagged', [
    ('<p>[your tip here]</p>', True), ('<p>Hi [Name],</p>', True), ('<p>[Company] is great</p>', True),
    ('<p>[TODO: link]</p>', True), ('<p>See [a] and [an]</p>', False), ('<p>Prices [as of May]</p>', False),
    ('<p>[Additional info] below</p>', False),
])
def test_placeholder_detection(html, flagged):
    assert bool(service.PLACEHOLDER_RE.search(html)) is flagged


def test_pages_send_a_content_security_policy_and_health_hides_the_version(env):
    page = env.client.get('/campaigns/')
    assert "script-src 'self'" in page.headers['Content-Security-Policy']
    assert env.client.get('/campaigns/health').get_json() == {'status': 'ok'}
    assert '<script>' not in env.client.get('/campaigns/calendar').text


@pytest.mark.parametrize('name', ['A' * 70 + ' <ceo@bank.example>', 'Sam "x" <ceo@bank.example>', 'Sam', 'Zoë Müller',
                                  'B' * 120, 'Line\r\nBcc: x@evil.example'])
def test_a_display_name_cannot_fake_the_sender(name):
    import email
    import email.policy
    import email.utils
    from app.render import build_message
    raw = build_message(mid='m1', from_email='me@example.com', from_name=name, to=['a@acme.test'],
                        subject='s', html='<p>x</p>').as_bytes()
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    assert email.utils.parseaddr(str(parsed['From']))[1] == 'me@example.com'
    assert parsed['Bcc'] is None


def test_exported_lists_cannot_run_formulas(env):
    list_id = make_list(env, csv_text='Email,First Name,Last Name\nx@acme.test,=cmd|\' /C calc\'!A0,+1\n')
    body = env.client.get(f'/campaigns/lists/{list_id}/export.csv').text
    assert "'=cmd" in body and ",'+1," in body


def test_bad_uploads_and_encodings(env):
    from app import contacts
    rows, _ = contacts.parse_csv('email,first_name\nana@acme.test,José\n'.encode('cp1252'))
    assert rows[0]['first_name'] == 'José'
    rows, _ = contacts.parse_csv(b'email\nana@acme.test\rbob@acme.test\n')
    assert [r['email'] for r in rows] == ['ana@acme.test', 'bob@acme.test']
    with pytest.raises(ValueError):
        contacts.parse_csv(b'email,x\na@acme.test,"' + b'y' * 200_000 + b'"\n')


def test_extreme_numbers_and_dates_do_not_crash_pages(env):
    huge = '9' * 25
    assert env.client.get(f'/campaigns/campaigns/{huge}').status_code in (302, 404)
    assert env.client.get(f'/campaigns/emails?page={huge}').status_code in (200, 302, 404)
    assert env.client.get('/campaigns/calendar?month=9999-12').status_code in (200, 302)
    assert env.post('/tasks', title='x', date='0001-01-01', time='00:00').status_code in (302, 400)
    assert env.post('/tasks', title='y', date='9999-12-31', time='00:00').status_code in (302, 400)
    assert env.client.get('/campaigns/').status_code == 200
    assert env.client.get('/campaigns/calendar').status_code == 200
    list_id = make_list(env, csv_text='email\nana@acme.test\n')
    campaign_id = campaign_for(env, list_id)
    for field in ('ab_percent', 'rate_per_hour', 'daily_cap', 'list_id'):
        response = env.post(f'/campaigns/{campaign_id}/save', name='Outreach', subject='S', html='<p>Hi</p>',
                            **{field: 'inf'})
        assert response.status_code in (302, 400), field


def test_logging_out_ends_copied_sessions(env):
    cookie = env.client.get_cookie('campaigns_session', path='/campaigns')
    stolen = env.app.test_client()
    stolen.set_cookie(cookie.key, cookie.value, path='/campaigns')
    assert stolen.get('/campaigns/').status_code == 200
    env.post('/logout')
    assert stolen.get('/campaigns/').status_code == 302


def test_javascript_links_from_lead_data_are_neutralised():
    from app.render import safe_links
    html = safe_links('<a href="javascript:alert(1)">x</a> <a href=" JaVaScRiPt:1">y</a> <a href="https://ok.example">z</a>')
    assert 'javascript' not in html.lower() and 'https://ok.example' in html


def test_many_domains_are_checked_without_blocking(env):
    rows = '\n'.join(f'x@d{i}.test' for i in range(300))
    list_id = make_list(env, csv_text='email\n' + rows + '\n')
    started = time.time()
    counts = leadtools.verify_list(env.db, list_id)
    assert counts['ok'] == 300 and time.time() - started < 5
    assert env.db.val('SELECT COUNT(*) FROM domain_checks') == 300
