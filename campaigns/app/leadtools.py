"""Tools for finding and checking leads.

- find_email: the likely address of a person at a company. Built in: common
  address patterns plus a check that the domain receives mail. With a Hunter.io
  key: Hunter's finder, which returns verified addresses with a score.
- verify_list: marks addresses that would bounce (domain does not receive mail,
  disposable mailbox) before a campaign uses them.
- fetch_site: reads a company's public website so the AI writes from real facts.

Addresses are never "verified" by opening SMTP connections to other mail
servers: providers treat that as address harvesting and block the sending IP.
"""
import concurrent.futures
import html as html_lib
import ipaddress
import json
import re
import socket
import unicodedata
from urllib.parse import urljoin, urlparse

import dns.exception
import dns.resolver
import requests

from . import contacts
from .db import loads, now

HUNTER_API = 'https://api.hunter.io/v2'
DOMAIN_CACHE_DAYS = 30
SITE_CACHE_DAYS = 30
MAX_PAGE_BYTES = 800_000

PATTERNS = (
    ('{first}.{last}', 'first.last'), ('{first}', 'first'), ('{f}{last}', 'flast'), ('{first}{last}', 'firstlast'),
    ('{f}.{last}', 'f.last'), ('{first}_{last}', 'first_last'), ('{first}{l}', 'firstl'), ('{last}', 'last'),
    ('{last}.{first}', 'last.first'), ('{first}-{last}', 'first-last'),
)
FREE_PROVIDERS = {
    'gmail.com', 'googlemail.com', 'yahoo.com', 'ymail.com', 'outlook.com', 'hotmail.com', 'live.com', 'msn.com',
    'aol.com', 'icloud.com', 'me.com', 'mac.com', 'gmx.com', 'gmx.de', 'gmx.net', 'web.de', 'mail.com', 'yandex.com',
    'yandex.ru', 'mail.ru', 'proton.me', 'protonmail.com', 'zoho.com', 'qq.com', '163.com', 'hotmail.co.uk',
    'yahoo.co.uk', 'orange.fr', 'free.fr', 'libero.it', 't-online.de',
}
DISPOSABLE = {
    'mailinator.com', '10minutemail.com', 'guerrillamail.com', 'guerrillamail.net', 'sharklasers.com', 'yopmail.com',
    'trashmail.com', 'tempmail.com', 'temp-mail.org', 'getnada.com', 'dispostable.com', 'maildrop.cc',
    'throwawaymail.com', 'fakeinbox.com', 'mintemail.com', 'mohmal.com', 'emailondeck.com', 'tempail.com',
    'moakt.com', 'spamgourmet.com', 'mailnesia.com', 'burnermail.io', 'tempr.email', 'discard.email',
}
WEBSITE_FIELDS = ('website', 'company_website', 'current_website', 'website_url', 'business_website', 'url', 'site',
                  'web', 'domain', 'company_domain', 'homepage')
# Columns that hold some other page, never the company's own site.
NOT_THEIR_SITE = ('preview', 'demo', 'mockup', 'linkedin', 'facebook', 'instagram', 'twitter', 'tiktok', 'youtube',
                  'logo', 'image', 'photo', 'file', 'calendly')


class ToolError(Exception):
    """Shown to the operator as is."""


# -- names and domains --------------------------------------------------------------
def _ascii(text):
    text = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]', '', text.lower())


def domain_from(value):
    """'https://www.Acme.com/about' or 'acme.com' or 'ana@acme.com' -> 'acme.com'."""
    value = str(value or '').strip().lower()
    if not value:
        return ''
    if '@' in value and '/' not in value:
        value = value.rsplit('@', 1)[-1]
    if '://' not in value:
        value = 'http://' + value
    host = (urlparse(value).hostname or '').strip('.')
    if host.startswith('www.'):
        host = host[4:]
    if not re.fullmatch(r'[a-z0-9-]+(\.[a-z0-9-]+)+', host or ''):
        return ''
    return host


