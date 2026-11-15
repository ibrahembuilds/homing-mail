"""Writing one email to one person, from any address Homing can send from."""
import flask

from .. import contacts
from ..sender import queue_single
from .ui import _db, bp


def senders(db):
    """Where an email can come from: this server's address, then each connected account."""
    settings = db.settings()
    out = []
    if settings.get('from_email'):
        out.append({'key': 'server', 'email': settings['from_email'], 'name': settings.get('from_name', ''),
                    'account_id': None})
    for account in db.q('SELECT * FROM mailboxes WHERE active = 1 ORDER BY email'):
        if account['email'].lower() != (settings.get('from_email') or '').lower():
            out.append({'key': str(account['id']), 'email': account['email'],
                        'name': account['from_name'] or settings.get('from_name', ''), 'account_id': account['id']})
    return out


def text_html(text):
    """Plain text as an email body fragment (paragraphs, line breaks, links); the sender wraps it
    in the account's look."""
    from ..render import text_to_html
    return text_to_html(text)


@bp.route('/compose/preview', methods=['POST'])
def compose_preview():
    """The email exactly as the recipient's mail program shows it."""
    data = flask.request.get_json(silent=True) or {}
    from ..render import FOOTER_MARK, wrap_email
    db = _db()
    settings = db.settings()
    text = str(data.get('text') or '')
    body = text_html(text) if text.strip() else '<p style="color:#999">Your message appears here.</p>'
    sender = next((o for o in senders(db) if o['key'] == data.get('sender')), None)
    name = sender['name'] if sender else settings.get('from_name', '')
    return {'html': wrap_email(body, settings, name, flask.current_app.cfg.BRAND_COLOR).replace(FOOTER_MARK, '')}


@bp.route('/compose', methods=['GET', 'POST'])
def compose():
    db = _db()
    options = senders(db)
    form = flask.request.form if flask.request.method == 'POST' else flask.request.args
    draft = {'to': form.get('to', '').strip(), 'subject': form.get('subject', ''), 'text': form.get('text', ''),
             'sender': form.get('sender') or (options[0]['key'] if options else '')}
    lead = db.one('SELECT * FROM contacts WHERE email = ?', contacts.normalize_email(draft['to']) or '')
    if flask.request.method == 'POST':
        error = None
        address = contacts.normalize_email(draft['to'])
        sender = next((o for o in options if o['key'] == draft['sender']), None)
        suppressed = contacts.is_suppressed(db, address) if address else None
        if not address:
            error = f'"{draft["to"]}" is not an email address.'
        elif sender is None:
            error = 'Choose the address it comes from. Set a sender in Settings first if the list is empty.'
        elif suppressed:
            error = f'{address} asked not to be emailed ({suppressed["reason"]}), so this cannot be sent.'
        elif not draft['subject'].strip() or not draft['text'].strip():
            error = 'Write a subject and a message.'
        if error:
            flask.flash(error, 'error')
        else:
            message_id = queue_single(
                db, kind='single', from_email=sender['email'], from_name=sender['name'], to=[address],
                subject=' '.join(draft['subject'].split())[:300], html=text_html(draft['text']),
                # A personal email: no open pixel and no rewritten links. Mail providers read those as bulk mail.
                text=draft['text'].strip(), track=False, contact_id=lead['id'] if lead else None,
                mailbox_id=sender['account_id'],
                reply_to=[r for r in [db.settings().get('reply_to')] if r and sender['account_id'] is None])
            if lead is not None and lead['lead_status'] == 'new':
                db.run("UPDATE contacts SET lead_status = 'contacted' WHERE id = ?", lead['id'])
            flask.flash(f'Sent to {address}. It goes out within a minute; follow it in the Email log.')
            return flask.redirect(flask.url_for('ui.email_detail', mid=message_id))
    return flask.render_template('compose.html', active='inbox', options=options, draft=draft, lead=lead)
