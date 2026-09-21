from __future__ import annotations

from contextlib import contextmanager
import ipaddress
import os
import socket
import ssl
import threading
from collections.abc import Callable, Iterator
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager


LogFn = Callable[[str], None]
_ENV_LOCK = threading.RLock()
_SSL_CONTEXT_LOCK = threading.RLock()
_MISSING = object()
_SSL_CONTEXT: ssl.SSLContext | None = None


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


def build_trusted_ssl_context() -> ssl.SSLContext:
    """Build an SSL context that trusts both certifi and the Windows root store.

    Some local tools (Steam++/Watt Toolkit, corporate TLS inspection, antivirus
    HTTPS scanning) intercept outbound TLS and re-sign certificates with their own
    root. Those roots are installed into the Windows certificate store, which
    browsers honour, but ``requests`` verifies against the ``certifi`` bundle only.
    The mismatch makes every Python HTTPS request fail with
    ``unable to get local issuer certificate`` even though the browser works fine.

    Loading the OS trust store on top of certifi keeps public-CA verification
    intact while accepting roots the user has explicitly installed.
    """
    context = ssl.create_default_context()
    try:
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except Exception:  # pragma: no cover - certifi is a hard dependency in practice
        pass
    try:
        context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    except Exception:  # pragma: no cover - platform dependent
        pass
    return context


def trusted_ssl_context() -> ssl.SSLContext:
    """Return the process-wide context, building it once on first use."""
    global _SSL_CONTEXT
    with _SSL_CONTEXT_LOCK:
        if _SSL_CONTEXT is None:
            _SSL_CONTEXT = build_trusted_ssl_context()
        return _SSL_CONTEXT


class TrustedHTTPSAdapter(HTTPAdapter):
    """HTTPAdapter that verifies certificates against certifi plus the OS store."""

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs: object,
    ) -> None:
        self._pool_ssl_context = trusted_ssl_context()
        pool_kwargs.pop("ssl_context", None)
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            ssl_context=self._pool_ssl_context,
            **pool_kwargs,
        )


def mount_trusted_https(session: requests.Session) -> requests.Session:
    """Make a session honour the OS trust store in addition to certifi."""
    session.mount("https://", TrustedHTTPSAdapter())
    session.mount("http://", HTTPAdapter())
    return session


def create_trusted_session() -> requests.Session:
    """Create a requests session that verifies against certifi and the OS store."""
    return mount_trusted_https(requests.Session())
