"""Fixed-origin official Notion OAuth; nothing in this module logs credentials."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser
import uuid

import httpx

ORIGIN = "https://mcp.notion.com"
MCP_URL = ORIGIN + "/mcp"
TOKEN_FILE = "notion-connection.json"


class ConnectionError(RuntimeError):
    """A safe error suitable for the user interface."""


class LoginRequired(ConnectionError):
    pass


def _private_dir(path: Path) -> None:
    path = Path(path).absolute()
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ConnectionError("连接资料目录不能使用快捷链接。")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        raise ConnectionError("连接资料目录不属于当前用户。")
    os.chmod(path, 0o700)


def _read_connection(path: Path) -> dict | None:
    _private_dir(path.parent)
    try:
        # Inspect the opened inode before reading. O_NONBLOCK prevents a FIFO from
        # hanging here while the CLI holds its cross-process queue lock.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ConnectionError("无法安全读取 Notion 连接资料。") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 65536:
            raise ConnectionError("Notion 连接资料权限异常，请重新连接。")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            data = json.load(stream)
        if not isinstance(data, dict) or data.get("issuer") != ORIGIN:
            raise ConnectionError("Notion 连接资料无效，请重新连接。")
        return data
    except (ValueError, UnicodeError):
        raise ConnectionError("Notion 连接资料无效，请重新连接。") from None
    finally:
        if fd >= 0:
            os.close(fd)


def _write_connection(path: Path, data: dict) -> None:
    _private_dir(path.parent)
    if path.is_symlink():
        raise ConnectionError("连接资料文件不能使用快捷链接。")
    temp = path.parent / (".connection-" + secrets.token_hex(12))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def connection_identity(state_dir: Path) -> dict:
    # Notion documents user_id/workspace_id on successful authorization-code
    # exchange and their absence on refresh. This is a Notion-specific guarantee,
    # not a generic OAuth requirement. Keep the initial IDs across refreshes.
    # https://developers.notion.com/guides/mcp/build-mcp-client#step-6-exchange-authorization-code-for-tokens
    data = _read_connection(Path(state_dir) / TOKEN_FILE) or {}
    identity = {}
    for key in ("user_id", "workspace_id"):
        try:
            identity[key] = str(uuid.UUID(data[key]))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise LoginRequired("Notion 尚未返回可核实的账号信息，请重新连接。") from None
    return identity


def has_connection(state_dir: Path) -> bool:
    data = _read_connection(Path(state_dir) / TOKEN_FILE)
    return bool(data and data.get("access_token"))


def disconnect(state_dir: Path) -> None:
    """Forget local credentials only. Official revocation remains in Notion settings."""
    path = Path(state_dir) / TOKEN_FILE
    _private_dir(path.parent)
    if path.is_symlink():
        raise ConnectionError("连接资料文件不能使用快捷链接。")
    path.unlink(missing_ok=True)


async def _request_json(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    if path not in {"/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server", "/register", "/token"}:
        raise ConnectionError("已阻止非官方连接请求。")
    try:
        async with client.stream(method, ORIGIN + path, **kwargs) as response:
            if response.status_code not in {200, 201}:
                if response.status_code in {400, 401, 403} and path == "/token":
                    raise LoginRequired("Notion 连接已失效，请重新点击连接。")
                raise ConnectionError("Notion 暂时未完成连接，请稍后重试。")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 65536:
                    raise ConnectionError("Notion 返回的连接资料过大，已停止。")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError
            return value
    except (httpx.HTTPError, ValueError, UnicodeError):
        raise ConnectionError("暂时无法连接 Notion，请检查网络后重试。") from None


def _validate_metadata(resource: dict, metadata: dict) -> None:
    expected = {"issuer": ORIGIN, "authorization_endpoint": ORIGIN + "/authorize", "token_endpoint": ORIGIN + "/token", "registration_endpoint": ORIGIN + "/register"}
    if resource.get("resource", "").rstrip("/") != ORIGIN or resource.get("authorization_servers") != [ORIGIN]:
        raise ConnectionError("Notion 官方连接地址发生变化，请先更新此 Skill。")
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise ConnectionError("Notion 官方连接地址发生变化，请先更新此 Skill。")
    if "S256" not in metadata.get("code_challenge_methods_supported", []) or "none" not in metadata.get("token_endpoint_auth_methods_supported", []):
        raise ConnectionError("Notion 的安全连接方式发生变化，请先更新此 Skill。")


class CallbackServer:
    """One-use callback, bound before its URI is registered; no remote listener."""
    def __init__(self, *, lifetime: float = 300):
        self.state = secrets.token_urlsafe(32)
        self.deadline = time.monotonic() + lifetime
        self.server = None
        self.future = None
        self.port = None
        self.used = False
        self.path = "/callback/" + secrets.token_urlsafe(24)

    async def __aenter__(self):
        self.future = asyncio.get_running_loop().create_future()
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=16384)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()
        if not self.future.done():
            self.future.cancel()

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.path}"

    def _accept(self, target: str, headers: dict[str, str]) -> str:
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.path != self.path or parsed.fragment:
            raise ValueError
        if headers.get("host") != f"127.0.0.1:{self.port}":
            raise ValueError
        if headers.get("origin") not in {None, ORIGIN, f"http://127.0.0.1:{self.port}"}:
            raise ValueError
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)
        if any(len(v) != 1 for v in query.values()) or set(query) - {"code", "state", "error", "error_description", "iss"}:
            raise ValueError
        if self.used or time.monotonic() > self.deadline:
            raise ValueError
        if not secrets.compare_digest(query.get("state", [""])[0], self.state):
            raise ValueError
        if "iss" in query and query["iss"] != [ORIGIN]:
            raise ValueError
        if "error" in query:
            self.used = True
            raise LoginRequired("你取消了 Notion 连接，可以稍后重新连接。")
        code = query.get("code", [""])[0]
        if not code or len(code) > 4096 or any(ord(c) < 32 for c in code):
            raise ValueError
        self.used = True
        return code

    async def _handle(self, reader, writer):
        status, body = 400, "连接未完成，请回到原窗口重试。"
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            if len(raw) > 16384:
                raise ValueError
            lines = raw.decode("ascii").split("\r\n")
            method, target, version = lines[0].split(" ")
            if method != "GET" or version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise ValueError
            headers = {}
            for line in lines[1:]:
                if not line:
                    continue
                key, value = line.split(":", 1)
                key = key.lower().strip()
                if key in headers:
                    raise ValueError
                headers[key] = value.strip()
            code = self._accept(target, headers)
            if not self.future.done():
                self.future.set_result(code)
            status, body = 200, "Notion 授权已收到。请回到 Hermes，正在确认连接并准备知识库。你可以关闭此页面。"
        except LoginRequired as exc:
            if not self.future.done():
                self.future.set_exception(exc)
        except (ValueError, UnicodeError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            content = ("<!doctype html><meta charset=utf-8><title>连接 Notion</title><p>" + body + "</p>").encode()
            writer.write(f"HTTP/1.1 {status} {'OK' if status == 200 else 'Bad Request'}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(content)}\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nContent-Security-Policy: default-src 'none'; frame-ancestors 'none'\r\nConnection: close\r\n\r\n".encode() + content)
            try:
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
            writer.close()
            await writer.wait_closed()

    async def wait(self) -> str:
        try:
            return await asyncio.wait_for(asyncio.shield(self.future), max(0, self.deadline - time.monotonic()))
        except asyncio.TimeoutError:
            raise LoginRequired("连接页面已过期，请重新点击连接 Notion。") from None


def _tokens(response: dict, old: dict | None = None) -> dict:
    old = old or {}
    access = response.get("access_token")
    if not isinstance(access, str) or not 1 <= len(access) <= 8192 or any(c.isspace() for c in access):
        raise ConnectionError("Notion 未返回有效连接，请重试。")
    if str(response.get("token_type", "")).lower() != "bearer":
        raise ConnectionError("Notion 连接方式不兼容，请更新 Skill。")
    expires = response.get("expires_in", 3600)
    if not isinstance(expires, int) or isinstance(expires, bool) or expires <= 0:
        raise ConnectionError("Notion 连接有效期异常，请重试。")
    refresh = response.get("refresh_token", old.get("refresh_token"))
    if refresh is not None and (not isinstance(refresh, str) or not 1 <= len(refresh) <= 8192):
        raise ConnectionError("Notion 未返回有效连接，请重试。")
    return {**old, "issuer": ORIGIN, "access_token": access, "refresh_token": refresh, "expires_at": time.time() + expires,
            "user_id": response.get("user_id", old.get("user_id")), "workspace_id": response.get("workspace_id", old.get("workspace_id"))}


async def access_token(state_dir: Path, *, interactive: bool = False, open_browser: bool = False, on_authorize=None) -> str:
    """Root CLI serializes access/refresh with its cross-process state lock."""
    path = Path(state_dir) / TOKEN_FILE
    stored = _read_connection(path)
    if stored and isinstance(stored.get("expires_at"), (int, float)) and stored["expires_at"] > time.time() + 90:
        return _tokens({"access_token": stored.get("access_token"), "token_type": "Bearer", "expires_in": 1}, stored)["access_token"]
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        if stored and stored.get("refresh_token") and stored.get("client_id"):
            try:
                response = await _request_json(client, "POST", "/token", data={"grant_type": "refresh_token", "refresh_token": stored["refresh_token"], "client_id": stored["client_id"]})
                renewed = _tokens(response, stored)
                _write_connection(path, renewed)
                return renewed["access_token"]
            except LoginRequired:
                if not interactive:
                    raise
        if not interactive:
            raise LoginRequired("还没有连接 Notion，请先运行连接向导。")
        resource = await _request_json(client, "GET", "/.well-known/oauth-protected-resource")
        metadata = await _request_json(client, "GET", "/.well-known/oauth-authorization-server")
        _validate_metadata(resource, metadata)
        async with CallbackServer() as callback:
            registration = await _request_json(client, "POST", "/register", json={"client_name": "Hermes 视频知识库", "redirect_uris": [callback.redirect_uri], "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none", "scope": "default"})
            client_id = registration.get("client_id")
            if not isinstance(client_id, str) or not 1 <= len(client_id) <= 2048 or registration.get("client_secret"):
                raise ConnectionError("Notion 未提供兼容的本机连接，请更新 Skill。")
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            authorize = ORIGIN + "/authorize?" + urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": callback.redirect_uri, "code_challenge": challenge, "code_challenge_method": "S256", "state": callback.state, "scope": "default", "resource": ORIGIN, "prompt": "consent"})
            if on_authorize:
                result = on_authorize(authorize)
                if hasattr(result, "__await__"):
                    await result
            elif not open_browser:
                print(json.dumps({"status": "awaiting_authorization", "message": "请在运行 Hermes 的这台电脑上打开链接，点击连接 Notion；连接完成后回到这里。", "connect_url": authorize}, ensure_ascii=False), flush=True)
            if open_browser:
                if not await asyncio.to_thread(webbrowser.open, authorize):
                    raise LoginRequired("无法自动打开浏览器，请重新运行向导并选择手动打开链接。")
            code = await callback.wait()
            response = await _request_json(client, "POST", "/token", data={"grant_type": "authorization_code", "client_id": client_id, "redirect_uri": callback.redirect_uri, "code": code, "code_verifier": verifier, "resource": ORIGIN})
            fresh = _tokens(response, {"client_id": client_id})
            for field in ("user_id", "workspace_id"):
                try:
                    fresh[field] = str(uuid.UUID(fresh[field]))
                except (KeyError, ValueError, TypeError, AttributeError):
                    raise LoginRequired("Notion 未返回可核实的账号信息，请更新 Skill 后重试。") from None
            if stored and any(stored.get(k) and fresh.get(k) != stored[k] for k in ("workspace_id", "user_id")):
                raise LoginRequired("这次选择了不同的 Notion 用户或空间。原知识库未改动，请用原账号连接；若要使用另一账号，请在独立的 Hermes 配置中重新安装。")
            _write_connection(path, fresh)
            return fresh["access_token"]
