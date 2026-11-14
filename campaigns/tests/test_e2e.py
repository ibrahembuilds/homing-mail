"""End-to-end tests. They drive the real application against a local SMTP
catcher, so every step from CSV upload to analytics is exercised.

    python -m pytest tests -q
"""
import email
import hashlib
import hmac
import http.server
import io
import json
import re
import threading
import time

import pytest

from app import agent, bounces, contacts, create_app, events, service, spamcheck, stats
from app import ai_anthropic, ai_openrouter
from app.config import Config
from app.sender import Engine
from tests.mailcatcher import MailCatcher

CSV = """Email,First Name,Last Name,Company
ana@example.com,Ana,Lopez,Acme
BOB@Example.com,Bob,,Globex
bob@example.com,Bobby,,Duplicate
not-an-email,Bad,Row,None
postmaster@example.com,Post,Master,System
carol@example.org,Carol,King,Initech
dave@example.net,Dave,Jones,Umbrella
"""

HTML = ('<h1>Hello {{first_name|there}}</h1><p>News for {{company}}.</p>'
        '<p><a href="https://shop.example.com/offer?x=1&amp;y=2">See the offer</a> or '
        '<a href="mailto:hi@example.com">write to us</a></p>')


@pytest.fixture
def env(tmp_path):
    catcher = MailCatcher().start()
    cfg = Config({
        'CAMPAIGNS_DATA': str(tmp_path), 'CAMPAIGNS_ADMIN_PASSWORD': 'secret-pw', 'CAMPAIGNS_AUTH': 'password',
        'PUBLIC_URL': 'https://mail.example.com', 'CAMPAIGNS_SMTP_HOST': '127.0.0.1',
        'CAMPAIGNS_SMTP_PORT': str(catcher.port), 'CAMPAIGNS_RSPAMD_URL': 'http://127.0.0.1:9',
        'BRAND_NAME': 'TestBrand',
    })
    app = create_app(cfg)
    app.config['TESTING'] = True
    client = app.test_client()
    page = client.get('/campaigns/login')
    token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    assert client.post('/campaigns/login', data={'csrf': token, 'password': 'secret-pw'}).status_code == 302
    # Signing in starts a fresh session, so the form token changes.
    token = re.search(r'name="csrf" content="([^"]+)"', client.get('/campaigns/').text).group(1)

    class Env:
        pass
    e = Env()
    e.app, e.db, e.cfg, e.client, e.catcher, e.csrf = app, app.db, cfg, client, catcher, token
    e.engine = Engine(cfg, app.db)
    e.db.set_setting('from_email', 'news@example.com')
    e.db.set_setting('from_name', 'Test Sender')
    e.db.set_setting('postal_address', '1 Test Street, Testville')

    def post(path, **data):
        data['csrf'] = token
        return client.post('/campaigns' + path, data=data)
    e.post = post

    def drain(rounds=40):
        """Runs the engine with pacing delays skipped."""
        total = 0
        for _ in range(rounds):
            e.db.run('UPDATE campaigns SET next_send_at = ?', time.time() - 1.5)
            sent = e.engine.tick()
            total += sent
            if not sent and not e.db.val("SELECT COUNT(*) FROM messages WHERE status = 'queued' "
                                         "AND scheduled_at <= ?", time.time(), default=0):
                break
        return total
    e.drain = drain
    yield e
    catcher.stop()
    e.db.close()


def make_list(e, name='Main list', csv_text=CSV):
    response = e.post('/lists', name=name)
    list_id = int(response.headers['Location'].rstrip('/').split('/')[-1])
    response = e.client.post(f'/campaigns/lists/{list_id}/import', data={
        'csrf': e.csrf, 'consent': 'yes', 'file': (io.BytesIO(csv_text.encode()), 'contacts.csv')},
        content_type='multipart/form-data')
    assert response.status_code == 302
    return list_id


def make_campaign(e, list_id, **extra):
    fields = {'name': 'Launch', 'subject': 'Hi {{first_name|there}}, news inside', 'html': HTML,
              'list_id': list_id, 'rate_per_hour': 100000}
    fields.update(extra)
    return service.create_campaign(e.db, e.db.settings(), fields)


def parsed(message):
    return email.message_from_bytes(message['raw'])


def body_of(message, subtype='html'):
    for part in parsed(message).walk():
        if part.get_content_type() == f'text/{subtype}':
            return part.get_payload(decode=True).decode()
    return ''


