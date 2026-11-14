"""Address checks before sending, subject line tests, the sequence builder and
the first-email-to-meeting funnel."""
import time

from app import agent, sender, service, stats
from tests.test_e2e import env, make_list, parsed  # noqa: F401  (env is a fixture)


def campaign_for(env, list_id, **extra):
    fields = {'name': 'Outreach', 'subject': 'Subject A', 'html': '<p>Hi {{first_name|there}}</p>',
              'list_id': list_id, 'rate_per_hour': 100000}
    fields.update(extra)
    return service.create_campaign(env.db, env.db.settings(), fields)


def sent_to(env):
    return sorted(m['to'][0] for m in env.catcher.messages)


# -- address checks -----------------------------------------------------------------------
def test_addresses_are_checked_when_the_campaign_starts(env):
    list_id = make_list(env, csv_text='email\nana@acme.test\nbob@gmial.com\nzed@nomail.test\n')
    assert env.db.val("SELECT COUNT(*) FROM contacts WHERE verify_status = ''") == 3

    page = env.client.get(f'/campaigns/campaigns/{campaign_for(env, list_id)}').text
    assert '3 new addresses now' in page and 'Also skip risky addresses' in page

    campaign_id = campaign_for(env, list_id, skip_risky=True)
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    env.drain()
    statuses = {r['email']: (r['verify_status'], r['verify_detail']) for r in env.db.q('SELECT * FROM contacts')}
    assert statuses['bob@gmial.com'] == ('risky', 'Possible typo: did you mean gmail.com?')
    assert statuses['zed@nomail.test'][0] == 'invalid'
    assert sent_to(env) == ['ana@acme.test']                       # risky skipped on request, invalid always

    env.catcher.messages.clear()
    service.schedule_campaign(env.db, env.db.settings(), campaign_for(env, list_id))
    env.drain()
    assert sent_to(env) == ['ana@acme.test', 'bob@gmial.com']


def test_dns_down_does_not_hold_up_sending(env, monkeypatch):
    import dns.exception
    from app import leadtools
    lookups = []

    class Down:
        def resolve(self, domain, kind):
            lookups.append(domain)
            raise dns.exception.Timeout()
    monkeypatch.setattr(leadtools, '_resolver', Down)
    list_id = make_list(env, csv_text='email\n' + '\n'.join(f'x@d{i}.test' for i in range(30)) + '\n')
    assert leadtools.verify_list(env.db, list_id) == {'ok': 0, 'invalid': 0, 'risky': 0, 'unknown': 30}
    assert lookups == ['gmail.com']                                 # one probe, not 30 timeouts
    assert leadtools.verify_list(env.db, list_id) is None           # retried after a day, not on every start


def test_check_button_on_the_campaign_page(env):
    list_id = make_list(env, csv_text='email\nana@acme.test\nzed@nomail.test\n')
    campaign_id = campaign_for(env, list_id)
    response = env.post(f'/campaigns/{campaign_id}/save', action='verify_list', name='Outreach', subject='Subject A',
                        html='<p>Hi</p>', list_id=list_id, ai_mode='')
    assert response.status_code == 302
    page = env.client.get(response.headers['Location']).text
    assert 'Checked 2 addresses: 1 would bounce' in page
    assert env.db.val("SELECT COUNT(*) FROM contacts WHERE verify_status = ''") == 0


# -- subject line test ---------------------------------------------------------------------
def test_subject_test_sends_the_winner_to_the_rest(env):
    rows = '\n'.join(f'lead{i}@acme.test' for i in range(10))
    list_id = make_list(env, csv_text='email\n' + rows + '\n')
    campaign_id = campaign_for(env, list_id, subject_b='Subject B', ab_percent=40, ab_wait_hours=4)
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    assert env.drain() == 4                                        # 40% of 10, two each
    subjects = sorted(parsed(m)['Subject'] for m in env.catcher.messages)
    assert subjects == ['Subject A', 'Subject A', 'Subject B', 'Subject B']
    campaign = service.get_campaign(env.db, campaign_id)
    assert campaign['ab_winner'] == '' and 'Testing two subject lines. The other 6 leads' in campaign['hold_reason']
    page = env.client.get(f'/campaigns/campaigns/{campaign_id}').text
    assert 'Subject line test' in page and 'leading' in page

    # B gets a reply; the wait passes.
    env.db.run("UPDATE messages SET replied_at = ? WHERE id = (SELECT id FROM messages WHERE variant = 'b' LIMIT 1)",
               time.time())
    env.db.run("UPDATE messages SET sent_at = sent_at - 5 * 3600 WHERE campaign_id = ?", campaign_id)
    env.catcher.messages.clear()
    assert env.drain() == 6
    assert {parsed(m)['Subject'] for m in env.catcher.messages} == {'Subject B'}
    campaign = service.get_campaign(env.db, campaign_id)
    assert campaign['ab_winner'] == 'b' and campaign['status'] == 'sent'
    assert sender.ab_results(env.db, campaign_id)['b']['reply_rate'] == 50.0
    assert 'winner' in env.client.get(f'/campaigns/campaigns/{campaign_id}').text


