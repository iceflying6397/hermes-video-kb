"""Opt-in fake-IP DNS recovery without permitting private network connections.

Only a wholly fake-IP (198.18.0.0/15) system answer may use the fixed TLS
resolver. Each returned address is still public and pinned by the downloader.
No credentials, source URL paths, queries, page text or media go to the resolver.
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
from urllib.parse import urlencode

from . import network

_FAKE = ipaddress.ip_network("198.18.0.0/15")
_RESOLVER_IP = "1.1.1.1"
_MAX_DNS_BYTES = 32768


def resolve_public(host: str, *, timeout: float = 8.0) -> tuple[str, ...]:
    start = time.monotonic()
    try:
        return network.resolve_public(host, timeout=timeout)
    except network.NonPublicAddress as error:
        addresses = error.addresses
        if not addresses or any(ipaddress.ip_address(x) not in _FAKE for x in addresses):
            raise
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host) or ".." in host:
        raise network.SourceError("域名格式异常，已停止解析。")
    remaining = min(timeout - (time.monotonic() - start), 8.0)
    if remaining <= 0:
        raise network.SourceError("域名解析超时，请稍后重试。")
    # IP SAN verification authenticates the fixed resolver without using broken
    # system DNS. Ordinary HTTPS validation is never disabled.
    connection = network._PinnedHTTPS(_RESOLVER_IP, _RESOLVER_IP, remaining)
    import threading
    watchdog = threading.Timer(remaining, connection.abort)
    watchdog.daemon = True
    watchdog.start()
    try:
        path = "/dns-query?" + urlencode({"name": host, "type": "A"})
        connection.request("GET", path, headers={"Accept": "application/dns-json", "Accept-Encoding": "identity", "Connection": "close"})
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise ValueError()
        if response.getheader("Content-Type", "").split(";", 1)[0].strip() not in {"application/dns-json", "application/json"}:
            raise ValueError()
        body = response.read(_MAX_DNS_BYTES + 1)
        if len(body) > _MAX_DNS_BYTES:
            raise ValueError()
        data = json.loads(body)
        if data.get("Status") != 0 or data.get("TC"):
            raise ValueError()
        questions = data.get("Question", [])
        if len(questions) != 1 or questions[0].get("name", "").rstrip(".").lower() != host or questions[0].get("type") != 1:
            raise ValueError()
        answers = data.get("Answer", [])
        if not isinstance(answers, list) or len(answers) > 32:
            raise ValueError()
        names = {host}
        for _ in range(8):
            expanded = names | {item.get("data", "").rstrip(".").lower() for item in answers
                                if item.get("type") == 5 and item.get("name", "").rstrip(".").lower() in names}
            if expanded == names:
                break
            names = expanded
        resolved = tuple(dict.fromkeys(item["data"] for item in answers if item.get("type") == 1
                                       and item.get("name", "").rstrip(".").lower() in names))
        if not resolved or any(not network._public_ip(x) for x in resolved):
            raise ValueError()
        return resolved
    except Exception:
        raise network.SourceError("当前代理的域名解析异常，公共 DNS 核验也未成功；已停止访问，未关闭内网保护。") from None
    finally:
        watchdog.cancel()
        connection.close()
