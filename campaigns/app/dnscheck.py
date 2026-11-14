"""Sender-domain DNS checks: the records inbox providers require before they
trust mail from a domain."""
import dns.exception
import dns.resolver
import dns.reversename


def _resolver():
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 5
    resolver.timeout = 3
    return resolver


def _txt(resolver, name):
    try:
        answers = resolver.resolve(name, 'TXT')
    except dns.exception.DNSException:
        return []
    return [b''.join(r.strings).decode('utf-8', 'replace') for r in answers]


def _records(resolver, name, kind):
    try:
        return [r.to_text().rstrip('.') for r in resolver.resolve(name, kind)]
    except dns.exception.DNSException:
        return []


def check_domain(domain, hostname='', selector='dkim'):
    """Returns a list of checks: {'name', 'status': ok|warn|bad, 'found', 'advice'}."""
    resolver = _resolver()
    domain = domain.strip().lower().rstrip('.')
    checks = []

    def add(name, status, found, advice=''):
        checks.append({'name': name, 'status': status, 'found': found, 'advice': advice})

    # MX
    mx = _records(resolver, domain, 'MX')
    if mx:
        add('MX', 'ok', ', '.join(mx))
    else:
        add('MX', 'bad', 'none', f'Add: {domain}. MX 10 {hostname or "mail." + domain}.  '
            'Without it bounces and replies cannot reach you.')

    # SPF
    spf = [t for t in _txt(resolver, domain) if t.lower().startswith('v=spf1')]
    if len(spf) > 1:
        add('SPF', 'bad', ' | '.join(spf), 'More than one SPF record makes SPF fail. Merge them into one.')
    elif not spf:
        add('SPF', 'bad', 'none', f'Add TXT on {domain}: "v=spf1 mx a:{hostname or "mail." + domain} ~all"')
    elif '+all' in spf[0].lower():
        add('SPF', 'bad', spf[0], '"+all" lets anyone send as you. End the record with ~all or -all.')
    else:
        add('SPF', 'ok', spf[0])

    # DKIM
    dkim_name = f'{selector}._domainkey.{domain}'
    dkim = [t for t in _txt(resolver, dkim_name) if 'p=' in t]
    if dkim and 'p=;' not in dkim[0].replace(' ', ''):
        add('DKIM', 'ok', f'{dkim_name}: {dkim[0][:60]}...')
    else:
        add('DKIM', 'bad', 'none', f'Generate the key in Admin > Mail domains > {domain}, '
            f'then publish the TXT record it shows at {dkim_name}.')

    # DMARC
    dmarc = [t for t in _txt(resolver, f'_dmarc.{domain}') if t.lower().startswith('v=dmarc1')]
    if not dmarc:
        add('DMARC', 'bad', 'none', f'Add TXT on _dmarc.{domain}: "v=DMARC1; p=none; rua=mailto:postmaster@{domain}". '
            'Gmail and Yahoo require DMARC from bulk senders.')
    elif 'p=none' in dmarc[0].replace(' ', '').lower():
        add('DMARC', 'warn', dmarc[0], 'Valid. After a few weeks of clean reports, move to p=quarantine.')
    else:
        add('DMARC', 'ok', dmarc[0])

    # Server identity: forward and reverse DNS must agree.
    if hostname:
        addresses = _records(resolver, hostname, 'A') + _records(resolver, hostname, 'AAAA')
        if not addresses:
            add('Server address', 'bad', 'none', f'{hostname} does not resolve. Add an A record for it.')
        else:
            add('Server address', 'ok', f'{hostname} -> {", ".join(addresses)}')
            reverse_ok, seen = False, []
            for address in addresses:
                try:
                    names = [r.to_text().rstrip('.').lower()
                             for r in resolver.resolve(dns.reversename.from_address(address), 'PTR')]
                except (dns.exception.DNSException, ValueError):
                    names = []
                seen += names
                reverse_ok = reverse_ok or hostname.lower() in names
            if reverse_ok:
                add('Reverse DNS (PTR)', 'ok', ', '.join(seen))
            else:
                add('Reverse DNS (PTR)', 'bad', ', '.join(seen) or 'none',
                    f'Ask your VPS provider to set the PTR of {addresses[0]} to {hostname}. '
                    'Gmail and Outlook reject or junk mail from servers without matching reverse DNS.')
    return checks
