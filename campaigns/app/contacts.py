"""Contact lists: CSV parsing, validation, import and suppression."""
import csv
import io
import json
import re

from .db import now

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")

# Mailboxes that exist to receive system mail, never marketing.
ROLE_LOCALPARTS = {
    'abuse', 'postmaster', 'mailer-daemon', 'noreply', 'no-reply', 'donotreply',
    'do-not-reply', 'spam', 'hostmaster', 'bounce', 'bounces', 'devnull',
}

EMAIL_HEADERS = ('email', 'e-mail', 'email address', 'e-mail address', 'mail', 'emailaddress')
FIRST_HEADERS = ('first name', 'firstname', 'first', 'fname', 'given name', 'givenname')
LAST_HEADERS = ('last name', 'lastname', 'last', 'lname', 'surname', 'family name')
NAME_HEADERS = ('name', 'full name', 'fullname', 'contact name')

MAX_ROWS = 500_000


def normalize_email(raw):
    """Returns a lower-cased valid address or None."""
    if raw is None:
        return None
    email = str(raw).strip().strip('<>"\'').strip()
    if email.lower().startswith('mailto:'):
        email = email[7:]
    if '@' not in email or len(email) > 254:
        return None
    local, _, domain = email.rpartition('@')
    try:
        domain = domain.strip().rstrip('.').encode('idna').decode('ascii')
    except UnicodeError:
        return None
    email = f'{local}@{domain}'.lower()
    if len(local) > 64 or '..' in local or local.startswith('.') or local.endswith('.'):
        return None
    return email if EMAIL_RE.match(email) else None


def is_role_address(email):
    return email.split('@', 1)[0] in ROLE_LOCALPARTS


def _slug(header):
    slug = re.sub(r'[^a-z0-9]+', '_', header.strip().lower()).strip('_')
    return slug or 'field'


def _decode(data):
    if isinstance(data, str):
        return data
    # UTF-16 only with a byte-order mark: without one it "decodes" any even-length file.
    encodings = ('utf-16',) if data[:2] in (b'\xff\xfe', b'\xfe\xff') else ('utf-8-sig', 'cp1252')
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    return data.decode('latin-1', errors='replace')


def _delimiter(text):
    """The separator used on the most lines; comma when nothing else is found."""
    lines = [line for line in text.splitlines()[:50] if line.strip()]
    best, best_lines = ',', 0
    for candidate in (',', ';', chr(9), '|'):
        used_on = sum(1 for line in lines if candidate in line)
        if used_on > best_lines:
            best, best_lines = candidate, used_on
    return best


def parse_upload(filename, data):
    """An uploaded file: Excel (.xlsx) or any CSV/TSV/plain-text list."""
    name = (filename or '').lower()
    if name.endswith('.xls'):
        raise ValueError('Old Excel files (.xls) cannot be read. In Excel use File > Save As > '
                         'Excel Workbook (.xlsx) or CSV, then upload that.')
    if name.endswith(('.xlsx', '.xlsm')) or data[:4] == b'PK\x03\x04':
        return parse_xlsx(data)
    return parse_csv(data)


def parse_xlsx(data):
    """The first sheet of an Excel workbook."""
    try:
        import openpyxl
    except ImportError:
        raise ValueError('Excel files need the openpyxl package. Save the sheet as CSV instead.')
    try:
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise ValueError('This file could not be read as an Excel workbook.')
    sheet = book.worksheets[0]
    table = []
    for row in sheet.iter_rows(values_only=True):
        cells = ['' if value is None else str(value).strip() for value in row]
        if any(cells):
            table.append(cells)
        if len(table) > MAX_ROWS + 1:
            break
    book.close()
    return parse_table(table)


def parse_csv(data):
    """Parses CSV / TSV / one-address-per-line text.

    Returns (rows, columns) where each row is
    {'email': raw, 'first_name', 'last_name', 'fields': {...}} and columns
    describes how the file's columns were understood.
    """
    text = _decode(data).replace('\x00', '').replace('\r\n', '\n').replace('\r', '\n')
    try:
        table = [row for row in csv.reader(io.StringIO(text), delimiter=_delimiter(text))
                 if any(c.strip() for c in row)]
    except csv.Error:
        raise ValueError('This file could not be read as CSV: a cell is too long or a quote is not closed.')
    return parse_table(table)