def guesses(first, last, domain):
    first, last = _ascii(first), _ascii(last)
    domain = domain_from(domain)
    if not first or not domain:
        return []
    out, seen = [], set()
    for pattern, name in PATTERNS:
        if ('{last}' in pattern or '{l}' in pattern) and not last:
            continue
        local = pattern.format(first=first, last=last, f=first[:1], l=last[:1])
        address = f'{local}@{domain}'
        if address not in seen and contacts.normalize_email(address):
            seen.add(address)
            out.append({'email': address, 'pattern': name})
    return out


# -- does a domain receive mail? -----------------------------------------------------
def _resolver():
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 4
    resolver.timeout = 2
    return resolver


def dns_reachable():
    """One live lookup (never cached): False when the resolver does not answer."""
    try:
        _resolver().resolve('gmail.com', 'MX')
        return True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return True
    except dns.exception.DNSException:
        return False


def check_domain(db, domain):
    """{'domain', 'mail': True/False/None, 'mx': [...], 'kind': business|free|disposable}.
    mail is None when DNS could not be reached (unknown, not bad)."""
    domain = domain_from(domain)
    if not domain:
        return {'domain': '', 'mail': False, 'mx': [], 'kind': 'invalid'}
    cached = _cached_domain(db, domain)
    if cached is not None:
        return cached
    accepts, mx = _lookup(domain)
    _store_domain(db, domain, accepts, mx)
    return {'domain': domain, 'mail': accepts, 'mx': mx[:5], 'kind': _domain_kind(domain)}


def _domain_kind(domain):
    return 'disposable' if domain in DISPOSABLE else 'free' if domain in FREE_PROVIDERS else 'business'


def _cached_domain(db, domain):
    cached = db.one('SELECT * FROM domain_checks WHERE domain = ?', domain)
    if cached is None or now() - cached['checked_at'] >= DOMAIN_CACHE_DAYS * 86400:
        return None
    return {'domain': domain, 'mail': bool(cached['accepts_mail']), 'mx': loads(cached['mx'], default=[]),
            'kind': _domain_kind(domain)}


def _store_domain(db, domain, accepts, mx):
    if accepts is not None:
        db.run('INSERT INTO domain_checks (domain, accepts_mail, mx, checked_at) VALUES (?, ?, ?, ?) '
               'ON CONFLICT(domain) DO UPDATE SET accepts_mail = excluded.accepts_mail, mx = excluded.mx, '
               'checked_at = excluded.checked_at', domain, 1 if accepts else 0, json.dumps(mx[:5]), now())


def _lookup(domain):
    """(accepts mail: True/False/None, mx hosts). DNS only, no database, so it can run in threads."""
    resolver = _resolver()
    mx, accepts = [], None
    try:
        answers = resolver.resolve(domain, 'MX')
        mx = sorted((r.preference, r.exchange.to_text().rstrip('.')) for r in answers)
        mx = [host for _, host in mx]
        # A "null MX" (RFC 7505) says the domain accepts no mail at all.
        accepts = bool(mx) and mx != ['']
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        try:  # No MX: mail goes to the domain's own address, if it has one.
            resolver.resolve(domain, 'A')
            accepts = True
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            accepts = False
        except dns.exception.DNSException:
            accepts = None
    except dns.exception.DNSException:
        accepts = None
    return accepts, mx


# -- Hunter.io (optional) --------------------------------------------------------------
def _hunter(path, api_key, params):
    try:
        reply = requests.get(f'{HUNTER_API}/{path}', params=dict(params, api_key=api_key), timeout=25)
    except requests.RequestException:
        raise ToolError('Could not reach Hunter.io.')
    if reply.status_code == 401:
        raise ToolError('Hunter.io rejected the API key. Check it in Settings → Lead tools.')
    if reply.status_code == 403:
        raise ToolError('Hunter.io rate limit reached. Wait a moment and try again.')
    if reply.status_code == 429:
        raise ToolError('The Hunter.io plan has no searches left this month.')
    if reply.status_code == 202:
        return None  # still working on it
    if reply.status_code >= 400:
        try:
            detail = (reply.json().get('errors') or [{}])[0].get('details', '')
        except ValueError:
            detail = ''
        raise ToolError(f'Hunter.io error ({reply.status_code}) {detail}'.strip())
    try:
        return reply.json().get('data') or {}
    except ValueError:
        raise ToolError('Hunter.io sent an answer that could not be read.')


