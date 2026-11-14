"""Pages for working with leads: inbox, leads, calendar, email finder, AI-written
emails, and connecting the sending domain. Registered on the ui blueprint."""
import datetime

import flask

from .. import ai, contacts, domainsetup, leadtools, news, planner, replies, service, writer
from ..ai_common import ProviderError
from ..db import loads, now
from ..sender import local_time, timezone
from .ui import PAGE, _back, _cfg, _db, bp


# -- inbox --------------------------------------------------------------------------
INBOX_VIEWS = (('todo', 'To answer'), ('interested', 'Interested'), ('all', 'All'), ('auto', 'Automatic'))


@bp.route('/inbox')
def inbox():
    db = _db()
    view = flask.request.args.get('view', 'todo')
    where = {'todo': 'r.handled_at IS NULL AND r.is_auto = 0',
             'interested': "r.category IN ('interested', 'meeting', 'question')",
             'all': 'r.is_auto = 0',
             'auto': 'r.is_auto = 1'}.get(view, 'r.handled_at IS NULL AND r.is_auto = 0')
    page = max(1, flask.request.args.get('page', 1, type=int))
    total = db.val(f'SELECT COUNT(*) FROM replies r WHERE {where}', default=0)
    rows = db.q(f'SELECT r.*, c.first_name, c.last_name, c.email, c.lead_status, k.name AS campaign_name '
                f'FROM replies r LEFT JOIN contacts c ON c.id = r.contact_id LEFT JOIN campaigns k ON k.id = r.campaign_id '
                f'WHERE {where} ORDER BY r.received_at DESC LIMIT ? OFFSET ?', PAGE, (page - 1) * PAGE)
    counts = {key: db.val(f'SELECT COUNT(*) FROM replies r WHERE {cond}', default=0) for key, cond in (
        ('todo', 'r.handled_at IS NULL AND r.is_auto = 0'),
        ('interested', "r.category IN ('interested', 'meeting', 'question')"))}
    settings = db.settings()
    return flask.render_template(
        'inbox.html', active='inbox', rows=rows, view=view, views=INBOX_VIEWS, counts=counts, total=total,
        page=page, pages=max(1, -(-total // PAGE)),
        connected=bool(settings['reply_imap_user'] and settings['reply_imap_password']) or bool(
            db.val("SELECT COUNT(*) FROM mailboxes WHERE active = 1 AND imap_host != ''", default=0)),
        status=loads(db.state('reply_status'), default=None), inbox_error=db.state('inbox_error'),
        reply_mode=settings.get('ai_reply_mode') or 'off')


def _reply(reply_id):
    row = _db().one('SELECT * FROM replies WHERE id = ?', reply_id)
    if row is None:
        flask.abort(404)
    return row


@bp.route('/inbox/<int:reply_id>')
def inbox_reply(reply_id):
    db = _db()
    row = _reply(reply_id)
    if row['read_at'] is None:
        db.run('UPDATE replies SET read_at = ? WHERE id = ?', now(), reply_id)
    contact = db.one('SELECT * FROM contacts WHERE id = ?', row['contact_id']) if row['contact_id'] else None
    return flask.render_template(
        'inbox_reply.html', active='inbox', r=row, contact=contact,
        extra=loads(contact['fields']) if contact else {},
        thread=replies.thread_for(db, row['contact_id'], row['from_email'], _cfg()),
        campaign=db.one('SELECT * FROM campaigns WHERE id = ?', row['campaign_id']) if row['campaign_id'] else None,
        tasks=planner.open_tasks(db, contact_id=row['contact_id']) if contact else [],
        statuses=service.LEAD_STATUSES, categories=writer.CATEGORIES, quick_due=planner.QUICK_DUE,
        ai_ready=ai.configured(db.settings()), session_draft=f'reply_draft_{reply_id}' in flask.session,
        draft=flask.session.pop(f'reply_draft_{reply_id}', '') or (row['draft'] if not row['handled_at'] else ''))


@bp.route('/inbox/<int:reply_id>/act', methods=['POST'])
def inbox_act(reply_id):
    db = _db()
    row = _reply(reply_id)
    form = flask.request.form
    action = form.get('action', '')
    target = flask.url_for('ui.inbox_reply', reply_id=reply_id)
    if action == 'send':
        service.reply_to_lead(db, _cfg(), db.settings(), row, form.get('text', ''))
        flask.flash('Reply sent. It is in the conversation below and in the Email log.')
        return flask.redirect(flask.url_for('ui.inbox'))
    if action == 'draft':
        flask.session[f'reply_draft_{reply_id}'] = form.get('text', '')
        contact = db.one('SELECT * FROM contacts WHERE id = ?', row['contact_id']) if row['contact_id'] else None
        try:
            draft = writer.draft_reply(db, db.settings(), row, contact,
                                       writer.sent_text_for(db, _cfg(), row['message_id']), form.get('wish', ''))
        except (ai.NotConfigured, ProviderError, writer.WriterError) as exc:
            raise service.Problem(f'The AI could not write a draft: {exc}')
        flask.session[f'reply_draft_{reply_id}'] = draft
        return flask.redirect(target + '#answer')
    if action == 'done':
        db.run('UPDATE replies SET handled_at = ?, read_at = COALESCE(read_at, ?) WHERE id = ?', now(), now(), reply_id)
        flask.flash('Marked as done.')
        return flask.redirect(flask.url_for('ui.inbox'))
    if action == 'reopen':
        db.run('UPDATE replies SET handled_at = NULL WHERE id = ?', reply_id)
    elif action == 'unread':
        db.run('UPDATE replies SET read_at = NULL WHERE id = ?', reply_id)
        return flask.redirect(flask.url_for('ui.inbox'))
    elif action == 'category':
        category = form.get('category', '')
        if category not in writer.CATEGORIES:
            raise service.Problem('Unknown category')
        replies.apply_category(db, reply_id, category)
        if category == 'unsubscribe':
            flask.flash(f'{row["from_email"]} will not receive any more emails.')
    elif action == 'resubscribe':
        contacts.unsuppress(db, row['from_email'])
        flask.flash(f'{row["from_email"]} can receive emails again.')
    return flask.redirect(target)


# -- leads ---------------------------------------------------------------------------
COMPANY_FIELDS = ('company', 'company_name', 'organization', 'organisation', 'account', 'business')


def company_of(contact):
    fields = loads(contact['fields']) if contact['fields'] else {}
    for key in COMPANY_FIELDS:
        if fields.get(key):
            return fields[key]
    return ''


@bp.route('/leads')
def leads():
    db = _db()
    status = flask.request.args.get('status', '')
    query = flask.request.args.get('q', '').strip()
    page = max(1, flask.request.args.get('page', 1, type=int))
    where, args = '1 = 1', []
    if status in dict(service.LEAD_STATUSES):
        where += ' AND c.lead_status = ?'
        args.append(status)
    if query:
        where += ' AND (c.email LIKE ? OR c.first_name LIKE ? OR c.last_name LIKE ? OR c.fields LIKE ?)'
        args += [f'%{query}%'] * 4
    total = db.val(f'SELECT COUNT(*) FROM contacts c WHERE {where}', *args, default=0)
    rows = db.q(f'SELECT c.*, '
                '(SELECT MAX(COALESCE(m.sent_at, 0)) FROM messages m WHERE m.contact_id = c.id) AS last_sent, '
                '(SELECT MAX(r.received_at) FROM replies r WHERE r.contact_id = c.id) AS last_reply, '
                '(SELECT MIN(t.due_at) FROM tasks t WHERE t.contact_id = c.id AND t.done_at IS NULL) AS next_task '
                f'FROM contacts c WHERE {where} ORDER BY COALESCE(last_reply, last_sent, c.updated_at) DESC '
                'LIMIT ? OFFSET ?', *args, PAGE, (page - 1) * PAGE)
    counts = {r['lead_status']: r['n'] for r in db.q('SELECT lead_status, COUNT(*) AS n FROM contacts GROUP BY lead_status')}
    return flask.render_template(
        'leads.html', active='leads', rows=[dict(r, company=company_of(r)) for r in rows], total=total, page=page,
        pages=max(1, -(-total // PAGE)), status=status, q=query, counts=counts, statuses=service.LEAD_STATUSES)


def _contact(contact_id):
    row = _db().one('SELECT * FROM contacts WHERE id = ?', contact_id)
    if row is None:
        flask.abort(404)
    return row


@bp.route('/leads/<int:contact_id>', methods=['GET', 'POST'])
def lead(contact_id):
    db = _db()
    row = _contact(contact_id)
    if flask.request.method == 'POST':
        form = flask.request.form
        if 'lead_status' in form:
            service.set_lead_status(db, contact_id, form['lead_status'])
        if 'notes' in form:
            db.run('UPDATE contacts SET notes = ?, updated_at = ? WHERE id = ?', form['notes'][:10000], now(), contact_id)
        flask.flash('Lead updated.')
        return flask.redirect(flask.url_for('ui.lead', contact_id=contact_id))
    headlines, news_error, news_company = None, '', news.company_of(row)
    if flask.request.args.get('news') and news_company:
        try:
            headlines = news.search(db, news_company)
        except news.NewsError as exc:
            news_error = str(exc)
    return flask.render_template(
        'lead.html', active='leads', c=row, extra=loads(row['fields']), company=company_of(row),
        news_company=news_company, headlines=headlines, news_error=news_error,
        thread=replies.thread_for(db, contact_id, row['email'], _cfg()),
        tasks=db.q('SELECT * FROM tasks WHERE contact_id = ? ORDER BY done_at IS NOT NULL, due_at', contact_id),
        lists=db.q('SELECT l.* FROM lists l JOIN list_members m ON m.list_id = l.id WHERE m.contact_id = ?', contact_id),
        suppressed=contacts.is_suppressed(db, row['email']), statuses=service.LEAD_STATUSES,
        quick_due=planner.QUICK_DUE)


# -- tasks and calendar --------------------------------------------------------------------
@bp.route('/tasks', methods=['POST'])
def task_create():
    db = _db()
    form = flask.request.form
    settings = db.settings()
    try:
        due = planner.due_at_from(settings, quick=form.get('quick') or None, date=form.get('date') or None,
                                  hour=form.get('time') or '09:00')
    except (ValueError, TypeError, OverflowError, OSError):
        raise service.Problem('Pick a day for the follow-up.')
    if not now() - 366 * 86400 <= due <= now() + 10 * 366 * 86400:
        raise service.Problem('Pick a day within the next ten years.')
    contact_id = form.get('contact_id', type=int)
    if contact_id:
        _contact(contact_id)
    planner.create_task(db, form.get('title') or 'Follow up', due, contact_id, form.get('reply_id', type=int),
                        form.get('note', ''))
    flask.flash(f'Added to your calendar for {local_time(settings, due).strftime("%a %d %b, %H:%M")}.')
    return flask.redirect(_safe_next(form.get('next')) or _back(flask.url_for('ui.calendar')))


def _safe_next(target):
    prefix = _cfg().URL_PREFIX + '/'
    return target if target and target.startswith(prefix) and '//' not in target else ''


@bp.route('/tasks/<int:task_id>', methods=['POST'])
def task_update(task_id):
    db = _db()
    action = flask.request.form.get('action')
    if action == 'done':
        db.run('UPDATE tasks SET done_at = ? WHERE id = ?', now(), task_id)
    elif action == 'undo':
        db.run('UPDATE tasks SET done_at = NULL WHERE id = ?', task_id)
    elif action == 'delete':
        db.run('DELETE FROM tasks WHERE id = ?', task_id)
    elif action and action.startswith('snooze_'):
        days = min(int(action.split('_')[1]), 365) if action.split('_')[1].isdigit() else 1
        db.run('UPDATE tasks SET due_at = MAX(due_at, ?) + ? WHERE id = ?', now(), days * 86400, task_id)
    return flask.redirect(_back(flask.url_for('ui.calendar')))


@bp.route('/calendar')
def calendar():
    db = _db()
    settings = db.settings()
    today = local_time(settings).date()
    try:
        year, month = (int(x) for x in flask.request.args.get('month', today.strftime('%Y-%m')).split('-'))
        datetime.date(year, month, 1)
        if not 1971 <= year <= 2999:
            raise ValueError
    except ValueError:
        year, month = today.year, today.month
    try:
        selected = datetime.date.fromisoformat(flask.request.args.get('day', ''))
    except ValueError:
        selected = today if (today.year, today.month) == (year, month) else datetime.date(year, month, 1)
    weeks = planner.month_view(db, settings, year, month)
    day_items = next((d['items'] for w in weeks for d in w if d['date'] == selected), [])
    prev_month = (datetime.date(year, month, 1) - datetime.timedelta(days=1)).strftime('%Y-%m')
    next_month = (datetime.date(year, month, 28) + datetime.timedelta(days=5)).strftime('%Y-%m')
    cfg = _cfg()
    return flask.render_template(
        'calendar.html', active='calendar', weeks=weeks, year=year, month=month, selected=selected, today=today,
        month_name=datetime.date(year, month, 1).strftime('%B %Y'), prev_month=prev_month, next_month=next_month,
        day_items=day_items, overdue=planner.open_tasks(db, now(), limit=20),
        upcoming=planner.open_tasks(db, now() + 14 * 86400, limit=40),
        feed_url=f'{cfg.PUBLIC_URL}{cfg.URL_PREFIX}/t/cal/{planner.feed_token(db)}.ics',
        tz=timezone(settings))


@bp.route('/calendar/feed', methods=['POST'])
def calendar_feed_reset():
    _db().set_setting('calendar_token', '')
    planner.feed_token(_db())
    flask.flash('New calendar address made. Subscribe again with the new address; the old one stopped working.')
    return flask.redirect(flask.url_for('ui.calendar') + '#feed')


# -- email finder and list checks ------------------------------------------------------------
@bp.route('/find', methods=['GET', 'POST'])
def finder():
    db = _db()
    settings = db.settings()
    result, people, checked, form = None, None, None, {}
    if flask.request.method == 'POST':
        form = flask.request.form
        try:
            if form.get('action') == 'check':
                checked = leadtools.check_many(db, settings, form.get('emails', ''),
                                               use_hunter=form.get('use_hunter') == 'on')
                if not checked:
                    raise service.Problem('Paste at least one email address to check.')
            elif form.get('action') == 'company':
                key = (settings.get('hunter_api_key') or '').strip()
                if not key:
                    raise service.Problem('Searching a whole company needs a Hunter.io key (Settings → Lead tools).')
                people = leadtools.hunter_domain(key, leadtools.domain_from(form.get('domain')), 20)
            else:
                result = leadtools.find_email(db, settings, form.get('first_name', ''), form.get('last_name', ''),
                                              form.get('domain', ''))
        except leadtools.ToolError as exc:
            raise service.Problem(str(exc))
    return flask.render_template(
        'finder.html', active='finder', result=result, people=people, checked=checked, form=form,
        lists=db.q('SELECT id, name FROM lists ORDER BY name'), has_hunter=bool(settings.get('hunter_api_key')))


@bp.route('/find/add', methods=['POST'])
def finder_add():
    db = _db()
    form = flask.request.form
    email = contacts.normalize_email(form.get('email'))
    if not email:
        raise service.Problem('That is not a valid email address.')
    if form.get('consent') != 'yes':
        raise service.Problem('Confirm you have a lawful reason to email this person.')
    list_id = form.get('list_id', type=int)
    if not list_id or not db.one('SELECT id FROM lists WHERE id = ?', list_id):
        raise service.Problem('Choose a list to add the lead to.')
    fields = {}
    if form.get('company'):
        fields['company'] = form['company'][:200]
    if form.get('domain'):
        fields['website'] = leadtools.domain_from(form['domain'])
    if form.get('position'):
        fields['job_title'] = form['position'][:200]
    summary = contacts.import_rows(db, list_id, [{'email': email, 'first_name': form.get('first_name', '')[:100],
                                                  'last_name': form.get('last_name', '')[:100], 'fields': fields}])
    added = summary['added'] + summary['existing']
    flask.flash(f'{email} added to the list.' if added else f'{email} was not added (already in the list or blocked).')
    return flask.redirect(flask.url_for('ui.finder'))


@bp.route('/lists/<int:list_id>/verify', methods=['POST'])
def list_verify(list_id):
    db = _db()
    if not db.one('SELECT id FROM lists WHERE id = ?', list_id):
        flask.abort(404)
    rows = db.q("SELECT c.id, c.email FROM list_members m JOIN contacts c ON c.id = m.contact_id "
                "WHERE m.list_id = ? AND c.status = 'active'", list_id)
    counts = leadtools.verify_contacts(db, rows)
    flask.session['verify_summary'] = counts
    return flask.redirect(flask.url_for('ui.list_detail', list_id=list_id))


@bp.route('/lists/<int:list_id>/remove-invalid', methods=['POST'])
def list_remove_invalid(list_id):
    db = _db()
    removed = db.run("DELETE FROM list_members WHERE list_id = ? AND contact_id IN "
                     "(SELECT id FROM contacts WHERE verify_status = 'invalid')", list_id).rowcount
    flask.flash(f'{removed} address{"es" if removed != 1 else ""} that would bounce removed from the list.')
    return flask.redirect(flask.url_for('ui.list_detail', list_id=list_id))


# -- AI-written emails ------------------------------------------------------------------------
@bp.route('/campaigns/<int:campaign_id>/written')
def campaign_written(campaign_id):
    db = _db()
    campaign = service.get_campaign(db, campaign_id)
    status = flask.request.args.get('status', '')
    page = max(1, flask.request.args.get('page', 1, type=int))
    where, args = 'p.campaign_id = ?', [campaign_id]
    if status in ('done', 'edited', 'failed', 'pending', 'working'):
        where += ' AND p.status = ?'
        args.append(status)
    total = db.val(f'SELECT COUNT(*) FROM personalizations p WHERE {where}', *args, default=0)
    rows = db.q(f'SELECT p.*, c.email, c.first_name, c.last_name FROM personalizations p '
                f'JOIN contacts c ON c.id = p.contact_id WHERE {where} ORDER BY c.id LIMIT ? OFFSET ?',
                *args, 20, (page - 1) * 20)
    return flask.render_template('written.html', active='campaigns', c=campaign, rows=rows, status=status,
                                 total=total, page=page, pages=max(1, -(-total // 20)),
                                 progress=writer.progress(db, campaign_id), editable=campaign['status'] != 'sent')


@bp.route('/campaigns/<int:campaign_id>/written/<int:contact_id>', methods=['POST'])
def campaign_written_edit(campaign_id, contact_id):
    db = _db()
    campaign = service.get_campaign(db, campaign_id)
    form = flask.request.form
    if form.get('action') == 'rewrite':
        db.run("UPDATE personalizations SET status = 'pending', attempts = 0, error = '', updated_at = ? "
               "WHERE campaign_id = ? AND contact_id = ?", now(), campaign_id, contact_id)
        flask.flash('This one will be written again in the background.')
    else:
        body = form.get('body', '').strip()
        if not body:
            raise service.Problem('The text cannot be empty.')
        if campaign['ai_mode'] == 'line':
            try:
                body = writer.clean_line(body)
            except writer.WriterError as exc:
                raise service.Problem(str(exc))
        if not db.run("UPDATE personalizations SET status = 'edited', body = ?, subject = ?, error = '', updated_at = ? "
                      "WHERE campaign_id = ? AND contact_id = ?", body, form.get('subject', '').strip()[:150], now(),
                      campaign_id, contact_id).rowcount:
            flask.abort(404)
        flask.flash('Saved. Your version is the one that will be sent.')
    return flask.redirect(_back(flask.url_for('ui.campaign_written', campaign_id=campaign_id)))


# -- connecting the domain ----------------------------------------------------------------------
@bp.route('/domains', methods=['GET', 'POST'])
def domains():
    db = _db()
    cfg = _cfg()
    settings = db.settings()
    default = settings['from_email'].rsplit('@', 1)[-1] if '@' in settings['from_email'] else cfg.DOMAIN
    domain = leadtools.domain_from(flask.request.values.get('domain', default) or '') or (default or '')
    plan, created = None, None
    if flask.request.method == 'POST':
        action = flask.request.form.get('action')
        if action == 'cloudflare_token':
            token = flask.request.form.get('token', '').strip()
            db.set_setting('cloudflare_token', token)
            flask.flash('Cloudflare token saved.' if token else 'Cloudflare token removed.')
            return flask.redirect(flask.url_for('ui.domains', domain=domain) + '#cloudflare')
        token = settings.get('cloudflare_token', '')
        if not token:
            raise service.Problem('Add a Cloudflare API token first.')
        try:
            if action == 'cloudflare_apply':
                created = domainsetup.cloudflare_apply(cfg, token, domain)
                flask.flash(f'Cloudflare: {len(created)} record{"s" if len(created) != 1 else ""} added. '
                            'DNS changes usually show within a few minutes.' if created else
                            'Cloudflare: nothing was missing.')
                return flask.redirect(flask.url_for('ui.domains', domain=domain))
            plan = domainsetup.cloudflare_plan(cfg, token, domain)
        except domainsetup.SetupError as exc:
            raise service.Problem(str(exc))
    records = domainsetup.status(cfg, domain) if domain else []
    if domain and domain == default:
        db.set_state('dns_ok', '1' if records and all(r['state'] == 'ok' for r in records) else '0')
    return flask.render_template(
        'domains.html', active='domains', domain=domain, records=records, plan=plan,
        hostname=cfg.HOSTNAME, ip=domainsetup.server_ip(cfg), mail_server=cfg.MAIL_SERVER, admin=cfg.WEB_ADMIN,
        has_cloudflare=bool(settings.get('cloudflare_token')),
        done=sum(1 for r in records if r['state'] == 'ok'))
