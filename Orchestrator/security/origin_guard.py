"""Origin + Host guard — closes the browser-mediated perimeter bypass.

Audit 2026-10-09 rank 13 (X03-security-backend-01, X05-architecture-03).

The Tailscale + LAN perimeter decides WHO can reach :9091. It cannot stop a
web page that is already open in the operator's browser — any tab, the CU
agent's own Chromium, a page a model fetched — from using that browser as a
relay: browsers open cross-origin WebSockets with no CORS check (/ws/agent
launches Claude Code, /app-proxy/9097 is the terminal, /cu/view/main/ws is the
real desktop), and the old `allow_origins=["*"]` + credentials CORS let any
page drive and READ the HTTP API. DNS rebinding goes one step further: an
attacker domain re-resolves to the box, so its page is "same-origin" with
itself and sends no foreign Origin at all — only the Host header gives it away.

This one pure-ASGI middleware (http AND websocket scopes; BaseHTTPMiddleware
never sees sockets) closes both:

* Host (anti-rebinding): only names an attacker cannot register publicly pass
  — IP literals, single-label names (localhost, the box hostname, MagicDNS
  short names, TestClient's "testserver"), .local / .localhost / .lan /
  .home.arpa / .internal / .home / .localdomain / .corp, any *.ts.net name,
  the box's own FQDN, and the configured extras. Anything else: 400 (http) /
  close 1008 (websocket).
* Origin: absent -> allowed (Android OkHttp, MCP, cron, curl, Twilio: native
  clients never send one). Same-origin with the request's Host -> allowed.
  A short list of cross-origin-but-trusted origins (user apps and zellij on
  the same host, loopback app/Portal origins, the Tauri wizard, configured
  extras) -> allowed WITH CORS headers, no credentials. Everything else,
  including `Origin: null` and every other *.ts.net site (Funnel sites are
  public and attacker-controllable) -> 403 JSON / close 1008 BEFORE accept.
  One carve-out: `Origin: null` may reach /app-proxy/<registered user app>/
  — the sandboxed #appPreviewFrame's own fetches to its own app. It cannot
  be narrowed further: a sandboxed document sends no Referer at all (even
  under Referrer-Policy: unsafe-url) and Sec-Fetch-Site: cross-site for its
  own requests (verified in Chrome 155), exactly like a foreign page's
  sandboxed srcdoc or data: frame. So any page can reach a registered user
  app through the proxy, much as it can on the app's own port — never the
  API, never zellij.
* Cross-site GET/HEAD with no Origin (audit M3 U20): an <img>, <script>,
  <iframe>, link click or window.open from a foreign page sends no Origin,
  so the tier above never sees it — yet a GET can still have side effects.
  Every current browser labels it Sec-Fetch-Site: cross-site; such a request
  is refused (403) unless its path is meant to be embedded or linked from
  anywhere: the static Portal tree and its media (/ui/ — claude.ai and other
  clients render generated media straight from /ui/uploads/), artifact
  downloads (/artifacts/<id>), the Portal and wizard entry pages (/,
  /onboarding/), Google's OAuth redirect (/auth/gmail/callback) and
  registered user apps (/app-proxy/<port>/ — the sandboxed preview's own
  subresource loads are cross-site, see above). same-site, same-origin,
  none (a typed URL) and an absent header (native clients, old browsers)
  are untouched.

No login, sessions or keys: the perimeter is still Tailscale + LAN.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import threading
from typing import Callable, Iterable, Optional, Tuple

from starlette.datastructures import MutableHeaders

logger = logging.getLogger(__name__)

# User-app port range (CLAUDE.md "Creating Web Apps": 8060-8099).
APP_PORT_RANGE = range(8060, 8100)

# The Tauri setup wizard's own origins (installer/src-tauri). Its window loads
# http://localhost:9091/onboarding/ (same-origin), so these only matter for
# Tauri-hosted pages; harmless to allow.
TAURI_ORIGINS = frozenset({"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"})

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Name suffixes nobody can register as a public domain, so a rebinding page
# can never present one as its Host: mDNS (.local), RFC 6761 .localhost,
# RFC 8375 .home.arpa, ICANN-reserved .internal, the router DHCP domains
# .lan / .localdomain (pfSense, UniFi) / .home (Fios) / .corp (.home and
# .corp are ICANN name-collision TLDs, never to be delegated), and Tailscale
# MagicDNS .ts.net (tailnet names are issued by Tailscale, never by an
# attacker's DNS).
LOCAL_NAME_SUFFIXES = (
    ".local", ".localhost", ".lan", ".home.arpa", ".internal",
    ".home", ".localdomain", ".corp", ".ts.net",
)

DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_WS_TO_HTTP = {"ws": "http", "wss": "https"}

_APP_PROXY_PATH = re.compile(r"^/app-proxy/(\d{1,5})(?:/|$)")

CORS_ALLOW_METHODS = "DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT"
CORS_MAX_AGE = "600"

REFUSED_ORIGIN = "origin not allowed"
REFUSED_HOST = "host not allowed"
REFUSED_CROSS_SITE = "cross-site request not allowed"
# The escape hatch, named in the 400 body and the refusal log so an operator
# whose box answers to an unusual local name can fix it from the error alone.
HOST_HINT = ("if this is a name of this BlackBox, add it to [security] "
             "extra_allowed_hosts in config.ini and restart")
ORIGIN_HINT = ("if this page is yours, add its origin to [security] "
               "extra_allowed_origins in config.ini and restart")
NULL_ORIGIN_HINT = ("Origin: null (a sandboxed frame, data: or file: page) is only "
                    "allowed on /app-proxy/<registered app>/")
CROSS_SITE_HINT = ("a page on another site may only load /ui/ assets and media, "
                   "/artifacts/ downloads, the Portal and wizard entry pages and "
                   "registered user apps; use the API from the Portal itself")

# Methods a cross-site page can send WITHOUT an Origin header (subresource
# loads, navigations, <form method=get>); browsers add Origin to the rest.
_NO_ORIGIN_METHODS = frozenset({"GET", "HEAD"})
# Cross-site embeddable paths (see the module docstring): whole trees ...
_CROSS_SITE_PREFIXES = ("/ui/", "/artifacts/")
# ... and single entry pages.
_CROSS_SITE_PAGES = frozenset({
    "/", "/ui", "/onboarding", "/onboarding/", "/onboarding/index.html",
    "/auth/gmail/callback",
})

# classify() verdicts
SAME = "same"        # same-origin: pass, no CORS headers needed
CORS = "cors"        # trusted cross-origin: pass and add CORS headers
PASS = "pass"        # allowed, but the proxied user app owns CORS on this path
REFUSE = "refuse"

_LOG_CAP = 1024      # distinct (what, value, prefix) refusal keys remembered


def split_host(value: str) -> Optional[Tuple[str, Optional[int]]]:
    """Parse a Host header: 'h', 'h:p', '[v6]', '[v6]:p'.

    Returns (lower-cased host without brackets or trailing dot, port or None),
    or None when the value is not a well-formed host[:port].
    """
    v = (value or "").strip().lower()
    if not v:
        return None
    if v.startswith("["):
        end = v.find("]")
        if end < 0:
            return None
        host, rest = v[1:end], v[end + 1:]
        if rest and not rest.startswith(":"):
            return None
        port_s = rest[1:] if rest else None
    else:
        if v.count(":") > 1:          # bare IPv6 is not a valid Host value
            return None
        host, sep, port_s = v.partition(":")
        if not sep:
            port_s = None
    port = None
    if port_s is not None:
        if not port_s.isdigit() or not 0 < int(port_s) < 65536:
            return None
        port = int(port_s)
    host = host.rstrip(".")
    if not host or any(c in host for c in "/\\@?# \t"):
        return None
    return host, port


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return True
    except ValueError:
        return False


def parse_origin(value: str) -> Optional[Tuple[str, str, Optional[int]]]:
    """Parse a serialized origin 'scheme://host[:port]'.

    Returns (scheme, host, effective port or None for schemes without a
    default such as tauri:), or None when it is not an origin ("null", a
    URL with a path, garbage).
    """
    v = (value or "").strip()
    scheme, sep, rest = v.partition("://")
    if not sep or not scheme.isalpha():
        return None
    scheme = scheme.lower()
    if rest.endswith("/"):            # tolerate a trailing slash from non-browser clients
        rest = rest[:-1]
    if not rest or any(c in rest for c in "/?#@"):
        return None
    parsed = split_host(rest)
    if parsed is None:
        return None
    host, port = parsed
    return scheme, host, port if port is not None else DEFAULT_PORTS.get(scheme)


def format_origin(scheme: str, host: str, port: Optional[int]) -> str:
    h = f"[{host}]" if ":" in host else host
    if port is None or port == DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{h}"
    return f"{scheme}://{h}:{port}"


def normalize_origin(value: str) -> Optional[str]:
    parsed = parse_origin(value)
    return format_origin(*parsed) if parsed else None


def _host_only(value: str) -> Optional[str]:
    """Config entries may be 'name', 'name:port' or a URL — keep the host.
    Wildcards are not patterns: '*.example.com' is unusable (None)."""
    v = (value or "").strip()
    if "://" in v:
        v = v.split("://", 1)[1].split("/", 1)[0]
    parsed = split_host(v)
    return parsed[0] if parsed and "*" not in parsed[0] else None


def _origin_only(value: str) -> Optional[str]:
    """Config entries may be an origin or a URL ([pairing] default_origin is
    https://<ts>/ui/) — keep scheme://host[:port]."""
    v = (value or "").strip()
    scheme, sep, rest = v.partition("://")
    if sep:
        for cut in "/?#":
            rest = rest.split(cut, 1)[0]
        v = f"{scheme}://{rest}"
    origin = normalize_origin(v)
    return origin if origin and "*" not in origin else None


def _usable(entries: Iterable[str], parse: Callable[[str], Optional[str]], key: str) -> frozenset:
    """Parse config entries, warning about each one that cannot be used
    instead of dropping it silently."""
    kept = set()
    for entry in entries:
        value = parse(entry)
        if value:
            kept.add(value)
        elif (entry or "").strip():
            logger.warning(
                "[ORIGIN-GUARD] ignoring [security] %s entry %r: not a usable %s "
                "(wildcards are not supported; list each name)",
                key, entry, "host" if key.endswith("hosts") else "origin")
    return frozenset(kept)


class OriginPolicy:
    """Which Hosts and Origins may use the API. Pure: everything box-specific
    is injected (see Orchestrator/security/perimeter.py for the live wiring).

    registered_app_ports is called per decision, so an app registration takes
    effect with no restart; if it raises, the guard fails closed for the
    registry-dependent rules only.
    """

    def __init__(
        self,
        *,
        orchestrator_port: int = 9091,
        zellij_port: int = 9097,
        extra_hosts: Iterable[str] = (),
        extra_origins: Iterable[str] = (),
        registered_app_ports: Callable[[], Iterable[int]] = lambda: (),
        box_names: Optional[Iterable[str]] = None,
    ) -> None:
        self.orchestrator_port = int(orchestrator_port)
        self.zellij_port = int(zellij_port)
        self._registered = registered_app_ports
        self.extra_hosts = _usable(extra_hosts, _host_only, "extra_allowed_hosts")
        self.extra_origins = _usable(extra_origins, _origin_only, "extra_allowed_origins")
        if box_names is None:
            box_names = []
            for fn in (socket.gethostname, socket.getfqdn):
                try:
                    box_names.append(fn())
                except OSError:
                    pass
        self.box_names = frozenset(h for h in (_host_only(x) for x in box_names) if h)

    # -- ports -------------------------------------------------------------

    def registered_ports(self) -> frozenset:
        try:
            return frozenset(int(p) for p in self._registered())
        except Exception as exc:  # noqa: BLE001 — fail closed, never 500 the request
            logger.warning("[ORIGIN-GUARD] registered-app ports unavailable: %s", exc)
            return frozenset()

    def _is_app_port(self, port: int, registered: frozenset) -> bool:
        return port in APP_PORT_RANGE or port == self.zellij_port or port in registered

    def user_app_proxy_port(self, path: str) -> Optional[int]:
        """The port when `path` is /app-proxy/<registered user app>/…, else None.
        zellij (the terminal) is never a "user app"."""
        m = _APP_PROXY_PATH.match(path or "")
        if not m:
            return None
        port = int(m.group(1))
        if port == self.zellij_port or port not in self.registered_ports():
            return None
        return port

    # -- Host --------------------------------------------------------------

    def host_allowed(self, host: str) -> bool:
        if _is_ip_literal(host) or "." not in host:
            return True
        if host.endswith(LOCAL_NAME_SUFFIXES):
            return True
        return host in self.extra_hosts or host in self.box_names

    def request_host_allowed(self, value: str) -> bool:
        parsed = split_host(value)
        return parsed is not None and self.host_allowed(parsed[0])

    # -- cross-site GET ----------------------------------------------------

    def cross_site_get_allowed(self, path: str) -> bool:
        """May a foreign page embed or link this path (no Origin sent)?"""
        path = path or "/"
        # Browsers normalize dot segments before sending; one that arrives
        # anyway is never on the allow-list, whatever its prefix.
        if any(seg in (".", "..") for seg in path.split("/")):
            return False
        if path in _CROSS_SITE_PAGES or path.startswith(_CROSS_SITE_PREFIXES):
            return True
        return self.user_app_proxy_port(path) is not None

    # -- Origin ------------------------------------------------------------

    def classify(self, origin: str, *, host: Optional[str], scheme: str = "http",
                 forwarded_proto: Optional[str] = None, path: str = "") -> str:
        if origin.strip().lower() == "null":
            # Opaque origin: sandboxed iframes, data: URLs, file: pages. Only
            # the sandboxed app preview talking to its own proxied app (see
            # the module docstring for why nothing narrower is possible).
            return PASS if self.user_app_proxy_port(path) is not None else REFUSE
        parsed = parse_origin(origin)
        if parsed is None:
            return REFUSE
        req = split_host(host) if host else None
        if req is not None and _same_origin(parsed, req, scheme, forwarded_proto):
            return SAME
        if not self._trusted_cross_origin(parsed, req):
            return REFUSE
        # The proxied user app owns CORS on its own paths (its server sends
        # Access-Control-Allow-Origin) — never stack a second ACAO on top.
        return PASS if self.user_app_proxy_port(path) is not None else CORS

    def _trusted_cross_origin(self, origin, req) -> bool:
        o_scheme, o_host, o_port = origin
        normalized = format_origin(o_scheme, o_host, o_port)
        if normalized in TAURI_ORIGINS or normalized in self.extra_origins:
            return True
        if o_scheme not in ("http", "https") or o_port is None:
            return False
        registered = self.registered_ports()
        if req is not None and o_host == req[0] and self._is_app_port(o_port, registered):
            return True          # a user app / zellij on this same host
        if o_host in LOOPBACK_HOSTS:
            return o_port == self.orchestrator_port or self._is_app_port(o_port, registered)
        return False


def _same_origin(origin, req, scope_scheme: str, forwarded_proto: Optional[str]) -> bool:
    """Is `origin` the very origin the browser used to reach this request?

    Behind `tailscale serve` the Host is preserved but we are reached over
    plain http from 127.0.0.1; the browser's scheme is in X-Forwarded-Proto.
    """
    o_scheme, o_host, o_port = origin
    r_host, r_port = req
    if o_scheme not in ("http", "https") or o_host != r_host:
        return False
    proto = (forwarded_proto or "").split(",", 1)[0].strip().lower()
    if proto not in DEFAULT_PORTS:
        proto = ""
    if r_port is None and not proto:
        # A portless Host means the browser hit a default port, i.e. a reverse
        # proxy fronts us without saying which scheme it terminated. Either
        # default-port origin of this exact host is that proxy's own page.
        return o_port == DEFAULT_PORTS[o_scheme]
    expected = _WS_TO_HTTP.get(proto or scope_scheme, proto or scope_scheme)
    if expected not in ("http", "https"):
        expected = "http"
    if r_port is None:
        r_port = DEFAULT_PORTS[expected]
    return o_scheme == expected and o_port == r_port


class OriginGuardMiddleware:
    """Pure-ASGI Host + Origin guard (see module docstring)."""

    def __init__(self, app, policy: Optional[OriginPolicy] = None) -> None:
        self.app = app
        self.policy = policy if policy is not None else OriginPolicy()
        self._logged: set = set()
        self._log_lock = threading.Lock()

    async def __call__(self, scope, receive, send) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        hosts, origins, forwarded_proto, preflight, request_headers, fetch_site = _read_headers(scope)
        path = scope.get("path") or "/"

        if len(hosts) > 1 or (hosts and not self.policy.request_host_allowed(hosts[0])):
            self._note(kind, "host", hosts[0] if hosts else "", path)
            await _refuse(kind, receive, send, 400, REFUSED_HOST, HOST_HINT)
            return

        if not origins:
            if (kind == "http" and fetch_site == "cross-site"
                    and scope.get("method") in _NO_ORIGIN_METHODS
                    and not self.policy.cross_site_get_allowed(path)):
                self._note(kind, "sec-fetch-site", fetch_site, path)
                await _refuse(kind, receive, send, 403, REFUSED_CROSS_SITE, CROSS_SITE_HINT)
                return
            await self.app(scope, receive, send)
            return

        origin = origins[0]
        if len(origins) > 1:
            verdict = REFUSE
        else:
            verdict = self.policy.classify(
                origin, host=hosts[0] if hosts else None,
                scheme=scope.get("scheme") or "http",
                forwarded_proto=forwarded_proto, path=path)

        if verdict == REFUSE:
            self._note(kind, "origin", origin, path)
            await _refuse(kind, receive, send, 403, REFUSED_ORIGIN)
            return

        if verdict == CORS and kind == "http":
            if scope.get("method") == "OPTIONS" and preflight:
                await _preflight(send, origin, request_headers)
                return
            await self.app(scope, receive, _cors_send(send, origin))
            return

        await self.app(scope, receive, send)

    def _note(self, kind: str, what: str, value: str, path: str) -> None:
        prefix = "/" + path.lstrip("/").split("/", 1)[0]
        key = (what, value[:200], prefix)
        with self._log_lock:
            if key in self._logged:
                return
            if len(self._logged) >= _LOG_CAP:
                self._logged.clear()
            self._logged.add(key)
        logger.warning(
            "[ORIGIN-GUARD] refused %s %s=%r on %r (first of its kind; later "
            "refusals for this %s under %r are not logged; %s)",
            kind, what, value[:200], path[:200], what, prefix[:64],
            HOST_HINT if what == "host"
            else CROSS_SITE_HINT if what == "sec-fetch-site"
            else NULL_ORIGIN_HINT if value.strip().lower() == "null" else ORIGIN_HINT)


def _read_headers(scope):
    hosts, origins = [], []
    forwarded_proto = None
    preflight = False
    request_headers = None
    fetch_site = None
    for raw_name, raw_value in scope.get("headers") or ():
        name = raw_name.lower()
        if name == b"host":
            hosts.append(raw_value.decode("latin-1"))
        elif name == b"origin":
            origins.append(raw_value.decode("latin-1"))
        elif name == b"x-forwarded-proto" and forwarded_proto is None:
            forwarded_proto = raw_value.decode("latin-1")
        elif name == b"access-control-request-method":
            preflight = True
        elif name == b"access-control-request-headers":
            request_headers = raw_value
        elif name == b"sec-fetch-site" and fetch_site is None:
            fetch_site = raw_value.decode("latin-1").strip().lower()
    return hosts, origins, forwarded_proto, preflight, request_headers, fetch_site


async def _refuse(kind: str, receive, send, status: int, reason: str,
                  hint: Optional[str] = None) -> None:
    if kind == "websocket":
        # ASGI order: take the client's connect event, then close BEFORE
        # accept — the server answers the upgrade with 403 and no route runs.
        await receive()
        await send({"type": "websocket.close", "code": 1008, "reason": reason})
        return
    payload = {"detail": reason}
    if hint:
        payload["hint"] = hint
    body = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"vary", b"Origin"),
    ]})
    await send({"type": "http.response.body", "body": body})


async def _preflight(send, origin: str, request_headers: Optional[bytes]) -> None:
    headers = [
        (b"access-control-allow-origin", origin.encode("latin-1")),
        (b"vary", b"Origin"),
        (b"access-control-allow-methods", CORS_ALLOW_METHODS.encode()),
        (b"access-control-max-age", CORS_MAX_AGE.encode()),
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", b"2"),
    ]
    if request_headers:
        headers.append((b"access-control-allow-headers", request_headers))
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": b"OK"})


def _cors_send(send, origin: str):
    async def wrapped(message):
        if message["type"] == "http.response.start":
            message["headers"] = list(message.get("headers") or [])
            headers = MutableHeaders(scope=message)
            headers["Access-Control-Allow-Origin"] = origin
            headers.add_vary_header("Origin")
        await send(message)
    return wrapped
