"""SSRF-safe GET for URLs that come from a model, a tool argument or a remote
caller (audit X03-security-backend-04, O02-chat-routes-A-06, O20-mcp-server-01).

Tailscale + LAN is the BlackBox perimeter by design, but a fetch the box makes
on someone else's behalf starts INSIDE it: every `tailscale serve` request and
every fetch the box makes for itself arrives at :9091 from 127.0.0.1, and the
MCP server (Funnel-exposed) runs web_fetch on the box. So "the caller is
loopback" proves nothing, and an unguarded fetch is a proxy into the box's own
API, the LAN inference hosts, other tailnet peers and cloud metadata.

safe_get() is the one guarded path:

  * http/https only, no userinfo, no odd IPv4 literals (127.1, 0x7f000001,
    2130706433, 0177.0.0.1 ...), no localhost names;
  * the host is resolved ONCE and EVERY address must be public unicast:
    loopback, RFC1918, link-local (169.254.169.254), CGNAT/tailnet
    100.64.0.0/10, multicast, reserved, unspecified, IPv6 ULA/link-local/
    site-local and IPv4-mapped/-compatible/NAT64/6to4 wrappers of a
    non-public IPv4 are refused, as is any address on this machine's own
    interfaces;
  * the connection is PINNED to the vetted address (the original Host header,
    TLS SNI and certificate check still use the name), so a DNS-rebinding name
    cannot swap the address between the check and the connect;
  * redirects are never followed by the transport: each Location is re-vetted
    from scratch, at most `max_redirects` (3) hops. Cookies a hop sets go to
    later hops on the same host (as requests did), and a raw UTF-8 Location is
    repaired the way requests repairs it;
  * the body is capped at `max_bytes` (stacked Content-Encodings, a
    decompression bomb, are refused) and the whole fetch at `timeout` seconds.
    The time cap is enforced at the socket by a watchdog, not between reads:
    urllib3's read timeout is per recv, so a server trickling one byte at a
    time would otherwise hold the fetch open for hours.

Deliberately stdlib + urllib3 + requests only (both the Orchestrator venv and
the lean MCP venv have them). There is no LAN allow-list: a fetch of a LAN
host has to be made by something other than a model-driven tool.
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urljoin, urlsplit

import requests
import requests.cookies
import urllib3
import urllib3.connection
from urllib3.exceptions import (
    ConnectTimeoutError,
    HTTPError as _Urllib3HTTPError,
    NewConnectionError,
    TimeoutError as _Urllib3TimeoutError,
)

DEFAULT_TIMEOUT = 25.0          # seconds, total (connect + every hop + body)
DEFAULT_MAX_BYTES = 1_000_000   # decoded body cap
DEFAULT_MAX_REDIRECTS = 3
CONNECT_TIMEOUT = 10.0          # per address, within the total budget
_CHUNK = 8192
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_LOCAL_ADDR_TTL = 30.0          # interface addresses change (DHCP, tailscale up)

_CGNAT = ipaddress.ip_network("100.64.0.0/10")     # RFC 6598; the tailnet lives here
_NAT64 = ipaddress.ip_network("64:ff9b::/96")      # RFC 6052 well-known prefix
_HOSTNAME_RE = re.compile(r"[a-z0-9_-]+(\.[a-z0-9_-]+)*")

# Test seam: the clock the total deadline is measured on.
_monotonic = time.monotonic


class FetchError(Exception):
    """The URL could not be fetched (network, TLS, DNS or policy)."""


class UnsafeURLError(FetchError):
    """Refused by policy before any connection was opened."""


class TooManyRedirects(FetchError):
    """More redirects than the caller allowed."""


class ResponseTooLarge(FetchError):
    def __init__(self, limit: int, size: Optional[int] = None):
        self.limit = limit
        self.size = size
        super().__init__(
            f"response exceeds {limit} bytes" + (f" (declared {size})" if size else ""))


class FetchTimeout(FetchError):
    """The total time budget ran out."""


class _ConnectFailed(Exception):
    """Connecting to ONE vetted address failed; the next one may still work."""

    def __init__(self, message: str, timed_out: bool = False):
        super().__init__(message)
        self.timed_out = timed_out


@dataclass(frozen=True)
class VettedURL:
    url: str                    # normalized (IDNA host, requoted path)
    scheme: str
    host: str                   # name or IP literal, no brackets, no trailing dot
    port: int
    target: str                 # request-line target: path + query
    addresses: tuple            # vetted public IPs, resolver order

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        return host if self.port == default else f"{host}:{self.port}"


@dataclass
class FetchResult:
    url: str                    # final URL after redirects
    status_code: int
    headers: Mapping[str, str]
    content: bytes


# ------------------------------------------------------------- address policy

def is_public_address(ip) -> bool:
    """True only for globally routable unicast addresses."""
    if ip.version == 6:
        if ip.ipv4_mapped is not None or int(ip) >> 32 == 0:
            return False            # ::ffff:a.b.c.d, ::a.b.c.d, ::, ::1
        if ip in _NAT64:            # judge the IPv4 it wraps
            return is_public_address(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip.sixtofour is not None or ip.teredo is not None or ip.is_site_local:
            return False
    elif ip in _CGNAT:
        return False
    return bool(ip.is_global and not (
        ip.is_multicast or ip.is_private or ip.is_reserved or ip.is_loopback
        or ip.is_link_local or ip.is_unspecified))


def _describe(ip) -> str:
    if ip.is_loopback:
        return "a loopback"
    if ip.is_unspecified:
        return "an unspecified"
    if ip.is_link_local:
        return "a link-local"
    if ip.version == 4 and ip in _CGNAT:
        return "a CGNAT/tailnet (100.64.0.0/10)"
    if ip.is_multicast:
        return "a multicast"
    if ip.version == 6 and (ip.ipv4_mapped is not None or int(ip) >> 32 == 0
                            or ip in _NAT64 or ip.sixtofour is not None):
        return "an IPv4-embedding IPv6"
    if ip.is_private:
        return "a private-network"
    return "a reserved or non-public"


def _local_addresses_uncached() -> frozenset:
    addrs: set = set()
    try:
        import psutil
        for snics in psutil.net_if_addrs().values():
            for snic in snics:
                if snic.family in (socket.AF_INET, socket.AF_INET6):
                    try:
                        addrs.add(ipaddress.ip_address(snic.address.split("%")[0]))
                    except ValueError:
                        pass
        return frozenset(addrs)
    except Exception:
        pass
    # Linux without psutil (the lean MCP venv): read the kernel tables.
    try:
        with open("/proc/net/if_inet6") as f:
            for line in f:
                h = line.split()[0]
                addrs.add(ipaddress.IPv6Address(int(h, 16)))
    except (OSError, ValueError, IndexError):
        pass
    try:
        last = None
        with open("/proc/net/fib_trie") as f:
            for line in f:
                s = line.strip()
                if s.startswith("|--"):
                    last = s[3:].strip()
                elif "/32 host LOCAL" in s and last:
                    addrs.add(ipaddress.IPv4Address(last))
    except (OSError, ValueError):
        pass
    return frozenset(addrs)


_local_cache: list = []   # [(stamp, frozenset)]


def _local_addresses() -> frozenset:
    """Addresses assigned to this machine (a public one reaches :9091 too)."""
    now = time.monotonic()
    if _local_cache and now - _local_cache[0][0] < _LOCAL_ADDR_TTL:
        return _local_cache[0][1]
    addrs = _local_addresses_uncached()
    _local_cache[:] = [(now, addrs)]
    return addrs


def _require_public(ip, host: str, literal: bool = False) -> None:
    if not is_public_address(ip):
        shown = host if literal else f"{host} resolves to {ip}, which"
        raise UnsafeURLError(f"{shown} is {_describe(ip)} address")
    if ip in _local_addresses():
        raise UnsafeURLError(f"{host} is an address of this machine")


def _ip_literal(host: str):
    """The host as an IP address, None for a name. Non-canonical IPv4 forms
    that the C resolver would still accept (127.1, 0x7f000001, 2130706433,
    0177.0.0.1) are refused instead of being guessed at."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
    except OSError:
        return None
    raise UnsafeURLError(f"{host!r} is a non-canonical IPv4 literal")


