"""Team page: who can sign in, with which role. Registered on the ui blueprint."""
import flask

from .. import service, team
from .ui import _cfg, _db, bp, current_user


def _problem(exc):
    raise service.Problem(str(exc))


def _sign_in_url():
    cfg = _cfg()
    return cfg.PUBLIC_URL + cfg.URL_PREFIX + '/'


@bp.route('/team', endpoint='team')
def team_page():
    db, cfg = _db(), _cfg()
    return flask.render_template(
        'team.html', active='team', people=team.everyone(db), roles=team.ROLES,
        proxy=cfg.AUTH_MODE == 'proxy', mail_api=team.mail_api_ready(cfg), domain=cfg.DOMAIN,
        admin_url=cfg.WEB_ADMIN, login=flask.session.pop('new_login', None), sign_in_url=_sign_in_url())



@bp.route('/team/add', methods=['POST'], endpoint='team_add')
def team_add():
    db, cfg = _db(), _cfg()
    me = current_user()
    form = flask.request.form
    email = team.normalize(form.get('email'))
    role = form.get('role', 'member')
    name = form.get('name', '').strip()
    if role == 'owner' and me['role'] != 'owner':
        raise service.Problem('Only an owner can add another owner.')
    if not email or email == 'admin':
        raise service.Problem('Enter the email address this person will sign in with.')
    if team.get(db, email):
        raise service.Problem(f'{email} is already on the team.')
    login = {'email': email, 'url': _sign_in_url(), 'password': None, 'note': ''}
    try:
        if cfg.AUTH_MODE != 'proxy':
            login['password'] = team.new_password()
            team.add(db, email, name, role, me['email'], password=login['password'])
        else:
            if form.get('create_mailbox') and team.mail_api_ready(cfg):
                password = team.new_password()
                if team.create_mailbox(cfg, email, password, name):
                    login['password'] = password
                    login['note'] = 'A mailbox was created for them on this server; this is its password too.'
                else:
                    login['note'] = 'They already have a mailbox on this server and sign in with its password.'
            else:
                login['note'] = ('They sign in with their mailbox on this server. If they do not have one yet, '
                                 'create it in Mail admin first.')
            team.add(db, email, name, role, me['email'])
    except team.TeamError as exc:
        _problem(exc)
    flask.session['new_login'] = login
    flask.flash(f'{email} can now sign in as {team.ROLE_NAMES[role].lower()}.')
    return flask.redirect(flask.url_for('ui.team'))


@bp.route('/team/<path:email>', methods=['POST'], endpoint='team_update')
def team_update(email):
    db, cfg = _db(), _cfg()
    me = current_user()
    target = team.get(db, email)
    if target is None:
        flask.abort(404)
    action = flask.request.form.get('action', '')
    try:
        if action == 'role':
            team.set_role(db, me, target['email'], flask.request.form.get('role', ''))
            flask.flash(f'{target["email"]} is now {team.ROLE_NAMES[flask.request.form["role"]].lower()}.')
        elif action == 'remove':
            team.remove(db, me, target['email'])
            flask.flash(f'{target["email"]} can no longer sign in to Homing.'
                        + (' Their mailbox on this server still exists; remove it in Mail admin if needed.'
                           if cfg.AUTH_MODE == 'proxy' else ''))
        elif action == 'password':
            team.check_change(db, me, target)
            password = team.new_password()
            if cfg.AUTH_MODE == 'proxy':
                if not team.mail_api_ready(cfg):
                    raise team.TeamError('Change their password in Mail admin.')
                team.set_mailbox_password(cfg, target['email'], password)
            else:
                team.set_password(db, target['email'], password)
            flask.session['new_login'] = {'email': target['email'], 'url': _sign_in_url(), 'password': password,
                                          'note': 'Their old password no longer works.'}
        else:
            raise team.TeamError('Unknown action.')
    except team.TeamError as exc:
        _problem(exc)
    return flask.redirect(flask.url_for('ui.team'))


@bp.route('/me/password', methods=['POST'], endpoint='me_password')
def me_password():
    """Change your own password (sign-in without a mail server)."""
    db, cfg = _db(), _cfg()
    me = current_user()
    if cfg.AUTH_MODE == 'proxy':
        return flask.redirect(cfg.WEB_ADMIN + '/user/password')
    form = flask.request.form
    new = form.get('new', '')
    current_ok = team.check_password(me, form.get('current', '')) or (
        me['email'] == cfg.OWNER_EMAIL and cfg.ADMIN_PASSWORD and form.get('current', '') == cfg.ADMIN_PASSWORD)
    if not current_ok:
        raise service.Problem('Your current password is not right.')
    if len(new) < 10:
        raise service.Problem('Use at least 10 characters for the new password.')
    if new != form.get('confirm', ''):
        raise service.Problem('The two new passwords are not the same.')
    team.set_password(db, me['email'], new)
    flask.session['epoch'] = team.get(db, me['email'])['session_epoch']   # stay signed in here
    flask.flash('Password changed. You were signed out everywhere else.')
    return flask.redirect(flask.request.form.get('next') or flask.url_for('ui.home'))
