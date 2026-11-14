"""The email accounts campaigns send from. Registered on the ui blueprint."""
import flask

from .. import health, mailboxes, service
from ..sender import day_start
from .ui import _cfg, _db, bp


def _problem(exc):
    flask.flash(str(exc), 'error')


@bp.route('/accounts')
def accounts():
    db = _db()
    settings = db.settings()
    items = mailboxes.summary(db, day_start(settings))
    active = [i for i in items if i['row']['active']]
    return flask.render_template(
        'accounts.html', active='accounts', items=items, providers=mailboxes.PROVIDERS,
        tls_modes=mailboxes.TLS_MODES, capacity=sum(i['row']['daily_limit'] for i in active),
        sent_today=sum(i['sent_today'] for i in items), adding=flask.request.args.get('add') or not items,
        server_from=settings.get('from_email', ''))


@bp.route('/accounts', methods=['POST'])
def account_add():
    db = _db()
    form = flask.request.form.to_dict()
    form.pop('csrf', None)
    try:
        mailbox_id = mailboxes.create(db, form)
    except mailboxes.Problem as exc:
        _problem(exc)
        return flask.redirect(flask.url_for('ui.accounts', add=1))
    account = mailboxes.get(db, mailbox_id)
    result = mailboxes.test(db, _cfg(), db.settings(), account)
    if result['smtp'] or result['imap']:
        flask.flash(f'{account["email"]} was added, but signing in failed: '
                    f'{result["smtp"] or result["imap"]}', 'error')
    else:
        flask.flash(f'{account["email"]} is connected and ready to send.')
    return flask.redirect(flask.url_for('ui.accounts') + f'#account-{mailbox_id}')


@bp.route('/accounts/<int:mailbox_id>', methods=['POST'])
def account_update(mailbox_id):
    db = _db()
    form = flask.request.form.to_dict()
    action = form.pop('action', 'save')
    form.pop('csrf', None)
    target = flask.url_for('ui.accounts') + f'#account-{mailbox_id}'
    try:
        account = mailboxes.get(db, mailbox_id)
        if action == 'delete':
            mailboxes.delete(db, mailbox_id)
            flask.flash(f'{account["email"]} was removed. Emails it already sent stay in the log.')
            return flask.redirect(flask.url_for('ui.accounts'))
        if action in ('pause', 'resume'):
            mailboxes.set_active(db, mailbox_id, action == 'resume')
            flask.flash(f'{account["email"]} {"paused" if action == "pause" else "is sending again"}.')
            return flask.redirect(target)
        if action == 'save':
            account = mailboxes.update(db, mailbox_id, form)
            flask.flash('Saved.')
        result = mailboxes.test(db, _cfg(), db.settings(), account)
        problems = [p for p in (result['smtp'], result['imap']) if p]
        if problems:
            flask.flash(f'Could not sign in to {account["email"]}: {problems[0]}', 'error')
        else:
            reading = ' and reading replies' if result['imap'] == '' else ''
            flask.flash(f'{account["email"]}: signing in for sending{reading} works.')
    except mailboxes.Problem as exc:
        _problem(exc)
    return flask.redirect(target)


# -- sending health --------------------------------------------------------------------
@bp.route('/sending-health')
def health_page():
    db = _db()
    settings = db.settings()
    return flask.render_template(
        'health.html', active='health', cap=health.capacity(db, settings), stats=health.stats(db),
        test=health.latest_test(db), seeds=health.seeds(db, _cfg(), settings),
        campaigns=db.q("SELECT id, name FROM campaigns WHERE TRIM(html) != '' OR TRIM(text) != '' "
                       "ORDER BY updated_at DESC LIMIT 30"),
        thresholds=health.THRESHOLDS)


@bp.route('/sending-health/placement', methods=['POST'])
def health_placement():
    db = _db()
    form = flask.request.form
    try:
        if form.get('action') == 'check':
            health.check_test(db, _cfg(), db.settings(), form.get('test_id', type=int))
            flask.flash('Checked the mailboxes.')
        else:
            health.start_test(db, _cfg(), db.settings(), form.get('campaign_id', type=int) or None)
            flask.flash('Test emails are on their way. Results appear here within a few minutes.')
    except service.Problem as exc:
        _problem(exc)
    return flask.redirect(flask.url_for('ui.health_page') + '#placement')
