"""The operator's web interface."""
import collections
import csv
import hmac
import io
import json
import re
import secrets
import time
from urllib.parse import urlparse

import flask

from markupsafe import Markup

from .. import (agent, agent_runs, bounces, contacts, dnscheck, leadtools, mailboxes, offers, perlead, planner,
                prospects, security, sender, service, stats, team, textfmt, writer)
from ..ai_common import ProviderError
from ..config import DEFAULT_SETTINGS, SECRET_SETTINGS
from ..db import loads, now
from ..render import Renderer
from ..sender import day_start, local_time, timezone, warmup_cap

bp = flask.Blueprint('ui', __name__)

PAGE = 50
_login_failures = collections.defaultdict(list)


def _db():
    return flask.current_app.db


def _cfg():
    return flask.current_app.cfg


# -- authentication and CSRF -----------------------------------------------------
def current_user():
    """The team member making this request, or None.

    Behind the mail server's web proxy, the proxy has checked the sign-in and
    says who it is (X-Auth-User), proven by the shared secret. Without it,
    the signed session cookie carries the email and a per-person epoch that
    signing out changes, so old copies of the cookie stop working.
    """
    if 'user' in flask.g:
        return flask.g.user
    cfg, db = _cfg(), _db()
    user = None
    flask.g.denied = ''
    if cfg.AUTH_MODE == 'proxy':
        supplied = flask.request.headers.get('X-Proxy-Auth', '')
        if cfg.PROXY_SECRET and hmac.compare_digest(supplied.encode(), cfg.PROXY_SECRET.encode()):
            email = flask.request.headers.get('X-Auth-User', '').strip().lower()
            # Proxies set up before team accounts existed let only administrators through.
            user = team.get(db, email or cfg.OWNER_EMAIL)
            if user is None or user['disabled']:
                flask.g.denied, user = email, None
    else:
        row = team.get(db, flask.session.get('user', ''))
        if row is not None and not row['disabled'] and row['session_epoch'] \
                and hmac.compare_digest(str(flask.session.get('epoch', '')), row['session_epoch']):
            user = row
    if user is not None:
        team.seen(db, user)
    flask.g.user = user
    return user


def csrf_token():
    if 'csrf' not in flask.session:
        flask.session['csrf'] = secrets.token_urlsafe(24)
    return flask.session['csrf']


@bp.before_request
def guard():
    if flask.request.endpoint == 'ui.login':
        pass
    elif current_user() is None:
        if _cfg().AUTH_MODE == 'proxy':
            if flask.g.denied:
                return flask.render_template('public.html', state='no_access', email=flask.g.denied), 403
            return flask.render_template('public.html', state='forbidden'), 403
        return flask.redirect(flask.url_for('ui.login'))
    elif not team.may(current_user()['role'], flask.request.endpoint, flask.request.method):
        return flask.render_template('public.html', state='admins_only', email=current_user()['email']), 403
    if flask.request.method == 'POST':
        supplied = flask.request.form.get('csrf') or flask.request.headers.get('X-CSRF-Token', '')
        expected = flask.session.get('csrf', '')
        if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
            return flask.render_template('public.html', state='expired'), 400


@bp.context_processor
def inject():
    db = _db()
    pending = service.pending_approvals(db)
    me = current_user()
    return {'csrf_token': csrf_token, 'pending_count': len(pending['campaigns']) + len(pending['messages']),
            'password_auth': _cfg().AUTH_MODE != 'proxy', 'me': me,
            'is_admin': bool(me and me['role'] in ('owner', 'admin')),
            'inbox_unread': db.val('SELECT COUNT(*) FROM replies WHERE read_at IS NULL AND is_auto = 0', default=0),
            'tasks_due': db.val('SELECT COUNT(*) FROM tasks WHERE done_at IS NULL AND due_at <= ?',
                                planner.end_of_today(db.settings()), default=0)}


@bp.errorhandler(service.Problem)
def handle_problem(problem):
    if flask.request.is_json or flask.request.headers.get('X-Requested-With') == 'fetch':
        return {'error': str(problem)}, 400
    flask.flash(str(problem), 'error')
    return flask.redirect(_back())


def _back(default=None):
    """Same-site referrer, or a fallback page."""
    referrer = flask.request.referrer or ''
    parsed = urlparse(referrer)
    if referrer and parsed.netloc == flask.request.host and parsed.path.startswith(_cfg().URL_PREFIX):
        return referrer
    return default or flask.url_for('ui.home')


@bp.route('/login', methods=['GET', 'POST'])
def login():
    cfg = _cfg()
    if cfg.AUTH_MODE == 'proxy':
        return flask.redirect(flask.url_for('ui.home'))
    error = None
    db = _db()
    email = ''
    if flask.request.method == 'POST':
        ip = flask.request.remote_addr or ''
        recent = [t for t in _login_failures[ip] if time.time() - t < 900]
        _login_failures[ip] = recent
        supplied = flask.request.form.get('password', '')
        email = flask.request.form.get('email', '').strip().lower()
        row = team.get(db, email) if email else None
        owner = team.get(db, cfg.OWNER_EMAIL)
        signed_in = None
        if len(recent) >= 10:
            error = 'Too many attempts. Try again in 15 minutes.'
        elif row is not None and not row['disabled'] and team.check_password(row, supplied):
            signed_in = row
        elif (not email or (owner is not None and email in (owner['email'], 'admin'))) and owner is not None \
                and cfg.ADMIN_PASSWORD and hmac.compare_digest(supplied.encode(), cfg.ADMIN_PASSWORD.encode()):
            signed_in = owner   # the installation's owner password always works for the first owner
        else:
            recent.append(time.time())
            error = 'Wrong email or password.'
        if signed_in is not None:
            flask.session.clear()
            flask.session['user'] = signed_in['email']
            flask.session['epoch'] = signed_in['session_epoch']
            flask.session.permanent = True
            csrf_token()
            return flask.redirect(flask.url_for('ui.home'))
    return flask.render_template('login.html', error=error, email=email, owner_email=cfg.OWNER_EMAIL,
                                 configured=bool(cfg.ADMIN_PASSWORD) or bool(db.val(
                                     'SELECT COUNT(*) FROM team WHERE password_hash IS NOT NULL', default=0)))


@bp.route('/logout', methods=['POST'])
def logout():
    me = current_user()
    if _cfg().AUTH_MODE == 'proxy':
        flask.session.clear()
        return flask.redirect('/sso/logout')   # the mail server's sign-out ends the shared sign-in
    if me is not None:
        team.end_sessions(_db(), me['email'])   # every copy of this person's cookie stops working
    flask.session.clear()
    return flask.redirect(flask.url_for('ui.login'))


# -- home -------------------------------------------------------------------------
def _setup_checklist(db, settings):
    cfg = _cfg()
    return [
        {'done': bool(contacts.normalize_email(settings['from_email'])), 'text': 'Set your default sender address',
         'href': flask.url_for('ui.settings') + '#sender'},
        {'done': bool(settings['postal_address'].strip()), 'text': 'Add your postal address (required by anti-spam law)',
         'href': flask.url_for('ui.settings') + '#sender'},
        {'done': db.state('dns_ok') == '1', 'text': 'Connect your domain (DNS records for SPF, DKIM and DMARC)',
         'href': flask.url_for('ui.domains')},
        {'done': bool(settings['reply_imap_user'] and settings['reply_imap_password']),
         'text': 'Connect the mailbox leads reply to, so replies show in the Inbox',
         'href': flask.url_for('ui.settings') + '#replies'},
        {'done': bool(settings['imap_user'] and settings['imap_password']),
         'text': 'Connect the bounce mailbox so bounces and complaints are counted',
         'href': flask.url_for('ui.settings') + '#bounces'},
        {'done': db.val('SELECT COUNT(*) FROM lists', default=0) > 0, 'text': 'Upload your first leads',
         'href': flask.url_for('ui.lists')},
        {'done': agent.configured(settings), 'text': 'Add an AI key to use the agent',
         'href': flask.url_for('ui.settings') + '#ai'},
    ]


