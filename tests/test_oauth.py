import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit, parse_qs

import httpx
import pytest

from video_kb import oauth


def metadata():
    return {"resource": oauth.ORIGIN, "authorization_servers": [oauth.ORIGIN]}, {
        "issuer": oauth.ORIGIN, "authorization_endpoint": oauth.ORIGIN + "/authorize",
        "token_endpoint": oauth.ORIGIN + "/token", "registration_endpoint": oauth.ORIGIN + "/register",
        "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"]}


def test_metadata_rejects_lookalike_and_weak_pkce():
    resource, meta = metadata()
    oauth._validate_metadata(resource, meta)
    for key in ["issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint"]:
        changed = {**meta, key: "https://mcp.notion.com.attacker.invalid/token"}
        with pytest.raises(oauth.ConnectionError):
            oauth._validate_metadata(resource, changed)
    with pytest.raises(oauth.ConnectionError):
        oauth._validate_metadata(resource, {**meta, "code_challenge_methods_supported": ["plain"]})


def test_private_storage_symlink_and_mode(tmp_path):
    path = tmp_path / "state" / oauth.TOKEN_FILE
    oauth._write_connection(path, {"issuer": oauth.ORIGIN, "access_token": "private"})
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert oauth._read_connection(path)["access_token"] == "private"
    os.chmod(path, 0o644)
    with pytest.raises(oauth.ConnectionError):
        oauth._read_connection(path)
    path.unlink()
    target = tmp_path / "target"
    target.write_text("untouched")
    path.symlink_to(target)
    with pytest.raises(oauth.ConnectionError):
        oauth._write_connection(path, {})
    assert target.read_text() == "untouched"


def test_fifo_connection_is_rejected_without_blocking(tmp_path):
    path = tmp_path / "state" / oauth.TOKEN_FILE
    path.parent.mkdir(mode=0o700)
    os.mkfifo(path, 0o600)
    script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from video_kb.oauth import _read_connection, ConnectionError
try:
    _read_connection(Path(sys.argv[2]))
except ConnectionError:
    raise SystemExit(0)
raise SystemExit(1)
"""
    # A subprocess makes a future regression fail with a timeout, not hang pytest.
    checked = subprocess.run([sys.executable, "-c", script, str(Path(oauth.__file__).parents[1]), str(path)],
                             capture_output=True, timeout=3)
    assert checked.returncode == 0


@pytest.mark.asyncio
async def test_callback_protects_state_host_origin_replay_and_expiry():
    async with oauth.CallbackServer() as cb:
        query = cb.path + "?" + urlencode({"code": "secret-code", "state": cb.state})
        headers = {"host": f"127.0.0.1:{cb.port}"}
        for modified in [{"host": "attacker.invalid"}, {**headers, "origin": "https://attacker.invalid"}]:
            with pytest.raises(ValueError):
                cb._accept(query, modified)
        with pytest.raises(ValueError):
            cb._accept(query + "&state=extra", headers)
        with pytest.raises(ValueError):
            cb._accept(query.replace(cb.state, "wrong"), headers)
        assert cb._accept(query, headers) == "secret-code"
        with pytest.raises(ValueError):
            cb._accept(query, headers)
    async with oauth.CallbackServer(lifetime=-1) as cb:
        with pytest.raises(ValueError):
            cb._accept(cb.path + "?" + urlencode({"code": "c", "state": cb.state}), {"host": f"127.0.0.1:{cb.port}"})


@pytest.mark.asyncio
async def test_loopback_receives_callback_no_secret_in_body():
    async with oauth.CallbackServer() as cb:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(cb.redirect_uri, params={"code": "secret-code", "state": cb.state})
        assert response.status_code == 200
        assert "secret-code" not in response.text and cb.state not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert await cb.wait() == "secret-code"


@pytest.mark.asyncio
async def test_oauth_flow_and_refresh_preserve_rotated_token(tmp_path, monkeypatch, capsys):
    resource, meta = metadata()
    calls, authorize = [], []
    async def request(client, method, path, **kwargs):
        calls.append((path, kwargs))
        if path.endswith("oauth-protected-resource"):
            return resource
        if path.endswith("oauth-authorization-server"):
            return meta
        if path == "/register":
            assert kwargs["json"]["token_endpoint_auth_method"] == "none"
            assert kwargs["json"]["redirect_uris"][0].startswith("http://127.0.0.1:")
            return {"client_id": "public-id"}
        return {"access_token": "secret-access", "refresh_token": "rotated-refresh", "token_type": "bearer", "expires_in": 3600, "workspace_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "user_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}
    async def wait(self):
        return "secret-code"
    monkeypatch.setattr(oauth, "_request_json", request)
    monkeypatch.setattr(oauth.CallbackServer, "wait", wait)
    token = await oauth.access_token(tmp_path / "state", interactive=True, on_authorize=authorize.append)
    assert token == "secret-access"
    params = parse_qs(urlsplit(authorize[0]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert "secret" not in capsys.readouterr().out
    connection = oauth._read_connection(tmp_path / "state" / oauth.TOKEN_FILE)
    connection["expires_at"] = time.time() - 1
    connection["refresh_token"] = "old-refresh"
    oauth._write_connection(tmp_path / "state" / oauth.TOKEN_FILE, connection)
    assert await oauth.access_token(tmp_path / "state") == token
    assert calls[-1][1]["data"]["refresh_token"] == "old-refresh"
    assert oauth._read_connection(tmp_path / "state" / oauth.TOKEN_FILE)["refresh_token"] == "rotated-refresh"


@pytest.mark.asyncio
async def test_no_implicit_browser_when_not_interactive(tmp_path):
    with pytest.raises(oauth.LoginRequired):
        await oauth.access_token(tmp_path / "state")


@pytest.mark.asyncio
async def test_auth_errors_are_redacted_and_redirect_is_not_followed():
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://attacker.invalid/token"}, text="secret-raw-error")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        with pytest.raises(oauth.ConnectionError) as exc:
            await oauth._request_json(client, "POST", "/token", data={"refresh_token": "sensitive"})
    assert "secret" not in str(exc.value)
    assert seen == [oauth.ORIGIN + "/token"]


@pytest.mark.asyncio
async def test_reconnect_different_workspace_does_not_overwrite_existing(tmp_path, monkeypatch):
    path = tmp_path / "state" / oauth.TOKEN_FILE
    old = {"issuer": oauth.ORIGIN, "access_token": "old-secret", "expires_at": 0, "client_id": "old-client", "workspace_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "user_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}
    oauth._write_connection(path, old)
    resource, meta = metadata()
    async def request(client, method, endpoint, **kwargs):
        if endpoint.endswith("oauth-protected-resource"):
            return resource
        if endpoint.endswith("oauth-authorization-server"):
            return meta
        if endpoint == "/register":
            return {"client_id": "new-client"}
        return {"access_token": "new-secret", "token_type": "Bearer", "expires_in": 3600, "workspace_id": "cccccccc-cccc-cccc-cccc-cccccccccccc", "user_id": old["user_id"]}
    async def wait(self):
        return "code"
    monkeypatch.setattr(oauth, "_request_json", request)
    monkeypatch.setattr(oauth.CallbackServer, "wait", wait)
    with pytest.raises(oauth.LoginRequired):
        await oauth.access_token(path.parent, interactive=True, on_authorize=lambda url: None)
    assert oauth._read_connection(path)["access_token"] == "old-secret"
