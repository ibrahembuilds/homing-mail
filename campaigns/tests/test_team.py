"""Team sign-in: owners, admins and members, with Homing's own passwords or
behind the mail server's sign-in."""
import re

import pytest

from app import create_app, team
from app.config import Config
from tests.test_providers import FakeAPI

H = {'X-Proxy-Auth': 'proxy-secret'}


def csrf_of(client, path='/campaigns/', headers=None):
    page = client.get(path, headers=headers or {}).text
    found = re.search(r'name="csrf" (?:content|value)="([^"]+)"', page)
    return found.group(1) if found else ''


def sign_in(app, email, password):
    client = app.test_client()
    token = csrf_of(client, '/campaigns/login')
    response = client.post('/campaigns/login', data={'csrf': token, 'email': email, 'password': password})
    return client, response


@pytest.fixture
def app(tmp_path):
    cfg = Config({'CAMPAIGNS_DATA': str(tmp_path), 'CAMPAIGNS_ADMIN_PASSWORD': 'owner-pw-123', 'CAMPAIGNS_AUTH': 'password',
                  'PUBLIC_URL': 'https://mail.example.com', 'CAMPAIGNS_RSPAMD_URL': 'http://127.0.0.1:9'})
    application = create_app(cfg)
    yield application
    application.db.close()


def add_person(app, owner, email, role='member', name=''):
    token = csrf_of(owner)
    owner.post('/campaigns/team/add', data={'csrf': token, 'email': email, 'role': role, 'name': name})
    page = owner.get('/campaigns/team').text
    found = re.search(r'id="cred-pw">([^<]+)<', page)
    return found.group(1) if found else None


def test_owner_signs_in_and_adds_a_member_who_gets_daily_pages_only(app):
    _, wrong = sign_in(app, 'admin', 'nope')
    assert 'Wrong email or password.' in wrong.text
    owner, ok = sign_in(app, '', 'owner-pw-123')                 # the owner may leave the email empty
    assert ok.status_code == 302
    assert 'Team' in owner.get('/campaigns/').text
    password = add_person(app, owner, 'Sara@Example.com', name='Sara')
    assert password and len(password) == 12
    assert 'Send these to them privately' not in owner.get('/campaigns/team').text   # shown only once

    sara, ok = sign_in(app, 'sara@example.com', password)
    assert ok.status_code == 302
    home = sara.get('/campaigns/')
    assert home.status_code == 200 and 'Sara' in home.text and '>member' in home.text
    assert 'href="/campaigns/settings"' not in home.text and 'href="/campaigns/team"' not in home.text
    for page in ('/campaigns/leads', '/campaigns/campaigns', '/campaigns/inbox', '/campaigns/calendar',
                 '/campaigns/analytics', '/campaigns/agent', '/campaigns/sending-health'):
        assert sara.get(page, follow_redirects=True).status_code == 200, page
    for page in ('/campaigns/settings', '/campaigns/team', '/campaigns/domains', '/campaigns/accounts'):
        response = sara.get(page)
        assert response.status_code == 403 and 'For admins only' in response.text, page
    token = csrf_of(sara)
    assert sara.post('/campaigns/settings', data={'csrf': token, 'section': 'sender'}).status_code == 403
    assert sara.post('/campaigns/suppressions', data={'csrf': token, 'emails': 'x@example.com'}).status_code == 403
    assert sara.get('/campaigns/suppressions').status_code == 200
    assert app.db.val("SELECT last_seen_at FROM team WHERE email = 'sara@example.com'") is not None


def test_roles_cannot_lock_the_owner_out(app):
    owner, _ = sign_in(app, 'admin', 'owner-pw-123')
    admin_pw = add_person(app, owner, 'ali@example.com', role='admin')
    ali, _ = sign_in(app, 'ali@example.com', admin_pw)
    token = csrf_of(ali)
    ali.post('/campaigns/team/add', data={'csrf': token, 'email': 'boss@example.com', 'role': 'owner'})
    assert team.get(app.db, 'boss@example.com') is None              # admins cannot make owners
    ali.post('/campaigns/team/add', data={'csrf': token, 'email': 'mo@example.com', 'role': 'member'})
    assert team.get(app.db, 'mo@example.com')['role'] == 'member'    # but can add members
    ali.post('/campaigns/team/admin', data={'csrf': token, 'action': 'remove'})
    assert team.get(app.db, 'admin')['role'] == 'owner'               # nor remove an owner
    token = csrf_of(owner)
    owner.post('/campaigns/team/admin', data={'csrf': token, 'action': 'role', 'role': 'member'})
    assert team.get(app.db, 'admin')['role'] == 'owner'               # the last owner stays an owner
    owner.post('/campaigns/team/admin', data={'csrf': token, 'action': 'remove'})
    assert team.get(app.db, 'admin') is not None                       # and cannot remove themselves
    with pytest.raises(team.TeamError, match='at least one owner'):
        team.set_role(app.db, team.get(app.db, 'admin'), 'admin', 'admin')