def hunter_find(api_key, first, last, domain):
    data = _hunter('email-finder', api_key, {'domain': domain, 'first_name': first, 'last_name': last})
    if not data or not data.get('email'):
        return None
    verification = data.get('verification') or {}
    return {'email': data['email'].lower(), 'score': data.get('score'),
            'status': verification.get('status') or 'unknown', 'source': 'Hunter.io'}


def hunter_domain(api_key, domain, limit=10):
    data = _hunter('domain-search', api_key, {'domain': domain, 'limit': limit})
    if not data:
        return {'pattern': None, 'people': []}
    people = []
    for item in data.get('emails') or []:
        people.append({'email': (item.get('value') or '').lower(), 'first_name': item.get('first_name') or '',
                       'last_name': item.get('last_name') or '', 'position': item.get('position') or '',
                       'confidence': item.get('confidence'), 'type': item.get('type') or ''})
    return {'pattern': data.get('pattern'), 'organization': data.get('organization') or '', 'people': people}


# -- the finder -------------------------------------------------------------------------
def find_email(db, settings, first, last, domain):
    """Best guesses for one person. Returns {'domain', 'mail', 'kind', 'candidates': [...], 'note'}.
    Candidates: {'email', 'confidence': high|medium|low, 'source', 'status'}."""
    domain = domain_from(domain)
    if not domain:
        raise ToolError('Enter the company website or domain, for example acme.com.')
    if not _ascii(first):
        raise ToolError('Enter at least a first name.')
    info = check_domain(db, domain)
    result = {'domain': domain, 'mail': info['mail'], 'kind': info['kind'], 'candidates': [], 'note': ''}
    if info['mail'] is False:
        result['note'] = f'{domain} does not receive email, so no address there can work.'
        return result
    if info['kind'] == 'free':
        result['note'] = (f'{domain} is a personal email provider; names do not tell you the address there. '
                          'Use the company\'s own domain.')
        return result
    hunter_key = (settings.get('hunter_api_key') or '').strip()
    if hunter_key:
        found = hunter_find(hunter_key, first, last, domain)
        if found:
            level = 'high' if found['status'] == 'valid' or (found['score'] or 0) >= 90 else 'medium'
            result['candidates'].append({'email': found['email'], 'confidence': level, 'source': 'Hunter.io',
                                         'status': found['status'], 'score': found['score']})
    # A pattern the company is known to use (from leads you already have there) comes first.
    known = _known_pattern(db, domain)
    for index, guess in enumerate(guesses(first, last, domain)):
        if any(c['email'] == guess['email'] for c in result['candidates']):
            continue
        if known:
            # This company's own pattern beats the general favourite.
            level = 'medium' if guess['pattern'] == known else 'low'
        else:
            level = 'medium' if index == 0 else 'low'
        result['candidates'].append({'email': guess['email'], 'confidence': level,
                                     'source': 'pattern used by your other leads at this company'
                                     if known and guess['pattern'] == known else f'common pattern ({guess["pattern"]})',
                                     'status': 'unverified'})
    result['candidates'].sort(key=lambda c: {'high': 0, 'medium': 1, 'low': 2}[c['confidence']])
    result['candidates'] = result['candidates'][:8]
    if not hunter_key:
        result['note'] = ('Guessed from common patterns and not verified. Send only to addresses you are '
                          'confident about: bounces hurt your sending reputation. Add a Hunter.io key in '
                          'Settings → Lead tools for verified results.')
    return result


def _known_pattern(db, domain):
    rows = db.q("SELECT email, first_name, last_name FROM contacts WHERE email LIKE ? AND first_name != '' LIMIT 50",
                f'%@{domain}')
    counts = {}
    for row in rows:
        local = row['email'].split('@')[0]
        for guess in guesses(row['first_name'], row['last_name'], domain):
            if guess['email'].split('@')[0] == local:
                counts[guess['pattern']] = counts.get(guess['pattern'], 0) + 1
                break
    return max(counts, key=counts.get) if counts else None