# -- contacts -------------------------------------------------------------------
def test_csv_import_validates_and_deduplicates(env):
    contacts.suppress(env.db, 'dave@example.net', 'bounced', 'old bounce')
    list_id = make_list(env)
    counts = contacts.list_counts(env.db, list_id)
    assert counts['total'] == 4          # ana, bob, carol, dave
    assert counts['active'] == 3         # dave is suppressed
    assert counts['bounced'] == 1
    bob = env.db.one("SELECT * FROM contacts WHERE email = 'bob@example.com'")
    assert bob['first_name'] == 'Bob' and json.loads(bob['fields'])['company'] == 'Globex'
    page = env.client.get(f'/campaigns/lists/{list_id}')
    assert 'ana@example.com' in page.text and 'postmaster@example.com' not in page.text
    # A second upload of the same file adds nothing.
    again = contacts.import_rows(env.db, list_id, contacts.parse_csv(CSV)[0])
    assert again['added'] == 0 and again['already_in_list'] == 4


def test_import_requires_consent_and_handles_plain_lists(env):
    response = env.post('/lists', name='Pasted')
    list_id = int(response.headers['Location'].split('/')[-1])
    refused = env.post(f'/lists/{list_id}/import', pasted='x@example.com')
    assert refused.status_code == 302 and contacts.list_counts(env.db, list_id)['total'] == 0
    env.post(f'/lists/{list_id}/import', consent='yes', pasted='x@example.com\ny@example.com;Y\n<z@example.com>')
    assert contacts.list_counts(env.db, list_id)['total'] == 3
    export = env.client.get(f'/campaigns/lists/{list_id}/export.csv')
    assert export.status_code == 200 and 'x@example.com' in export.text


# -- the full campaign path -------------------------------------------------------
def test_campaign_send_track_unsubscribe(env):
    env.db.set_setting('bounce_address', 'bounces@example.com')
    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id)
    response = env.post(f'/campaigns/{campaign_id}/save', action='schedule', when='now', name='Launch',
                        subject='Hi {{first_name|there}}, news inside', html=HTML, list_id=list_id,
                        rate_per_hour=100000, track_opens='on', track_clicks='on')
    assert response.status_code == 302
    assert env.db.val('SELECT status FROM campaigns WHERE id = ?', campaign_id) == 'scheduled'
    assert env.drain() == 4
    assert env.db.val('SELECT status FROM campaigns WHERE id = ?', campaign_id) in ('sending', 'sent')
    env.engine.tick()
    assert env.db.val('SELECT status FROM campaigns WHERE id = ?', campaign_id) == 'sent'

    by_to = {m['to'][0]: m for m in env.catcher.messages}
    assert set(by_to) == {'ana@example.com', 'bob@example.com', 'carol@example.org', 'dave@example.net'}
    ana = by_to['ana@example.com']
    headers = parsed(ana)
    assert headers['Subject'] == 'Hi Ana, news inside'
    assert headers['From'] == 'Test Sender <news@example.com>'
    assert headers['List-Unsubscribe-Post'] == 'List-Unsubscribe=One-Click'
    unsubscribe_url = headers['List-Unsubscribe'].strip('<>')
    assert unsubscribe_url.startswith('https://mail.example.com/campaigns/t/u/')
    row = env.db.one("SELECT * FROM messages WHERE to_email = 'ana@example.com'")
    assert ana['from'] == f'bounces+{row["id"]}@example.com'       # per-message return path
    html = body_of(ana)
    assert 'Hello Ana' in html and 'News for Acme' in html
    assert 'shop.example.com' not in html                           # link is tracked
    assert 'mailto:hi@example.com' in html                          # mailto is left alone
    assert '1 Test Street' in html and unsubscribe_url in html      # legal footer
    text = body_of(ana, 'plain')
    assert 'Hello Ana' in text and 'https://shop.example.com/offer?x=1&y=2' in text and unsubscribe_url in text

    prefix = 'https://mail.example.com'
    pixel = re.search(r'src="(https://mail\.example\.com/campaigns/t/o/[^"]+)"', html).group(1)[len(prefix):]
    click = re.search(r'href="(https://mail\.example\.com/campaigns/t/c/[^"]+)"', html).group(1)[len(prefix):]
    public = env.app.test_client()                                  # no login cookie
    agent_header = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0) Gecko Firefox'}

    # An open seconds after delivery is a scanner, not a person.
    assert public.get(pixel, headers=agent_header).mimetype == 'image/gif'
    row = env.db.one('SELECT * FROM messages WHERE id = ?', row['id'])
    assert row['open_count'] == 1 and row['human_open'] == 0
    env.db.run('UPDATE messages SET sent_at = sent_at - 120 WHERE id = ?', row['id'])
    public.get(pixel, headers=agent_header)
    redirect = public.get(click, headers=agent_header)
    assert redirect.status_code == 302 and redirect.headers['Location'] == 'https://shop.example.com/offer?x=1&y=2'
    row = env.db.one('SELECT * FROM messages WHERE id = ?', row['id'])
    assert row['human_open'] == 1 and row['human_click'] == 1 and row['click_count'] == 1

    # Tampered links do nothing.
    assert public.get(click[:-3] + 'abc').status_code == 404
    assert public.get(pixel.replace('.gif', 'x.gif')).status_code == 200   # still a pixel, nothing recorded
    assert env.db.val('SELECT open_count FROM messages WHERE id = ?', row['id']) == 2

    # Unsubscribe: GET only asks, POST (also the mail client's one-click) acts.
    path = unsubscribe_url[len(prefix):]
    assert 'Yes, unsubscribe me' in public.get(path).text
    assert env.db.val("SELECT status FROM contacts WHERE email = 'ana@example.com'") == 'active'
    assert 'You are unsubscribed' in public.post(path, data={'List-Unsubscribe': 'One-Click'}).text
    assert env.db.val("SELECT status FROM contacts WHERE email = 'ana@example.com'") == 'unsubscribed'
    assert contacts.is_suppressed(env.db, 'ana@example.com')['reason'] == 'unsubscribed'

    data = stats.campaign(env.db, campaign_id)
    assert (data['sent'], data['opened'], data['clicked'], data['unsubscribed']) == (4, 1, 1, 1)
    assert data['open_rate'] == 25.0
    for path in (f'/campaigns/{campaign_id}', '/analytics', '/', '/emails', f'/emails/{row["id"]}'):
        assert env.client.get('/campaigns' + path).status_code == 200

    # The unsubscribed contact is skipped by the next campaign.
    second = make_campaign(env, list_id, name='Second')
    service.schedule_campaign(env.db, env.db.settings(), second)
    env.catcher.messages.clear()
    assert env.drain() == 3
    assert 'ana@example.com' not in {m['to'][0] for m in env.catcher.messages}


