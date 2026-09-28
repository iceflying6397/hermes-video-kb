"""Bounded public HTTPS transport. Content requests never carry account secrets."""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import queue
import threading
import ssl
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


class SourceError(ValueError):
    """A short, credential-free error safe to show to users."""


class NonPublicAddress(SourceError):
    def __init__(self, addresses):
        self.addresses = tuple(addresses)
        super().__init__("这个链接指向本机或非公开网络，已停止访问。")


_HOSTS = {
    "douyin.com": "douyin", "www.douyin.com": "douyin", "v.douyin.com": "douyin",
    "www.iesdouyin.com": "douyin", "iesdouyin.com": "douyin",
    "xiaohongshu.com": "xiaohongshu", "www.xiaohongshu.com": "xiaohongshu",
    "xhslink.com": "xiaohongshu", "www.xhslink.com": "xiaohongshu",
    "xhslink.cn": "xiaohongshu", "www.xhslink.cn": "xiaohongshu",
    "mp.weixin.qq.com": "wechat",
}
_MAX_URL = 2048
MAX_BODY = 2 * 1024 * 1024
TOTAL_TIMEOUT = 20.0
MAX_REDIRECTS = 4
_DNS_SLOTS = threading.BoundedSemaphore(2)


def validate_source_url(url: str) -> str:
    """Return platform for an exact supported HTTPS URL, or reject before DNS."""
    if not isinstance(url, str) or not url or len(url) > _MAX_URL or re.search(r"[\x00-\x20\x7f\\]", url):
        raise SourceError("请发送一个完整的公开视频或文章链接。")
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        raise SourceError("链接格式不正确。") from None
    host = p.hostname or ""
    harmless_fragment = host == "mp.weixin.qq.com" and p.fragment == "rd"
    if p.scheme != "https" or p.username is not None or p.password is not None or (p.fragment and not harmless_fragment) or port not in (None, 443) or host not in _HOSTS:
        raise SourceError("只支持抖音、小红书和微信公众号的官方 HTTPS 链接。")
    if p.netloc.lower() not in {host, host + ":443"}:
        raise SourceError("链接地址不受支持。")
    path = p.path
    if host == "mp.weixin.qq.com":
        allowed = bool(re.fullmatch(r"/s(?:/[A-Za-z0-9_-]{5,160})?/?", path))
        if path.rstrip("/") == "/s":
            q = dict(parse_qsl(p.query, keep_blank_values=True))
            allowed = allowed and all(q.get(k) for k in ("__biz", "mid", "idx", "sn"))
    elif host in {"xhslink.cn", "www.xhslink.cn"}:
        allowed = bool(re.fullmatch(r"/o/[A-Za-z0-9_-]{3,100}/?", path))
    elif host.endswith("xhslink.com"):
        allowed = bool(re.fullmatch(r"/(?:a/)?[A-Za-z0-9_-]{3,100}/?", path))
    elif _HOSTS[host] == "xiaohongshu":
        allowed = bool(re.fullmatch(r"/(?:explore|discovery/item)/[A-Fa-f0-9]{16,40}/?", path))
    elif host == "v.douyin.com":
        allowed = bool(re.fullmatch(r"/[A-Za-z0-9_-]{3,100}/?", path))
    else:
        allowed = bool(re.fullmatch(r"/(?:video|note)/[0-9]{5,30}/?", path) or re.fullmatch(r"/share/(?:video|note)/[0-9]{5,30}/?", path))
    if not allowed:
        raise SourceError("这个链接不是支持的视频、笔记或文章页面。")
    return _HOSTS[host]


def normalize_source_url(url: str) -> str:
    """Stable dedup identity. Fetching always uses original URL (XHS needs xsec_token)."""
    platform = validate_source_url(url)
    p = urlsplit(url)
    host = p.hostname
    path = p.path.rstrip("/")
    if platform == "douyin" and host != "v.douyin.com":
        kind, identifier = path.split("/")[-2:]
        return f"https://www.douyin.com/{kind}/{identifier}"
    if platform == "xiaohongshu" and host not in {"xhslink.com", "www.xhslink.com", "xhslink.cn", "www.xhslink.cn"}:
        return "https://www.xiaohongshu.com/explore/" + path.rsplit("/", 1)[-1].lower()
    if platform == "wechat" and path == "/s":
        # Duplicate identity parameters would make canonicalization ambiguous.
        pairs = parse_qsl(p.query, keep_blank_values=True)
        keys = ("__biz", "mid", "idx", "sn")
        if any(sum(k == wanted for k, _ in pairs) != 1 for wanted in keys):
            raise SourceError("公众号链接包含重复或缺少的文章标识。")
        return urlunsplit(("https", host, path, urlencode([(k, dict(pairs)[k]) for k in keys]), ""))
    return urlunsplit(("https", host, path, "", ""))


def source_identity(url: str) -> str | None:
    """A resolved content ID; short-link tokens are not content identities."""
    canonical = normalize_source_url(url)
    host = urlsplit(canonical).hostname or ""
    if host in {"v.douyin.com", "xhslink.com", "www.xhslink.com", "xhslink.cn", "www.xhslink.cn"}:
        return None
    return canonical


def source_urls(text: str) -> list[str]:
    """Extract URLs only. Share text never becomes an executable instruction."""
    if not isinstance(text, str) or len(text) > 20000:
        raise SourceError("分享文字过长，请发送视频链接。")
    urls = []
    for candidate in re.findall(r'https://[^\s<>\"\u3000]+', text):
        candidate = candidate.rstrip("。，、！!？?；;：:）)]}~～")
        try:
            validate_source_url(candidate)
        except SourceError:
            continue
        if candidate not in urls:
            urls.append(candidate)
    if not urls:
        raise SourceError("没有找到支持的公开视频或文章链接。")
    if len(urls) > 10:
        raise SourceError("一次最多处理 10 个链接，请分批发送。")
    return urls


