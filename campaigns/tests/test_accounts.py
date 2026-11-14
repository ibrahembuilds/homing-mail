"""Email accounts: connecting mailboxes, rotating a campaign between them,
daily limits per account, follow-ups from the same account, and an account
whose server refuses the sign-in. Every account points at the SMTP catcher,
which checks the username and password."""
import time

from app import mailboxes, service
from tests.test_e2e import env, make_campaign, make_list, parsed  # noqa: F401  (env is a fixture)


def add_account(e, address, password, limit=30, **extra):
    fields = {'provider': 'other', 'email': address, 'smtp_host': '127.0.0.1', 'smtp_port': e.catcher.port,
              'smtp_tls': 'none', 'smtp_password': password, 'daily_limit': limit, 'imap_host': ''}
    fields.update(extra)
    response = e.post('/accounts', **fields)
    assert response.status_code == 302
    return e.db.val('SELECT id FROM mailboxes WHERE email = ?', address)


def sent_from(e):
    return {m['to'][0]: (parsed(m)['From'], m['from'], m['user']) for m in e.catcher.messages}


def test_campaign_rotates_accounts_within_daily_limits(env):
    env.catcher.passwords = {'sam@send-one.test': 'pw-one', 'kim@send-two.test': 'pw-two'}
    one = add_account(env, 'sam@send-one.test', 'pw-one', limit=2, from_name='Sam Lee')
    two = add_account(env, 'kim@send-two.test', 'pw-two', limit=1)
    assert env.db.val('SELECT status FROM mailboxes WHERE id = ?', one) == 'ok'   # signed in when added
    assert env.catcher.messages == []                                              # nothing was sent to check
    page = env.client.get('/campaigns/accounts').text
    assert 'sam@send-one.test' in page and '0</span> / 2 today' in page and 'Staying out of spam' in page

    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id, mailbox_ids=[one, two])
    settings = env.db.settings()
    assert service.get_campaign(env.db, campaign_id)['mailbox_ids'] == f'{one},{two}'
    service.schedule_campaign(env.db, settings, campaign_id)
    assert env.drain() == 3                                  # 2 + 1: the fourth lead waits for tomorrow
    hold = env.db.val('SELECT hold_reason FROM campaigns WHERE id = ?', campaign_id)
    assert hold.startswith('All email accounts reached their daily limits (3 emails today)')

    sent = sent_from(env)
    users = sorted(user for _, _, user in sent.values())
    assert users == ['kim@send-two.test', 'sam@send-one.test', 'sam@send-one.test']
    for header, envelope, user in sent.values():
        assert envelope == user and user in header            # From, return path and sign-in all match
        if user == 'sam@send-one.test':
            assert header.startswith('Sam Lee')
    rows = env.db.q("SELECT mailbox_id, from_email FROM messages WHERE campaign_id = ? AND status = 'sent'",
                    campaign_id)
    assert sorted(r['mailbox_id'] for r in rows) == sorted([one, one, two])
    assert mailboxes.sent_since(env.db, 0) == {one: 2, two: 1}

    # Tomorrow the accounts have room again.
    env.db.run("UPDATE messages SET sent_at = sent_at - 86400 WHERE status = 'sent'")
    assert env.drain() == 1
    assert env.db.val("SELECT COUNT(*) FROM messages WHERE campaign_id = ? AND status = 'queued'", campaign_id) == 0


def test_follow_up_leaves_from_the_account_that_wrote_first(env):
    first = add_account(env, 'sam@send-one.test', 'pw')
    second = add_account(env, 'kim@send-two.test', 'pw')
    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id, mailbox_ids=f'{first},{second}')
    service.add_step(env.db, campaign_id, {'delay_days': 1, 'html': '<p>Just following up.</p>'})
    env.engine.follow_up_interval = 0
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    assert env.drain() == 4
    env.db.run('UPDATE messages SET sent_at = sent_at - 1.1 * 86400')
    assert env.drain() == 4
    by_lead = {}
    for m in env.catcher.messages:
        by_lead.setdefault(m['to'][0], []).append(m)
    assert len(by_lead) == 4
    for lead, (opening, follow_up) in by_lead.items():
        assert parsed(follow_up)['Subject'].startswith('Re: ')
        assert opening['user'] == follow_up['user'], lead
        assert parsed(opening)['From'] == parsed(follow_up)['From']
        assert parsed(follow_up)['In-Reply-To'] == parsed(opening)['Message-ID']
    assert {m['user'] for m in env.catcher.messages} == {'sam@send-one.test', 'kim@send-two.test'}


def test_account_with_wrong_password_is_skipped(env):
    env.catcher.passwords = {'good@send-one.test': 'right'}
    good = add_account(env, 'good@send-one.test', 'right')
    bad = add_account(env, 'bad@send-two.test', 'wrong')
    bad_row = mailboxes.get(env.db, bad)
    assert bad_row['status'] == 'error' and '535' in bad_row['last_error']
    assert 'Username and Password not accepted' in env.client.get('/campaigns/accounts').text

    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id, mailbox_ids=[good, bad])
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    assert env.drain() == 4                                   # everything leaves from the working account
    assert {m['user'] for m in env.catcher.messages} == {'good@send-one.test'}
    assert mailboxes.get(env.db, bad)['retry_at'] > time.time()
    assert env.db.val('SELECT COUNT(*) FROM messages WHERE mailbox_id = ?', bad) == 0

    # Fixing the password and testing again brings it back.
    env.catcher.passwords['bad@send-two.test'] = 'fixed'
    env.post(f'/accounts/{bad}', action='save', smtp_password='fixed')
    fixed = mailboxes.get(env.db, bad)
    assert fixed['status'] == 'ok' and fixed['retry_at'] == 0 and fixed['smtp_password'] == 'fixed'