def test_click_cannot_be_pointed_at_another_campaigns_link(env):
    list_id = make_list(env)
    first = make_campaign(env, list_id)
    service.schedule_campaign(env.db, env.db.settings(), first)
    env.drain()
    foreign = env.db.run("INSERT INTO links (campaign_id, url) VALUES (999, 'https://evil.example/')").lastrowid
    message = env.db.one('SELECT id FROM messages LIMIT 1')
    from app import security
    signature = security.sign(env.cfg.SECRET_KEY, 'c', message['id'], foreign)
    assert env.client.get(f'/campaigns/t/c/{message["id"]}/{foreign}/{signature}').status_code == 404


# -- pacing ---------------------------------------------------------------------
def test_rate_limit_spaces_messages(env):
    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id, rate_per_hour=60)
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    assert env.engine.tick() == 1
    assert env.engine.tick() == 0                      # next one is a minute away
    gap = env.db.val('SELECT next_send_at FROM campaigns WHERE id = ?', campaign_id) - time.time()
    assert 55 < gap <= 60
    service.update_campaign(env.db, env.db.settings(), campaign_id, {'delay_min': 5, 'delay_max': 9})
    env.db.run('UPDATE campaigns SET next_send_at = 0')
    assert env.engine.tick() == 1
    gap = env.db.val('SELECT next_send_at FROM campaigns WHERE id = ?', campaign_id) - time.time()
    assert 4 < gap <= 9


def test_daily_cap_window_warmup_and_domain_cap(env):
    list_id = make_list(env)
    settings = env.db.settings()
    capped = make_campaign(env, list_id, daily_cap=1)
    service.schedule_campaign(env.db, settings, capped)
    assert env.drain() == 1
    assert 'Daily limit' in env.db.val('SELECT hold_reason FROM campaigns WHERE id = ?', capped)
    service.set_state(env.db, capped, 'cancel')

    # A window that excludes the current hour holds the campaign.
    from app.sender import local_time
    hour = local_time(settings).hour
    windowed = make_campaign(env, list_id, window_start=f'{(hour + 2) % 24:02d}:00', window_end=f'{(hour + 3) % 24:02d}:00')
    service.schedule_campaign(env.db, settings, windowed)
    assert env.drain() == 0
    assert 'sending window' in env.db.val('SELECT hold_reason FROM campaigns WHERE id = ?', windowed)
    service.set_state(env.db, windowed, 'cancel')

    env.db.set_setting('warmup_enabled', '1')
    env.db.set_setting('warmup_start', '2')
    env.db.set_setting('warmup_started_at', time.time())
    warm = make_campaign(env, list_id)
    service.schedule_campaign(env.db, env.db.settings(), warm)
    assert env.drain() == 1                            # 1 already sent today + 1 = limit of 2
    assert 'Warm-up limit of 2' in env.db.val('SELECT hold_reason FROM campaigns WHERE id = ?', warm)
    service.set_state(env.db, warm, 'cancel')
    env.db.set_setting('warmup_enabled', '0')

    env.db.set_setting('domain_hourly_cap', '1')
    env.db.run('UPDATE messages SET sent_at = sent_at - 7200 WHERE sent_at IS NOT NULL')
    limited = make_campaign(env, list_id)
    service.schedule_campaign(env.db, env.db.settings(), limited)
    assert env.drain() == 3                            # one per domain; the second example.com address waits
    assert 'Per-domain limit' in env.db.val('SELECT hold_reason FROM campaigns WHERE id = ?', limited)


