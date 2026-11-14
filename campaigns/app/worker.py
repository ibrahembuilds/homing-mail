"""Background worker. Run exactly one per database:

    python -m app.worker

Three loops run side by side, so a slow mailbox or AI call never holds up
sending:
- sender: hands queued mail to the mail server at the campaign's pace, queues
  follow-ups as they fall due, delivers webhooks;
- reader: reads the bounce mailbox, the reply mailbox and the inbox of every
  connected email account, lets the AI sort new replies;
- writer: writes each lead's personal email for campaigns with AI writing on.
"""
import json
import logging
import signal
import threading

from . import _load_secret, bounces, events, health, mailboxes, replies, writer
from .config import Config
from .db import DB, now
from .sender import Engine

log = logging.getLogger('campaigns.worker')
READ_INTERVAL = 60


def _sender(cfg, db, stop):
    engine = Engine(cfg, db)
    engine.recover()
    while not stop.is_set():
        sent = 0
        try:
            sent = engine.tick()
            events.deliver_webhooks(db)
            db.set_state('worker_heartbeat', now())
        except Exception:
            log.exception('sending pass failed')
            stop.wait(5)
        stop.wait(0.2 if sent else 1.0)


def _reader(cfg, db, stop):
    while not stop.is_set():
        try:
            settings = db.settings()
            result = bounces.poll(db, cfg, settings)
            result['at'] = now()
            db.set_state('bounce_status', json.dumps(result))
            result = replies.poll(db, cfg, settings)
            result['at'] = now()
            db.set_state('reply_status', json.dumps(result))
            mailboxes.poll(db, cfg, settings)
            replies.classify_pending(db, cfg, settings)
            replies.auto_answer(db, cfg, settings)
        except Exception:
            log.exception('mailbox pass failed')
        try:
            health.check_pending(db, cfg, db.settings())
        except Exception:
            log.exception('placement check failed')
        stop.wait(READ_INTERVAL)


def _writer(cfg, db, stop):
    writer.recover(db)
    while not stop.is_set():
        written = 0
        try:
            written = writer.run_pending(db, cfg, stop)
        except Exception:
            log.exception('writing pass failed')
            stop.wait(10)
        stop.wait(0.5 if written else 5)


def run(cfg=None, stop=None):
    cfg = cfg or Config()
    cfg.SECRET_KEY = _load_secret(cfg)
    stop = stop or threading.Event()
    db = DB(cfg.DB_PATH)
    db.init(seed=cfg.SEED)
    log.info('worker started, database %s', cfg.DB_PATH)
    threads = [threading.Thread(target=loop, args=(cfg, DB(cfg.DB_PATH), stop), name=name, daemon=True)
               for name, loop in (('sender', _sender), ('reader', _reader), ('writer', _writer))]
    for thread in threads:
        thread.start()
    stop.wait()
    for thread in threads:
        thread.join(timeout=30)
    db.close()
    log.info('worker stopped')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    stop = threading.Event()
    for name in ('SIGTERM', 'SIGINT'):
        signal.signal(getattr(signal, name), lambda *_: stop.set())
    run(stop=stop)


if __name__ == '__main__':
    main()