def test_campaign_page_picks_accounts_and_tests_send_from_them(env):
    account = add_account(env, 'sam@send-one.test', 'pw', from_name='Sam')
    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id)
    page = env.client.get(f'/campaigns/campaigns/{campaign_id}').text
    assert 'name="mailbox_ids"' in page and 'sam@send-one.test' in page
    env.post(f'/campaigns/{campaign_id}/save', name='Launch', subject='Hi', html='<p>Hello</p>',
             list_id=list_id, accounts_shown='1', mailbox_ids=str(account))
    assert service.get_campaign(env.db, campaign_id)['mailbox_ids'] == str(account)

    # A test email goes out through the account, as the real ones will.
    service.send_test(env.db, env.cfg, env.db.settings(), campaign_id, 'me@example.com')
    env.engine.tick()
    test = env.catcher.messages[-1]
    assert test['user'] == 'sam@send-one.test' and 'sam@send-one.test' in parsed(test)['From']

    # Pausing the only account blocks the launch with a clear reason.
    env.post(f'/accounts/{account}', action='pause')
    problems = service.readiness(env.db, env.db.settings(), service.get_campaign(env.db, campaign_id))
    assert 'None of the email accounts chosen to send from is active.' in problems

    # Unticking every account goes back to the campaign's own From address.
    env.post(f'/campaigns/{campaign_id}/save', name='Launch', subject='Hi', html='<p>Hello</p>',
             list_id=list_id, accounts_shown='1')
    assert service.get_campaign(env.db, campaign_id)['mailbox_ids'] == ''

    # Removing an account takes it out of campaigns.
    service.update_campaign(env.db, env.db.settings(), campaign_id, {'mailbox_ids': [account]})
    env.post(f'/accounts/{account}', action='delete')
    assert env.db.val('SELECT COUNT(*) FROM mailboxes') == 0
    assert service.get_campaign(env.db, campaign_id)['mailbox_ids'] == ''


def test_replies_are_read_from_the_account_and_answered_from_it(env, monkeypatch):
    from app import replies
    from tests.test_outreach import FakeIMAP, raw_reply
    account = add_account(env, 'sam@send-one.test', 'pw', imap_host='imap.send-one.test')
    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id, mailbox_ids=[account])
    service.add_step(env.db, campaign_id, {'delay_days': 1, 'html': '<p>Bump</p>'})
    env.engine.follow_up_interval = 0
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    assert env.drain() == 4
    first = env.db.val("SELECT id FROM messages WHERE to_email = 'ana@example.com' AND step = 0")
    FakeIMAP.store = [raw_reply('Ana Lopez <ana@example.com>', first, 'Yes, tell me more.'),
                      raw_reply('Sam <sam@send-one.test>', None, 'my own sent copy')]
    FakeIMAP.calls = []
    monkeypatch.setattr(replies.imaplib, 'IMAP4_SSL', FakeIMAP)
    result = mailboxes.poll(env.db, env.cfg, env.db.settings())
    assert result == {'accounts': 1, 'replies': 1, 'errors': []}
    assert ('connect', 'imap.send-one.test', 993) in FakeIMAP.calls and ('login', 'sam@send-one.test') in FakeIMAP.calls
    reply = env.db.one("SELECT * FROM replies WHERE from_email = 'ana@example.com'")
    assert reply['mailbox'] == 'sam@send-one.test' and reply['campaign_id'] == campaign_id
    assert env.db.val('SELECT COUNT(*) FROM replies') == 1                       # our own address is skipped
    assert 'Yes, tell me more.' in env.client.get('/campaigns/inbox').text

    # The follow-up to Ana is stopped, the others still go out.
    env.db.run('UPDATE messages SET sent_at = sent_at - 1.1 * 86400')
    env.catcher.messages.clear()
    assert env.drain() == 3 and 'ana@example.com' not in {m['to'][0] for m in env.catcher.messages}

    # Answering from the inbox leaves through the same account, in the same thread.
    env.catcher.messages.clear()
    service.reply_to_lead(env.db, env.cfg, env.db.settings(), reply, 'Great, here are the details.')
    env.engine.tick()
    answer = env.catcher.messages[-1]
    assert answer['user'] == 'sam@send-one.test' and answer['to'] == ['ana@example.com']
    assert 'sam@send-one.test' in parsed(answer)['From'] and parsed(answer)['Subject'].startswith('Re: ')


def test_account_details_are_checked(env):
    assert env.post('/accounts', provider='google', email='me@gmail.com', smtp_password='').status_code == 302
    assert env.db.val('SELECT COUNT(*) FROM mailboxes') == 0          # an app password is required
    fields = mailboxes.clean({'provider': 'google', 'email': 'Me@Gmail.com', 'smtp_password': 'abcd efgh ijkl mnop'})
    assert fields['smtp_host'] == 'smtp.gmail.com' and fields['smtp_port'] == 587 and fields['smtp_tls'] == 'starttls'
    assert fields['imap_host'] == 'imap.gmail.com' and fields['smtp_password'] == 'abcdefghijklmnop'
    assert fields['smtp_user'] == fields['imap_user'] == 'me@gmail.com'
    assert mailboxes.is_free('me@gmail.com') and not mailboxes.is_free('sam@acme.com')
    here = mailboxes.clean({'provider': 'server', 'email': 'sam@acme.com'})
    assert here['smtp_host'] == '' and 'smtp_password' not in here               # this server needs no password
    add_account(env, 'sam@send-one.test', 'pw')
    assert env.post('/accounts', provider='other', email='SAM@send-one.test', smtp_host='x',
                    smtp_password='pw').status_code == 302
    assert env.db.val('SELECT COUNT(*) FROM mailboxes') == 1                  # no duplicates