def test_pause_resume_cancel(env):
    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id, rate_per_hour=60)
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    assert env.engine.tick() == 1
    env.post(f'/campaigns/{campaign_id}/state', action='pause')
    assert env.drain() == 0
    env.post(f'/campaigns/{campaign_id}/state', action='resume')
    assert env.engine.tick() == 1
    env.post(f'/campaigns/{campaign_id}/state', action='cancel')
    assert env.drain() == 0
    assert env.db.val("SELECT COUNT(*) FROM messages WHERE status = 'cancelled'") == 2


def test_mail_server_down_keeps_queue_and_recovers(env):
    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id)
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    good_port = env.cfg.SMTP_PORT
    env.cfg.SMTP_PORT = 9                               # nothing listens here
    assert env.engine.tick() == 0
    assert env.db.val("SELECT COUNT(*) FROM messages WHERE status = 'queued'") == 4
    assert env.db.val("SELECT COUNT(*) FROM messages WHERE attempts > 0") == 0
    assert env.db.state('server_down')
    env.cfg.SMTP_PORT = good_port
    env.db.set_state('server_down_until', 0)
    assert env.drain() == 4 and not env.db.state('server_down')


def test_interrupted_send_is_never_repeated(env):
    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id)
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    env.engine.tick()
    env.db.run("UPDATE messages SET status = 'sending' WHERE rowid = (SELECT MAX(rowid) FROM messages)")
    before = len(env.catcher.messages)
    env.engine.recover()
    env.drain()
    assert env.db.val("SELECT COUNT(*) FROM messages WHERE error LIKE 'Interrupted%'") == 1
    assert len({m['to'][0] for m in env.catcher.messages}) == len(env.catcher.messages) >= before


# -- bounces and complaints -------------------------------------------------------
DSN = """From: MAILER-DAEMON@mail.example.com
To: bounces+{mid}@example.com
Subject: Undelivered Mail Returned to Sender
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="b1"

--b1
Content-Type: text/plain

Delivery failed.

--b1
Content-Type: message/delivery-status

Reporting-MTA: dns; mail.example.com

Final-Recipient: rfc822; {rcpt}
Action: failed
Status: {status}
Diagnostic-Code: smtp; {diag}

--b1
Content-Type: text/rfc822-headers

X-Message-Ref: {mid}
Subject: Hi

--b1--
"""

ARF = """From: complaints@provider.example
To: bounces@example.com
Subject: FW: complaint
MIME-Version: 1.0
Content-Type: multipart/report; report-type=feedback-report; boundary="b2"

--b2
Content-Type: text/plain

This is a spam complaint.

--b2
Content-Type: message/feedback-report

Feedback-Type: abuse
User-Agent: Provider/1.0
Version: 1

--b2
Content-Type: message/rfc822

Message-ID: <{mid}@mail.example.com>
Subject: Hi
To: {rcpt}

body
--b2--
"""


def test_bounces_and_complaints_update_lists(env):
    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id)
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    env.drain()
    ids = {r['to_email']: r['id'] for r in env.db.q('SELECT id, to_email FROM messages')}

    hard = bounces.parse_report(DSN.format(mid=ids['ana@example.com'], rcpt='ana@example.com', status='5.1.1',
                                           diag='550 5.1.1 user unknown').encode())
    assert hard == {'kind': 'bounce', 'message_id': ids['ana@example.com'], 'recipient': 'ana@example.com',
                    'hard': True, 'detail': '5.1.1 smtp; 550 5.1.1 user unknown'}
    assert bounces.apply_report(env.db, hard)
    assert contacts.is_suppressed(env.db, 'ana@example.com')['reason'] == 'bounced'

    soft = bounces.parse_report(DSN.format(mid=ids['bob@example.com'], rcpt='bob@example.com', status='4.2.2',
                                           diag='452 mailbox full').encode())
    assert soft['hard'] is False and bounces.apply_report(env.db, soft)
    assert contacts.is_suppressed(env.db, 'bob@example.com') is None      # one soft bounce does not suppress
    assert env.db.val("SELECT soft_bounces FROM contacts WHERE email = 'bob@example.com'") == 1

    complaint = bounces.parse_report(ARF.format(mid=ids['carol@example.org'], rcpt='carol@example.org').encode())
    assert complaint['kind'] == 'complaint' and bounces.apply_report(env.db, complaint)
    assert contacts.is_suppressed(env.db, 'carol@example.org')['reason'] == 'complained'

    assert bounces.parse_report(b'From: a@b.c\nSubject: hello\n\njust a normal email') is None
    data = stats.campaign(env.db, campaign_id)
    assert (data['bounced'], data['hard_bounced'], data['complained'], data['delivered']) == (2, 1, 1, 2)