# -- checking single addresses ------------------------------------------------------------
COMMON_DOMAINS = ('gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'icloud.com', 'aol.com', 'live.com',
                  'msn.com', 'gmx.com', 'gmx.de', 'web.de', 'yahoo.co.uk', 'hotmail.co.uk', 'proton.me',
                  'protonmail.com', 'googlemail.com', 'me.com', 'mail.com', 'yandex.com', 'zoho.com')
HUNTER_STATUS = {
    'valid': ('valid', 'Hunter.io confirmed the mailbox exists.'),
    'invalid': ('invalid', 'Hunter.io found that this mailbox does not exist.'),
    'accept_all': ('risky', 'The domain accepts every address, so this one cannot be confirmed.'),
    'webmail': ('valid', 'A personal webmail address that looks deliverable.'),
    'disposable': ('invalid', 'A throwaway mailbox.'),
    'unknown': ('unknown', 'Hunter.io could not tell.'),
}


# Real domains one keystroke away from a big provider.
LOOKALIKES = {'ymail.com', 'email.com', 'cloud.com', 'gmx.net', 'mail.ru', 'rocketmail.com', 'hotmail.de',
              'hotmail.fr', 'hotmail.it', 'hotmail.es', 'outlook.de', 'outlook.fr', 'yahoo.de', 'yahoo.fr'}
# Endings that are almost always a slip for .com (gmail.co, gmail.cm).
TYPO_TLDS = {'co', 'cm', 'con', 'om', 'cmo', 'ocm', 'cpm', 'vom', 'xom', 'comm', 'coom', 'cim', 'c'}


