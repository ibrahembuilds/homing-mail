"""Public endpoints reached from inside delivered emails: open pixel, click
redirect and unsubscribe. Every URL carries a signature, so ids cannot be
guessed or altered."""
import base64

import flask

from .. import contacts, events, security
from ..db import now

bp = flask.Blueprint('public', __name__)

PIXEL = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
AUTOMATED_AGENTS = ('bot', 'spider', 'crawl', 'scanner', 'proofpoint', 'mimecast', 'barracuda', 'symantec',
                    'forcepoint', 'headless', 'python-requests', 'curl/', 'wget', 'go-http-client', 'okhttp',
                    'preview', 'safelinks', 'urldefense')


def _db():
    return flask.current_app.db


def _cfg():
    return flask.current_app.cfg


def client_ip():
    if _cfg().AUTH_MODE == 'proxy':
        return flask.request.headers.get('X-Real-IP') or flask.request.remote_addr or ''
    return flask.request.remote_addr or ''


def looks_automated(message, ip, agent, is_open):
    """Opens and clicks made by software rather than a person: security
    scanners that follow every link on arrival, and privacy proxies that
    fetch images whether or not the mail is read."""
    if flask.request.method == 'HEAD' or not agent:
        return True
    lowered = agent.lower()
    if any(marker in lowered for marker in AUTOMATED_AGENTS):
        return True
    if message['sent_at'] and now() - message['sent_at'] < 10:
        return True
    # Apple Mail Privacy Protection fetches through Apple's 17.0.0.0/8 range.
    return is_open and ip.startswith('17.')


def _message(mid):
    if len(mid) != 32:
        return None
    return _db().one('SELECT * FROM messages WHERE id = ?', mid)


@bp.route('/o/<mid>/<sig>.gif')
def open_pixel(mid, sig):
    db = _db()
    if security.verify(_cfg().SECRET_KEY, sig, 'o', mid):
        message = _message(mid)
        if message is not None and message['sent_at']:
            ip, agent = client_ip(), flask.request.headers.get('User-Agent', '')
            automated = looks_automated(message, ip, agent, True)
            ts = now()
            with db.tx():
                db.run('UPDATE messages SET open_count = open_count + 1, opened_at = COALESCE(opened_at, ?), '
                       'human_open = MAX(human_open, ?) WHERE id = ?', ts, 0 if automated else 1, mid)
                if message['open_count'] < 25:
                    events.record(db, message, 'opened', automated=automated, ip=ip, user_agent=agent)
    response = flask.Response(PIXEL, mimetype='image/gif')
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
    response.headers['Pragma'] = 'no-cache'
    return response


@bp.route('/c/<mid>/<int:link_id>/<sig>')
def click(mid, link_id, sig):
    db = _db()
    if not security.verify(_cfg().SECRET_KEY, sig, 'c', mid, link_id):
        flask.abort(404)
    link = db.one('SELECT * FROM links WHERE id = ?', link_id)
    message = _message(mid)
    # The destination comes only from our own table, never from the request,
    # so this cannot be used as an open redirect.
    if link is None or message is None or link['campaign_id'] != (message['campaign_id'] or 0):
        flask.abort(404)
    ip, agent = client_ip(), flask.request.headers.get('User-Agent', '')
    automated = looks_automated(message, ip, agent, False)
    human = 0 if automated else 1
    ts = now()
    with db.tx():
        db.run('UPDATE messages SET click_count = click_count + 1, clicked_at = COALESCE(clicked_at, ?), '
               'human_click = MAX(human_click, ?), opened_at = COALESCE(opened_at, ?), '
               'human_open = MAX(human_open, ?) WHERE id = ?', ts, human, ts, human, mid)
        if message['click_count'] < 50:
            events.record(db, message, 'clicked', automated=automated, link_id=link_id, ip=ip, user_agent=agent,
                          detail=link['url'], extra={'click': {'link': link['url']}})
    return flask.redirect(link['url'], code=302)


@bp.route('/cal/<token>.ics')
def calendar_feed(token):
    """The follow-up calendar for Google Calendar, Outlook or Apple Calendar.
    The address itself is the password, like a private calendar link."""
    import hmac
    from .. import planner
    db = _db()
    expected = db.setting('calendar_token')
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        flask.abort(404)
    cfg = _cfg()
    body = planner.ics_feed(db, db.settings(), cfg.PUBLIC_URL + cfg.URL_PREFIX, cfg.BRAND_NAME)
    return flask.Response(body, mimetype='text/calendar', headers={'Cache-Control': 'private, max-age=300'})


@bp.route('/u/<mid>/<sig>', methods=['GET', 'POST'])
def unsubscribe(mid, sig):
    db = _db()
    if not security.verify(_cfg().SECRET_KEY, sig, 'u', mid):
        return flask.render_template('public.html', state='invalid'), 404
    message = _message(mid)
    if message is None:
        return flask.render_template('public.html', state='invalid'), 404
    if message['kind'] == 'test':
        return flask.render_template('public.html', state='test')
    email = message['to_email']
    if flask.request.method == 'POST':
        if flask.request.form.get('action') == 'resubscribe':
            current = contacts.is_suppressed(db, email)
            if current and current['reason'] == 'unsubscribed':
                contacts.unsuppress(db, email)
            return flask.render_template('public.html', state='resubscribed', email=email)
        # Reached both by the button below and by mail clients' one-click
        # unsubscribe (RFC 8058), which POSTs "List-Unsubscribe=One-Click".
        with db.tx():
            first = message['unsubscribed_at'] is None
            if first:
                db.run('UPDATE messages SET unsubscribed_at = ? WHERE id = ?', now(), mid)
            contacts.suppress(db, email, 'unsubscribed', 'Used the unsubscribe link')
            if first:
                events.record(db, message, 'unsubscribed', ip=client_ip(),
                              user_agent=flask.request.headers.get('User-Agent', ''))
        return flask.render_template('public.html', state='done', email=email)
    current = contacts.is_suppressed(db, email)
    if current and current['reason'] == 'unsubscribed':
        return flask.render_template('public.html', state='done', email=email)
    return flask.render_template('public.html', state='confirm', email=email)