def test_recipient_refused_by_server_is_suppressed(env):
    env.catcher.refuse['carol@example.org'] = '550 5.1.1 no such user'
    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id)
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    assert env.drain() == 3
    row = env.db.one("SELECT * FROM messages WHERE to_email = 'carol@example.org'")
    assert row['status'] == 'bounced' and row['bounce_type'] == 'hard' and '550' in row['error']
    assert contacts.is_suppressed(env.db, 'carol@example.org')['reason'] == 'bounced'


def test_campaign_pauses_itself_on_high_bounce_rate(env):
    rows = 'email\n' + '\n'.join(f'user{i}@example.com' for i in range(70))
    list_id = make_list(env, csv_text=rows)
    campaign_id = make_campaign(env, list_id)
    service.schedule_campaign(env.db, env.db.settings(), campaign_id)
    env.engine.tick()                                   # launch pass
    for _ in range(6):                                  # a fast rate really sends 10 per pass
        env.db.run('UPDATE campaigns SET next_send_at = ?', time.time() - 1.5)
        assert env.engine.tick() == 10
    sent = env.db.val('SELECT COUNT(*) FROM messages WHERE sent_at IS NOT NULL')
    assert 60 <= sent < 70
    env.db.run("UPDATE messages SET status = 'bounced', bounced_at = sent_at, bounce_type = 'hard' WHERE rowid IN "
               "(SELECT rowid FROM messages WHERE sent_at IS NOT NULL LIMIT 7)")
    env.db.run('UPDATE campaigns SET next_send_at = ?', time.time() - 1.5)
    assert env.engine.tick() == 0
    campaign = env.db.one('SELECT * FROM campaigns WHERE id = ?', campaign_id)
    assert campaign['status'] == 'paused' and 'Paused automatically' in campaign['hold_reason']


# -- API and webhooks -------------------------------------------------------------
def api_key(env):
    env.post('/settings/api-keys', name='test')
    page = env.client.get('/campaigns/settings')
    return re.search(r'class="secret">(em_[^<]+)<', page.text).group(1)


def test_api_send_status_idempotency_and_errors(env):
    key = api_key(env)
    auth = {'Authorization': f'Bearer {key}'}
    api = env.app.test_client()
    assert api.post('/campaigns/api/emails', json={}).status_code == 401
    assert api.post('/campaigns/api/emails', json={}, headers={'Authorization': 'Bearer em_wrong'}).status_code == 403
    missing = api.post('/campaigns/api/emails', json={'to': 'x@example.com'}, headers=auth)
    assert missing.status_code == 422 and missing.json['name'] == 'missing_required_field'

    body = {'from': 'Shop <orders@example.com>', 'to': ['buyer@example.com'], 'subject': 'Your order',
            'html': '<p>Thanks! <a href="https://shop.example.com/o/1">Track it</a></p>',
            'reply_to': 'help@example.com', 'headers': {'X-Order': '1', 'From': 'spoof@evil.example'},
            'tags': [{'name': 'type', 'value': 'order'}]}
    first = api.post('/campaigns/api/emails', json=body, headers=dict(auth, **{'Idempotency-Key': 'order-1'}))
    again = api.post('/campaigns/api/emails', json=body, headers=dict(auth, **{'Idempotency-Key': 'order-1'}))
    assert first.status_code == 200 and first.json['id'] == again.json['id']
    assert env.engine.tick() == 1 and len(env.catcher.messages) == 1
    sent = parsed(env.catcher.messages[0])
    assert sent['From'] == 'Shop <orders@example.com>' and sent['X-Order'] == '1' and sent['Reply-To'] == 'help@example.com'
    assert 'List-Unsubscribe' not in sent                      # transactional mail has no marketing footer
    assert '/campaigns/t/c/' in body_of(env.catcher.messages[0])
    status = api.get(f'/campaigns/api/emails/{first.json["id"]}', headers=auth).json
    assert status['last_event'] == 'sent' and [e['type'] for e in status['events']] == ['email.queued', 'email.sent']

    batch = api.post('/campaigns/api/emails/batch', headers=auth, json=[
        {'to': 'a@example.com', 'subject': 'A', 'text': 'a'}, {'to': 'b@example.com', 'subject': 'B', 'text': 'b'}])
    assert len(batch.json['data']) == 2 and env.engine.tick() == 2
    bad_batch = api.post('/campaigns/api/emails/batch', headers=auth, json=[
        {'to': 'c@example.com', 'subject': 'C', 'text': 'c'}, {'to': 'nope', 'subject': 'D', 'text': 'd'}])
    assert bad_batch.status_code == 422 and env.engine.tick() == 0      # nothing from a bad batch is sent

    contacts.suppress(env.db, 'gone@example.com', 'bounced', 'old')
    blocked = api.post('/campaigns/api/emails', headers=auth, json={'to': 'gone@example.com', 'subject': 'x', 'text': 'x'})
    env.engine.tick()
    assert api.get(f'/campaigns/api/emails/{blocked.json["id"]}', headers=auth).json['last_event'] == 'failed'

    later = api.post('/campaigns/api/emails', headers=auth, json={
        'to': 'x@example.com', 'subject': 'later', 'text': 'x', 'scheduled_at': '2099-01-01T00:00:00Z'})
    assert env.engine.tick() == 0
    assert api.post(f'/campaigns/api/emails/{later.json["id"]}/cancel', headers=auth).status_code == 200
    contact = api.post('/campaigns/api/contacts', headers=auth, json={'email': 'New@Example.com', 'first_name': 'N'})
    assert contact.json['object'] == 'contact'

    env.post('/settings/api-keys', action='revoke', id=env.db.val('SELECT id FROM api_keys'))
    assert api.get('/campaigns/api/lists', headers=auth).status_code == 403


