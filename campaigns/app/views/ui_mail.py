"""Inbox profiles: every connected mailbox, read live, one tab per address."""
import flask

from .. import mailview
from .ui import _cfg, _db, bp


def _profiles():
    db = _db()
    return mailview.profiles(db, _cfg(), db.settings())


def _profile(key):
    for item in _profiles():
        if item['key'] == key:
            return item
    flask.abort(404)


def _folder(value):
    return value if value in mailview.FOLDER_LABELS else 'inbox'


@bp.route('/inbox/mail/<key>')
def mailbox(key):
    prof = _profile(key)
    folder = _folder(flask.request.args.get('folder', 'inbox'))
    query = flask.request.args.get('q', '').strip()[:100]
    listing, error = {'messages': [], 'total': 0}, None
    try:
        listing = mailview.list_messages(prof['box'], folder, query=query)
    except mailview.MailError as exc:
        error = str(exc)
    return flask.render_template('mailbox.html', active='inbox', profiles=_profiles(), profile=prof, folder=folder,
                                 folders=mailview.FOLDERS, listing=listing, error=error, q=query)


@bp.route('/inbox/mail/<key>/<folder>/<uid>')
def mailbox_message(key, folder, uid):
    prof = _profile(key)
    folder = _folder(folder)
    try:
        message = mailview.read_message(prof['box'], folder, uid)
    except mailview.MailError as exc:
        flask.flash(str(exc), 'error')
        return flask.redirect(flask.url_for('ui.mailbox', key=key, folder=folder))
    lead = _db().one('SELECT * FROM contacts WHERE email = ?', (message['from_email'] or '').lower())
    return flask.render_template('mail_message.html', active='inbox', profiles=_profiles(), profile=prof,
                                 folder=folder, folders=mailview.FOLDERS, message=message, lead=lead)


@bp.route('/inbox/mail/<key>/<folder>/<uid>/reply', methods=['POST'])
def mailbox_reply(key, folder, uid):
    db = _db()
    prof = _profile(key)
    folder = _folder(folder)
    try:
        original = mailview.read_message(prof['box'], folder, uid)
        mailview.reply(db, db.settings(), prof, original, flask.request.form.get('text', ''))
    except mailview.MailError as exc:
        flask.flash(str(exc), 'error')
    else:
        flask.flash(f'Reply sent from {prof["email"]}. It goes out within a minute.')
    return flask.redirect(flask.url_for('ui.mailbox_message', key=key, folder=folder, uid=uid))
