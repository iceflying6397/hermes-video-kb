"""Synthetic API compatibility and header boundary tests; never real credentials."""
import json
from types import SimpleNamespace

import pytest

from video_kb import providers
from test_provider_runtime import fake_hermes


RUNTIME = {"api_mode": "chat_completions", "base_url": "https://api.example.test/v1",
           "model": "synthetic-model", "api_key": "SYNTHETIC-KEY"}
MESSAGES = [{"role": "system", "content": "Summarize only."}, {"role": "user", "content": "Untrusted text."}]


def capture_request(monkeypatch):
    requests = []
    class Response:
        status = 200
        def getheader(self, name): return None
        def read(self, n):
            return json.dumps({"content": [{"type": "text", "text": "{}"}],
                               "choices": [{"message": {"content": "{}"}}]}).encode()
    class Connection:
        def __init__(self, host, port, **kwargs):
            assert host in {"api.example.test", "api.minimax.io", "api.anthropic.com"}
            assert kwargs["timeout"] == 55
        def request(self, method, path, body, headers):
            requests.append({"path": path, "body": json.loads(body), "headers": headers})
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(providers.http.client, "HTTPSConnection", Connection)
    return requests


@pytest.mark.parametrize("extra", [None, {}, {"X-Gateway-Token": "synthetic"}, {"Authorization": "Bearer gateway"}, {"X-Api-Key": "gateway"}])
def test_api_extra_headers_reach_only_configured_request(monkeypatch, extra):
    calls = capture_request(monkeypatch)
    assert providers._request({**RUNTIME, "extra_headers": extra}, MESSAGES) == "{}"
    request = calls[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["accept-encoding"] == "identity"
    assert "tools" not in request["body"]
    for key, value in (extra or {}).items():
        assert request["headers"][key.lower()] == value
    assert not {"authorization", "x-api-key"}.issubset(request["headers"])


@pytest.mark.parametrize("extra", [
    [], "headers", {"X": 1}, {"X": "line\r\ninjected"}, {"bad:name": "value"}, {"X": "☃"},
    {"X": "a" * 4097}, {str(i): "a" * 4096 for i in range(5)},
    {str(i): "a" for i in range(33)}, {"X": "a", "x": "b"},
    {"Authorization": "a", "X-Api-Key": "b"},
    *[{name: "invalid"} for name in ("Host", "Content-Length", "Transfer-Encoding", "Content-Type",
                                   "Content-Encoding", "Accept-Encoding", "Connection", "Upgrade",
                                   "Expect", "TE", "Trailer", "Proxy-Authorization", "X-Forwarded-Host")],
])
def test_invalid_extra_headers_rejected_before_connection(monkeypatch, extra):
    monkeypatch.setattr(providers.http.client, "HTTPSConnection", lambda *a, **k: pytest.fail("must not connect"))
    with pytest.raises(providers.ProviderUnavailable):
        providers._request({**RUNTIME, "extra_headers": extra}, MESSAGES)


@pytest.mark.parametrize("extra,available", [({"X-Gateway-Token": "synthetic"}, True), ({"Host": "evil.test"}, False), ({"X": 9}, False)])
def test_custom_header_preflight_matches_request_validation(tmp_path, extra, available):
    root, _ = fake_hermes(tmp_path, settings={
        "model": {"provider": "custom:gateway", "default": "fake"},
        "providers": {"gateway": {"base_url": RUNTIME["base_url"], "api_mode": "chat_completions", "extra_headers": extra}},
    })
    assert providers.check_provider(config={"hermes_root": str(root)})["available"] is available


@pytest.mark.parametrize("style,auth", [("api_key", "x-api-key"), ("bearer", "authorization"), ("oauth", "authorization"), ("kimi", "x-api-key")])
def test_anthropic_native_auth_style_connects_to_wire(monkeypatch, style, auth):
    identity_calls = []
    def identity(system, tools, messages, rename):
        assert tools == [] and messages == [MESSAGES[1]]
        identity_calls.append(True)
        return [{"type": "text", "text": "native identity"}, {"type": "text", "text": system}]
    adapter = SimpleNamespace(_auth_style=lambda *a: style,
        _common_betas_for_base_url=lambda base: ["native-common"],
        _beta_header=lambda betas: {"anthropic-beta": ",".join(betas)},
        _OAUTH_ONLY_BETAS=["native-oauth"], _CLAUDE_CODE_VERSION_FALLBACK="synthetic-version",
        _apply_claude_code_identity=identity)
    monkeypatch.setattr(providers, "_anthropic_adapter", lambda: adapter)
    calls = capture_request(monkeypatch)
    providers._request({**RUNTIME, "api_mode": "anthropic_messages", "base_url": "https://api.minimax.io/anthropic"}, MESSAGES)
    request = calls[0]
    assert request["path"] == "/anthropic/v1/messages"
    assert request["headers"][auth] == ("Bearer " if auth == "authorization" else "") + RUNTIME["api_key"]
    assert not {"authorization", "x-api-key"}.issubset(request["headers"])
    assert "tools" not in request["body"] and request["body"]["max_tokens"] == 3000
    if style == "oauth":
        assert identity_calls == [True]
        assert request["headers"]["x-app"] == "cli"
        assert request["headers"]["user-agent"] == "claude-code/synthetic-version (external, cli)"
        assert request["headers"]["anthropic-beta"] == "native-common,native-oauth"
        assert request["body"]["system"][0]["text"] == "native identity"
    else:
        assert identity_calls == []

