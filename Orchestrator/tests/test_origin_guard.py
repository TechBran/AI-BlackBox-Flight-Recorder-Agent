# Orchestrator/tests/test_origin_guard.py
"""Origin + Host guard — the browser-mediated perimeter bypass (audit rank 13:
X03-security-backend-01, X05-architecture-03).

The Tailscale + LAN perimeter decides who can REACH :9091. It cannot stop a
page already open in the operator's browser (any tab, the CU agent's own
Chromium, a model-fetched page) from relaying through that browser:
WebSocket upgrades carry no CORS check, the old wildcard CORS let any page
read API responses, and DNS rebinding makes an attacker domain resolve to the
box. These tests pin the guard's contract on a MINIMAL ASGI app wrapped with
the real middleware (so a refusal provably happens before any route), and the
pure Host/Origin helpers. The real-app enumeration lives in
test_perimeter_real_app.py.

Hermetic: no network, no live services, no config.ini dependence (the policy
is built explicitly here).
"""
import logging

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from Orchestrator.security.origin_guard import (
    REFUSED_HOST,
    REFUSED_ORIGIN,
    OriginGuardMiddleware,
    OriginPolicy,
    split_host,
)

TAILNET = "box.tail1234.ts.net"
LAN = "192.168.1.153"


def _policy(**over):
    kw = dict(
        orchestrator_port=9091,
        zellij_port=9097,
        extra_hosts=["blackbox.example.com"],
        extra_origins=["https://dev.example:5173"],
        registered_app_ports=lambda: {7001, 8065},
        box_names=["box.corp.example"],
    )
    kw.update(over)
    return OriginPolicy(**kw)


class _Recorder:
    """Minimal ASGI app: answers 200 'reached' / accepts the socket, and
    records every scope that got past the guard."""

    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        self.calls.append(scope["path"])
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.send", "text": "reached"})
            await send({"type": "websocket.close", "code": 1000})
            return
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"reached"})


@pytest.fixture
def rec():
    return _Recorder()


@pytest.fixture
def client(rec):
    return TestClient(OriginGuardMiddleware(rec, policy=_policy()))


def _get(client, path="/agent/apps", host=f"{LAN}:9091", origin=None, **extra):
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    headers.update(extra)
    return client.get(path, headers=headers)


# ------------------------------------------------------------ Host tier

@pytest.mark.parametrize("host", [
    "localhost:9091", "127.0.0.1:9091", "[::1]:9091", f"{LAN}:9091",
    "[fd7a:115c:a1e0::1]", "testserver", "blackbox-mini",
    "box.local", "box.localhost", "box.lan", "box.home.arpa", "box.internal",
    TAILNET, "BOX.Tail1234.TS.NET.", f"{TAILNET}:8443",
    "blackbox.example.com",          # [security] extra_allowed_hosts
    "box.corp.example:9091",         # the box's own FQDN
])
def test_local_and_configured_hosts_pass(client, rec, host):
    r = _get(client, host=host)
    assert r.status_code == 200, (host, r.text)
    assert rec.calls == ["/agent/apps"]


@pytest.mark.parametrize("host", [
    "attacker.example", "attacker.example:9091", "evil.com.",
    "ts.net.evil.com", "localhost.evil.com", "box.local.evil.com",
])
def test_rebinding_hosts_refused_400(client, rec, host):
    """DNS rebinding: the attacker's name resolves to the box, so the page is
    same-origin with itself and sends NO Origin — only the Host tells."""
    r = _get(client, host=host)
    assert r.status_code == 400
    assert r.json() == {"detail": REFUSED_HOST}
    assert rec.calls == []


@pytest.mark.parametrize("value,expected", [
    ("LocalHost:9091", ("localhost", 9091)),
    ("[::1]:9091", ("::1", 9091)),
    ("[fe80::1]", ("fe80::1", None)),
    ("name.ts.net.", ("name.ts.net", None)),
    ("10.0.0.2", ("10.0.0.2", None)),
])
def test_split_host_parses(value, expected):
    assert split_host(value) == expected


@pytest.mark.parametrize("value", [
    "", "   ", "host:abc", "host:0", "host:70000", "[::1", "[::1]x",
    "user@host", "a b", "host/x", "::1",
])
def test_split_host_rejects_malformed(value):
    assert split_host(value) is None


def test_malformed_host_refused(client, rec):
    assert _get(client, host="host:abc").status_code == 400
    assert rec.calls == []


# ---------------------------------------------------------- Origin tier