def _distance(a, b):
    """Edit distance counting a swap of neighbours as one edit, so gmial.com is
    one slip from gmail.com."""
    rows = [list(range(len(b) + 1))]
    for i, ca in enumerate(a, 1):
        row = [i]
        for j, cb in enumerate(b, 1):
            best = min(rows[-1][j] + 1, row[j - 1] + 1, rows[-1][j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                best = min(best, rows[-2][j - 2] + 1)
            row.append(best)
        rows.append(row)
    return rows[-1][-1]


def typo_suggestion(domain):
    """The big provider a domain looks like a slip of ('gmial.com' -> 'gmail.com'), or ''.
    Only single slips on the longer provider names count, so real companies with
    short domains (ge.com, acme.com) are not flagged."""
    domain = (domain or '').lower()
    if domain in COMMON_DOMAINS or domain in FREE_PROVIDERS or domain in DISPOSABLE or domain in LOOKALIKES:
        return ''
    label, _, tld = domain.partition('.')
    best, best_key = '', None
    for known in COMMON_DOMAINS:
        known_label, _, known_tld = known.partition('.')
        if label == known_label:
            if tld not in TYPO_TLDS:
                continue
        elif not (len(known_label) >= 5 and len(label) >= 4 and _distance(label, known_label) == 1
                  and (tld == known_tld or tld in TYPO_TLDS)):
            continue
        key = (_distance(label, known_label), _distance(tld, known_tld))
        if best_key is None or key < best_key:
            best, best_key = known, key
    return best


def check_address(db, settings, raw, use_hunter=True):
    """Whether one address works, without emailing it.

    Returns {'email', 'verdict': valid|risky|invalid|unknown, 'checks': [{'name', 'state', 'detail'}],
    'suggestion'}. 'valid' without Hunter.io means "nothing wrong found", not "confirmed".
    """
    checks = []
    original = str(raw or '').strip()
    email = contacts.normalize_email(original)
    if not email:
        return {'email': original, 'verdict': 'invalid', 'suggestion': '',
                'checks': [{'name': 'Format', 'state': 'bad', 'detail': 'This is not a valid email address.'}]}
    checks.append({'name': 'Format', 'state': 'ok', 'detail': 'Looks like a real email address.'})
    local, domain = email.rsplit('@', 1)
    suggestion = ''
    fix = typo_suggestion(domain)
    if fix:
        suggestion = f'{local}@{fix}'
        checks.append({'name': 'Typo', 'state': 'warn', 'detail': f'Did you mean {suggestion}?'})
    info = check_domain(db, domain)
    if info['mail'] is False:
        checks.append({'name': 'Domain', 'state': 'bad', 'detail': f'{domain} does not receive email.'})
    elif info['mail'] is None:
        checks.append({'name': 'Domain', 'state': 'warn', 'detail': f'Could not look up {domain} right now.'})
    else:
        where = ', '.join(info['mx'][:2]) or domain
        checks.append({'name': 'Domain', 'state': 'ok', 'detail': f'{domain} receives email (at {where}).'})
    if info['kind'] == 'disposable':
        checks.append({'name': 'Mailbox type', 'state': 'bad', 'detail': 'A throwaway mailbox that stops working soon.'})
    elif contacts.is_role_address(email):
        checks.append({'name': 'Mailbox type', 'state': 'warn',
                       'detail': 'A system mailbox (postmaster, no-reply…). Nobody reads marketing sent there.'})
    elif info['kind'] == 'free':
        checks.append({'name': 'Mailbox type', 'state': 'ok', 'detail': 'A personal address at a free email provider.'})
    else:
        checks.append({'name': 'Mailbox type', 'state': 'ok', 'detail': 'A company address.'})

    verdict = 'invalid' if any(c['state'] == 'bad' for c in checks) else (
        'unknown' if info['mail'] is None else 'risky' if any(c['state'] == 'warn' for c in checks) else 'valid')
    hunter_key = (settings.get('hunter_api_key') or '').strip()
    if use_hunter and hunter_key and verdict != 'invalid':
        data = _hunter('email-verifier', hunter_key, {'email': email})
        if data is None:
            checks.append({'name': 'Mailbox', 'state': 'warn', 'detail': 'Hunter.io is still checking. Try again in a minute.'})
        else:
            status = data.get('status') or 'unknown'
            verdict, detail = HUNTER_STATUS.get(status, ('unknown', f'Hunter.io answered "{status}".'))
            score = data.get('score')
            checks.append({'name': 'Mailbox', 'state': {'valid': 'ok', 'invalid': 'bad'}.get(verdict, 'warn'),
                           'detail': detail + (f' Score {score}/100.' if score is not None else '')})
    elif verdict == 'valid':
        checks.append({'name': 'Mailbox', 'state': 'warn',
                       'detail': 'The mailbox itself was not confirmed (add a Hunter.io key to confirm it).'})
    return {'email': email, 'verdict': verdict, 'checks': checks, 'suggestion': suggestion}


def check_many(db, settings, text, limit=50, use_hunter=True):
    """Checks up to `limit` addresses from pasted text (any separators)."""
    found = re.findall(r'[^\s,;<>"\']+@[^\s,;<>"\']+', str(text or ''))
    seen, out = set(), []
    for raw in found:
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(check_address(db, settings, raw, use_hunter=use_hunter))
        if len(out) >= limit:
            break
    return out


# -- list verification --------------------------------------------------------------------
def verify_contacts(db, contact_rows, workers=12):
    """Checks every address's domain; stores verify_status on each contact.
    Returns counts per status: ok, invalid, risky, unknown."""
    domains = sorted({row['email'].rsplit('@', 1)[-1].lower() for row in contact_rows})[:5000]
    results, missing = {}, []
    for domain in domains:
        cached = _cached_domain(db, domain)
        if cached is None:
            missing.append(domain)
        else:
            results[domain] = cached
    # With DNS unreachable every lookup would wait for its timeout; one probe
    # first keeps a large list from holding up the sending worker.
    if missing and dns_reachable():
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            looked_up = list(zip(missing, pool.map(_lookup, missing)))
        with db.tx():
            for domain, (accepts, mx) in looked_up:
                _store_domain(db, domain, accepts, mx)
                results[domain] = {'domain': domain, 'mail': accepts, 'mx': mx[:5], 'kind': _domain_kind(domain)}
    counts = {'ok': 0, 'invalid': 0, 'risky': 0, 'unknown': 0}
    ts = now()
    with db.tx():
        for row in contact_rows:
            domain = row['email'].rsplit('@', 1)[-1].lower()
            info = results.get(domain)
            fix = typo_suggestion(domain)
            if info is None or info['mail'] is None:
                status, detail = 'unknown', 'Could not check the domain right now'
            elif info['mail'] is False:
                status, detail = 'invalid', f'{domain} does not receive email' + (f' (did you mean {fix}?)' if fix else '')
            elif info['kind'] == 'disposable':
                status, detail = 'invalid', 'Disposable mailbox'
            elif fix:
                status, detail = 'risky', f'Possible typo: did you mean {fix}?'
            elif contacts.is_role_address(row['email']):
                status, detail = 'risky', 'System mailbox'
            else:
                status, detail = 'ok', ''
            counts[status] += 1
            db.run('UPDATE contacts SET verify_status = ?, verify_detail = ?, verified_at = ? WHERE id = ?',
                   status, detail, ts, row['id'])
    return counts


def verify_list(db, list_id, recheck_days=30):
    """Checks list members never checked, checked over recheck_days ago, or whose
    domain could not be looked up a day ago or more. Returns the counts from
    verify_contacts, or None when nothing needed checking."""
    ts = now()
    rows = db.q("SELECT c.id, c.email FROM list_members m JOIN contacts c ON c.id = m.contact_id "
                "WHERE m.list_id = ? AND c.status = 'active' AND (c.verify_status = '' OR c.verified_at IS NULL "
                "OR c.verified_at < ? OR (c.verify_status = 'unknown' AND c.verified_at < ?))",
                list_id, ts - recheck_days * 86400, ts - 86400)
    return verify_contacts(db, rows) if rows else None


def list_quality(db, list_id):
    """How many active members of a list are ok, risky, invalid, unknown or not checked yet."""
    out = {'ok': 0, 'risky': 0, 'invalid': 0, 'unknown': 0, 'unchecked': 0}
    for row in db.q("SELECT c.verify_status AS s, COUNT(*) AS n FROM list_members m JOIN contacts c "
                    "ON c.id = m.contact_id WHERE m.list_id = ? AND c.status = 'active' GROUP BY c.verify_status",
                    list_id):
        out[row['s'] or 'unchecked'] = out.get(row['s'] or 'unchecked', 0) + row['n']
    out['total'] = sum(out.values())
    return out


# -- reading a company website ----------------------------------------------------------------
def _public_host(host):
    """True when every address the host name points to is on the public internet.
    Stops a lead record from making the server fetch its own internal services."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(a.split('%')[0]).is_global for a in addresses)


def _safe_get(url, max_redirects=3):
    for _ in range(max_redirects + 1):
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ToolError('Only web addresses (http or https) can be read.')
        if parsed.port not in (None, 80, 443):
            raise ToolError('Only standard web ports can be read.')
        if not _public_host(parsed.hostname):
            raise ToolError(f'{parsed.hostname} is not a public website.')
        try:
            reply = requests.get(url, timeout=(5, 10), allow_redirects=False, stream=True,
                                 headers={'User-Agent': 'Mozilla/5.0 (compatible; CampaignsResearch/1.0)',
                                          'Accept': 'text/html,application/xhtml+xml'})
        except requests.RequestException:
            raise ToolError(f'Could not reach {parsed.hostname}.')
        if reply.is_redirect or reply.status_code in (301, 302, 303, 307, 308):
            location = reply.headers.get('Location')
            reply.close()
            if not location:
                raise ToolError('The site redirected without saying where.')
            url = urljoin(url, location)
            continue
        if reply.status_code >= 400:
            reply.close()
            raise ToolError(f'The site answered with an error ({reply.status_code}).')
        if 'html' not in (reply.headers.get('Content-Type') or 'text/html').lower():
            reply.close()
            raise ToolError('That address is not a web page.')
        body = b''
        for chunk in reply.iter_content(32768):
            body += chunk
            if len(body) >= MAX_PAGE_BYTES:
                break
        reply.close()
        return url, body.decode(reply.encoding or 'utf-8', 'replace')
    raise ToolError('The site redirected too many times.')


def _page_facts(url, page):
    def first(pattern):
        match = re.search(pattern, page, re.IGNORECASE | re.DOTALL)
        return html_lib.unescape(re.sub(r'\s+', ' ', match.group(1))).strip() if match else ''
    title = first(r'<title[^>]*>(.*?)</title>')
    description = (first(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']')
                   or first(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']'))
    body = re.sub(r'(?is)<(script|style|noscript|svg|nav|footer|header|form)\b.*?</\1>', ' ', page)
    headings = [html_lib.unescape(re.sub(r'<[^>]+>|\s+', ' ', h)).strip()
                for h in re.findall(r'(?is)<h[1-3][^>]*>(.*?)</h[1-3]>', body)]
    text = html_lib.unescape(re.sub(r'(?s)<[^>]+>', ' ', body))
    text = re.sub(r'\s+', ' ', text).strip()
    return {'url': url, 'title': title[:200], 'description': description[:500],
            'headings': [h for h in headings if h][:12], 'text': text[:3000]}


def fetch_site(db, address):
    """Facts from a company's home page, cached for 30 days."""
    domain = domain_from(address)
    if not domain:
        raise ToolError('No website to read.')
    cached = db.one('SELECT * FROM site_cache WHERE domain = ?', domain)
    if cached is not None and now() - cached['fetched_at'] < SITE_CACHE_DAYS * 86400:
        data = loads(cached['data'], default=None)
        if data is None:
            raise ToolError(cached['error'] or 'The site could not be read earlier.')
        return data
    try:
        try:
            url, page = _safe_get(f'https://{domain}/')
        except ToolError:
            url, page = _safe_get(f'https://www.{domain}/')
        data = _page_facts(url, page)
    except ToolError as exc:
        db.run('INSERT INTO site_cache (domain, data, error, fetched_at) VALUES (?, NULL, ?, ?) '
               'ON CONFLICT(domain) DO UPDATE SET data = NULL, error = excluded.error, fetched_at = excluded.fetched_at',
               domain, str(exc), now() - (SITE_CACHE_DAYS - 1) * 86400)   # retry failures after a day
        raise
    db.run('INSERT INTO site_cache (domain, data, error, fetched_at) VALUES (?, ?, \'\', ?) '
           'ON CONFLICT(domain) DO UPDATE SET data = excluded.data, error = \'\', fetched_at = excluded.fetched_at',
           domain, json.dumps(data), now())
    return data


def website_from_fields(fields):
    """The company website among a lead's columns, or ''."""
    for key in WEBSITE_FIELDS:
        if fields.get(key) and domain_from(fields[key]):
            return domain_from(fields[key])
    for key, value in fields.items():
        lowered = str(key).lower()
        if ('website' in lowered or lowered.endswith('_domain')) and not any(w in lowered for w in NOT_THEIR_SITE):
            if domain_from(value):
                return domain_from(value)
    return ''


def website_for(contact):
    """The company website of a lead: a website column, else the email domain
    when it is a company domain."""
    found = website_from_fields(loads(contact['fields']))
    if found:
        return found
    domain = contact['email'].rsplit('@', 1)[-1].lower()
    if domain in FREE_PROVIDERS or domain in DISPOSABLE:
        return ''
    return domain


# -- addresses a company publishes on its own website --------------------------------------
PAGE_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}')
HREF_RE = re.compile(r'href\s*=\s*["\']([^"\'#]+)', re.IGNORECASE)
CONTACT_WORDS = ('contact', 'get-in-touch', 'getintouch', 'enquir', 'inquir', 'reach-us', 'about', 'book')
NOT_AN_ADDRESS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.css', '.js', '.ico', '.pdf')
PLACEHOLDER_DOMAINS = ('example.com', 'example.org', 'example.net', 'domain.com', 'yourdomain', 'yoursite',
                       'website.com', 'company.com', 'mysite.com', 'email.com', 'sentry.io', 'sentry-next',
                       'wixpress.com', 'godaddy.com', 'squarespace.com', 'wordpress.com', 'ingest.sentry')


def _cloudflare(hexstr):
    """Cloudflare hides addresses as hex, XOR-ed with the first byte."""
    try:
        key = int(hexstr[:2], 16)
        return ''.join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr) - 1, 2))
    except ValueError:
        return ''