def test_sign_out_removal_and_new_passwords_end_only_that_persons_sessions(app):
    owner, _ = sign_in(app, 'admin', 'owner-pw-123')
    first = add_person(app, owner, 'sara@example.com')
    sara, _ = sign_in(app, 'sara@example.com', first)
    sara_phone, _ = sign_in(app, 'sara@example.com', first)
    sara.post('/campaigns/logout', data={'csrf': csrf_of(sara)})
    assert sara_phone.get('/campaigns/').status_code == 302          # signed out on every device
    assert owner.get('/campaigns/').status_code == 200                # the owner is not affected

    sara, _ = sign_in(app, 'sara@example.com', first)
    owner.post('/campaigns/team/sara@example.com', data={'csrf': csrf_of(owner), 'action': 'password'})
    second = re.search(r'id="cred-pw">([^<]+)<', owner.get('/campaigns/team').text).group(1)
    assert sara.get('/campaigns/').status_code == 302                  # old session ended
    assert sign_in(app, 'sara@example.com', first)[1].status_code == 200     # old password refused
    sara, ok = sign_in(app, 'sara@example.com', second)
    assert ok.status_code == 302

    response = sara.post('/campaigns/me/password', data={'csrf': csrf_of(sara), 'current': second,
                                                           'new': 'a-much-longer-pass', 'confirm': 'a-much-longer-pass'})
    assert response.status_code == 302 and sara.get('/campaigns/').status_code == 200   # stays signed in here
    assert sign_in(app, 'sara@example.com', 'a-much-longer-pass')[1].status_code == 302

    owner.post('/campaigns/team/sara@example.com', data={'csrf': csrf_of(owner), 'action': 'remove'})
    assert sara.get('/campaigns/').status_code == 302
    assert sign_in(app, 'sara@example.com', 'a-much-longer-pass')[1].status_code == 200


@pytest.fixture
def proxy_app(tmp_path):
    api = FakeAPI([(200, {'code': 200, 'message': 'User sara@example.com has been created'}),
                   (409, {'code': 409, 'message': 'User old@example.com already exists'}),
                   (200, {'code': 200, 'message': 'User sara@example.com has been updated'})])
    cfg = Config({'CAMPAIGNS_DATA': str(tmp_path), 'CAMPAIGNS_AUTH': 'proxy', 'CAMPAIGNS_PROXY_SECRET': 'proxy-secret',
                  'HOSTNAMES': 'mail.example.com', 'DOMAIN': 'example.com', 'SECRET_KEY': 'k' * 16,
                  'INITIAL_ADMIN_ACCOUNT': 'admin', 'INITIAL_ADMIN_DOMAIN': 'example.com',
                  'API': 'true', 'API_TOKEN': 'mail-api-token', 'CAMPAIGNS_MAIL_API': api.url + '/api/v1'})
    application = create_app(cfg)
    yield application, api
    api.close()
    application.db.close()


def test_behind_the_mail_server_sign_in(proxy_app):
    app, api = proxy_app
    client = app.test_client()
    assert client.get('/campaigns/').status_code == 403                                     # no proof from the proxy
    assert client.get('/campaigns/', headers={'X-Proxy-Auth': 'wrong'}).status_code == 403
    assert client.get('/campaigns/', headers=H).status_code == 200                          # older proxy config: owner
    owner = dict(H, **{'X-Auth-User': 'admin@example.com'})
    assert 'Team' in client.get('/campaigns/', headers=owner).text
    stranger = client.get('/campaigns/', headers=dict(H, **{'X-Auth-User': 'random@example.com'}))
    assert stranger.status_code == 403 and 'random@example.com' in stranger.text and 'No access yet' in stranger.text

    token = csrf_of(client, headers=owner)
    client.post('/campaigns/team/add', headers=owner, data={'csrf': token, 'email': 'sara@example.com', 'name': 'Sara',
                                                              'role': 'member', 'create_mailbox': 'on'})
    created = api.requests[0]
    assert created['path'] == '/api/v1/user' and created['headers']['authorization'] == 'Bearer mail-api-token'
    assert created['body']['email'] == 'sara@example.com' and len(created['body']['raw_password']) == 12
    page = client.get('/campaigns/team', headers=owner).text
    assert created['body']['raw_password'] in page and 'A mailbox was created for them' in page

    client.post('/campaigns/team/add', headers=owner, data={'csrf': token, 'email': 'old@example.com',
                                                              'role': 'admin', 'create_mailbox': 'on'})
    assert 'already have a mailbox' in client.get('/campaigns/team', headers=owner).text
    assert team.get(app.db, 'old@example.com')['role'] == 'admin'

    member = dict(H, **{'X-Auth-User': 'sara@example.com'})
    assert client.get('/campaigns/leads', headers=member).status_code == 200
    assert client.get('/campaigns/settings', headers=member).status_code == 403

    client.post('/campaigns/team/sara@example.com', headers=owner, data={'csrf': token, 'action': 'password'})
    reset = api.requests[2]
    assert reset['path'] == '/api/v1/user/sara@example.com' and 'raw_password' in reset['body']
    out = client.post('/campaigns/logout', headers=owner, data={'csrf': token})
    assert out.status_code == 302 and out.headers['Location'] == '/sso/logout'
