"""Every page stays quick with a realistic amount of data: 5,000 leads and
25,000 sent emails across five campaigns."""
import random
import time

from app import service
from tests.test_e2e import env, make_list  # noqa: F401  (env is a fixture)

LEADS, CAMPAIGNS = 5000, 5
PAGE_BUDGET = 0.5   # seconds, generous for slow machines


def _fill(env):
    db = env.db
    list_id = make_list(env, csv_text='email\nseed@acme.test\n')
    ts = time.time() - 20 * 86400
    rng = random.Random(1)
    with db.tx():
        for i in range(LEADS):
            db.run("INSERT INTO contacts (email, first_name, fields, created_at, updated_at, lead_status) "
                   "VALUES (?, ?, '{\"company\": \"Co\"}', ?, ?, ?)", f'lead{i}@d{i % 400}.test', f'Lead{i}', ts, ts,
                   rng.choice(['new', 'contacted', 'replied', 'interested', 'meeting']))
        db.run('INSERT OR IGNORE INTO list_members (list_id, contact_id, added_at) SELECT ?, id, ? FROM contacts', list_id, ts)
    ids = []
    for n in range(CAMPAIGNS):
        cid = service.create_campaign(db, db.settings(), {'name': f'Campaign {n}', 'subject': 'Hi',
                                                          'html': '<p>Hi</p>', 'list_id': list_id})
        db.run("UPDATE campaigns SET status = 'sent', started_at = ?, finished_at = ? WHERE id = ?", ts, ts, cid)
        with db.tx():
            db.run("INSERT INTO messages (id, campaign_id, contact_id, kind, to_email, to_domain, from_email, subject, "
                   "status, sent_at, opened_at, replied_at, created_at) "
                   "SELECT lower(hex(randomblob(16))), ?, id, 'campaign', email, substr(email, instr(email, '@') + 1), "
                   "'me@example.com', 'Hi', 'sent', ?, CASE WHEN id % 3 = 0 THEN ? END, CASE WHEN id % 17 = 0 THEN ? END, ? "
                   "FROM contacts", cid, ts + n, ts + n + 60, ts + n + 120, ts + n)
        ids.append(cid)
    return list_id, ids


def test_pages_are_fast_with_lots_of_data(env):
    list_id, campaign_ids = _fill(env)
    lead = env.db.val('SELECT id FROM contacts LIMIT 1 OFFSET 100')
    timings = {}
    for path in ('/', '/inbox', '/leads', '/leads?status=replied', f'/leads/{lead}', '/lists', f'/lists/{list_id}',
                 '/campaigns', f'/campaigns/{campaign_ids[0]}', '/calendar', '/analytics', '/emails',
                 '/suppressions', '/accounts', '/settings', '/agent'):
        env.client.get('/campaigns' + path, follow_redirects=True)         # warm the template cache
        started = time.perf_counter()
        response = env.client.get('/campaigns' + path, follow_redirects=True)
        timings[path] = time.perf_counter() - started
        assert response.status_code == 200, path
    report = ', '.join(f'{p} {t * 1000:.0f}ms' for p, t in sorted(timings.items(), key=lambda x: -x[1]))
    print('\npage times:', report)
    slow = {p: round(t, 2) for p, t in timings.items() if t > PAGE_BUDGET}
    assert not slow, report
