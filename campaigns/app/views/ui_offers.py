"""Offers: what the owner sells, written by hand or with the offer assistant."""
import flask

from .. import agent, offers, service
from .ui import _db, bp


@bp.route('/offers')
def offers_page():
    db = _db()
    edit_id = flask.request.args.get('edit', type=int)
    editing = None
    if edit_id:
        try:
            editing = offers.get(db, edit_id)
        except service.Problem:
            flask.abort(404)
    threads = db.q("SELECT t.* FROM agent_threads t WHERE t.kind = 'offer' AND EXISTS "
                   "(SELECT 1 FROM agent_messages m WHERE m.thread_id = t.id) ORDER BY t.updated_at DESC LIMIT 8")
    rows = offers.everything(db)
    return flask.render_template(
        'offers.html', active='offers', offers=rows, editing=editing, fields=offers.FIELDS, threads=threads,
        writing=bool(editing) or flask.request.args.get('new') == '1' or not rows,
        missing={row['id']: offers.completeness(row) for row in rows},
        used={r['offer_id']: r['n'] for r in db.q('SELECT offer_id, COUNT(*) AS n FROM campaigns '
                                                   'WHERE offer_id IS NOT NULL GROUP BY offer_id')})


@bp.route('/offers/card')
def offers_card():
    """The offer the assistant is working on (the one changed last), for the live card."""
    db = _db()
    return flask.render_template('offer_card.html',
                                 offer=db.one('SELECT * FROM offers ORDER BY updated_at DESC LIMIT 1'))


@bp.route('/offers/save', methods=['POST'])
def offers_save():
    db = _db()
    form = flask.request.form
    offer_id = form.get('offer_id', type=int)
    saved = offers.save(db, {key: form.get(key, '') for key in offers.KEYS}, offer_id=offer_id or None,
                        make_default=form.get('make_default') == 'on')
    flask.flash('Offer saved. The agent and the per-lead writer use it from now on.')
    return flask.redirect(flask.url_for('ui.offers_page') + f'#offer-{saved}')


@bp.route('/offers/<int:offer_id>/default', methods=['POST'])
def offers_default(offer_id):
    offers.set_default(_db(), offer_id)
    flask.flash('Default offer changed.')
    return flask.redirect(flask.url_for('ui.offers_page'))


@bp.route('/offers/<int:offer_id>/delete', methods=['POST'])
def offers_delete(offer_id):
    offers.delete(_db(), offer_id)
    flask.flash('Offer deleted.')
    return flask.redirect(flask.url_for('ui.offers_page'))


@bp.route('/offers/assistant', methods=['POST'])
def offers_assistant():
    """Opens a conversation with the offer assistant (an empty one is reused)."""
    db = _db()
    empty = db.one("SELECT t.id FROM agent_threads t WHERE t.kind = 'offer' AND NOT EXISTS "
                   "(SELECT 1 FROM agent_messages m WHERE m.thread_id = t.id) LIMIT 1")
    thread_id = empty['id'] if empty else agent.new_thread(db, 'offer')
    return flask.redirect(flask.url_for('ui.agent_thread', thread_id=thread_id))
