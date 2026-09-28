import json

import pytest

from video_kb import network, public_dns
from video_kb.runtime_settings import read_settings, set_public_dns


def response(address="93.184.216.34", *, host="www.xiaohongshu.com"):
    return {"Status": 0, "TC": False, "Question": [{"name": host, "type": 1}],
            "Answer": [{"name": host, "type": 1, "data": address}]}


class Connection:
    calls = []
    value = response()
    def __init__(self, host, address, timeout):
        self.calls.append((host, address))
    def request(self, method, path, headers):
        self.calls.append((method, path, headers))
    def getresponse(self):
        return self
    status = 200
    def getheader(self, name, default=None):
        return {"Content-Type": "application/dns-json"}.get(name, default)
    def read(self, n):
        return json.dumps(self.value).encode()
    def abort(self):
        pass
    def close(self):
        pass


@pytest.fixture
def resolver(monkeypatch):
    Connection.calls = []
    Connection.value = response()
    monkeypatch.setattr(network, "_PinnedHTTPS", Connection)
    def fake(host, **kw):
        raise network.NonPublicAddress(("198.18.0.38",))
    monkeypatch.setattr(network, "resolve_public", fake)


def test_only_fake_ip_uses_fixed_encrypted_dns(resolver):
    assert public_dns.resolve_public("www.xiaohongshu.com") == ("93.184.216.34",)
    assert Connection.calls[0] == ("1.1.1.1", "1.1.1.1")
    assert Connection.calls[1][1] == "/dns-query?name=www.xiaohongshu.com&type=A"


@pytest.mark.parametrize("ips", [("127.0.0.1",), ("10.0.0.1",), ("198.18.0.2", "10.0.0.1"), ("::1",)])
def test_ordinary_private_addresses_never_use_fallback(resolver, monkeypatch, ips):
    def private(*a, **kw):
        raise network.NonPublicAddress(ips)
    monkeypatch.setattr(network, "resolve_public", private)
    with pytest.raises(network.SourceError):
        public_dns.resolve_public("www.xiaohongshu.com")
    assert Connection.calls == []


@pytest.mark.parametrize("answer", [response("127.0.0.1"), response(host="attacker.invalid"),
                                   {**response(), "TC": True}, {**response(), "Status": 2}])
def test_rejects_private_or_unrelated_or_partial_dns(resolver, answer):
    Connection.value = answer
    with pytest.raises(network.SourceError):
        public_dns.resolve_public("www.xiaohongshu.com")


def test_settings_are_opt_in_and_profile_local(tmp_path):
    assert read_settings(tmp_path / "profile") == {}
    set_public_dns(tmp_path / "profile", True)
    assert read_settings(tmp_path / "profile") == {"public_dns_fallback": True}
    assert read_settings(tmp_path / "other") == {}
    assert (tmp_path / "profile" / "settings.json").stat().st_mode & 0o777 == 0o600


def test_public_system_dns_does_not_send_external_lookup(resolver, monkeypatch):
    monkeypatch.setattr(network, "resolve_public", lambda *a, **kw: ("8.8.8.8",))
    assert public_dns.resolve_public("www.xiaohongshu.com") == ("8.8.8.8",)
    assert Connection.calls == []