def _resolve(host: str, port: int) -> list:
    """All addresses for host, resolver order (test seam)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


# ------------------------------------------------------------------- vetting

def vet_url(url: str) -> VettedURL:
    """Normalize url and resolve + vet its host. Raises UnsafeURLError for a
    policy refusal, FetchError when the name does not resolve."""
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("empty URL")
    url = url.strip()
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise UnsafeURLError(
            f"scheme {scheme or '(none)'!r} is not allowed (only http and https)")
    try:
        prepared = requests.models.PreparedRequest()
        prepared.prepare_url(url, None)     # IDNA host + requoted path, as requests does
        url = prepared.url
    except (requests.exceptions.RequestException, ValueError) as e:
        raise UnsafeURLError(f"invalid URL: {e}") from None
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise UnsafeURLError("URL has no host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise UnsafeURLError("invalid port") from None
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")

    literal = _ip_literal(host)
    if literal is not None:
        _require_public(literal, host, literal=True)
        addresses = (str(literal),)
    else:
        if len(host) > 253 or not _HOSTNAME_RE.fullmatch(host):
            raise UnsafeURLError(f"invalid host name {host!r}")
        if host == "localhost" or host.endswith(".localhost"):
            raise UnsafeURLError(f"{host} is a loopback name")
        try:
            resolved = _resolve(host, port)
        except (OSError, UnicodeError) as e:
            raise FetchError(f"could not resolve {host}: {e}") from None
        vetted = []
        for raw in resolved:
            try:
                ip = ipaddress.ip_address(raw)
            except ValueError:
                raise UnsafeURLError(f"{host} resolves to an unparseable address") from None
            _require_public(ip, host)
            if str(ip) not in vetted:
                vetted.append(str(ip))
        if not vetted:
            raise FetchError(f"could not resolve {host}")
        addresses = tuple(vetted)
    return VettedURL(url=url, scheme=scheme, host=host, port=port,
                     target=target, addresses=addresses)


# ----------------------------------------------------------------- transport

def _ca_bundle() -> Optional[str]:
    try:
        import certifi
        return certifi.where()
    except Exception:
        return None     # urllib3 falls back to the system trust store


def _shutdown(sock) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass        # already closed by the peer


class _Watchdog:
    """Shuts a fetch's sockets down when its total budget runs out.

    The blocked recv (TLS handshake, status line, headers or body) then sees
    EOF and safe_get reports FetchTimeout. It holds a dup() of each socket,
    never the original descriptor, so it can never shut down a descriptor the
    OS has since reused for another connection; release()/stop() close the
    dups (an open dup would keep the TCP connection alive)."""

    def __init__(self, deadline: float):
        self.fired = False
        self._deadline = deadline
        self._lock = threading.Lock()
        self._socks: list = []
        self._stopped = False
        self._timer: Optional[threading.Timer] = None    # started by the first watch()

    def watch(self, sock) -> None:
        try:
            dup = sock.dup()
        except OSError:
            return
        with self._lock:
            if not self._stopped:
                self._socks.append(dup)
                if self._timer is None:
                    self._timer = threading.Timer(
                        max(0.0, self._deadline - _monotonic()), self._fire)
                    self._timer.daemon = True
                    self._timer.start()
                if self.fired:
                    _shutdown(dup)
                return
        dup.close()

    def _fire(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self.fired = True
            for sock in self._socks:
                _shutdown(sock)

    def release(self) -> None:
        """Close the handles of a finished hop."""
        with self._lock:
            socks, self._socks = self._socks, []
        for sock in socks:
            sock.close()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            timer = self._timer
        if timer is not None:
            timer.cancel()
        self.release()


class _WatchedConnection:
    """Hands each freshly connected socket (before any TLS wrap, so the
    handshake is covered too) to the fetch's watchdog."""

    _bb_watchdog: Optional[_Watchdog] = None

    def _new_conn(self):
        sock = super()._new_conn()
        if self._bb_watchdog is not None:
            self._bb_watchdog.watch(sock)
        return sock


