"""Work that outlives a web request: agent turns and email searches.

The web app is one process with threads (see the Dockerfile), so a turn runs as
a thread there and the request returns at once. What the turn is doing lives in
agent_threads (status, activity, heartbeat_at) and in the stored messages; the
chat page polls them. A slow AI call can no longer time out the page.
"""
import logging
import threading

from . import agent, prospects, service
from .ai_common import ProviderError
from .db import now

log = logging.getLogger('campaigns.agent')

STALE_AFTER = 15 * 60          # a running turn with no sign of life for this long is treated as dead
INTERRUPTED = 'This answer was cut short (the server restarted). Send your message again to continue.'
INLINE = False                 # tests: run in the calling thread instead of a new one
_threads = {}


class Busy(Exception):
    """The agent is still working on this conversation."""


def spawn(name, target, *args):
    """Runs target(*args) in a background thread (or inline in tests)."""
    if INLINE:
        target(*args)
        return None
    worker = threading.Thread(target=target, args=args, name=name, daemon=True)
    _threads[name] = worker
    worker.start()
    return worker


def join(name, timeout=60):
    """Waits for a background job (tests and shutdown)."""
    worker = _threads.get(name)
    if worker is not None:
        worker.join(timeout)


def start(db, cfg, thread_id, text=None, shown=None, attachment=None, complete=None):
    """Stores the owner's message (when there is one) and starts the agent on it.
    Returns the stored message, or None for a retry."""
    ts = now()
    claimed = db.run("UPDATE agent_threads SET status = 'running', activity = 'Thinking', error = '', "
                     "heartbeat_at = ?, stop_requested = 0 WHERE id = ? AND (status != 'running' OR "
                     "heartbeat_at IS NULL OR heartbeat_at < ?)", ts, thread_id, ts - STALE_AFTER).rowcount
    if not claimed:
        raise Busy('The agent is still working on your last message.')
    try:
        saved = agent.add_user_message(db, thread_id, text, shown, attachment) if text is not None else None
    except Exception:
        db.run("UPDATE agent_threads SET status = 'idle', activity = '' WHERE id = ?", thread_id)
        raise
    spawn(f'agent-{thread_id}', _work, db, cfg, thread_id, complete)
    return saved


def _work(db, cfg, thread_id, complete=None):
    def on_step(text):
        db.run('UPDATE agent_threads SET activity = ?, heartbeat_at = ? WHERE id = ?', text, now(), thread_id)

    def should_stop():
        return bool(db.val('SELECT stop_requested FROM agent_threads WHERE id = ?', thread_id, default=0))

    status, error = 'idle', ''
    try:
        agent.run(db, cfg, thread_id, complete=complete, on_step=on_step, should_stop=should_stop)
    except ProviderError as exc:
        log.warning('agent conversation %s: the AI provider failed: %s', thread_id, exc)
        status, error = 'error', str(exc)
    except service.Problem as exc:
        status, error = 'error', str(exc)
    except Exception as exc:
        log.exception('agent conversation %s failed', thread_id)
        status, error = 'error', f'Something went wrong on the server ({type(exc).__name__}: {exc})'[:500]
    finally:
        try:
            db.run("UPDATE agent_threads SET status = ?, error = ?, activity = '', heartbeat_at = ?, "
                   "stop_requested = 0 WHERE id = ?", status, error, now(), thread_id)
        finally:
            if not INLINE:
                db.close()   # this thread's connection


def state(db, thread_id):
    """{'status': idle|running|error, 'activity', 'error'} for the chat page."""
    row = db.one('SELECT status, activity, error, heartbeat_at FROM agent_threads WHERE id = ?', thread_id)
    if row is None:
        return {'status': 'idle', 'activity': '', 'error': ''}
    if row['status'] == 'running' and (row['heartbeat_at'] or 0) < now() - STALE_AFTER:
        db.run("UPDATE agent_threads SET status = 'error', error = ?, activity = '' WHERE id = ? AND status = 'running'",
               INTERRUPTED, thread_id)
        return {'status': 'error', 'activity': '', 'error': INTERRUPTED}
    return {'status': row['status'], 'activity': row['activity'], 'error': row['error']}


def stop(db, thread_id):
    db.run("UPDATE agent_threads SET stop_requested = 1 WHERE id = ? AND status = 'running'", thread_id)


def find_emails(db, list_id, ids=None):
    """Starts the email search for a list's leads without an address."""
    marked = db.run("UPDATE prospects SET status = 'searching', updated_at = ? WHERE list_id = ? AND "
                    "status = 'needs_email'", now(), list_id).rowcount
    if marked:
        spawn(f'emails-{list_id}', _find, db, list_id, ids)
    return marked


def _find(db, list_id, ids):
    try:
        prospects.find_emails(db, db.settings(), list_id=list_id, ids=ids, limit=200)
    except Exception:
        log.exception('email search for list %s failed', list_id)
        db.run("UPDATE prospects SET status = 'needs_email' WHERE list_id = ? AND status = 'searching'", list_id)
    finally:
        if not INLINE:
            db.close()


def recover(db):
    """At start of the web process: nothing can still be running."""
    db.run("UPDATE agent_threads SET status = 'error', error = ?, activity = '' WHERE status = 'running'", INTERRUPTED)
    prospects.recover(db)


__all__ = ['Busy', 'INLINE', 'find_emails', 'join', 'recover', 'spawn', 'start', 'state', 'stop']