def _public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        # Reject special IPv6 translation/tunneling ranges as well as private/local space.
        return ip.is_global and not ip.is_multicast and not ip.is_unspecified and not ip.is_reserved and not (isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo))
    except ValueError:
        return False


def resolve_public(host: str, *, timeout: float = 8.0) -> tuple[str, ...]:
    # OS DNS calls are not covered by socket timeouts. A bounded daemon worker
    # prevents a stalled resolver from blocking the collector indefinitely.
    if not _DNS_SLOTS.acquire(blocking=False):
        raise SourceError("域名解析尚未完成，请稍后重试。")
    results = queue.Queue(maxsize=1)
    def lookup():
        try:
            results.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except Exception:
            results.put(None)
        finally:
            _DNS_SLOTS.release()
    threading.Thread(target=lookup, daemon=True, name="video-kb-dns").start()
    try:
        infos = results.get(timeout=max(0.1, min(timeout, 8.0)))
    except queue.Empty:
        raise SourceError("域名解析超时，请稍后重试。") from None
    if not infos:
        raise SourceError("暂时无法连接内容平台，请稍后重试。")
    addresses = tuple(dict.fromkeys(item[4][0] for item in infos))
    if not addresses or any(not _public_ip(address) for address in addresses):
        raise NonPublicAddress(addresses)
    return addresses


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Use validated numeric IP, retaining original TLS SNI/certificate/Host checks."""
    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def abort(self):
        active = getattr(self, "_active_socket", None) or self.sock
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.close()

    def connect(self):
        family = socket.AF_INET6 if ":" in self.address else socket.AF_INET
        raw = socket.socket(family, socket.SOCK_STREAM)
        raw.settimeout(self.timeout)
        try:
            raw.connect((self.address, 443))
            if raw.getpeername()[0] != self.address or not _public_ip(raw.getpeername()[0]):
                raise SourceError("内容平台连接地址发生变化，已停止访问。")
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            self._active_socket = self.sock
        except BaseException:
            raw.close()
            raise


@dataclass(frozen=True)
class Fetched:
    url: str
    body: bytes
    content_type: str


def fetch_source(url: str, *, max_body: int = MAX_BODY, total_timeout: float = TOTAL_TIMEOUT, resolver=None) -> Fetched:
    # Caller may lower budgets for tests, but cannot relax shipped safety ceilings.
    max_body = min(max(1, max_body), MAX_BODY)
    deadline = time.monotonic() + min(max(0.1, total_timeout), TOTAL_TIMEOUT)
    initial_platform = validate_source_url(url)
    identity = source_identity(url)
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        if validate_source_url(current) != initial_platform:
            raise SourceError("内容平台跳转到了其他网站，已停止访问。")
        current_identity = source_identity(current)
        if identity and current_identity != identity:
            raise SourceError("链接跳转到了不同内容，已停止访问。")
        identity = identity or current_identity
        p = urlsplit(current)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceError("内容平台响应超时，请稍后重试。")
        addresses = (resolver or resolve_public)(p.hostname or "", timeout=remaining)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceError("内容平台响应超时，请稍后重试。")
        connection = _PinnedHTTPS(p.hostname or "", addresses[0], min(remaining, 8.0))
        # Header/chunk parsers may perform many reads, each individually within
        # the socket timeout. A wall-clock watchdog also ends slow-drip replies.
        watchdog = threading.Timer(remaining, getattr(connection, "abort", connection.close))
        watchdog.daemon = True
        watchdog.start()
        try:
            path = urlunsplit(("", "", p.path, p.query, ""))
            connection.request("GET", path, headers={"User-Agent": "HermesVideoKB/2.0 (+public-content-reader)", "Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "identity", "Connection": "close"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location or hop == MAX_REDIRECTS:
                    raise SourceError("链接跳转次数过多或跳转地址无效。")
                current = urljoin(current, location)
                continue
            if response.status != 200:
                raise SourceError("平台暂时不允许读取这个内容，请确认链接公开可访问。")
            encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
            if encoding != "identity":
                raise SourceError("平台返回了不支持的压缩格式，已停止下载。")
            content_type = response.getheader("Content-Type") or ""
            if content_type.split(";", 1)[0].strip().lower() not in {"text/html", "application/xhtml+xml"}:
                raise SourceError("链接返回的不是可读取的公开内容页面。")
            length = response.getheader("Content-Length")
            if length and (not length.isdigit() or int(length) > max_body):
                raise SourceError("页面过大，已停止下载。")
            parts, count = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceError("内容平台响应超时，请稍后重试。")
                if connection.sock is not None:
                    connection.sock.settimeout(min(remaining, 8.0))
                # read1 bounds each socket operation; read(n) could mask a slow-drip peer.
                piece = response.read1(min(65536, max_body - count + 1))
                if not piece:
                    break
                count += len(piece)
                if count > max_body:
                    raise SourceError("页面过大，已停止下载。")
                parts.append(piece)
            return Fetched(current, b"".join(parts), content_type)
        except SourceError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            raise SourceError("暂时无法安全读取内容，请稍后重试。") from None
        finally:
            watchdog.cancel()
            connection.close()
    raise SourceError("链接跳转次数过多。")