@bp.route('/')
def home():
    db = _db()
    settings = db.settings()
    since = now() - 30 * 86400
    return flask.render_template(
        'home.html', active='home',
        totals=stats.overall(db, since),
        series=stats.daily(db, 30, tz=timezone(settings)),
        campaigns=_campaign_rows(db, limit=6),
        checklist=_setup_checklist(db, settings),
        approvals=service.pending_approvals(db),
        warmup=warmup_cap(settings),
        sent_today=db.val("SELECT COUNT(*) FROM messages WHERE kind = 'campaign' AND sent_at >= ?",
                          local_time(settings).replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),
                          default=0),
        server_down=db.state('server_down'),
        new_replies=db.q('SELECT r.*, c.first_name, c.last_name, c.email FROM replies r '
                         'LEFT JOIN contacts c ON c.id = r.contact_id WHERE r.handled_at IS NULL AND r.is_auto = 0 '
                         'ORDER BY r.received_at DESC LIMIT 5'),
        today_tasks=planner.open_tasks(db, planner.end_of_today(settings), limit=8),
        writer_error=db.state('writer_error'),
        lists_count=db.val('SELECT COUNT(*) FROM lists', default=0),
        contacts_count=db.val("SELECT COUNT(*) FROM contacts WHERE status = 'active'", default=0))


# -- lists ------------------------------------------------------------------------
@bp.route('/lists', methods=['GET', 'POST'])
def lists():
    db = _db()
    if flask.request.method == 'POST':
        name = flask.request.form.get('name', '').strip()[:120]
        if not name:
            raise service.Problem('Give the list a name.')
        rows, columns = _read_upload()
        with db.tx():
            list_id = db.run('INSERT INTO lists (name, description, created_at) VALUES (?, ?, ?)',
                             name, flask.request.form.get('description', '').strip()[:500], now()).lastrowid
        if rows:
            summary = prospects.import_upload(db, list_id, rows)
            summary['columns'] = columns
            flask.session['import_summary'] = summary
        return flask.redirect(flask.url_for('ui.list_detail', list_id=list_id))
    rows = [{'row': r, 'counts': contacts.list_counts(db, r['id'])} for r in db.q('SELECT * FROM lists ORDER BY id DESC')]
    return flask.render_template('lists.html', active='lists', lists=rows)


def _list(list_id):
    row = _db().one('SELECT * FROM lists WHERE id = ?', list_id)
    if row is None:
        flask.abort(404)
    return row


