"""Shared test setup: no test looks up real DNS. Every domain receives mail,
except names containing "nomail", which do not exist."""
import dns.resolver
import pytest

from app import leadtools


class _Exchange:
    def __init__(self, host):
        self.host = host

    def to_text(self):
        return self.host + '.'


class _MX:
    preference = 10

    def __init__(self, domain):
        self.exchange = _Exchange('mx.' + domain)


class FakeResolver:
    def resolve(self, domain, kind):
        if 'nomail' in domain:
            raise dns.resolver.NXDOMAIN()
        return [_MX(domain)] if kind == 'MX' else ['192.0.2.1']


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch):
    monkeypatch.setattr(leadtools, '_resolver', FakeResolver)
