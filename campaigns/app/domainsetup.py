"""Connecting a sending domain: the DNS records it needs, whether each one is
in place, and (optionally) creating the missing ones at Cloudflare.

Cloudflare changes are additive: records that are missing are created; a record
that exists with a different value is reported with the suggested value and
never overwritten, because changing an existing MX or SPF can break mail the
domain already receives elsewhere.
"""
import os
import re
import socket

import dns.exception
import dns.resolver
import requests

CLOUDFLARE_API = 'https://api.cloudflare.com/client/v4'


class SetupError(Exception):
    """Shown to the operator as is."""


# -- what the domain needs ---------------------------------------------------------
def dkim_public_key(cfg, domain):
    """The public DKIM key for the domain, read from the mail server's key file,
    or '' when there is none yet."""
    path = cfg.DKIM_PATH.format(domain=domain, selector=cfg.DKIM_SELECTOR)
    if not os.path.exists(path):
        return ''
    try:
        from cryptography.hazmat.primitives import serialization
        with open(path, 'rb') as handle:
            key = serialization.load_pem_private_key(handle.read(), password=None)
        pem = key.public_key().public_bytes(encoding=serialization.Encoding.PEM,
                                            format=serialization.PublicFormat.SubjectPublicKeyInfo)
    except (OSError, ValueError, ImportError):
        return ''
    return pem.decode().replace('\n', '').split('-----')[2]


def server_ip(cfg):
    if cfg.PUBLIC_IP:
        return cfg.PUBLIC_IP
    if cfg.HOSTNAME:
        try:
            return socket.gethostbyname(cfg.HOSTNAME)
        except OSError:
            return ''
    return ''


def required_records(cfg, domain):
    """[{'key', 'type', 'name', 'value', 'priority', 'purpose', 'where'}]."""
    hostname = cfg.HOSTNAME or f'mail.{domain}'
    ip = server_ip(cfg)
    dkim = dkim_public_key(cfg, domain)
    records = [
        {'key': 'a', 'type': 'A', 'name': hostname, 'value': ip or '<your server IP>', 'priority': None,
         'purpose': 'Points the mail server name at your VPS.'},
        {'key': 'mx', 'type': 'MX', 'name': domain, 'value': hostname, 'priority': 10,
         'purpose': 'Lets replies and bounces reach this server.'},
        {'key': 'spf', 'type': 'TXT', 'name': domain, 'value': f'v=spf1 mx a:{hostname} ~all', 'priority': None,
         'purpose': 'Says this server may send for your domain.'},
        {'key': 'dkim', 'type': 'TXT', 'name': f'{cfg.DKIM_SELECTOR}._domainkey.{domain}',
         'value': f'v=DKIM1; k=rsa; p={dkim}' if dkim else '', 'priority': None,
         'purpose': 'The signature key receivers use to check your emails are genuine.'},
        {'key': 'dmarc', 'type': 'TXT', 'name': f'_dmarc.{domain}',
         'value': f'v=DMARC1; p=none; rua=mailto:postmaster@{domain}', 'priority': None,
         'purpose': 'Required by Gmail and Yahoo for bulk senders. Start with p=none.'},
    ]
    return records


# -- what is published now ------------------------------------------------------------
def _resolver():
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 5
    resolver.timeout = 3
    return resolver


def _lookup(name, kind):
    try:
        answers = _resolver().resolve(name, kind)
    except dns.exception.DNSException:
        return []
    if kind == 'TXT':
        return [b''.join(r.strings).decode('utf-8', 'replace') for r in answers]
    if kind == 'MX':
        return [r.exchange.to_text().rstrip('.').lower() for r in answers]
    return [r.to_text().rstrip('.') for r in answers]


def _norm(value):
    return re.sub(r'\s+', ' ', str(value or '').strip().strip('"')).lower()


def status(cfg, domain):
    """Each required record with what DNS shows now and whether it is right:
    state ok | missing | different | waiting (needs the mail server first)."""
    out = []
    for record in required_records(cfg, domain):
        record = dict(record)
        if record['key'] == 'a':
            found = _lookup(record['name'], 'A')
            record['found'] = found
            ip = server_ip(cfg)
            record['state'] = 'ok' if ip and ip in found else ('missing' if not found else
                                                               'ok' if not cfg.PUBLIC_IP else 'different')
        elif record['key'] == 'mx':
            found = _lookup(domain, 'MX')
            record['found'] = found
            record['state'] = 'ok' if record['value'].lower() in found else ('missing' if not found else 'different')
            if record['state'] == 'different':
                record['warning'] = (f'{domain} already receives mail at {", ".join(found[:2])}. Changing the MX '
                                     'moves your inbox to this server. To keep it, send from a separate domain '
                                     'or subdomain instead.')
        elif record['key'] == 'spf':
            found = [t for t in _lookup(domain, 'TXT') if _norm(t).startswith('v=spf1')]
            record['found'] = found
            hostname = (cfg.HOSTNAME or f'mail.{domain}').lower()
            if not found:
                record['state'] = 'missing'
            elif len(found) > 1:
                record['state'] = 'different'
                record['warning'] = 'There is more than one SPF record; receivers then ignore SPF. Merge them.'
            elif f'a:{hostname}' in _norm(found[0]) or ' mx' in _norm(found[0]) and record['found']:
                record['state'] = 'ok'
            else:
                record['state'] = 'different'
                record['suggested'] = merged_spf(found[0], hostname)
        elif record['key'] == 'dkim':
            found = [t for t in _lookup(record['name'], 'TXT') if 'p=' in t]
            record['found'] = found
            if not record['value']:
                record['state'] = 'waiting'
                record['warning'] = ('No DKIM key yet. Create it in Mail admin → Mail domains → '
                                     f'{domain} → Regenerate keys, then reload this page.')
            elif not found:
                record['state'] = 'missing'
            else:
                published = re.sub(r'\s+', '', found[0])
                record['state'] = 'ok' if record['value'].split('p=')[1] in published else 'different'
        elif record['key'] == 'dmarc':
            found = [t for t in _lookup(record['name'], 'TXT') if _norm(t).startswith('v=dmarc1')]
            record['found'] = found
            record['state'] = 'ok' if found else 'missing'
        out.append(record)
    return out