def test_missing_origin_passes_without_cors(client, rec):
    """Non-browser clients (Android OkHttp, MCP, cron, curl, Twilio) never
    send an Origin — they must keep working untouched."""
    r = _get(client)
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.parametrize("host,origin,xfp", [
    (f"{LAN}:9091", f"http://{LAN}:9091", None),             # LAN
    ("localhost:9091", "http://localhost:9091", None),        # on-box browser
    ("[::1]:9091", "http://[::1]:9091", None),
    (TAILNET, f"https://{TAILNET}", "https"),                 # tailscale serve
    (TAILNET, f"https://{TAILNET}", None),                    # proxy w/o XFP
    (f"{TAILNET}:8443", f"https://{TAILNET}:8443", "https"),  # serve on a port
])
def test_same_origin_passes(client, rec, host, origin, xfp):
    extra = {"x-forwarded-proto": xfp} if xfp else {}
    r = _get(client, host=host, origin=origin, **extra)
    assert r.status_code == 200, r.text
    assert rec.calls == ["/agent/apps"]


@pytest.mark.parametrize("host,origin,xfp", [
    ("evil", "https://evil.example", None),
    (f"{LAN}:9091", "https://evil.example", None),
    (f"{LAN}:9091", "null", None),                            # sandboxed frame / data: URL
    (TAILNET, f"https://other.tail9999.ts.net", "https"),     # someone else's Funnel site
    (TAILNET, f"https://{TAILNET}:8443", "https"),            # OUR Funnel port (public)
    (TAILNET, f"http://{TAILNET}", "https"),                  # scheme downgrade
    (f"{LAN}:9091", f"http://{LAN}:9092", None),              # other port, not an app
    (f"{LAN}:9091", "http://10.0.0.9:8065", None),            # app port, OTHER host
    (f"{LAN}:9091", "http://localhost:3000", None),           # loopback non-app port
    (f"{LAN}:9091", f"http://{LAN}:7002", None),              # unregistered port
    (f"{LAN}:9091", "chrome-extension://abcdef", None),
    (f"{LAN}:9091", "file://", None),
    (f"{LAN}:9091", "garbage", None),
    (f"{LAN}:9091", f"http://{LAN}:9091/path", None),         # not an origin
])
def test_foreign_origins_refused_403_without_cors(client, rec, host, origin, xfp):
    extra = {"x-forwarded-proto": xfp} if xfp else {}
    r = _get(client, host=host, origin=origin, **extra)
    assert r.status_code == 403, (origin, r.text)
    assert r.json() == {"detail": REFUSED_ORIGIN}
    assert "access-control-allow-origin" not in r.headers
    assert rec.calls == []


def test_post_from_foreign_origin_never_reaches_route(client, rec):
    """The no-cors text/plain 'simple' POST the old wildcard CORS + content-
    agnostic request.json() let through (X05-architecture-03)."""
    r = client.post("/chat/stream", content=b'{"messages":[]}',
                    headers={"host": f"{LAN}:9091", "origin": "https://evil.example",
                             "content-type": "text/plain"})
    assert r.status_code == 403
    assert rec.calls == []


@pytest.mark.parametrize("host,origin", [
    (f"{LAN}:9091", f"http://{LAN}:8065"),        # user app, same host
    (f"{LAN}:9091", f"http://{LAN}:8099"),
    (f"{LAN}:9091", f"http://{LAN}:9097"),        # zellij-web, same host
    (f"{LAN}:9091", f"http://{LAN}:7001"),        # registered app outside 8060-8099
    (TAILNET, f"https://{TAILNET}:8065"),
    (TAILNET, "http://localhost:8065"),           # loopback app on the box
    ("localhost:9091", "http://127.0.0.1:9091"),  # loopback alias of the Portal
    (f"{LAN}:9091", "http://[::1]:9091"),
    (f"{LAN}:9091", "tauri://localhost"),         # installer wizard
    (f"{LAN}:9091", "http://tauri.localhost"),
    (f"{LAN}:9091", "https://tauri.localhost"),
    (f"{LAN}:9091", "https://dev.example:5173"),  # [security] extra_allowed_origins
])
def test_trusted_cross_origins_get_cors(client, rec, host, origin):
    r = _get(client, host=host, origin=origin)
    assert r.status_code == 200, (origin, r.text)
    assert r.headers["access-control-allow-origin"] == origin
    assert "origin" in r.headers["vary"].lower()
    # No credentials: nothing cross-origin needs cookies on :9091.
    assert "access-control-allow-credentials" not in r.headers


def test_same_origin_gets_no_cors_headers(client):
    r = _get(client, origin=f"http://{LAN}:9091")
    assert "access-control-allow-origin" not in r.headers


def test_preflight_for_trusted_origin(client, rec):
    origin = f"http://{LAN}:8065"
    r = client.options("/chat/stream", headers={
        "host": f"{LAN}:9091", "origin": origin,
        "access-control-request-method": "POST",
        "access-control-request-headers": "content-type, x-requested-with",
    })
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin
    assert "POST" in r.headers["access-control-allow-methods"]
    assert r.headers["access-control-allow-headers"] == "content-type, x-requested-with"
    assert "access-control-allow-credentials" not in r.headers
    assert rec.calls == []          # answered by the guard, route untouched