class _WatchedHTTPConnection(_WatchedConnection, urllib3.connection.HTTPConnection):
    pass


class _WatchedHTTPSConnection(_WatchedConnection, urllib3.connection.HTTPSConnection):
    pass


class _WatchedPool:
    def __init__(self, *args, watchdog: Optional[_Watchdog] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._bb_watchdog = watchdog

    def _new_conn(self):
        conn = super()._new_conn()
        conn._bb_watchdog = self._bb_watchdog
        return conn


class _PinnedHTTPPool(_WatchedPool, urllib3.HTTPConnectionPool):
    ConnectionCls = _WatchedHTTPConnection


class _PinnedHTTPSPool(_WatchedPool, urllib3.HTTPSConnectionPool):
    ConnectionCls = _WatchedHTTPSConnection


def _open_pinned(vetted: VettedURL, ip: str, headers: Mapping[str, str], timeout: float,
                 watchdog: Optional[_Watchdog] = None):
    """GET vetted.target from `ip`, sending Host/SNI/cert-check for vetted.host.
    Never follows redirects, never retries. Returns an unread urllib3 response."""
    t = urllib3.Timeout(connect=min(CONNECT_TIMEOUT, timeout), read=timeout)
    if vetted.scheme == "https":
        pool = _PinnedHTTPSPool(
            ip, vetted.port, timeout=t, retries=False, maxsize=1,
            cert_reqs="CERT_REQUIRED", ca_certs=_ca_bundle(),
            assert_hostname=vetted.host, server_hostname=vetted.host, watchdog=watchdog)
    else:
        pool = _PinnedHTTPPool(ip, vetted.port, timeout=t, retries=False, maxsize=1,
                               watchdog=watchdog)
    hdrs = {k: v for k, v in headers.items() if k.lower() != "host"}
    hdrs["Host"] = vetted.host_header
    try:
        return pool.urlopen("GET", vetted.target, headers=hdrs, redirect=False,
                            retries=False, preload_content=False, decode_content=True,
                            assert_same_host=False, timeout=t)
    except NewConnectionError as e:      # refused / unreachable (subclass of the next)
        raise _ConnectFailed(str(e)) from e
    except ConnectTimeoutError as e:
        raise _ConnectFailed(str(e), timed_out=True) from e


def _header(resp, name: str) -> Optional[str]:
    headers = resp.headers or {}
    value = headers.get(name)
    if value is None:
        for k, v in headers.items():
            if k.lower() == name.lower():
                return v
    return value


def _close(resp) -> None:
    for meth in ("close", "release_conn"):
        fn = getattr(resp, meth, None)
        if fn:
            try:
                fn()
            except Exception:
                pass
            return


def _expired(watchdog: Optional[_Watchdog]) -> bool:
    return watchdog is not None and watchdog.fired


def _connect(vetted: VettedURL, headers: Mapping[str, str], deadline: float,
             watchdog: Optional[_Watchdog] = None):
    last: Optional[_ConnectFailed] = None
    for ip in vetted.addresses:
        remaining = deadline - _monotonic()
        if remaining <= 0 or _expired(watchdog):
            raise FetchTimeout(f"timed out connecting to {vetted.host}")
        try:
            return _open_pinned(vetted, ip, headers, remaining, watchdog=watchdog)
        except _ConnectFailed as e:
            last = e
        except _Urllib3TimeoutError as e:
            raise FetchTimeout(f"timed out waiting for {vetted.host}") from e
        except (_Urllib3HTTPError, http.client.HTTPException, OSError) as e:
            if _expired(watchdog):      # the watchdog's shutdown looks like a hang-up
                raise FetchTimeout(f"timed out waiting for {vetted.host}") from e
            raise FetchError(f"{type(e).__name__}: {e}") from e
    if last is not None and last.timed_out:
        raise FetchTimeout(f"timed out connecting to {vetted.host}")
    raise FetchError(f"could not connect to {vetted.host}: {last}")


def _read_capped(resp, max_bytes: int, deadline: float,
                 watchdog: Optional[_Watchdog] = None) -> bytes:
    declared = (_header(resp, "Content-Length") or "").strip()
    if declared.isdigit() and int(declared) > max_bytes:
        raise ResponseTooLarge(max_bytes, int(declared))
    # The size cap counts DECODED bytes, after urllib3 has inflated a chunk.
    # Stacked codings multiply, and the urllib3 2.5 in the Orchestrator lock
    # inflates them with no bound (the rank-35 floor upgrades it): a few
    # hundred bytes become hundreds of MB. One coding inflates at most ~1000x
    # per 8 KB read, which the cap then cuts off.
    codings = [c.strip().lower() for c in (_header(resp, "Content-Encoding") or "").split(",")]
    codings = [c for c in codings if c and c != "identity"]
    if len(codings) > 1:
        raise FetchError(f"refusing stacked Content-Encodings ({', '.join(codings)})")
    body = bytearray()
    try:
        for chunk in resp.stream(_CHUNK, decode_content=True):
            body += chunk
            if len(body) > max_bytes:
                raise ResponseTooLarge(max_bytes)
            if _monotonic() > deadline:
                raise FetchTimeout("timed out reading the response body")
    except _Urllib3TimeoutError as e:
        raise FetchTimeout("timed out reading the response body") from e
    except (_Urllib3HTTPError, http.client.HTTPException, OSError) as e:
        if _expired(watchdog):
            raise FetchTimeout("timed out reading the response body") from e
        raise FetchError(f"{type(e).__name__}: {e}") from e
    # A body without Content-Length ends at EOF, and the watchdog's shutdown is
    # an EOF: never hand back a page that the deadline cut short.
    if _expired(watchdog) or _monotonic() > deadline:
        raise FetchTimeout("timed out reading the response body")
    return bytes(body)


def _redirect_target(location: str) -> str:
    """http.client decodes header bytes as latin-1, but a raw non-ASCII
    Location is almost always UTF-8 (requests' get_redirect_target does the
    same repair)."""
    location = location.strip()
    try:
        return location.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return location


def safe_get(url: str, *, headers: Optional[Mapping[str, str]] = None,
             timeout: float = DEFAULT_TIMEOUT, max_bytes: int = DEFAULT_MAX_BYTES,
             max_redirects: int = DEFAULT_MAX_REDIRECTS) -> FetchResult:
    """GET a public http(s) URL with the guard described in the module doc.

    Returns the final response (any status). Raises UnsafeURLError when the
    URL or any redirect hop is refused, TooManyRedirects, ResponseTooLarge,
    FetchTimeout, or FetchError for DNS/connect/TLS/protocol failures."""
    deadline = _monotonic() + timeout
    send = {"Accept-Encoding": "gzip, deflate", "Connection": "close", **(headers or {})}
    # Cookies set by a redirect hop, sent on later hops by the stdlib cookie
    # policy (same host/domain and path only). Scoped to this one call.
    jar = requests.cookies.RequestsCookieJar()
    watchdog = _Watchdog(deadline)
    current = url
    try:
        for hop in range(max_redirects + 1):
            if _monotonic() >= deadline:
                raise FetchTimeout("timed out following redirects")
            vetted = vet_url(current)
            hop_headers = dict(send)
            cookie_view = requests.Request("GET", vetted.url).prepare()
            cookie = requests.cookies.get_cookie_header(jar, cookie_view)
            if cookie:
                hop_headers["Cookie"] = cookie
            resp = _connect(vetted, hop_headers, deadline, watchdog)
            try:
                location = _header(resp, "Location") if resp.status in _REDIRECT_STATUSES else None
                if location:
                    if hop >= max_redirects:
                        raise TooManyRedirects(f"more than {max_redirects} redirects")
                    requests.cookies.extract_cookies_to_jar(jar, cookie_view, resp)
                    current = urljoin(vetted.url, _redirect_target(location))
                    continue
                content = _read_capped(resp, max_bytes, deadline, watchdog)
                return FetchResult(url=vetted.url, status_code=resp.status,
                                   headers=resp.headers or {}, content=content)
            finally:
                _close(resp)
                watchdog.release()
    finally:
        watchdog.stop()
    raise TooManyRedirects(f"more than {max_redirects} redirects")  # unreachable