def parse_table(table):
    """Rows of cells (first row may be headers) -> (rows, columns)."""
    if not table:
        return [], {'email': None, 'fields': []}

    first = [c.strip() for c in table[0]]
    has_header = not any(normalize_email(c) for c in first)
    if has_header:
        headers = [c.lower() for c in first]
        body = table[1:]
    else:
        headers = [''] * len(first)
        body = table

    width = max(len(r) for r in table)
    headers += [''] * (width - len(headers))

    def find(names):
        for i, h in enumerate(headers):
            if h in names or h.replace('_', ' ') in names:   # first_name, as many tools export it
                return i
        return None

    email_col = find(EMAIL_HEADERS)
    if email_col is None:
        # Pick the column where most of the first rows look like addresses.
        best, best_hits = None, 0
        for col in range(width):
            hits = sum(1 for r in body[:50] if col < len(r) and normalize_email(r[col]))
            if hits > best_hits:
                best, best_hits = col, hits
        email_col = best
    if email_col is None:
        return [], {'email': None, 'fields': []}

    first_col, last_col, name_col = find(FIRST_HEADERS), find(LAST_HEADERS), find(NAME_HEADERS)
    special = {email_col, first_col, last_col, name_col}
    extra_cols = [(i, _slug(h)) for i, h in enumerate(headers) if h and i not in special]

    rows = []
    for raw in body[:MAX_ROWS]:
        def cell(i):
            return raw[i].strip() if i is not None and i < len(raw) else ''
        first_name, last_name = cell(first_col), cell(last_col)
        if not first_name and not last_name and name_col is not None:
            parts = cell(name_col).split(None, 1)
            first_name = parts[0] if parts else ''
            last_name = parts[1] if len(parts) > 1 else ''
        fields = {key: cell(i) for i, key in extra_cols if cell(i)}
        rows.append({'email': cell(email_col), 'first_name': first_name,
                     'last_name': last_name, 'fields': fields})
    columns = {
        'email': headers[email_col] or f'column {email_col + 1}',
        'first_name': headers[first_col] if first_col is not None else (headers[name_col] if name_col is not None else None),
        'last_name': headers[last_col] if last_col is not None else None,
        'fields': [key for _, key in extra_cols],
        'truncated': len(body) > MAX_ROWS,
    }
    return rows, columns


def import_rows(db, list_id, rows):
    """Adds parsed rows to a list. Returns a summary of what happened."""
    summary = {'total': len(rows), 'added': 0, 'existing': 0, 'already_in_list': 0,
               'invalid': 0, 'duplicates': 0, 'role': 0, 'suppressed': 0, 'invalid_samples': []}
    seen = set()
    ts = now()
    with db.tx():
        suppressed = {r['email'].lower(): r['reason'] for r in db.q('SELECT email, reason FROM suppressions')}
        for row in rows:
            email = normalize_email(row.get('email'))
            if not email:
                summary['invalid'] += 1
                if len(summary['invalid_samples']) < 10 and row.get('email'):
                    summary['invalid_samples'].append(str(row['email'])[:80])
                continue
            if email in seen:
                summary['duplicates'] += 1
                continue
            seen.add(email)
            if is_role_address(email):
                summary['role'] += 1
                continue
            contact = db.one('SELECT id, fields, first_name, last_name FROM contacts WHERE email = ?', email)
            if contact is None:
                status = suppressed.get(email, 'active')
                if status == 'manual':
                    status = 'unsubscribed'
                cur = db.run(
                    'INSERT INTO contacts (email, first_name, last_name, fields, status, created_at, updated_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    email, row.get('first_name', ''), row.get('last_name', ''),
                    json.dumps(row.get('fields') or {}), status, ts, ts)
                contact_id = cur.lastrowid
                new_contact = True
            else:
                contact_id = contact['id']
                new_contact = False
                merged = json.loads(contact['fields'] or '{}')
                merged.update(row.get('fields') or {})
                db.run('UPDATE contacts SET first_name = ?, last_name = ?, fields = ?, updated_at = ? WHERE id = ?',
                       row.get('first_name') or contact['first_name'],
                       row.get('last_name') or contact['last_name'],
                       json.dumps(merged), ts, contact_id)
            if email in suppressed:
                summary['suppressed'] += 1
            cur = db.run('INSERT OR IGNORE INTO list_members (list_id, contact_id, added_at) VALUES (?, ?, ?)',
                         list_id, contact_id, ts)
            if cur.rowcount == 0:
                summary['already_in_list'] += 1
            elif new_contact:
                summary['added'] += 1
            else:
                summary['existing'] += 1
    return summary


def suppress(db, email, reason, detail=''):
    """Stops all future mail to an address."""
    email = (email or '').strip().lower()
    if not email:
        return
    ts = now()
    with db.tx():
        db.run('INSERT INTO suppressions (email, reason, detail, created_at) VALUES (?, ?, ?, ?) '
               'ON CONFLICT(email) DO UPDATE SET reason = excluded.reason, detail = excluded.detail',
               email, reason, detail[:500], ts)
        status = 'unsubscribed' if reason == 'manual' else reason
        db.run('UPDATE contacts SET status = ?, status_reason = ?, updated_at = ? WHERE email = ?',
               status, detail[:500], ts, email)
        db.run("UPDATE messages SET status = 'cancelled', error = ? "
               "WHERE to_email = ? AND status = 'queued' AND kind = 'campaign'",
               f'Recipient {reason}', email)


def unsuppress(db, email):
    email = (email or '').strip().lower()
    with db.tx():
        db.run('DELETE FROM suppressions WHERE email = ?', email)
        db.run("UPDATE contacts SET status = 'active', status_reason = '', soft_bounces = 0, updated_at = ? "
               "WHERE email = ?", now(), email)


def is_suppressed(db, email):
    return db.one('SELECT reason FROM suppressions WHERE email = ?', (email or '').lower())


def list_counts(db, list_id):
    row = db.one(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN c.status = 'active' THEN 1 ELSE 0 END) AS active, "
        "SUM(CASE WHEN c.status = 'unsubscribed' THEN 1 ELSE 0 END) AS unsubscribed, "
        "SUM(CASE WHEN c.status = 'bounced' THEN 1 ELSE 0 END) AS bounced, "
        "SUM(CASE WHEN c.status = 'complained' THEN 1 ELSE 0 END) AS complained "
        "FROM list_members m JOIN contacts c ON c.id = m.contact_id WHERE m.list_id = ?", list_id)
    return {k: (row[k] or 0) for k in row.keys()}