def test_webhooks_are_signed_and_delivered(env):
    received = []

    class Hook(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            received.append((dict(self.headers), body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass
    server = http.server.HTTPServer(('127.0.0.1', 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f'http://127.0.0.1:{server.server_address[1]}/hook'
        env.post('/settings/webhooks', url=url, events='email.sent')
        env.post('/settings/webhooks', url='https://127.0.0.1/hook', events='email.sent')
        assert not env.db.val('SELECT COUNT(*) FROM webhooks')   # private addresses need the opt-in
        env.cfg.WEBHOOK_PRIVATE = True
        env.post('/settings/webhooks', url=url, events='email.sent')
        secret = env.db.val('SELECT secret FROM webhooks')
        from app.sender import queue_single
        queue_single(env.db, kind='api', from_email='a@example.com', to=['b@example.com'], subject='s', html='<p>x</p>')
        env.engine.tick()
        events.deliver_webhooks(env.db)
    finally:
        server.shutdown()
    assert len(received) == 1                                   # "queued" was not subscribed
    headers, body = received[0]
    payload = json.loads(body)
    assert payload['type'] == 'email.sent' and payload['data']['to'] == 'b@example.com'
    expected = hmac.new(secret.encode(), headers['X-Webhook-Timestamp'].encode() + b'.' + body, hashlib.sha256).hexdigest()
    assert headers['X-Webhook-Signature'] == f'sha256={expected}'
    assert env.db.val("SELECT status FROM webhook_deliveries") == 'delivered'


# -- content check ----------------------------------------------------------------
def test_content_check_flags_junk_patterns_and_passes_clean_mail(env):
    bad = spamcheck.lint('RE: ACT NOW!!! FREE MONEY WINNER', '<img src="x.png"><a href="https://bit.ly/x">https://paypal.com</a>',
                         '', 'me@gmail.com')
    titles = ' | '.join(f['title'] for f in bad)
    for needle in ('capital letters', 'reply or forward', 'shorteners', 'one address but opens another',
                   'almost only images', 'free mail provider'):
        assert needle in titles
    assert spamcheck.verdict(bad, None) == 'fix'
    clean = spamcheck.lint('Your March product update', '<p>Hi Ana,</p><p>Here is what changed this month in the '
                           'product, with three improvements you asked for and one fix.</p>'
                           '<p><a href="https://example.com/changelog">Read the changelog</a></p>', '', 'news@example.com')
    assert spamcheck.verdict(clean, None) == 'good'
    assert spamcheck.verdict(clean, {'score': 9.0, 'required': 15}) == 'review'
    assert spamcheck.verdict(clean, {'score': 16.0, 'required': 15}) == 'fix'

    list_id = make_list(env)
    campaign_id = make_campaign(env, list_id)
    env.post(f'/campaigns/{campaign_id}/save', action='check', name='Launch', subject='Hello there', html=HTML, list_id=list_id)
    report = json.loads(env.db.val('SELECT spam_report FROM campaigns WHERE id = ?', campaign_id))
    assert report['verdict'] in ('good', 'review') and report['rspamd'] is None
    assert '<span class="section-num">5</span>Check' in env.client.get(f'/campaigns/campaigns/{campaign_id}').text
    preview = env.client.get(f'/campaigns/campaigns/{campaign_id}/preview')
    csp = preview.headers['Content-Security-Policy']
    assert "default-src 'none'" in csp and "frame-ancestors 'self'" in csp and 'Hello Ana' in preview.text


# -- the agent --------------------------------------------------------------------
def scripted(steps):
    """A stand-in for the model: returns the given turns in order."""
    turns = iter(steps)
    seen = []

    def complete(messages):
        seen.append(json.loads(json.dumps(messages)))
        return next(turns)
    complete.seen = seen
    return complete


def call(tool, **args):
    return {'id': f'call_{tool}_{len(args)}', 'name': tool, 'input': args}


def test_agent_drafts_checks_and_waits_for_approval(env):
    list_id = make_list(env)
    thread = agent.new_thread(env.db)
    model = scripted([
        {'text': '', 'tool_calls': [call('get_overview'), call('list_contact_lists')]},
        {'text': 'Drafting it now.', 'tool_calls': [call(
            'create_campaign', name='March update', subject='What changed in March, {{first_name|there}}',
            html='<p>Hi {{first_name|there}},</p><p>Here are the three things we improved this month for {{company}}.</p>'
                 '<p><a href="https://example.com/march">Read the full update</a></p>', list_id=list_id)]},
        {'text': '', 'tool_calls': [call('schedule_campaign', campaign_id=1)]},
        {'text': 'The campaign is drafted and waiting for your approval.', 'tool_calls': []},
    ])
    new = agent.reply(env.db, env.cfg, thread, 'Send a March update to my list', complete=model)
    assert new[-1]['content']['text'].endswith('waiting for your approval.')
    overview = json.loads(new[2]['content']['results'][0]['output'])
    assert overview['brand'] == 'TestBrand' and overview['sends_need_owner_approval'] is True
    lists = json.loads(new[2]['content']['results'][1]['output'])
    assert lists[0]['active_contacts'] == 4 and 'company' in lists[0]['merge_tags']
    scheduled = json.loads(new[6]['content']['results'][0]['output'])
    assert scheduled['status'] == 'pending_approval' and 'NOT scheduled' in scheduled['note']
    # The model saw every tool result, in order, before answering.
    assert [m['role'] for m in model.seen[-1]] == ['user', 'assistant', 'tool', 'assistant', 'tool', 'assistant', 'tool']

    assert env.db.val('SELECT status FROM campaigns WHERE id = 1') == 'pending_approval'
    assert env.drain() == 0 and not env.catcher.messages               # nothing leaves without approval
    page = env.client.get(f'/campaigns/agent/{thread}')
    assert 'Waiting for your approval' in page.text and 'March update' in page.text
    env.post('/campaigns/1/state', action='approve')
    assert env.drain() == 4
    assert parsed(env.catcher.messages[0])['Subject'].startswith('What changed in March')

    # A single email from the agent is parked the same way, then approved.
    model = scripted([{'text': '', 'tool_calls': [call('send_email', to='friend@example.com', subject='Hello',
                                                       html='<p>Hi</p>')]},
                      {'text': 'It is waiting for your approval.', 'tool_calls': []}])
    agent.reply(env.db, env.cfg, thread, 'Email my friend', complete=model)
    held = env.db.one("SELECT * FROM messages WHERE kind = 'agent'")
    assert held['status'] == 'pending_approval' and env.engine.tick() == 0
    env.post(f'/emails/{held["id"]}/decide', action='approve')
    assert env.engine.tick() == 1


def test_agent_can_send_directly_when_allowed_and_is_stopped_by_bad_content(env):
    env.db.set_setting('ai_require_approval', '0')
    list_id = make_list(env)
    thread = agent.new_thread(env.db)
    model = scripted([
        {'text': '', 'tool_calls': [call('create_campaign', name='Bad', subject='RE: ACT NOW!!! WINNER FREE MONEY',
                                         html='<img src="a.png">', list_id=list_id)]},
        {'text': '', 'tool_calls': [call('schedule_campaign', campaign_id=1)]},
        {'text': '', 'tool_calls': [call('update_campaign', campaign_id=1, subject='Our spring catalogue is here',
                                         html='<p>Hi {{first_name|there}}, the spring catalogue is ready with the new '
                                              'range you asked about.</p><p><a href="https://example.com/c">Browse it</a></p>')]},
        {'text': '', 'tool_calls': [call('schedule_campaign', campaign_id=1), call('no_such_tool')]},
        {'text': 'Scheduled.', 'tool_calls': []},
    ])
    new = agent.reply(env.db, env.cfg, thread, 'go', complete=model)
    refused = new[4]['content']['results'][0]
    assert refused['is_error'] and 'must be fixed first' in refused['output']
    final = new[8]['content']['results']
    assert json.loads(final[0]['output'])['status'] == 'scheduled' and final[1]['is_error']
    assert env.drain() == 4


def test_provider_message_formats(env):
    history = [
        {'role': 'user', 'content': {'text': 'hi'}},
        {'role': 'assistant', 'content': {'text': 'looking', 'provider': 'anthropic',
                                          'tool_calls': [{'id': 't1', 'name': 'get_overview', 'input': {}}],
                                          'raw': [{'type': 'thinking', 'thinking': '', 'signature': 'sig'},
                                                  {'type': 'text', 'text': 'looking'},
                                                  {'type': 'tool_use', 'id': 't1', 'name': 'get_overview', 'input': {}}]}},
        {'role': 'tool', 'content': {'results': [{'id': 't1', 'name': 'get_overview', 'output': '{}', 'is_error': False}]}},
    ]
    claude = ai_anthropic._messages(history)
    assert claude[1]['content'][0]['type'] == 'thinking'                 # replayed unchanged
    assert claude[2] == {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': '{}'}]}
    other = ai_openrouter._messages('SYSTEM', history)
    assert other[0] == {'role': 'system', 'content': 'SYSTEM'}
    assert other[2]['tool_calls'][0]['function'] == {'name': 'get_overview', 'arguments': '{}'}
    assert other[3] == {'role': 'tool', 'tool_call_id': 't1', 'content': '{}'}
    assert ai_openrouter.pick_default(['openai/x', 'anthropic/claude-haiku-9', 'anthropic/claude-opus-9']) == 'anthropic/claude-opus-9'
    for tool in agent.TOOLS:
        assert tool['input_schema']['type'] == 'object' and hasattr(agent.Toolbox, f'tool_{tool["name"]}')


# -- access control ---------------------------------------------------------------
def test_pages_need_login_and_forms_need_csrf(env):
    stranger = env.app.test_client()
    for path in ('/', '/lists', '/settings', '/agent', '/emails', '/analytics'):
        response = stranger.get('/campaigns' + path)
        assert response.status_code == 302 and response.headers['Location'].endswith('/campaigns/login')
    assert stranger.post('/campaigns/lists', data={'name': 'x'}).status_code in (302, 400)
    assert env.db.val('SELECT COUNT(*) FROM lists') == 0
    assert env.client.post('/campaigns/lists', data={'name': 'no token'}).status_code == 400
    assert env.client.post('/campaigns/lists', data={'name': 'wrong', 'csrf': 'nope'}).status_code == 400
    assert env.db.val('SELECT COUNT(*) FROM lists') == 0
    assert stranger.get('/campaigns/health').status_code == 200


def test_proxy_mode_trusts_only_the_shared_secret(tmp_path):
    cfg = Config({'CAMPAIGNS_DATA': str(tmp_path), 'CAMPAIGNS_AUTH': 'proxy', 'CAMPAIGNS_PROXY_SECRET': 's3cret',
                  'HOSTNAMES': 'mail.example.com', 'SECRET_KEY': 'k' * 16})
    app = create_app(cfg)
    client = app.test_client()
    assert client.get('/campaigns/').status_code == 403
    assert client.get('/campaigns/', headers={'X-Proxy-Auth': 'wrong'}).status_code == 403
    page = client.get('/campaigns/', headers={'X-Proxy-Auth': 's3cret'})
    assert page.status_code == 200 and 'Mail admin' in page.text
    assert cfg.PUBLIC_URL == 'https://mail.example.com'
    app.db.close()


def test_settings_keep_secrets_out_of_the_page(env):
    env.post('/settings', section='ai', ai_provider='anthropic', ai_key_anthropic='sk-ant-test-123',
             ai_model_anthropic='', ai_require_approval='on')
    assert env.db.setting('ai_key_anthropic') == 'sk-ant-test-123'
    page = env.client.get('/campaigns/settings').text
    assert 'sk-ant-test-123' not in page and 'Saved. Type a new value' in page
    env.post('/settings', section='ai', ai_provider='anthropic', ai_key_anthropic='', ai_require_approval='on')
    assert env.db.setting('ai_key_anthropic') == 'sk-ant-test-123'       # blank field keeps the key
    env.post('/settings', section='sender', from_email='not an email', timezone='UTC')
    assert env.db.setting('from_email') == 'news@example.com'
    env.post('/settings', section='sender', from_email='hi@example.com', timezone='Mars/Phobos')
    assert env.db.setting('timezone') == 'UTC'