def emails_in_page(page):
    """Addresses written on a web page: mailto links, plain text, Cloudflare-protected
    and "name [at] site [dot] com" forms."""
    from urllib.parse import unquote
    text = html_lib.unescape(page)
    text = re.sub(r'\s*[\[(]\s*at\s*[\])]\s*', '@', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*[\[(]\s*dot\s*[\])]\s*', '.', text, flags=re.IGNORECASE)
    found = [_cloudflare(h) for h in re.findall(r'data-cfemail=["\']([0-9a-fA-F]+)', page)]
    found += [_cloudflare(h) for h in re.findall(r'/cdn-cgi/l/email-protection#([0-9a-fA-F]+)', page)]
    found += re.findall(r'mailto:([^"\'?>\s]+)', text, flags=re.IGNORECASE)
    found += PAGE_EMAIL_RE.findall(text)
    out = []
    for raw in found:
        email = contacts.normalize_email(unquote(raw or ''))
        if not email or email in out or email.endswith(NOT_AN_ADDRESS):
            continue
        domain = email.split('@', 1)[1]
        if any(p in domain for p in PLACEHOLDER_DOMAINS) or contacts.is_role_address(email):
            continue
        out.append(email)
    return out


def _contact_pages(base_url, page, limit=2):
    host = (urlparse(base_url).hostname or '').removeprefix('www.')
    links = []
    for href in HREF_RE.findall(page):
        url = urljoin(base_url, html_lib.unescape(href.strip())).split('#')[0]
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or (parsed.hostname or '').removeprefix('www.') != host:
            continue
        path = parsed.path.lower()
        if any(w in path for w in CONTACT_WORDS) and not path.endswith(NOT_AN_ADDRESS) and url not in links:
            links.append(url)
    links.sort(key=lambda u: 0 if 'contact' in urlparse(u).path.lower() else 1)
    return links[:limit]


def _own(email, domain):
    email_domain = email.split('@', 1)[1]
    return email_domain == domain or email_domain.endswith('.' + domain) or domain.endswith('.' + email_domain)


def site_emails(db, address):
    """Addresses a company publishes on its own site: the home page and up to two contact or
    about pages. Cached for 30 days.
    Returns {'domain', 'emails': [{'email', 'page', 'own_domain'}], 'pages'}."""
    domain = domain_from(address)
    if not domain:
        raise ToolError('No website to read.')
    key = f'emails:{domain}'
    cached = db.one('SELECT * FROM site_cache WHERE domain = ?', key)
    if cached is not None and now() - cached['fetched_at'] < SITE_CACHE_DAYS * 86400:
        data = loads(cached['data'], default=None)
        if data is None:
            raise ToolError(cached['error'] or 'The site could not be read earlier.')
        return data
    page, error = None, ToolError(f'Could not reach {domain}.')
    for start in (f'https://{domain}/', f'https://www.{domain}/', f'http://{domain}/'):
        try:
            url, page = _safe_get(start)
            break
        except ToolError as exc:
            error = exc
    if page is None:
        db.run('INSERT INTO site_cache (domain, data, error, fetched_at) VALUES (?, NULL, ?, ?) '
               'ON CONFLICT(domain) DO UPDATE SET data = NULL, error = excluded.error, fetched_at = excluded.fetched_at',
               key, str(error), now() - (SITE_CACHE_DAYS - 1) * 86400)   # try again after a day
        raise error
    pages, found = [url], [(e, url) for e in emails_in_page(page)]
    for link in _contact_pages(url, page):
        try:
            link_url, link_page = _safe_get(link)
        except ToolError:
            continue
        pages.append(link_url)
        found += [(e, link_url) for e in emails_in_page(link_page)]
    emails, seen = [], set()
    for email, where in found:
        if email not in seen:
            seen.add(email)
            emails.append({'email': email, 'page': where, 'own_domain': _own(email, domain)})
    emails.sort(key=lambda e: (not e['own_domain'], e['email'].split('@', 1)[1] not in FREE_PROVIDERS))
    data = {'domain': domain, 'emails': emails[:10], 'pages': pages}
    db.run("INSERT INTO site_cache (domain, data, error, fetched_at) VALUES (?, ?, '', ?) "
           "ON CONFLICT(domain) DO UPDATE SET data = excluded.data, error = '', fetched_at = excluded.fetched_at",
           key, json.dumps(data), now())
    return data
