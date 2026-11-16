"""SQLite storage. One connection per thread, WAL mode so the web process and
the sending worker can share the file."""
import contextlib
import json
import os
import sqlite3
import threading
import time

from .config import DEFAULT_SETTINGS

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    fields TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',      -- active | unsubscribed | bounced | complained
    status_reason TEXT NOT NULL DEFAULT '',
    soft_bounces INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS list_members (
    list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    added_at REAL NOT NULL,
    PRIMARY KEY (list_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_members_contact ON list_members(contact_id);

-- Addresses that must never be mailed again. Kept separately from contacts so
-- deleting and re-importing a list cannot resurrect them.
CREATE TABLE IF NOT EXISTS suppressions (
    email TEXT PRIMARY KEY COLLATE NOCASE,
    reason TEXT NOT NULL,                        -- unsubscribed | bounced | complained | manual
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    preheader TEXT NOT NULL DEFAULT '',
    from_name TEXT NOT NULL DEFAULT '',
    from_email TEXT NOT NULL DEFAULT '',
    reply_to TEXT NOT NULL DEFAULT '',
    html TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    list_id INTEGER REFERENCES lists(id) ON DELETE SET NULL,
    -- draft | pending_approval | scheduled | sending | paused | sent | cancelled
    status TEXT NOT NULL DEFAULT 'draft',
    scheduled_at REAL,
    started_at REAL,
    finished_at REAL,
    next_send_at REAL NOT NULL DEFAULT 0,
    rate_per_hour INTEGER NOT NULL DEFAULT 120,
    delay_min INTEGER,
    delay_max INTEGER,
    window_start TEXT NOT NULL DEFAULT '',       -- "09:00" local time, empty = any time
    window_end TEXT NOT NULL DEFAULT '',
    window_days TEXT NOT NULL DEFAULT '0123456', -- 0 = Monday
    daily_cap INTEGER NOT NULL DEFAULT 0,
    track_opens INTEGER NOT NULL DEFAULT 1,
    track_clicks INTEGER NOT NULL DEFAULT 1,
    spam_score REAL,
    spam_report TEXT NOT NULL DEFAULT '',
    hold_reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'user',     -- user | agent | api
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL DEFAULT 0,
    url TEXT NOT NULL,
    UNIQUE (campaign_id, url)
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    kind TEXT NOT NULL DEFAULT 'campaign',       -- campaign | api | test | agent
    to_email TEXT NOT NULL,
    to_domain TEXT NOT NULL DEFAULT '',
    from_email TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    -- pending_approval | queued | sending | sent | failed | bounced | complained | cancelled
    status TEXT NOT NULL DEFAULT 'queued',
    scheduled_at REAL NOT NULL DEFAULT 0,
    sent_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    opened_at REAL,
    open_count INTEGER NOT NULL DEFAULT 0,
    human_open INTEGER NOT NULL DEFAULT 0,
    clicked_at REAL,
    click_count INTEGER NOT NULL DEFAULT 0,
    human_click INTEGER NOT NULL DEFAULT 0,
    bounced_at REAL,
    bounce_type TEXT NOT NULL DEFAULT '',        -- hard | soft
    complained_at REAL,
    unsubscribed_at REAL,
    payload TEXT NOT NULL DEFAULT '',            -- JSON body for non-campaign mail
    idempotency_key TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_campaign ON messages(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_msg_queue ON messages(status, kind, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_msg_sent ON messages(sent_at);
CREATE INDEX IF NOT EXISTS idx_msg_to ON messages(to_email);
CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_idem ON messages(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_once ON messages(campaign_id, contact_id)
    WHERE campaign_id IS NOT NULL AND contact_id IS NOT NULL AND kind = 'campaign';

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    campaign_id INTEGER,
    type TEXT NOT NULL,   -- queued sent failed opened clicked bounced complained unsubscribed
    at REAL NOT NULL,
    automated INTEGER NOT NULL DEFAULT 0,
    link_id INTEGER,
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_msg ON events(message_id);
CREATE INDEX IF NOT EXISTS idx_events_campaign ON events(campaign_id, type);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL
);

CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    events TEXT NOT NULL DEFAULT '*',
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending | delivered | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    response TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wh_pending ON webhook_deliveries(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS agent_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT 'New conversation',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
    role TEXT NOT NULL,                          -- user | assistant | tool
    content TEXT NOT NULL,                       -- JSON, provider-neutral
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_thread ON agent_messages(thread_id, id);

CREATE TABLE IF NOT EXISTS worker_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# Applied in order to every database whose PRAGMA user_version is lower.
# Never edit a released migration; add a new one.
MIGRATIONS = [
    # 1: leads, per-lead AI writing, follow-up sequences, replies, tasks
    """
    ALTER TABLE contacts ADD COLUMN lead_status TEXT NOT NULL DEFAULT 'new';
    ALTER TABLE contacts ADD COLUMN notes TEXT NOT NULL DEFAULT '';
    ALTER TABLE campaigns ADD COLUMN ai_mode TEXT NOT NULL DEFAULT '';
    ALTER TABLE campaigns ADD COLUMN ai_instructions TEXT NOT NULL DEFAULT '';
    ALTER TABLE messages ADD COLUMN step INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE messages ADD COLUMN parent_id TEXT;
    ALTER TABLE messages ADD COLUMN replied_at REAL;
    DROP INDEX IF EXISTS idx_msg_once;
    CREATE UNIQUE INDEX idx_msg_once ON messages(campaign_id, contact_id, step)
        WHERE campaign_id IS NOT NULL AND contact_id IS NOT NULL AND kind = 'campaign';
    CREATE INDEX IF NOT EXISTS idx_msg_contact ON messages(contact_id, sent_at);
    CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(lead_status);
    CREATE TABLE campaign_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        delay_days REAL NOT NULL DEFAULT 3,
        send_if TEXT NOT NULL DEFAULT 'no_reply',
        as_reply INTEGER NOT NULL DEFAULT 1,
        subject TEXT NOT NULL DEFAULT '',
        html TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        UNIQUE (campaign_id, position)
    );
    CREATE TABLE personalizations (
        campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
        contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending',
        subject TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        model TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL,
        PRIMARY KEY (campaign_id, contact_id)
    );
    CREATE INDEX idx_pers_status ON personalizations(status);
    CREATE TABLE replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mailbox TEXT NOT NULL,
        uid TEXT NOT NULL,
        header_id TEXT NOT NULL DEFAULT '',
        message_id TEXT,
        campaign_id INTEGER,
        contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
        from_email TEXT NOT NULL,
        from_name TEXT NOT NULL DEFAULT '',
        subject TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        received_at REAL NOT NULL,
        is_auto INTEGER NOT NULL DEFAULT 0,
        category TEXT,
        summary TEXT NOT NULL DEFAULT '',
        read_at REAL,
        handled_at REAL,
        created_at REAL NOT NULL,
        UNIQUE (mailbox, uid)
    );
    CREATE INDEX idx_replies_contact ON replies(contact_id, received_at);
    CREATE INDEX idx_replies_inbox ON replies(handled_at, received_at);
    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
        reply_id INTEGER REFERENCES replies(id) ON DELETE SET NULL,
        title TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        due_at REAL NOT NULL,
        done_at REAL,
        created_at REAL NOT NULL
    );
    CREATE INDEX idx_tasks_due ON tasks(done_at, due_at);
    CREATE TABLE ai_usage (
        month TEXT NOT NULL,
        kind TEXT NOT NULL,
        calls INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (month, kind)
    )
    """,
    # 2: address checks, website research
    """
    ALTER TABLE contacts ADD COLUMN verify_status TEXT NOT NULL DEFAULT '';
    ALTER TABLE contacts ADD COLUMN verify_detail TEXT NOT NULL DEFAULT '';
    ALTER TABLE contacts ADD COLUMN verified_at REAL;
    ALTER TABLE campaigns ADD COLUMN ai_research INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE domain_checks (
        domain TEXT PRIMARY KEY,
        accepts_mail INTEGER NOT NULL,
        mx TEXT NOT NULL DEFAULT '[]',
        checked_at REAL NOT NULL
    );
    CREATE TABLE site_cache (
        domain TEXT PRIMARY KEY,
        data TEXT,
        error TEXT NOT NULL DEFAULT '',
        fetched_at REAL NOT NULL
    )
    """,
    # 3: email accounts that campaigns send from, in rotation
    """
    CREATE TABLE mailboxes (
        id INTEGER PRIMARY KEY,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        from_name TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT 'other',
        smtp_host TEXT NOT NULL DEFAULT '',
        smtp_port INTEGER NOT NULL DEFAULT 587,
        smtp_tls TEXT NOT NULL DEFAULT 'starttls',
        smtp_user TEXT NOT NULL DEFAULT '',
        smtp_password TEXT NOT NULL DEFAULT '',
        imap_host TEXT NOT NULL DEFAULT '',
        imap_port INTEGER NOT NULL DEFAULT 993,
        imap_user TEXT NOT NULL DEFAULT '',
        imap_password TEXT NOT NULL DEFAULT '',
        daily_limit INTEGER NOT NULL DEFAULT 30,
        active INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'new',
        last_error TEXT NOT NULL DEFAULT '',
        retry_at REAL NOT NULL DEFAULT 0,
        checked_at REAL,
        created_at REAL NOT NULL
    );
    ALTER TABLE campaigns ADD COLUMN mailbox_ids TEXT NOT NULL DEFAULT '';
    ALTER TABLE messages ADD COLUMN mailbox_id INTEGER;
    CREATE INDEX messages_mailbox_sent ON messages (mailbox_id, sent_at)
    """,
    # 4: recent news per lead, AI answers to replies, inbox placement tests
    """
    ALTER TABLE campaigns ADD COLUMN ai_news INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE news_cache (
        query TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        fetched_at REAL NOT NULL
    );
    ALTER TABLE replies ADD COLUMN draft TEXT NOT NULL DEFAULT '';
    ALTER TABLE replies ADD COLUMN drafted_at REAL;
    CREATE TABLE placement_tests (
        id INTEGER PRIMARY KEY,
        campaign_id INTEGER,
        token TEXT NOT NULL,
        results TEXT NOT NULL DEFAULT '[]',
        created_at REAL NOT NULL,
        checked_at REAL
    )
    """,
    # 5: subject line tests, skipping risky addresses
    """
    ALTER TABLE campaigns ADD COLUMN subject_b TEXT NOT NULL DEFAULT '';
    ALTER TABLE campaigns ADD COLUMN ab_percent INTEGER NOT NULL DEFAULT 20;
    ALTER TABLE campaigns ADD COLUMN ab_wait_hours REAL NOT NULL DEFAULT 4;
    ALTER TABLE campaigns ADD COLUMN ab_winner TEXT NOT NULL DEFAULT '';
    ALTER TABLE campaigns ADD COLUMN ab_decided_at REAL;
    ALTER TABLE campaigns ADD COLUMN skip_risky INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE messages ADD COLUMN variant TEXT NOT NULL DEFAULT ''
    """,
    # 6: team: who can sign in, and with which role
    """
    CREATE TABLE team (
        email TEXT PRIMARY KEY COLLATE NOCASE,
        name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'member',
        password_hash TEXT,
        session_epoch TEXT NOT NULL DEFAULT '',
        disabled INTEGER NOT NULL DEFAULT 0,
        added_by TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        last_seen_at REAL
    )
    """,
    # 7: agent runs in the background, offers, leads still missing an email, research per lead
    """
    ALTER TABLE agent_threads ADD COLUMN kind TEXT NOT NULL DEFAULT 'outreach';
    ALTER TABLE agent_threads ADD COLUMN status TEXT NOT NULL DEFAULT 'idle';
    ALTER TABLE agent_threads ADD COLUMN activity TEXT NOT NULL DEFAULT '';
    ALTER TABLE agent_threads ADD COLUMN error TEXT NOT NULL DEFAULT '';
    ALTER TABLE agent_threads ADD COLUMN heartbeat_at REAL;
    ALTER TABLE agent_threads ADD COLUMN stop_requested INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE contacts ADD COLUMN research TEXT NOT NULL DEFAULT '';
    ALTER TABLE campaigns ADD COLUMN offer_id INTEGER;
    CREATE TABLE offers (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        audience TEXT NOT NULL DEFAULT '',
        problem TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL DEFAULT '',
        details TEXT NOT NULL DEFAULT '',
        proof TEXT NOT NULL DEFAULT '',
        call_to_action TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        is_default INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE prospects (
        id INTEGER PRIMARY KEY,
        list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
        first_name TEXT NOT NULL DEFAULT '',
        last_name TEXT NOT NULL DEFAULT '',
        fields TEXT NOT NULL DEFAULT '{}',
        website TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'needs_email',
        email TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX idx_prospects_list ON prospects(list_id, status)
    """,
    # 8: case studies on offers
    """
    ALTER TABLE offers ADD COLUMN case_studies TEXT NOT NULL DEFAULT ''
    """,
    # 9: leads the owner left out of one campaign
    """
    CREATE TABLE campaign_skips (
        campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
        contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        PRIMARY KEY (campaign_id, contact_id)
    )
    """,
]

# What the columns above hold:
#   contacts.lead_status   new | contacted | replied | interested | meeting | won | not_interested | lost
#   campaign_steps         follow-ups; position 1 = first follow-up, sent delay_days after the previous
#                          email; send_if no_reply | no_reply_no_open; as_reply keeps the same thread
#   personalizations       per-lead AI text: status pending | working | done | edited | failed;
#                          body is the line (ai_mode "line") or the full HTML (ai_mode "full")
#   replies.category       interested | question | meeting | not_interested | unsubscribe |
#                          out_of_office | other
#   ai_usage.kind          agent | writer | inbox
#   contacts.verify_status '' (not checked) | ok | risky | invalid | unknown
#   mailboxes              sending accounts; smtp_host '' = this server; status new | ok | error;
#                          retry_at = not used again before then after a connection failure
#   campaigns.mailbox_ids  comma-separated mailboxes.id to rotate through; '' = the campaign's own From
#   campaigns.ai_news      the writer also reads recent headlines about each lead's company
#   replies.draft          the AI's proposed answer, waiting for the operator (ai_reply_mode "draft")
#   placement_tests        a test email sent to every connected account; results says, per account,
#                          whether it landed in the inbox, spam, or was not found
#   campaigns.subject_b    second subject to test; ab_percent of the list gets A or B first, the rest
#                          waits ab_wait_hours after the test group is out, then gets ab_winner (a | b)
#   messages.variant       '' (no test) | a | b (test group) | rest (waits for the winner)
#   campaigns.skip_risky   also leave out system mailboxes and likely typos
#   team.role              owner | admin | member (see team.py); password_hash is only used without
#                          a mail server; session_epoch changes to sign someone out everywhere
#   agent_threads.kind     outreach (the main agent) | offer (helps write the owner's offer)
#   agent_threads.status   idle | running | error; activity = what a running turn is doing now,
#                          heartbeat_at = last sign of life (see agent_runs.py)
#   contacts.research      facts the agent found about the lead, with sources; the writer uses them
#   offers                 what the owner sells and to whom; one is_default, campaigns.offer_id picks one
#   prospects              uploaded leads without a usable email; status needs_email | found (published
#                          on their own site, added to the list) | guessed (pattern, needs the owner's OK)
#                          | not_found | added | dismissed


class DB:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()

    @property
    def conn(self):
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            # isolation_level=None: autocommit, transactions are explicit (tx()).
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA busy_timeout=30000')
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, 'conn', None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def init(self, seed=None):
        self.conn.executescript(SCHEMA)
        self.migrate()
        for key, value in (seed or {}).items():
            if value:
                self.run('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', key, str(value))

    def migrate(self):
        """Brings the database up to the current version, one migration per transaction."""
        version = self.conn.execute('PRAGMA user_version').fetchone()[0]
        for number, script in enumerate(MIGRATIONS, start=1):
            if number <= version:
                continue
            statements = [s.strip() for s in script.split(';') if s.strip()]
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                for statement in statements:
                    self.conn.execute(statement)
                self.conn.execute(f'PRAGMA user_version = {number}')
            except BaseException:
                self.conn.execute('ROLLBACK')
                raise
            self.conn.execute('COMMIT')

    def track_ai(self, kind, usage):
        """Adds one AI call's token counts to this month's totals."""
        usage = usage or {}
        self.run('INSERT INTO ai_usage (month, kind, calls, input_tokens, output_tokens) VALUES (?, ?, 1, ?, ?) '
                 'ON CONFLICT(month, kind) DO UPDATE SET calls = calls + 1, '
                 'input_tokens = input_tokens + excluded.input_tokens, '
                 'output_tokens = output_tokens + excluded.output_tokens',
                 time.strftime('%Y-%m'), kind, int(usage.get('input') or 0), int(usage.get('output') or 0))

    # -- query helpers -----------------------------------------------------
    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def val(self, sql, *args, default=None):
        row = self.conn.execute(sql, args).fetchone()
        return default if row is None or row[0] is None else row[0]

    def run(self, sql, *args):
        return self.conn.execute(sql, args)

    def many(self, sql, rows):
        return self.conn.executemany(sql, rows)

    @contextlib.contextmanager
    def tx(self):
        """Write transaction. Nested use joins the outer transaction."""
        if self.conn.in_transaction:
            yield self
            return
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            yield self
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise
        else:
            self.conn.execute('COMMIT')

    # -- settings ----------------------------------------------------------
    def settings(self):
        values = dict(DEFAULT_SETTINGS)
        for row in self.q('SELECT key, value FROM settings'):
            values[row['key']] = row['value']
        return values

    def setting(self, key):
        return self.val('SELECT value FROM settings WHERE key = ?', key,
                        default=DEFAULT_SETTINGS.get(key, ''))

    def set_setting(self, key, value):
        self.run('INSERT INTO settings (key, value) VALUES (?, ?) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value', key, str(value))

    def state(self, key, default=''):
        return self.val('SELECT value FROM worker_state WHERE key = ?', key, default=default)

    def set_state(self, key, value):
        self.run('INSERT INTO worker_state (key, value) VALUES (?, ?) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value', key, str(value))


def now():
    return time.time()


def loads(text, default=None):
    if not text:
        return {} if default is None else default
    try:
        return json.loads(text)
    except ValueError:
        return {} if default is None else default