@bp.route('/lists/<int:list_id>')
def list_detail(list_id):
    db = _db()
    row = _list(list_id)
    page = max(1, flask.request.args.get('page', 1, type=int))
    query = flask.request.args.get('q', '').strip()
    status = flask.request.args.get('status', '')
    where, args = 'm.list_id = ?', [list_id]
    if query:
        where += " AND (c.email LIKE ? OR c.first_name LIKE ? OR c.last_name LIKE ?)"
        args += [f'%{query}%'] * 3
    if status in ('active', 'unsubscribed', 'bounced', 'complained'):
        where += ' AND c.status = ?'
        args.append(status)
    elif status in ('invalid', 'risky', 'unknown'):
        where += ' AND c.verify_status = ?'
        args.append(status)
    total = db.val(f'SELECT COUNT(*) FROM list_members m JOIN contacts c ON c.id = m.contact_id WHERE {where}',
                   *args, default=0)
    members = db.q(f'SELECT c.* FROM list_members m JOIN contacts c ON c.id = m.contact_id WHERE {where} '
                   'ORDER BY m.added_at DESC, c.id DESC LIMIT ? OFFSET ?', *args, PAGE, (page - 1) * PAGE)
    summary = flask.session.pop('import_summary', None)
    return flask.render_template(
        'list.html', active='lists', list=row, counts=contacts.list_counts(db, list_id),
        members=[dict(m, extra=_brief_fields(loads(m['fields']))) for m in members], total=total, page=page,
        pages=max(1, -(-total // PAGE)), q=query, status=status, summary=summary,
        verify=flask.session.pop('verify_summary', None),
        verify_counts={r['verify_status'] or 'unchecked': r['n'] for r in db.q(
            'SELECT c.verify_status, COUNT(*) AS n FROM list_members m JOIN contacts c ON c.id = m.contact_id '
            'WHERE m.list_id = ? GROUP BY c.verify_status', list_id)},
        results=next((r for r in stats.by_list(db) if r['key'] == list_id), None),
        waiting=[dict(p, label=prospects.label(p)) for p in prospects.waiting(db, list_id)],
        pcounts=prospects.counts(db, list_id), prospect_labels=prospects.STATUSES)


@bp.route('/lists/<int:list_id>/find-emails', methods=['POST'])
def list_find_emails(list_id):
    _list(list_id)
    started = agent_runs.find_emails(_db(), list_id)
    flask.flash(f'Looking for the emails of {started} lead{"" if started == 1 else "s"} on their websites.' if started
                else 'No leads are waiting for an email search.')
    return flask.redirect(flask.url_for('ui.list_detail', list_id=list_id) + '#no-email')


@bp.route('/lists/<int:list_id>/prospects/<int:prospect_id>', methods=['POST'])
def list_prospect(list_id, prospect_id):
    db = _db()
    _list(list_id)
    if not db.one('SELECT id FROM prospects WHERE id = ? AND list_id = ?', prospect_id, list_id):
        flask.abort(404)
    if flask.request.form.get('action') == 'dismiss':
        prospects.dismiss(db, prospect_id)
    else:
        try:
            prospects.set_email(db, prospect_id, flask.request.form.get('email', ''))
        except ValueError as exc:
            raise service.Problem(str(exc))
        flask.flash('Added to the list.')
    return flask.redirect(flask.url_for('ui.list_detail', list_id=list_id) + '#no-email')


BRIEF_KEYS = ('company', 'business', 'company_name', 'organisation', 'organization', 'role', 'title', 'job_title',
              'location', 'city', 'country', 'website', 'current_website', 'domain')


def _brief_fields(fields, limit=3):
    """A few short, useful columns of a lead for tables: (shown, how many more)."""
    keys = [k for k in BRIEF_KEYS if fields.get(k)] + [k for k in fields if k not in BRIEF_KEYS]
    shown = [(k, fields[k]) for k in keys if fields[k] and len(str(fields[k])) <= 80][:limit]
    return {'shown': shown, 'more': max(0, len([k for k in fields if fields[k]]) - len(shown))}


def _read_upload(required=False):
    """The uploaded file or pasted text of an import form: (rows, columns)."""
    upload = flask.request.files.get('file')
    pasted = flask.request.form.get('pasted', '').strip()
    if (upload is None or not upload.filename) and not pasted:
        if required:
            raise service.Problem('Choose a file (CSV or Excel) or paste addresses.')
        return [], None
    if flask.request.form.get('consent') != 'yes':
        raise service.Problem('Confirm you have a lawful reason to email these people (the tick box above Upload).')
    try:
        if upload is not None and upload.filename:
            rows, columns = contacts.parse_upload(upload.filename, upload.read())
        else:
            rows, columns = contacts.parse_csv(pasted)
    except ValueError as exc:
        raise service.Problem(str(exc))
    if not rows:
        raise service.Problem('No leads were found. The file needs a column of email addresses or websites.')
    return rows, columns


@bp.route('/lists/<int:list_id>/import', methods=['POST'])
def list_import(list_id):
    db = _db()
    _list(list_id)
    rows, columns = _read_upload(required=True)
    summary = prospects.import_upload(db, list_id, rows)
    summary['columns'] = columns
    flask.session['import_summary'] = summary
    return flask.redirect(flask.url_for('ui.list_detail', list_id=list_id))


@bp.route('/lists/<int:list_id>/export.csv')
def list_export(list_id):
    db = _db()
    row = _list(list_id)
    members = db.q('SELECT c.* FROM list_members m JOIN contacts c ON c.id = m.contact_id WHERE m.list_id = ? '
                   'ORDER BY c.email', list_id)
    keys = sorted({key for m in members for key in loads(m['fields'])})
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['email', 'first_name', 'last_name', 'status'] + keys)
    for member in members:
        extra = loads(member['fields'])
        writer.writerow([_csv_safe(v) for v in (member['email'], member['first_name'], member['last_name'],
                                                 member['status'])] + [_csv_safe(extra.get(k, '')) for k in keys])
    name = ''.join(c if c.isalnum() else '-' for c in row['name']).strip('-') or 'list'
    return flask.Response(buffer.getvalue(), mimetype='text/csv',
                          headers={'Content-Disposition': f'attachment; filename="{name}.csv"'})


def _csv_safe(value):
    """Stops spreadsheet programs from running a cell as a formula."""
    value = str(value)
    return "'" + value if value[:1] in ('=', '+', '-', '@', '\t', '\r') else value


@bp.route('/lists/<int:list_id>/remove', methods=['POST'])
def list_remove_member(list_id):
    _db().run('DELETE FROM list_members WHERE list_id = ? AND contact_id = ?', list_id,
              flask.request.form.get('contact_id', type=int))
    return flask.redirect(_back(flask.url_for('ui.list_detail', list_id=list_id)))


@bp.route('/lists/<int:list_id>/delete', methods=['POST'])
def list_delete(list_id):
    db = _db()
    _list(list_id)
    if db.val("SELECT COUNT(*) FROM campaigns WHERE list_id = ? AND status IN ('scheduled', 'sending', 'paused')",
              list_id, default=0):
        raise service.Problem('A campaign is still sending to this list. Cancel it first.')
    with db.tx():
        db.run('DELETE FROM lists WHERE id = ?', list_id)
        # Contacts that are in no list and were never mailed serve no purpose.
        db.run('DELETE FROM contacts WHERE id NOT IN (SELECT contact_id FROM list_members) '
               'AND id NOT IN (SELECT contact_id FROM messages WHERE contact_id IS NOT NULL)')
    flask.flash('List deleted. Unsubscribes and bounces from it are kept on the suppression list.')
    return flask.redirect(flask.url_for('ui.lists'))


@bp.route('/suppressions', methods=['GET', 'POST'])
def suppressions():
    db = _db()
    if flask.request.method == 'POST':
        action = flask.request.form.get('action')
        if action == 'remove':
            contacts.unsuppress(db, flask.request.form.get('email', ''))
            flask.flash('Address removed from the suppression list.')
        else:
            added = 0
            for line in flask.request.form.get('emails', '').replace(',', '\n').splitlines():
                email = contacts.normalize_email(line)
                if email:
                    contacts.suppress(db, email, 'manual', 'Added by hand')
                    added += 1
            flask.flash(f'{added} address{"es" if added != 1 else ""} added.')
        return flask.redirect(flask.url_for('ui.suppressions'))
    page = max(1, flask.request.args.get('page', 1, type=int))
    reason = flask.request.args.get('reason', '')
    where, args = '1 = 1', []
    if reason in ('unsubscribed', 'bounced', 'complained', 'manual'):
        where, args = 'reason = ?', [reason]
    total = db.val(f'SELECT COUNT(*) FROM suppressions WHERE {where}', *args, default=0)
    rows = db.q(f'SELECT * FROM suppressions WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?',
                *args, PAGE, (page - 1) * PAGE)
    counts = {r['reason']: r['n'] for r in db.q('SELECT reason, COUNT(*) AS n FROM suppressions GROUP BY reason')}
    return flask.render_template('suppressions.html', active='lists', rows=rows, total=total, page=page,
                                 pages=max(1, -(-total // PAGE)), reason=reason, counts=counts)


# -- campaigns --------------------------------------------------------------------
def _campaign_rows(db, limit=200):
    rows = db.q('SELECT c.*, l.name AS list_name FROM campaigns c LEFT JOIN lists l ON l.id = c.list_id '
                'ORDER BY c.id DESC LIMIT ?', limit)
    return [{'row': r, 'stats': stats.campaign(db, r['id']) if r['started_at'] else None} for r in rows]


@bp.route('/campaigns')
def campaigns():
    return flask.render_template('campaigns.html', active='campaigns', campaigns=_campaign_rows(_db()))


@bp.route('/campaigns/new', methods=['POST'])
def campaign_new():
    db = _db()
    campaign_id = service.create_campaign(db, db.settings(), {
        'name': 'Untitled campaign', 'subject': '', 'html': STARTER_HTML,
        'list_id': flask.request.form.get('list_id', type=int)})
    return flask.redirect(flask.url_for('ui.campaign', campaign_id=campaign_id))


STARTER_HTML = """<h1 style="font-size:22px;margin:0 0 16px">Hello {{first_name|there}},</h1>
<p>Write your message here. Keep it short and tell the reader why it matters to them.</p>
<p><a href="https://example.com" style="display:inline-block;padding:10px 18px;background:#1f2937;color:#ffffff;text-decoration:none;border-radius:6px">Call to action</a></p>
<p>Thanks,<br>Your name</p>"""


@bp.route('/campaigns/<int:campaign_id>')
def campaign(campaign_id):
    db = _db()
    settings = db.settings()
    row = service.get_campaign(db, campaign_id)
    editable = row['status'] in service.EDITABLE
    context = dict(
        active='campaigns', c=row, editable=editable, settings=settings,
        lists=[{'row': r, 'counts': contacts.list_counts(db, r['id'])} for r in db.q('SELECT * FROM lists ORDER BY name')],
        report=loads(row['spam_report'], default=None) if row['spam_report'] else None,
        problems=service.readiness(db, settings, row) if editable else [],
        local_now=local_time(settings).strftime('%Y-%m-%dT%H:%M'),
        scheduled_local=local_time(settings, row['scheduled_at']).strftime('%Y-%m-%dT%H:%M') if row['scheduled_at'] else '',
        test_default=settings['reply_to'] or settings['from_email'],
        steps=db.q('SELECT * FROM campaign_steps WHERE campaign_id = ? ORDER BY position', campaign_id),
        steps_editable=service.steps_editable(row), send_if=service.SEND_IF,
        ai_modes=writer.MODES, ai_ready=agent.configured(settings),
        writing=writer.progress(db, campaign_id) if row['ai_mode'] in writer.AI_MODES else None,
        own=perlead.counts(db, row) if row['ai_mode'] == 'own' else None,
        own_columns=perlead.columns(db, row['list_id']) if row['list_id'] else [],
        own_guess=perlead.guess(perlead.columns(db, row['list_id'])) if row['list_id'] else ('', ''),
        writing_samples=db.q("SELECT p.*, c.email, c.first_name, c.last_name FROM personalizations p "
                             "JOIN contacts c ON c.id = p.contact_id WHERE p.campaign_id = ? "
                             "AND p.status IN ('done', 'edited', 'failed') ORDER BY p.updated_at DESC LIMIT 3",
                             campaign_id) if row['ai_mode'] in writer.AI_MODES else [],
        preview_results=flask.session.pop(f'ai_preview_{campaign_id}', None),
        writer_error=db.state('writer_error'),
        accounts=mailboxes.summary(db, day_start(settings)),
        chosen_accounts=mailboxes.parse_ids(row['mailbox_ids']),
        quality=leadtools.list_quality(db, row['list_id']) if row['list_id'] else None,
        step_templates=service.STEP_TEMPLATES, ab_on=sender.ab_active(row))
    if row['started_at']:
        context.update(
            stats=stats.campaign(db, campaign_id),
            series=stats.daily(db, 14, campaign_id, tz=timezone(settings)),
            links=stats.top_links(db, campaign_id),
            domains=stats.by_domain(db, 0, campaign_id),
            eta=stats.hours_until_done(db, row),
            step_stats=stats.by_step(db, campaign_id),
            ab=sender.ab_results(db, campaign_id) if sender.ab_active(row) else None,
            funnel=stats.funnel(db, campaign_id=campaign_id),
            failures=db.q("SELECT to_email, status, bounce_type, error FROM messages WHERE campaign_id = ? "
                          "AND status IN ('failed', 'bounced') ORDER BY rowid DESC LIMIT 15", campaign_id))
    return flask.render_template('campaign.html', **context)


def _form_campaign():
    data = flask.request.form.to_dict()
    data['window_days'] = flask.request.form.getlist('window_days') or '0123456'
    data['track_opens'] = '1' if flask.request.form.get('track_opens') else '0'
    data['track_clicks'] = '1' if flask.request.form.get('track_clicks') else '0'
    if 'ai_mode' in data:
        data['ai_research'] = '1' if flask.request.form.get('ai_research') else '0'
        data['ai_news'] = '1' if flask.request.form.get('ai_news') else '0'
        data['skip_risky'] = '1' if flask.request.form.get('skip_risky') else '0'
    if data.pop('accounts_shown', None):
        data['mailbox_ids'] = flask.request.form.getlist('mailbox_ids')
    for key in ('csrf', 'action', 'test_to', 'send_at', 'when'):
        data.pop(key, None)
    for key in [k for k in data if k.startswith('step_')]:
        data.pop(key)
    return data


def _save_steps(db, campaign_id):
    """Saves the follow-up editors that were on the page."""
    form = flask.request.form
    for position in {int(k.split('_')[1]) for k in form if k.startswith('step_') and k.split('_')[1].isdigit()}:
        prefix = f'step_{position}_'
        service.update_step(db, campaign_id, position, {
            'delay_days': form.get(prefix + 'delay_days'), 'send_if': form.get(prefix + 'send_if', 'no_reply'),
            'as_reply': '1' if form.get(prefix + 'as_reply') else '0', 'subject': form.get(prefix + 'subject', ''),
            'html': form.get(prefix + 'html', '')})


@bp.route('/campaigns/<int:campaign_id>/save', methods=['POST'])
def campaign_save(campaign_id):
    db = _db()
    cfg = _cfg()
    settings = db.settings()
    row = service.get_campaign(db, campaign_id)
    form = flask.request.form
    action = form.get('action', 'save')
    if row['status'] in service.EDITABLE:
        row = service.update_campaign(db, settings, campaign_id, _form_campaign())
    else:
        pacing = {k: v for k, v in _form_campaign().items()
                  if k in ('rate_per_hour', 'delay_min', 'delay_max', 'window_start', 'window_end', 'window_days', 'daily_cap')}
        row = service.update_campaign(db, settings, campaign_id, pacing)
    if service.steps_editable(row):
        _save_steps(db, campaign_id)
    target = flask.url_for('ui.campaign', campaign_id=campaign_id)
    if action.startswith('add_step'):
        position = service.add_step(db, campaign_id, template=action.partition(':')[2] or None)
        return flask.redirect(target + f'#followup-{position}')
    if action == 'verify_list':
        if row['list_id']:
            counts = leadtools.verify_list(db, row['list_id'])
            flask.flash('Every address on the list was already checked.' if counts is None else
                        f'Checked {sum(counts.values())} address{"es" if sum(counts.values()) != 1 else ""}: '
                        f'{counts["invalid"]} would bounce, '
                        f'{counts["risky"]} risky.')
        return flask.redirect(target + '#check')
    if action.startswith('delete_step_'):
        position = action.rsplit('_', 1)[1]
        if not position.isdigit():
            raise service.Problem('Unknown follow-up')
        service.delete_step(db, campaign_id, int(position))
        flask.flash('Follow-up deleted.')
        return flask.redirect(target + '#followups')
    if action == 'ai_preview':
        try:
            results = writer.preview(db, settings, row, 3)
        except (agent.ai.NotConfigured, agent.ProviderError) as exc:
            raise service.Problem(str(exc))
        flask.session[f'ai_preview_{campaign_id}'] = [
            {'email': r['contact']['email'], 'name': f'{r["contact"]["first_name"]} {r["contact"]["last_name"]}'.strip(),
             'subject': r['subject'], 'body': r['body'], 'error': r['error']} for r in results]
        return flask.redirect(target + '#ai')
    if action == 'ai_all':
        added = writer.ensure_rows(db, row)
        flask.flash(f'The AI is writing the emails for {added or "the remaining"} leads in the background. '
                    'You can leave this page; sending waits until each email is ready.')
        return flask.redirect(target + '#ai')
    if action == 'own_columns':
        if not row['list_id']:
            raise service.Problem('Choose the list first.')
        body_col = form.get('own_body_col', '')
        if not body_col:
            raise service.Problem("Choose the column that holds each lead's message.")
        result = perlead.fill_from_columns(db, row, form.get('own_subject_col', ''), body_col)
        flask.flash(f'{result["filled"]} lead{"s" if result["filled"] != 1 else ""} now have their own email from '
                    f'"{body_col}".' + (f' {result["empty"]} had nothing in that column and get the main email.'
                                        if result['empty'] else ''))
        return flask.redirect(target + '#ai')
    if action == 'own_draft':
        if not row['list_id']:
            raise service.Problem('Choose the list first.')
        added = perlead.fill_from_draft(db, row)
        flask.flash(f'{added} lead{"s" if added != 1 else ""} got a copy of the main email to edit.' if added
                    else 'Every lead already has their own email.')
        return flask.redirect(flask.url_for('ui.campaign_each', campaign_id=campaign_id))
    if action == 'ai_retry':
        count = writer.retry_failed(db, campaign_id)
        flask.flash(f'{count} email{"s" if count != 1 else ""} will be written again.')
        return flask.redirect(target + '#ai')
    if action == 'check':
        service.check_campaign(db, cfg, settings, row)
        return flask.redirect(target + '#check')
    if action == 'test':
        service.send_test(db, cfg, settings, campaign_id, form.get('test_to', ''))
        flask.flash(f'Test queued for {form.get("test_to", "").strip()}. It should arrive within a minute.')
        return flask.redirect(target + '#send')
    if action == 'schedule':
        service.check_campaign(db, cfg, settings, row)
        when = form.get('send_at') if form.get('when') == 'later' else None
        service.schedule_campaign(db, settings, campaign_id, when)
        flask.flash('Campaign scheduled.' if when else 'Campaign started.')
        return flask.redirect(target)
    flask.flash('Saved.')
    return flask.redirect(target)


@bp.route('/campaigns/<int:campaign_id>/state', methods=['POST'])
def campaign_state(campaign_id):
    db = _db()
    action = flask.request.form.get('action', '')
    if action == 'approve':
        service.approve_campaign(db, db.settings(), campaign_id)
        flask.flash('Approved. The campaign is scheduled.')
    elif action == 'duplicate':
        new_id = service.duplicate_campaign(db, campaign_id)
        return flask.redirect(flask.url_for('ui.campaign', campaign_id=new_id))
    elif action == 'delete':
        service.delete_campaign(db, campaign_id)
        flask.flash('Campaign deleted.')
        return flask.redirect(flask.url_for('ui.campaigns'))
    else:
        service.set_state(db, campaign_id, action)
    return flask.redirect(_back(flask.url_for('ui.campaign', campaign_id=campaign_id)))


@bp.route('/campaigns/<int:campaign_id>/preview')
def campaign_preview(campaign_id):
    """The first email as one lead gets it (?contact=id), else as a sample lead gets it."""
    db = _db()
    row = service.get_campaign(db, campaign_id)
    contact_id = flask.request.args.get('contact', type=int)
    contact = None
    if contact_id:
        contact = db.one('SELECT c.* FROM list_members m JOIN contacts c ON c.id = m.contact_id '
                         'WHERE m.list_id = ? AND c.id = ?', row['list_id'] or 0, contact_id)
        if contact is None:
            flask.abort(404)
    contact = contact or service.sample_contact(db, row)
    extra = {}
    if row['ai_mode'] and contact['id']:
        content = sender.message_content(db, row, {'contact_id': contact['id'], 'step': 0, 'parent_id': None})
        extra = {'content': content['content'], 'ai_line': content['ai_line']}
    subject, html, _ = Renderer(_cfg(), db).campaign_bodies(row, contact, '0' * 32, db.settings(), tracking=False,
                                                           **extra)
    if flask.request.args.get('framed'):
        html = _framed(subject, contact, html)
    response = flask.Response(html, mimetype='text/html')
    # The preview is untrusted author HTML: no scripts, no access to this site.
    # Author HTML: no scripts, forms or plugins; may only be framed by this app.
    response.headers['Content-Security-Policy'] = ("default-src 'none'; img-src * data:; style-src 'unsafe-inline'; "
                                                   "form-action 'none'; frame-ancestors 'self'")
    return response


def _framed(subject, contact, html):
    """A preview page with the subject and recipient above the email."""
    import html as html_lib
    head = ('<div style="font:14px Arial,sans-serif;padding:12px 16px;background:#f6f6fa;border-bottom:1px solid #e1e0ea">'
            f'<div style="color:#5f5d76">To: {html_lib.escape(contact["email"])}</div>'
            f'<div style="font-size:16px;font-weight:bold;margin-top:4px">{html_lib.escape(subject)}</div></div>')
    if '<body' in html.lower():
        return re.sub(r'(<body[^>]*>)', lambda m: m.group(1) + head, html, count=1, flags=re.IGNORECASE)
    return head + html


# -- emails -----------------------------------------------------------------------
@bp.route('/emails')
def emails():
    db = _db()
    page = max(1, flask.request.args.get('page', 1, type=int))
    status = flask.request.args.get('status', '')
    kind = flask.request.args.get('kind', '')
    query = flask.request.args.get('q', '').strip()
    where, args = '1 = 1', []
    if status in ('queued', 'sent', 'failed', 'bounced', 'complained', 'cancelled', 'pending_approval'):
        where += ' AND m.status = ?'
        args.append(status)
    elif status == 'opened':
        where += ' AND m.opened_at IS NOT NULL'
    elif status == 'clicked':
        where += ' AND m.clicked_at IS NOT NULL'
    if kind in ('campaign', 'api', 'test', 'agent', 'single', 'reply'):
        where += ' AND m.kind = ?'
        args.append(kind)
    if query:
        where += ' AND (m.to_email LIKE ? OR m.subject LIKE ?)'
        args += [f'%{query}%'] * 2
    total = db.val(f'SELECT COUNT(*) FROM messages m WHERE {where}', *args, default=0)
    rows = db.q(f'SELECT m.*, c.name AS campaign_name FROM messages m LEFT JOIN campaigns c ON c.id = m.campaign_id '
                f'WHERE {where} ORDER BY COALESCE(m.sent_at, m.created_at) DESC LIMIT ? OFFSET ?',
                *args, PAGE, (page - 1) * PAGE)
    return flask.render_template('emails.html', active='emails', rows=rows, total=total, page=page,
                                 pages=max(1, -(-total // PAGE)), status=status, kind=kind, q=query)


@bp.route('/emails/<mid>')
def email_detail(mid):
    db = _db()
    row = db.one('SELECT m.*, c.name AS campaign_name FROM messages m LEFT JOIN campaigns c ON c.id = m.campaign_id '
                 'WHERE m.id = ?', mid)
    if row is None:
        flask.abort(404)
    history = db.q('SELECT e.*, l.url FROM events e LEFT JOIN links l ON l.id = e.link_id '
                   'WHERE e.message_id = ? ORDER BY e.id', mid)
    return flask.render_template('email.html', active='emails', m=row, events=history, payload=loads(row['payload']))


@bp.route('/emails/<mid>/decide', methods=['POST'])
def email_decide(mid):
    db = _db()
    if flask.request.form.get('action') == 'approve':
        service.approve_message(db, mid)
        flask.flash('Approved. The email is being sent.')
    else:
        service.discard_message(db, mid)
        flask.flash('Discarded.')
    return flask.redirect(_back(flask.url_for('ui.emails')))


# -- analytics --------------------------------------------------------------------
@bp.route('/analytics')
def analytics():
    db = _db()
    settings = db.settings()
    days = flask.request.args.get('days', 30, type=int)
    days = days if days in (7, 30, 90, 365) else 30
    since = now() - days * 86400
    campaign_rows = stats.by_campaign(db, since)
    per_campaign = stats.funnel_counts(db, since, per_campaign=True)
    for row in campaign_rows:
        stages = per_campaign.get(row['key'], {})
        row.update(interested=stages.get('interested', 0), meetings=stages.get('meeting', 0))
    return flask.render_template(
        'analytics.html', active='analytics', days=days,
        totals=stats.overall(db, since),
        funnel=stats.funnel(db, since),
        accounts=stats.by_account(db, since),
        series=stats.daily(db, min(days, 90), tz=timezone(settings)),
        campaigns=campaign_rows,
        lists=stats.by_list(db, since),
        domains=stats.by_domain(db, since),
        links=stats.top_links(db, None, since),
        bounce_mailbox=bool(settings['imap_user'] and settings['imap_password']))


# -- agent ------------------------------------------------------------------------
def _thread(thread_id):
    row = _db().one('SELECT * FROM agent_threads WHERE id = ?', thread_id)
    if row is None:
        flask.abort(404)
    return row


_OLD_UPLOAD_RE = re.compile(r'\n*\[Uploaded (?P<file>[^:\]]+):[^\]]*\]\s*$')


def _user_block(message):
    body = message['content']
    shown = body.get('shown')
    attachment = body.get('attachment')
    if shown is None:
        shown = body.get('text', '')
        match = _OLD_UPLOAD_RE.search(shown)       # conversations from before attachments were stored apart
        if match:
            attachment = attachment or {'file': match.group('file').strip()}
            shown = shown[:match.start()]
    html = Markup('<br>'.join(Markup.escape(line) for line in shown.strip().split('\n')))
    return {'kind': 'user', 'id': message['id'], 'html': html, 'attachment': attachment}


def _present(messages):
    """Stored agent messages -> blocks for the chat: the owner's messages, the agent's answers
    (formatted), and the steps it took folded into one block between them."""
    out, calls = [], {}
    for message in messages:
        body = message['content']
        if message['role'] == 'user':
            out.append(_user_block(message))
        elif message['role'] == 'assistant':
            for call in body.get('tool_calls') or []:
                calls[call['id']] = call
            if body.get('text', '').strip():
                out.append({'kind': 'assistant', 'id': message['id'],
                            'html': textfmt.markdown(textfmt.plain_punctuation(body['text']))})
            if body.get('note'):
                out.append({'kind': 'note', 'id': message['id'], 'text': body['note']})
        elif message['role'] == 'tool':
            items = []
            for result in body.get('results', []):
                call = calls.get(result['id'], {})
                error = bool(result.get('is_error'))
                items.append({'error': error, 'text': _tool_summary(result.get('name', ''), call.get('input') or {},
                                                                     result.get('output', ''), error)})
            if out and out[-1]['kind'] == 'steps':
                out[-1]['items'] += items
            else:
                out.append({'kind': 'steps', 'id': message['id'], 'items': items})
    return out


TOOL_LABELS = {
    'get_overview': 'Looked at the account', 'list_contact_lists': 'Looked at your lists',
    'list_campaigns': 'Looked at the campaigns', 'get_campaign': 'Opened campaign',
    'create_campaign': 'Created a draft campaign', 'update_campaign': 'Edited campaign',
    'check_campaign': 'Checked the email', 'send_test_email': 'Sent a test email',
    'schedule_campaign': 'Scheduling campaign', 'change_campaign_state': 'Changed campaign state',
    'get_statistics': 'Read the results', 'send_email': 'Prepared an email',
    'set_ai_writing': 'Set up AI writing', 'preview_ai_writing': 'Wrote samples for a few leads',
    'use_own_email_per_lead': 'Gave each lead their own email',
    'add_follow_up': 'Added a follow-up', 'get_inbox': 'Read the inbox', 'send_reply': 'Prepared a reply',
    'set_lead_status': 'Moved a lead', 'create_task': 'Added a follow-up to the calendar',
    'list_tasks': 'Looked at the calendar', 'find_email': 'Looked for an email address',
    'check_email': 'Checked email addresses', 'research_website': 'Read a website',
    'list_leads': 'Read the leads', 'lead_news': 'Read the latest news',
    'set_reply_autopilot': 'Changed how the AI answers replies', 'search_leads': 'Searched your leads',
    'get_lead': 'Opened a lead', 'research_lead': 'Researched a lead', 'save_lead_notes': 'Saved notes on a lead',
    'update_lead': 'Updated a lead', 'list_prospects': 'Looked at leads without an email',
    'find_missing_emails': 'Looked for emails on their websites', 'set_prospect_email': 'Added a lead to the list',
    'create_list': 'Created a list', 'add_leads': 'Added leads', 'get_offers': 'Read your offers',
    'save_offer': 'Saved the offer', 'web_search': 'Searched the web',
}


def _tool_summary(name, args, output, is_error):
    label = TOOL_LABELS.get(name, name.replace('_', ' ').capitalize())
    try:
        data = json.loads(output)
    except ValueError:
        data = {}
    if is_error:
        return f'{label}: {data.get("error", "failed") if isinstance(data, dict) else "failed"}'
    detail = ''
    if isinstance(data, dict):
        if name in ('create_campaign', 'update_campaign', 'check_campaign'):
            check = data.get('check') or {}
            verdict = {'good': 'no problems', 'review': 'worth a look', 'fix': 'needs fixes'}.get(check.get('verdict'), '')
            detail = f'#{data.get("campaign_id")}' + (f', {verdict}' if verdict else '')
        elif name == 'schedule_campaign':
            waiting = data.get('status') == 'pending_approval'
            detail = (f'#{data.get("campaign_id")} to {data.get("recipients")} contacts: '
                      + ('waiting for your approval' if waiting else f'scheduled for {data.get("start_local")}'))
        elif name == 'send_email':
            detail = f'to {data.get("to")}: ' + ('waiting for your approval' if data.get('status') == 'pending_approval' else 'queued')
        elif name == 'send_test_email':
            detail = f'to {data.get("to")}'
        elif name == 'change_campaign_state':
            detail = f'#{data.get("campaign_id")} is now {data.get("status")}'
        elif name in ('get_campaign', 'get_statistics') and args.get('campaign_id'):
            detail = f'#{args["campaign_id"]}'
        elif name == 'find_missing_emails' and 'checked' in data:
            detail = (f'{len(data.get("added") or [])} found and added, {len(data.get("guessed") or [])} guessed, '
                      f'{len(data.get("not_found") or [])} not found')
        elif name == 'add_leads':
            detail = f'{data.get("added", 0)} added' + (f', {data["need_an_email"]} need an email' if data.get('need_an_email') else '')
        elif name in ('research_lead', 'get_lead', 'save_lead_notes', 'update_lead'):
            detail = str(data.get('lead') or data.get('email') or args.get('email') or '')
        elif name == 'web_search':
            detail = f'"{args.get("query", "")}"'
        elif name == 'save_offer':
            detail = f'"{data.get("name", "")}"'
    if name == 'research_website' and args.get('url'):
        detail = args['url']
    if name in ('find_missing_emails', 'add_leads') and detail:
        return f'{label}: {detail}'
    return f'{label} {detail}'.strip()


def _agent_state(thread_id):
    db = _db()
    pending = service.pending_approvals(db)
    return dict(agent_runs.state(db, thread_id), pending=len(pending['campaigns']) + len(pending['messages']))


def _blocks_html(blocks):
    return flask.render_template('chat_blocks.html', blocks=blocks)


@bp.route('/agent')
def agent_home():
    db = _db()
    latest = db.one("SELECT id FROM agent_threads WHERE kind = 'outreach' ORDER BY updated_at DESC LIMIT 1")
    thread_id = latest['id'] if latest else agent.new_thread(db)
    return flask.redirect(flask.url_for('ui.agent_thread', thread_id=thread_id))


@bp.route('/agent/new', methods=['POST'])
def agent_new():
    db = _db()
    kind = flask.request.form.get('kind', 'outreach')
    kind = kind if kind in agent.PERSONAS else 'outreach'
    empty = db.one('SELECT t.id FROM agent_threads t WHERE t.kind = ? AND NOT EXISTS '
                   '(SELECT 1 FROM agent_messages m WHERE m.thread_id = t.id) LIMIT 1', kind)
    thread_id = empty['id'] if empty else agent.new_thread(db, kind)
    return flask.redirect(flask.url_for('ui.agent_thread', thread_id=thread_id))


@bp.route('/agent/<int:thread_id>')
def agent_thread(thread_id):
    db = _db()
    settings = db.settings()
    thread = _thread(thread_id)
    provider = settings['ai_provider']
    messages = agent.history(db, thread_id)
    last_user = next((m['id'] for m in reversed(messages) if m['role'] == 'user'), 0)
    return flask.render_template(
        'agent.html', active='offers' if thread['kind'] == 'offer' else 'agent', thread=thread,
        threads=db.q('SELECT * FROM agent_threads WHERE kind = ? ORDER BY updated_at DESC LIMIT 30', thread['kind']),
        blocks=_present(messages), last_user=last_user, state=agent_runs.state(db, thread_id),
        configured=agent.configured(settings), approvals=service.pending_approvals(db),
        require_approval=settings['ai_require_approval'] == '1',
        provider_label=agent.PROVIDERS[provider].LABEL if provider in agent.PROVIDERS else '',
        model=settings.get(f'ai_model_{provider}') or getattr(agent.PROVIDERS.get(provider), 'DEFAULT_MODEL', ''),
        offer=db.one('SELECT * FROM offers ORDER BY updated_at DESC LIMIT 1'))


def _attachment_note(db, attachment):
    """What the agent is told about leads attached in the chat (from the database, not the browser)."""
    try:
        list_id = int(attachment.get('list_id'))
    except (TypeError, ValueError):
        return None, None
    row = db.one('SELECT * FROM lists WHERE id = ?', list_id)
    if row is None:
        return None, None
    counts = contacts.list_counts(db, list_id)
    waiting = prospects.counts(db, list_id)
    columns = set()
    for member in db.q('SELECT c.fields FROM list_members m JOIN contacts c ON c.id = m.contact_id '
                       'WHERE m.list_id = ? LIMIT 50', list_id):
        columns.update(loads(member['fields']).keys())
    for prospect in db.q('SELECT fields FROM prospects WHERE list_id = ? LIMIT 50', list_id):
        columns.update(loads(prospect['fields']).keys())
    file = str(attachment.get('file') or row['name'])[:120]
    need = waiting.get('needs_email', 0)
    note = (f'[Attached file "{file}": it became list "{row["name"]}" (list_id {list_id}) with {counts["total"]} '
            f'leads that have an email' + (f' and {need} without one (find_missing_emails can look for them)' if need else '')
            + (f'. Columns: {", ".join(sorted(columns)[:30])}' if columns else '') + '.]')
    info = {'list_id': list_id, 'name': row['name'], 'file': file, 'leads': counts['total'], 'needs_email': need}
    return note, info


@bp.route('/agent/<int:thread_id>/send', methods=['POST'])
def agent_send(thread_id):
    db = _db()
    thread = _thread(thread_id)
    data = flask.request.get_json(silent=True) or {}
    text = str(data.get('text', '')).strip()[:20000]
    note, info = _attachment_note(db, data.get('attachment') or {}) if data.get('attachment') else (None, None)
    if not text and info is None:
        return {'error': 'Type a message first.'}, 400
    shown = text
    if info is not None and not text:
        text = shown = ('Read these leads and tell me who they are. Find emails for the ones without one, '
                        'then suggest a first email.' if thread['kind'] == 'outreach' else 'Here are the leads I email.')
    try:
        saved = agent_runs.start(db, _cfg(), thread_id, f'{text}\n\n{note}' if note else text, shown=shown,
                                 attachment=info)
    except agent_runs.Busy as exc:
        return {'error': str(exc)}, 409
    except service.Problem as exc:
        return {'error': str(exc)}, 400
    return {'after': saved['id'], 'html': _blocks_html(_present([saved])), **_agent_state(thread_id)}


@bp.route('/agent/<int:thread_id>/poll')
def agent_poll(thread_id):
    db = _db()
    _thread(thread_id)
    after = flask.request.args.get('after', 0, type=int)
    messages = [m for m in agent.history(db, thread_id) if m['id'] > after]
    return {'html': _blocks_html(_present(messages)), **_agent_state(thread_id)}


@bp.route('/agent/<int:thread_id>/stop', methods=['POST'])
def agent_stop(thread_id):
    _thread(thread_id)
    agent_runs.stop(_db(), thread_id)
    return {'ok': True}


@bp.route('/agent/<int:thread_id>/retry', methods=['POST'])
def agent_retry(thread_id):
    db = _db()
    _thread(thread_id)
    try:
        agent_runs.start(db, _cfg(), thread_id)
    except agent_runs.Busy as exc:
        return {'error': str(exc)}, 409
    except service.Problem as exc:
        return {'error': str(exc)}, 400
    return _agent_state(thread_id)


@bp.route('/agent/<int:thread_id>/upload', methods=['POST'])
def agent_upload(thread_id):
    """Leads attached in the chat become a new list; the next message tells the agent about it."""
    db = _db()
    _thread(thread_id)
    upload = flask.request.files.get('file')
    if upload is None or not upload.filename:
        return {'error': 'Choose a CSV or Excel file.'}, 400
    if flask.request.form.get('consent') != 'yes':
        return {'error': 'Confirm you have a lawful reason to email these people.'}, 400
    try:
        rows, columns = contacts.parse_upload(upload.filename, upload.read())
    except ValueError as exc:
        return {'error': str(exc)}, 400
    if not rows:
        return {'error': 'No leads were found. The file needs a column of email addresses or websites.'}, 400
    name = (upload.filename.rsplit('.', 1)[0].replace('_', ' ').replace('-', ' ').strip() or 'Leads')[:100]
    with db.tx():
        list_id = db.run('INSERT INTO lists (name, description, created_at) VALUES (?, ?, ?)',
                         name, 'Uploaded in the AI chat', now()).lastrowid
    summary = prospects.import_upload(db, list_id, rows)
    if not summary['added'] + summary['existing'] + summary['needs_email']:
        db.run('DELETE FROM lists WHERE id = ?', list_id)
        return {'error': 'No usable leads: every row lacked both an email address and a website.'}, 400
    named = [key for key in ('first_name', 'last_name') if columns.get(key)]
    return {'list_id': list_id, 'name': name, 'file': upload.filename[:120],
            'added': summary['added'] + summary['existing'], 'needs_email': summary['needs_email'],
            'invalid': summary['invalid'], 'suppressed': summary['suppressed'],
            'columns': ((['email'] if columns.get('email') else []) + named + columns['fields'])[:20]}


@bp.route('/agent/<int:thread_id>/delete', methods=['POST'])
def agent_delete(thread_id):
    db = _db()
    thread = _thread(thread_id)
    db.run('DELETE FROM agent_threads WHERE id = ?', thread_id)
    if thread['kind'] == 'offer':
        return flask.redirect(flask.url_for('ui.offers_page'))
    return flask.redirect(flask.url_for('ui.agent_home'))


# -- settings ---------------------------------------------------------------------
CHECKBOXES = ('warmup_enabled', 'track_opens', 'track_clicks', 'ai_require_approval', 'imap_verify_tls',
              'ai_web_search')
SECTIONS = {
    'sender': ('from_name', 'from_email', 'reply_to', 'postal_address', 'timezone', 'email_style',
               'email_signature', 'email_logo_url'),
    'pacing': ('rate_per_hour', 'delay_min', 'delay_max', 'window_start', 'window_end', 'window_days', 'daily_cap',
               'domain_hourly_cap', 'warmup_enabled', 'warmup_start', 'warmup_growth', 'warmup_max',
               'track_opens', 'track_clicks'),
    'ai': ('ai_provider', 'ai_key_openrouter', 'ai_key_anthropic', 'ai_model_openrouter', 'ai_model_anthropic',
           'ai_model_writer', 'ai_require_approval', 'ai_web_search', 'ai_brand_voice'),
    'replies': ('reply_imap_user', 'reply_imap_password', 'reply_imap_host', 'reply_imap_port', 'ai_reply_mode'),
    'leadtools': ('hunter_api_key',),
    'bounces': ('bounce_address', 'imap_host', 'imap_port', 'imap_user', 'imap_password', 'imap_verify_tls'),
    'smtp': ('smtp_host', 'smtp_port', 'smtp_user', 'smtp_password', 'smtp_tls'),
}


@bp.route('/settings', methods=['GET', 'POST'])
def settings():
    db = _db()
    if flask.request.method == 'POST':
        form = flask.request.form
        section = form.get('section', '')
        if section not in SECTIONS:
            raise service.Problem('Unknown settings section.')
        current = db.settings()
        values = {}
        for key in SECTIONS[section]:
            if key in CHECKBOXES:
                values[key] = '1' if form.get(key) else '0'
            elif key == 'window_days':
                values[key] = ''.join(sorted(set(form.getlist('window_days')))) or '0123456'
            elif key in SECRET_SETTINGS:
                # An empty secret field means "keep what is stored".
                if form.get(key, '').strip():
                    values[key] = form.get(key).strip()
                elif form.get(f'clear_{key}'):
                    values[key] = ''
            else:
                values[key] = form.get(key, '').strip()
        _validate_settings(section, values, current)
        with db.tx():
            for key, value in values.items():
                db.set_setting(key, value)
        flask.flash('Settings saved.')
        return flask.redirect(flask.url_for('ui.settings') + f'#{section}')
    values = db.settings()
    return flask.render_template(
        'settings.html', active='settings', s=values,
        has_secret={key: bool(values[key]) for key in SECRET_SETTINGS},
        api_keys=db.q('SELECT * FROM api_keys WHERE revoked_at IS NULL ORDER BY id DESC'),
        webhooks=db.q('SELECT * FROM webhooks ORDER BY id DESC'),
        new_key=flask.session.pop('new_api_key', None),
        new_webhook_secret=flask.session.pop('new_webhook_secret', None),
        warmup_today=warmup_cap(values), bounce_status=loads(db.state('bounce_status'), default=None),
        local_now=local_time(values).strftime('%H:%M'), api_base=_cfg().PUBLIC_URL + _cfg().URL_PREFIX + '/api',
        anthropic_default=agent.ai_anthropic.DEFAULT_MODEL,
        reply_status=loads(db.state('reply_status'), default=None),
        ai_usage=db.q('SELECT * FROM ai_usage ORDER BY month DESC, kind LIMIT 12'))


def _validate_settings(section, values, current):
    if section == 'sender':
        for key in ('from_email', 'reply_to'):
            if values[key] and not contacts.normalize_email(values[key]):
                raise service.Problem(f'"{values[key]}" is not a valid email address.')
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(values['timezone'] or 'UTC')
        except Exception:
            raise service.Problem(f'Unknown timezone "{values["timezone"]}". Use a name like Europe/Berlin or Asia/Dubai.')
        values['timezone'] = values['timezone'] or 'UTC'
        values['email_style'] = values['email_style'] if values['email_style'] in ('branded', 'plain') else 'branded'
        values['email_signature'] = values['email_signature'][:600]
        if values['email_logo_url'] and not values['email_logo_url'].startswith('https://'):
            raise service.Problem('The logo address must start with https://')
    elif section == 'pacing':
        for key in ('rate_per_hour', 'delay_min', 'delay_max', 'daily_cap', 'domain_hourly_cap', 'warmup_start', 'warmup_max'):
            if values[key] and not values[key].isdigit():
                raise service.Problem(f'{key.replace("_", " ").capitalize()} must be a whole number.')
            if values[key] and int(values[key]) > 10_000_000:
                values[key] = '10000000'
        if not values['rate_per_hour'] or int(values['rate_per_hour']) < 1:
            values['rate_per_hour'] = DEFAULT_SETTINGS['rate_per_hour']
        for key in ('window_start', 'window_end'):
            if values[key] and not service.TIME_RE.match(values[key]):
                raise service.Problem('Sending window times must look like 09:00.')
        try:
            if float(values['warmup_growth'] or 1.5) < 1:
                raise ValueError
        except ValueError:
            raise service.Problem('Warm-up growth must be a number of at least 1, for example 1.5.')
        if values['warmup_enabled'] == '1' and current.get('warmup_enabled') != '1':
            _db().set_setting('warmup_started_at', now())
    elif section == 'ai':
        if values['ai_provider'] not in agent.PROVIDERS:
            raise service.Problem('Choose OpenRouter or Anthropic.')
    elif section == 'replies':
        if values['reply_imap_port'] and not values['reply_imap_port'].isdigit():
            raise service.Problem('IMAP port must be a number.')
        if values['ai_reply_mode'] not in ('off', 'draft', 'send'):
            values['ai_reply_mode'] = 'off'
    elif section == 'bounces':
        if values['bounce_address'] and not contacts.normalize_email(values['bounce_address']):
            raise service.Problem(f'"{values["bounce_address"]}" is not a valid email address.')
        if values['imap_port'] and not values['imap_port'].isdigit():
            raise service.Problem('IMAP port must be a number.')
    elif section == 'smtp':
        if values['smtp_port'] and not values['smtp_port'].isdigit():
            raise service.Problem('SMTP port must be a number.')
        if values['smtp_tls'] not in ('', 'none', 'starttls', 'ssl'):
            raise service.Problem('Unknown SMTP security option.')


@bp.route('/settings/models', methods=['POST'])
def settings_models():
    """Model list for the provider selected in the form (JSON)."""
    db = _db()
    data = flask.request.get_json(silent=True) or {}
    provider = agent.PROVIDERS.get(data.get('provider'))
    if provider is None:
        return {'error': 'Unknown provider.'}, 400
    key = str(data.get('key') or '').strip() or db.setting(f'ai_key_{provider.NAME}')
    if not key and provider.NAME == 'anthropic':
        return {'error': 'Enter the API key first.'}, 400
    try:
        if hasattr(provider, 'model_catalog'):
            catalog, recommended = provider.model_catalog(key)
            models = [m['id'] for m in catalog]
        else:
            models = provider.list_models(key)
            catalog, recommended = [{'id': m, 'name': m, 'input': None, 'output': None} for m in models], {}
    except ProviderError as exc:
        return {'error': str(exc)}, 502
    suggested = provider.pick_default(models) if hasattr(provider, 'pick_default') else provider.DEFAULT_MODEL
    return {'models': models[:300], 'catalog': catalog[:300], 'recommended': recommended, 'suggested': suggested}


@bp.route('/settings/bounces/check', methods=['POST'])
def settings_bounce_check():
    db = _db()
    result = bounces.poll(db, _cfg(), db.settings())
    result['at'] = now()
    db.set_state('bounce_status', json.dumps(result))
    if not result.get('configured'):
        flask.flash('Enter the mailbox user and password first.', 'error')
    elif result.get('ok'):
        flask.flash(f'Mailbox reached. {result["seen"]} new message(s): {result["bounces"]} bounce(s), '
                    f'{result["complaints"]} complaint(s).')
    else:
        flask.flash(f'Could not read the mailbox: {result.get("error")}', 'error')
    return flask.redirect(flask.url_for('ui.settings') + '#bounces')


@bp.route('/settings/api-keys', methods=['POST'])
def settings_api_keys():
    db = _db()
    if flask.request.form.get('action') == 'revoke':
        db.run('UPDATE api_keys SET revoked_at = ? WHERE id = ?', now(), flask.request.form.get('id', type=int))
        flask.flash('API key revoked.')
    else:
        name = flask.request.form.get('name', '').strip()[:80] or 'API key'
        plain, prefix, hashed = security.new_api_key()
        db.run('INSERT INTO api_keys (name, prefix, key_hash, created_at) VALUES (?, ?, ?, ?)',
               name, prefix, hashed, now())
        flask.session['new_api_key'] = plain
    return flask.redirect(flask.url_for('ui.settings') + '#api')


@bp.route('/settings/webhooks', methods=['POST'])
def settings_webhooks():
    db = _db()
    form = flask.request.form
    if form.get('action') == 'delete':
        db.run('DELETE FROM webhooks WHERE id = ?', form.get('id', type=int))
        flask.flash('Webhook removed.')
    else:
        url = form.get('url', '').strip()
        parsed = urlparse(url)
        private_ok = _cfg().WEBHOOK_PRIVATE
        if parsed.scheme not in (('http', 'https') if private_ok else ('https',)) or not parsed.hostname:
            raise service.Problem('The webhook URL must start with https://')
        if not private_ok and not leadtools._public_host(parsed.hostname):
            raise service.Problem(f'{parsed.hostname} is not a public address. Webhooks to this server\'s own '
                                  'network need CAMPAIGNS_WEBHOOK_PRIVATE=1.')
        chosen = [e for e in form.getlist('events') if e.startswith('email.')]
        secret = security.new_secret()
        db.run('INSERT INTO webhooks (url, secret, events, created_at) VALUES (?, ?, ?, ?)',
               url[:500], secret, ','.join(chosen) or '*', now())
        flask.session['new_webhook_secret'] = secret
    return flask.redirect(flask.url_for('ui.settings') + '#webhooks')
