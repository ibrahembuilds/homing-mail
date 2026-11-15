"""Campaign service: Flask application factory."""
import datetime
import hashlib
import os
import secrets
import sqlite3

import flask
from werkzeug.exceptions import BadRequest, NotFound

from .config import Config
from .db import DB, now
from .sender import timezone

__version__ = '0.3.0'

# Pages run only the app's own script. Inline styles stay allowed for the brand
# colour and email previews; images may come from anywhere, as in a real inbox.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https: http:; "
       "font-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; form-action 'self'; "
       "base-uri 'self'; object-src 'none'")


def _load_secret(cfg):
    """Tracking links are signed with this key, so it must survive restarts."""
    if cfg.SECRET_KEY:
        return cfg.SECRET_KEY
    path = os.path.join(cfg.DATA_DIR, 'secret.key')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as handle:
            return handle.read().strip()
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    key = secrets.token_urlsafe(48)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def create_app(cfg=None):
    cfg = cfg or Config()
    cfg.SECRET_KEY = _load_secret(cfg)
    prefix = cfg.URL_PREFIX

    app = flask.Flask(__name__, static_url_path=prefix + '/static')
    app.config.update(
        SECRET_KEY=cfg.SECRET_KEY,
        SESSION_COOKIE_NAME='campaigns_session',
        SESSION_COOKIE_PATH=prefix,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=cfg.SECURE_COOKIES,
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12),
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,
        SEND_FILE_MAX_AGE_DEFAULT=365 * 86400,
    )
    app.cfg = cfg
    app.db = DB(cfg.DB_PATH)
    app.db.init(seed=cfg.SEED)
    from . import team
    team.ensure_owner(app.db, cfg.OWNER_EMAIL)
    from . import agent_runs
    agent_runs.recover(app.db)       # nothing can still be running in a fresh process

    from .views import api, public, ui, ui_accounts, ui_compose, ui_mail, ui_offers, ui_outreach, ui_team  # noqa: F401  (pages on ui.bp)
    app.register_blueprint(ui.bp, url_prefix=prefix)
    app.register_blueprint(public.bp, url_prefix=prefix + '/t')
    app.register_blueprint(api.bp, url_prefix=prefix + '/api')

    @app.route('/')
    def root():
        return flask.redirect(prefix + '/')

    @app.route(prefix + '/health')
    def health():
        app.db.val('SELECT 1')
        return {'status': 'ok'}

    @app.errorhandler(OverflowError)
    def number_too_large(_):
        # An id or page number beyond what SQLite stores cannot match anything.
        return NotFound()

    @app.errorhandler(sqlite3.InterfaceError)
    @app.errorhandler(sqlite3.ProgrammingError)
    def wrong_value_type(_):
        return BadRequest()

    @app.after_request
    def security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('Referrer-Policy', 'same-origin')
        if cfg.SECURE_COOKIES:
            response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000')
        if response.mimetype == 'text/html':
            response.headers.setdefault('Content-Security-Policy', CSP)
            response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
            response.headers.setdefault('Cache-Control', 'no-store')
        return response

    # -- template helpers ----------------------------------------------------
    def local(ts):
        return datetime.datetime.fromtimestamp(ts, timezone(app.db.settings()))

    @app.template_filter('dt')
    def filter_dt(ts, fmt='%d %b %Y %H:%M'):
        return local(ts).strftime(fmt) if ts else ''

    @app.template_filter('ago')
    def filter_ago(ts):
        if not ts:
            return ''
        seconds = now() - ts
        future = seconds < 0
        seconds = abs(seconds)
        for size, unit in ((86400, 'day'), (3600, 'hour'), (60, 'minute')):
            if seconds >= size:
                count = int(seconds // size)
                text = f'{count} {unit}{"s" if count != 1 else ""}'
                return f'in {text}' if future else f'{text} ago'
        return 'in a moment' if future else 'just now'

    @app.template_filter('num')
    def filter_num(value):
        try:
            return f'{int(value):,}'
        except (TypeError, ValueError):
            return value

    @app.template_filter('pct')
    def filter_pct(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        return f'{number:.2f}%' if 0 < number < 1 else f'{number:.1f}%'

    # Stylesheet and script URLs carry a hash of their content, so browsers can
    # keep them for a year and still pick up every change.
    digest = hashlib.sha256()
    for name in ('app.css', 'app.js'):
        with open(os.path.join(app.static_folder, name), 'rb') as handle:
            digest.update(handle.read())
    asset_version = digest.hexdigest()[:12]

    @app.context_processor
    def inject():
        return {'cfg': cfg, 'brand': cfg.BRAND_NAME, 'prefix': prefix, 'app_version': __version__,
                'asset_version': asset_version, 'now_ts': now()}

    @app.template_filter('fromjson')
    def filter_fromjson(text):
        from .db import loads
        return loads(text)

    return app
