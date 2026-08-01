from __future__ import annotations

from contextlib import contextmanager
import ipaddress
import os
import socket
import threading
from collections.abc import Callable, Iterator
from urllib.parse import urlparse

import requests


LogFn = Callable[[str], None]
_ENV_LOCK = threading.RLock()
_MISSING = object()


def _proxy_endpoint(proxy_url: str) -> tuple[str, int] | None:
    value = str(proxy_url or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if not host:
        return None
    try:
        address = ipaddress.ip_address(host)
        is_loopback = address.is_loopback
    except ValueError:
        is_loopback = host.lower() == "localhost"
    if not is_loopback:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 1080 if parsed.scheme.lower().startswith("socks") else 443 if parsed.scheme.lower() == "https" else 80
    return host, port


def _proxy_endpoint_reachable(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_unreachable_loopback_proxy(
    probe_url: str = "https://www.douyin.com/",
) -> tuple[str, int] | None:
    proxies = requests.utils.get_environ_proxies(probe_url)
    proxy_url = str(proxies.get("https") or proxies.get("all") or proxies.get("http") or "")
    endpoint = _proxy_endpoint(proxy_url)
    if endpoint is None:
        return None
    host, port = endpoint
    if _proxy_endpoint_reachable(host, port):
        return None
    return endpoint


@contextmanager
def bypass_unreachable_loopback_proxy(
    log: LogFn | None = None,
    *,
    probe_url: str = "https://www.douyin.com/",
) -> Iterator[tuple[str, int] | None]:
    """Temporarily bypass a dead localhost proxy without changing Windows settings."""
    logger = log or (lambda _message: None)
    with _ENV_LOCK:
        endpoint = find_unreachable_loopback_proxy(probe_url)
        if endpoint is None:
            yield None
            return

        host, port = endpoint
        previous = {
            key: os.environ.get(key, _MISSING)
            for key in ("NO_PROXY", "no_proxy")
        }
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        logger(
            f"检测到系统/进程代理 {host}:{port} 未启动，"
            "本次任务已自动切换为直连；不会修改 Windows 代理设置。"
        )
        try:
            yield endpoint
        finally:
            for key, value in previous.items():
                if value is _MISSING:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = str(value)