def test_small_lists_test_everyone_and_full_ai_mode_skips_the_test(env):
    list_id = make_list(env, csv_text='email\na@acme.test\nb@acme.test\nc@acme.test\n')
    campaign_id = campaign_for(env, list_id, subject_b='Subject B')
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    assert env.drain() == 3
    assert service.get_campaign(env.db, campaign_id)['status'] == 'sent'
    full = service.get_campaign(env.db, campaign_for(env, list_id, subject_b='Other', ai_mode='full'))
    assert not sender.ab_active(full)


# -- sequence -------------------------------------------------------------------------------
def test_sequence_templates_conditions_and_timeline(env):
    list_id = make_list(env, csv_text='email\nana@acme.test\nbob@acme.test\n')
    campaign_id = campaign_for(env, list_id)
    env.post(f'/campaigns/{campaign_id}/save', action='add_step:opened', name='Outreach', subject='Subject A',
             html='<p>Hi</p>', list_id=list_id, ai_mode='')
    campaign = service.get_campaign(env.db, campaign_id)
    assert 'Follow-up 1 depends on opens: turn on open tracking.' in service.readiness(env.db, env.db.settings(), campaign)
    env.post(f'/campaigns/{campaign_id}/save', action='add_step:value', name='Outreach', subject='Subject A',
             html='<p>Hi</p>', list_id=list_id, ai_mode='', track_opens='1')
    steps = env.db.q('SELECT * FROM campaign_steps WHERE campaign_id = ? ORDER BY position', campaign_id)
    assert [(s['send_if'], s['delay_days']) for s in steps] == [('opened_no_reply', 2), ('no_reply', 4)]
    campaign = service.get_campaign(env.db, campaign_id)
    assert 'Follow-up 2 still has a [placeholder] to fill in.' in service.readiness(env.db, env.db.settings(), campaign)
    page = env.client.get(f'/campaigns/campaigns/{campaign_id}').text
    assert 'data-sequence' in page and 'day 2' in page and 'day 6' in page and '3 emails over 6 days' in page

    service.delete_step(env.db, campaign_id, 2)
    assert 'Follow-up 1 still has the example text "Your name". Replace it with your own.' in service.readiness(
        env.db, env.db.settings(), service.get_campaign(env.db, campaign_id))
    service.update_step(env.db, campaign_id, 1, {'html': '<p>Worth a quick chat?</p><p>Thanks,<br>Sam</p>'})
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    assert env.drain() == 2
    env.db.run("UPDATE messages SET sent_at = sent_at - 3 * 86400, opened_at = ? WHERE to_email = 'ana@acme.test'",
               time.time())
    env.db.run("UPDATE messages SET sent_at = sent_at - 3 * 86400 WHERE to_email = 'bob@acme.test'")
    step = env.db.one('SELECT * FROM campaign_steps WHERE campaign_id = ?', campaign_id)
    assert [r['to_email'] for r in sender.follow_up_candidates(env.db, campaign_id, step)] == ['ana@acme.test']

    # Bob has not opened yet: once Ana's follow-up is out the campaign stays open for him.
    env.drain()
    campaign = service.get_campaign(env.db, campaign_id)
    assert campaign['status'] == 'sending' and 'leads who open the earlier email' in campaign['hold_reason']
    env.db.run("UPDATE messages SET sent_at = sent_at - ? WHERE to_email = 'bob@acme.test'",
               sender.OPEN_WAIT_DAYS * 86400)
    env.engine.tick()
    assert service.get_campaign(env.db, campaign_id)['status'] == 'sent'


def test_agent_can_set_a_subject_test(env):
    list_id = make_list(env, csv_text='email\nana@acme.test\n')
    out, is_error = agent.Toolbox(env.db, env.cfg).run('create_campaign', {
        'name': 'Agent', 'subject': 'A', 'subject_b': 'B', 'ab_percent': 30, 'skip_risky': True,
        'html': '<p>Hi</p>', 'list_id': list_id})
    assert not is_error
    row = env.db.one('SELECT * FROM campaigns WHERE name = ?', 'Agent')
    assert (row['subject_b'], row['ab_percent'], row['skip_risky']) == ('B', 30, 1)


# -- funnel ---------------------------------------------------------------------------------
def test_funnel_from_first_email_to_meeting(env):
    rows = '\n'.join(f'lead{i}@acme.test' for i in range(5))
    list_id = make_list(env, csv_text='email\n' + rows + '\n')
    campaign_id = campaign_for(env, list_id)
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    assert env.drain() == 5
    db = env.db
    db.run("UPDATE messages SET opened_at = ? WHERE to_email IN ('lead0@acme.test', 'lead1@acme.test')", time.time())
    db.run("UPDATE messages SET replied_at = ? WHERE to_email IN ('lead1@acme.test', 'lead2@acme.test')", time.time())
    contact = db.val("SELECT id FROM contacts WHERE email = 'lead2@acme.test'")
    service.set_lead_status(db, contact, 'meeting')

    stages = {s['key']: s for s in stats.funnel(db, campaign_id=campaign_id)}
    assert [stages[k]['count'] for k in ('contacted', 'opened', 'replied', 'interested', 'meeting')] == [5, 3, 2, 1, 1]
    assert stages['replied']['of_previous'] == 66.7 and stages['meeting']['of_total'] == 20.0

    page = env.client.get('/campaigns/analytics').text
    assert 'From first email to meeting' in page and 'Meeting booked' in page and 'Reply rate' in page
    assert 'From first email to meeting' in env.client.get(f'/campaigns/campaigns/{campaign_id}').text