def merged_spf(existing, hostname):
    """Adds this server to an existing SPF record, keeping everything in it."""
    value = existing.strip().strip('"')
    if f'a:{hostname}' in value:
        return value
    parts = value.split()
    for index, part in enumerate(parts):
        if re.fullmatch(r'[~?+-]?all|redirect=.*', part):
            return ' '.join(parts[:index] + [f'a:{hostname}'] + parts[index:])
    return ' '.join(parts + [f'a:{hostname}', '~all'])


# -- Cloudflare ------------------------------------------------------------------------
def _cf(token, method, path, **kwargs):
    try:
        reply = requests.request(method, f'{CLOUDFLARE_API}{path}', timeout=20,
                                 headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                                 **kwargs)
        data = reply.json()
    except (requests.RequestException, ValueError):
        raise SetupError('Could not reach Cloudflare.')
    if not data.get('success'):
        errors = data.get('errors') or [{}]
        message = errors[0].get('message') or f'HTTP {reply.status_code}'
        if reply.status_code in (401, 403):
            message = ('Cloudflare refused the token. It needs the "Zone → DNS → Edit" permission for this '
                       f'domain. ({message})')
        raise SetupError(f'Cloudflare: {message}')
    return data.get('result')


def cloudflare_zone(token, domain):
    """The Cloudflare zone holding the domain (the domain itself or a parent)."""
    labels = domain.split('.')
    for start in range(len(labels) - 1):
        name = '.'.join(labels[start:])
        zones = _cf(token, 'GET', '/zones', params={'name': name})
        if zones:
            return zones[0]
    raise SetupError(f'{domain} is not in this Cloudflare account.')


def cloudflare_plan(cfg, token, domain):
    """What Cloudflare auto-setup would do: {'zone', 'create': [...], 'skip': [...]}.
    Only records that do not exist at all are created."""
    zone = cloudflare_zone(token, domain)
    zone_name = zone['name']
    existing = _cf(token, 'GET', f'/zones/{zone["id"]}/dns_records', params={'per_page': 5000}) or []
    plan = {'zone': {'id': zone['id'], 'name': zone_name}, 'create': [], 'skip': []}
    for record in required_records(cfg, domain):
        name = record['name'].lower()
        if not (name == zone_name or name.endswith('.' + zone_name)):
            plan['skip'].append(dict(record, reason=f'{record["name"]} is not in the {zone_name} zone'))
            continue
        if not record['value'] or record['value'].startswith('<'):
            plan['skip'].append(dict(record, reason='Value not known yet (see the table above)'))
            continue
        same_name = [r for r in existing if r.get('name', '').lower() == name and r.get('type') == record['type']]
        if record['key'] == 'spf':
            same_name = [r for r in same_name if _norm(r.get('content')).startswith('v=spf1')]
        elif record['key'] in ('dkim', 'dmarc'):
            same_name = [r for r in same_name if record['value'].split(';')[0].lower() in _norm(r.get('content'))]
        if record['key'] == 'a':
            same_name = [r for r in existing if r.get('name', '').lower() == name and r.get('type') in ('A', 'CNAME')]
        if not same_name:
            plan['create'].append(record)
            continue
        current = same_name[0].get('content', '')
        if _norm(current) == _norm(record['value']) or (record['key'] == 'mx' and _norm(current) == name):
            plan['skip'].append(dict(record, reason='Already set'))
        elif record['key'] == 'a' and same_name[0].get('proxied'):
            plan['skip'].append(dict(record, reason='Exists but is proxied (orange cloud). Mail needs it '
                                                    'DNS-only (grey cloud): change it in Cloudflare.'))
        else:
            reason = 'Exists with a different value; change it by hand if you are sure'
            if record['key'] == 'spf':
                reason += f'. Suggested: {merged_spf(current, (cfg.HOSTNAME or "mail." + domain).lower())}'
            plan['skip'].append(dict(record, reason=reason, current=current))
    return plan


def cloudflare_apply(cfg, token, domain):
    """Creates the missing records. Returns the list of created records."""
    plan = cloudflare_plan(cfg, token, domain)
    created = []
    for record in plan['create']:
        body = {'type': record['type'], 'name': record['name'], 'ttl': 1}
        if record['type'] == 'TXT':
            body['content'] = f'"{record["value"]}"'
        else:
            body['content'] = record['value']
        if record['type'] == 'MX':
            body['priority'] = record['priority']
        if record['type'] in ('A', 'AAAA', 'CNAME'):
            body['proxied'] = False   # mail cannot pass through Cloudflare's proxy
        _cf(token, 'POST', f'/zones/{plan["zone"]["id"]}/dns_records', json=body)
        created.append(record)
    return created