def test_preflight_for_foreign_origin_refused(client, rec):
    r = client.options("/chat/stream", headers={
        "host": f"{LAN}:9091", "origin": "https://evil.example",
        "access-control-request-method": "POST",
    })
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers
    assert rec.calls == []


def test_duplicate_origin_headers_refused(client, rec):
    r = client.get("/agent/apps", headers=[
        ("host", f"{LAN}:9091"), ("origin", f"http://{LAN}:9091"),
        ("origin", "https://evil.example"),
    ])
    assert r.status_code == 403
    assert rec.calls == []


# ------------------------------------------- sandboxed app-preview carve-out

def test_null_origin_allowed_only_on_registered_user_app_proxy(client, rec):
    """#appPreviewFrame drops allow-same-origin (P07-html-css-13), so a user
    app's own fetches to /app-proxy/<its port>/ arrive with Origin: null.
    Those reach only the proxied app — never the API, never zellij."""
    r = _get(client, path="/app-proxy/8065/api/items", origin="null")
    assert r.status_code == 200
    # The upstream app owns CORS on its own proxied paths: the guard must not
    # stack a second Access-Control-Allow-Origin on top of the app's.
    assert "access-control-allow-origin" not in r.headers
    assert rec.calls == ["/app-proxy/8065/api/items"]


@pytest.mark.parametrize("path", [
    "/app-proxy/9097/",              # zellij — the terminal
    "/app-proxy/8066/",              # not registered
    "/app-proxy/9091/ws/agent/x",    # never the API through the proxy
    "/app-proxyx/8065/",
    "/chat/stream",
])
def test_null_origin_refused_everywhere_else(client, rec, path):
    assert _get(client, path=path, origin="null").status_code == 403
    assert rec.calls == []


def test_foreign_origin_refused_on_user_app_proxy_too(client, rec):
    assert _get(client, path="/app-proxy/8065/", origin="https://evil.example").status_code == 403
    assert rec.calls == []


# ------------------------------------------------------------- WebSockets

def test_ws_foreign_origin_closed_1008_before_accept(client, rec):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/agent/x", headers={"origin": "https://evil.example"}):
            pass
    assert exc.value.code == 1008
    assert exc.value.reason == REFUSED_ORIGIN
    assert rec.calls == []


def test_ws_null_origin_closed_1008(client, rec):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/app-proxy/9097/ws/terminal", headers={"origin": "null"}):
            pass
    assert exc.value.code == 1008
    assert rec.calls == []


@pytest.mark.parametrize("headers", [{}, {"origin": "http://testserver"}])
def test_ws_same_origin_or_missing_origin_accepted(client, rec, headers):
    with client.websocket_connect("/ws/agent/x", headers=headers) as ws:
        assert ws.receive_text() == "reached"
    assert rec.calls == ["/ws/agent/x"]


def test_ws_rebinding_host_closed_1008(rec):
    """websocket_connect pins Host to the base URL, so drive the guard with a
    hand-built scope (what uvicorn hands us for `Host: attacker.example`)."""
    import asyncio

    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "websocket", "scheme": "ws", "path": "/ws/agent/x",
             "headers": [(b"host", b"attacker.example:9091")]}
    asyncio.run(OriginGuardMiddleware(rec, policy=_policy())(scope, receive, send))
    assert sent == [{"type": "websocket.close", "code": 1008, "reason": REFUSED_HOST}]
    assert rec.calls == []


def test_lifespan_passes_through(rec):
    with TestClient(OriginGuardMiddleware(rec, policy=_policy())):
        pass


# ----------------------------------------------------------------- logging

def test_refusals_logged_once_per_origin_and_prefix(client, caplog):
    caplog.set_level(logging.WARNING, logger="Orchestrator.security.origin_guard")
    for _ in range(3):
        _get(client, path="/chat/stream", origin="https://evil.example")
    _get(client, path="/chat/save", origin="https://evil.example")   # same prefix
    _get(client, path="/mint", origin="https://evil.example")        # new prefix
    lines = [r.getMessage() for r in caplog.records if "[ORIGIN-GUARD]" in r.getMessage()]
    assert len(lines) == 2, lines
    assert "https://evil.example" in lines[0] and "/chat" in lines[0]


def test_registered_ports_read_live(rec):
    ports = {8065}
    c = TestClient(OriginGuardMiddleware(rec, policy=_policy(registered_app_ports=lambda: ports)))
    assert _get(c, origin=f"http://{LAN}:7005").status_code == 403
    ports.add(7005)                       # an app registers — no restart
    assert _get(c, origin=f"http://{LAN}:7005").status_code == 200


def test_registered_ports_provider_failure_fails_closed(rec):
    def boom():
        raise RuntimeError("registry unavailable")
    c = TestClient(OriginGuardMiddleware(rec, policy=_policy(registered_app_ports=boom)))
    assert _get(c, path="/app-proxy/8065/", origin="null").status_code == 403
    # The fixed 8060-8099 range still works without the registry.
    assert _get(c, origin=f"http://{LAN}:8065").status_code == 200
