# Orchestrator/tests/test_app_proxy_allowlist.py
"""/app-proxy/{port} hardening (audit X05-architecture-01, X03-security-backend-16).

Pre-fix the HTTP and WebSocket proxies forwarded to ANY loopback port
1024-65535 (Chrome DevTools on 9222, MCP on 9093, the Orchestrator itself,
llama-swap, Asterisk ARI ...) and replaced every client Cookie with zellij's
MASTER session cookie — the terminal credential — for every port, while
follow_redirects=True let an upstream bounce the proxy anywhere.

Post-fix contract:
- only zellij-web (9097) plus ports in the registered-apps registry are
  proxied; reserved infrastructure ports never are, even if registered;
- the master cookie goes ONLY to 9097; user apps get the client's own cookies
  minus zellij's session_token (a terminal credential), and no 9097 response
  ever hands the browser a zellij cookie;
- redirects are passed back to the browser (Location re-rooted under
  /app-proxy/{port}/ when it points at the upstream), never followed;
- user-app responses carry `Content-Security-Policy: sandbox` (no
  allow-same-origin), so LLM-written app code runs in an opaque origin on
  every surface — the Portal iframe, Android's external-browser open, a
  pasted URL — and can't drive the Portal API.

Hermetic: the upstream is an httpx.MockTransport / a fake websockets.connect;
the zellij master cookie is monkeypatched. Nothing dials a real port.
"""
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import Orchestrator.app  # noqa: F401 — registers every route on the shared app
from Orchestrator import state
from Orchestrator.checkpoint import app
from Orchestrator.cli_agent import zellij_client
from Orchestrator.models import RegisteredApp
from Orchestrator.routes import agent_routes

MASTER = "MASTER-TEST-COOKIE"


class _Upstream:
    """Stands in for every proxied service via httpx.MockTransport."""

    def __init__(self):
        self.requests = []
        self.client_kwargs = []
        self.reply = lambda req: httpx.Response(
            200, text="upstream-ok", headers={"content-type": "text/plain"})

    def _handle(self, request):
        self.requests.append(request)
        return self.reply(request)


@pytest.fixture
def upstream(monkeypatch):
    up = _Upstream()
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        up.client_kwargs.append(dict(kwargs))
        kwargs["transport"] = httpx.MockTransport(up._handle)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(zellij_client, "get_master_session_cookie", lambda: MASTER)
    monkeypatch.setattr(zellij_client, "refresh_master_session_cookie", lambda: MASTER)
    return up


@pytest.fixture
def register(monkeypatch):
    def _register(port):
        app_id = f"test-app-{port}"
        monkeypatch.setitem(state.APP_REGISTRY, app_id, RegisteredApp(
            app_id=app_id, name=f"Test {port}", port=port, directory="",
            session_id="manual", operator="system", created_at=""))
    return _register


@pytest.fixture
def client():
    return TestClient(app)


# ----------------------------------------------------------- port allowlist

@pytest.mark.parametrize("port", [9222, 9093, 9098, 11434, 8088, 8065])
def test_unregistered_port_refused_without_dialing(client, upstream, port):
    r = client.get(f"/app-proxy/{port}/json/version")
    assert r.status_code == 403
    assert upstream.requests == []


@pytest.mark.parametrize("port", [
    9091, 9093, 9222,              # Orchestrator (guard bypass), MCP, Chrome DevTools
    5901, 5903, 5999, 6099, 6101,  # CU desktop: x11vnc + websockify bridges
    9092, 8088, 5038, 9022, 8021,  # telephony: AudioSocket, ARI, AMI, drachtio, ESL
    9098, 11434, 8091,             # local models: llama-swap, Ollama, vLLM reranker
])
def test_reserved_ports_refused_even_when_registered(client, upstream, register, port):
    """POST /agent/apps/register takes any port, so a registry entry alone
    never makes loopback infrastructure proxiable under the Portal origin."""
    register(port)
    r = client.get(f"/app-proxy/{port}/")
    assert r.status_code == 403
    assert "reserved" in r.json()["detail"]
    assert upstream.requests == []


def test_reserved_ports_cover_the_cu_desktop_slots():
    """Pin the CU stack's real port constants inside the reserved set, so a
    slot-range change in display.py / native_stream.py can't open a hole."""
    from Orchestrator.browser import display, native_stream
    from Orchestrator.security.perimeter import reserved_ports
    reserved = reserved_ports()
    for n in range(display.MAX_VIRTUAL_SESSIONS):
        assert display.VNC_BASE_PORT + n in reserved
        assert display.WEBSOCKIFY_BASE_PORT + n in reserved
    assert native_stream.MAIN_VNC_PORT in reserved
    assert native_stream.MAIN_WS_PORT in reserved


@pytest.fixture
def fresh_reserved(monkeypatch):
    """reserved_ports() is computed once from config; rebuild it per test."""
    from Orchestrator.security import perimeter
    perimeter.reserved_ports.cache_clear()
    yield perimeter
    perimeter.reserved_ports.cache_clear()


def test_configured_loopback_service_port_is_reserved(fresh_reserved, monkeypatch):
    monkeypatch.setattr(fresh_reserved, "LOCAL_MODELS_BASE_URL", "http://127.0.0.1:7123/v1")
    monkeypatch.setattr(fresh_reserved, "OLLAMA_BASE_URL", "http://localhost:7124")
    reserved = fresh_reserved.reserved_ports()
    assert {7123, 7124} <= reserved


def test_remote_service_port_is_not_reserved(fresh_reserved, monkeypatch, register):
    """A model server on ANOTHER machine says nothing about what listens
    here: its port may well be a user app's port on this box."""
    monkeypatch.setattr(fresh_reserved, "LOCAL_MODELS_BASE_URL", "http://192.168.1.153:8070/v1")
    monkeypatch.setattr(fresh_reserved, "OLLAMA_BASE_URL", "http://gpu-box.lan:8071")
    reserved = fresh_reserved.reserved_ports()
    assert 8070 not in reserved and 8071 not in reserved
    register(8070)
    assert fresh_reserved.proxy_port_allowed(8070)


def test_zellij_is_never_a_reserved_port(fresh_reserved):
    assert 9097 not in fresh_reserved.reserved_ports()
    assert fresh_reserved.proxy_port_allowed(9097)


@pytest.mark.parametrize("port", [9091, 9222, 8088, 8091, 5999])
def test_registering_a_reserved_port_is_refused(client, monkeypatch, port):
    """Fail at registration, with the reason, instead of a silent 403 later."""
    registry, saved = {}, []
    monkeypatch.setattr(agent_routes, "APP_REGISTRY", registry)
    monkeypatch.setattr(agent_routes, "save_app_registry", lambda: saved.append(1))
    r = client.post("/agent/apps/register", json={"name": "x", "port": port})
    assert r.status_code == 400
    assert "reserved" in r.json()["detail"]
    assert registry == {} and saved == []


@pytest.mark.parametrize("port", [8065, "8065", 7001])
def test_registering_a_user_app_port_still_works(client, monkeypatch, port):
    registry = {}
    monkeypatch.setattr(agent_routes, "APP_REGISTRY", registry)
    monkeypatch.setattr(agent_routes, "save_app_registry", lambda: None)
    r = client.post("/agent/apps/register", json={"name": "x", "port": port})
    assert r.status_code == 200
    assert r.json()["app"]["port"] == port
    assert len(registry) == 1


def test_out_of_range_port_still_400(client, upstream):
    assert client.get("/app-proxy/80/").status_code == 400


def test_registered_app_is_proxied(client, upstream, register):
    register(8065)
    r = client.get("/app-proxy/8065/api/items?x=1")
    assert r.status_code == 200
    assert r.text == "upstream-ok"
    assert str(upstream.requests[0].url) == "http://localhost:8065/api/items?x=1"


# ------------------------------------------------------------------ cookies

def test_zellij_gets_master_cookie_and_never_client_cookies(client, upstream):
    r = client.get("/app-proxy/9097/info/version", headers={"cookie": "mine=1; other=2"})
    assert r.status_code == 200
    assert upstream.requests[0].headers.get_list("cookie") == [f"session_token={MASTER}"]


def test_user_app_gets_client_cookies_not_the_master(client, upstream, register):
    register(8065)
    client.get("/app-proxy/8065/", headers={"cookie": "mine=1; other=2"})
    sent = upstream.requests[0].headers.get("cookie")
    assert sent == "mine=1; other=2"
    assert MASTER not in (sent or "")


def test_user_app_without_cookies_gets_none(client, upstream, register):
    register(8065)
    client.get("/app-proxy/8065/")
    assert "cookie" not in upstream.requests[0].headers


@pytest.mark.parametrize("cookie,expected", [
    ("mine=1; session_token=ZELLIJ-SESSION; other=2", "mine=1; other=2"),
    ("session_token=ZELLIJ-SESSION", None),
    ("session_token=a; session_token=b; mine=1", "mine=1"),
    ("mine=1;session_token=ZELLIJ-SESSION", "mine=1"),
])
def test_user_app_never_receives_the_zellij_session_cookie(client, upstream, register,
                                                         cookie, expected):
    """A zellij session_token the browser holds for the Portal origin (from
    zellij-web's own login page, or the retired Portal cookie bridge) is a
    terminal credential: it must never reach LLM-written app servers."""
    register(8065)
    client.get("/app-proxy/8065/", headers={"cookie": cookie})
    sent = upstream.requests[0].headers.get("cookie")
    assert sent == expected
    assert "ZELLIJ-SESSION" not in (sent or "")


def test_zellij_never_hands_the_browser_a_session_cookie(client, upstream):
    """The proxy injects the master cookie upstream, so the browser never
    needs zellij's cookie — and holding one would leak it to user apps."""
    upstream.reply = lambda req: httpx.Response(200, text="ok", headers=[
        ("content-type", "text/plain"),
        ("set-cookie", "session_token=ZELLIJ-SESSION; HttpOnly; SameSite=Strict; Path=/"),
    ])
    r = client.post("/app-proxy/9097/command/login", json={"auth_token": "t"})
    assert r.status_code == 200
    assert "set-cookie" not in r.headers
    assert "session_token" not in client.cookies


def test_zellij_login_then_user_app_end_to_end(client, upstream, register):
    """The review probe: a 9097 login through the proxy, then a user-app
    request from the same browser, must not carry the terminal session."""
    register(8065)
    upstream.reply = lambda req: httpx.Response(200, text="ok", headers=[
        ("content-type", "text/plain"),
        ("set-cookie", "session_token=ZELLIJ-SESSION; HttpOnly; SameSite=Strict; Path=/"),
    ])
    client.post("/app-proxy/9097/command/login", json={"auth_token": "t"})
    client.get("/app-proxy/8065/")
    assert "ZELLIJ-SESSION" not in (upstream.requests[-1].headers.get("cookie") or "")


def test_user_app_set_cookies_passed_through_one_by_one(client, upstream, register):
    """Cookie-based user apps now keep their own cookies: several Set-Cookie
    headers must reach the browser as separate headers, not comma-joined."""
    register(8065)
    upstream.reply = lambda req: httpx.Response(200, text="ok", headers=[
        ("content-type", "text/plain"),
        ("set-cookie", "a=1; Path=/app-proxy/8065/; Expires=Wed, 21 Oct 2026 07:28:00 GMT"),
        ("set-cookie", "b=2; Path=/app-proxy/8065/"),
    ])
    r = client.get("/app-proxy/8065/")
    assert r.headers.get_list("set-cookie") == [
        "a=1; Path=/app-proxy/8065/; Expires=Wed, 21 Oct 2026 07:28:00 GMT",
        "b=2; Path=/app-proxy/8065/",
    ]


# ---------------------------------------------------------------- redirects

@pytest.mark.parametrize("location,expected", [
    ("http://localhost:8065/next?a=1", "/app-proxy/8065/next?a=1"),
    ("http://127.0.0.1:8065/", "/app-proxy/8065/"),
    ("/login", "/app-proxy/8065/login"),
    ("/app-proxy/8065/already", "/app-proxy/8065/already"),
    ("relative/page", "relative/page"),
    ("https://example.org/x", "https://example.org/x"),
    ("http://localhost:9222/json", "http://localhost:9222/json"),
])
def test_redirects_passed_through_not_followed(client, upstream, register, location, expected):
    register(8065)
    upstream.reply = lambda req: httpx.Response(302, headers={"location": location})
    r = client.get("/app-proxy/8065/start", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == expected
    assert len(upstream.requests) == 1                      # never followed
    assert upstream.client_kwargs[0].get("follow_redirects") is False


# ------------------------------------------- sandboxed user-app responses

def test_user_app_responses_are_csp_sandboxed(client, upstream, register):
    register(8065)
    csp = client.get("/app-proxy/8065/").headers["content-security-policy"]
    assert "sandbox" in csp
    assert "allow-scripts" in csp
    assert "allow-same-origin" not in csp


def test_zellij_responses_are_not_sandboxed(client, upstream):
    """The zellij iframe NEEDS same-origin (extra-keys reaches contentWindow.term)."""
    assert "content-security-policy" not in client.get("/app-proxy/9097/").headers


def test_user_app_html_gets_storage_stand_in(client, upstream, register):
    """Opaque-origin documents throw on localStorage; LLM-written apps read it
    at top level (grocery-store, pacman) and would die on load."""
    register(8065)
    upstream.reply = lambda req: httpx.Response(
        200, text="<!doctype html><html><head><title>t</title></head><body></body></html>",
        headers={"content-type": "text/html; charset=utf-8"})
    html = client.get("/app-proxy/8065/").text
    assert "data-blackbox-app-proxy" in html
    assert html.index("data-blackbox-app-proxy") < html.index("<title>")
    assert html.lower().startswith("<!doctype html>")      # no quirks mode


def test_zellij_html_untouched_by_storage_stand_in(client, upstream):
    upstream.reply = lambda req: httpx.Response(
        200, text="<html><head></head><body></body></html>",
        headers={"content-type": "text/html"})
    assert "data-blackbox-app-proxy" not in client.get("/app-proxy/9097/").text


# The sandboxed preview's own requests: Origin: null and (Chrome) no Referer.
PREVIEW = {"origin": "null"}


def test_sandboxed_app_fetch_gets_cors(client, upstream, register):
    """The sandboxed app's own fetch() arrives with Origin: null."""
    register(8065)
    r = client.get("/app-proxy/8065/api/stats", headers=PREVIEW)
    assert r.status_code == 200
    assert r.headers.get_list("access-control-allow-origin") == ["*"]


def test_upstream_cors_not_duplicated(client, upstream, register):
    register(8065)
    upstream.reply = lambda req: httpx.Response(
        200, text="{}", headers={"content-type": "application/json",
                                 "access-control-allow-origin": "*"})
    r = client.get("/app-proxy/8065/api/stats", headers=PREVIEW)
    assert r.headers.get_list("access-control-allow-origin") == ["*"]


def test_sandboxed_app_preflight_answered_locally(client, upstream, register):
    register(8065)
    r = client.options("/app-proxy/8065/api/items", headers={
        **PREVIEW, "access-control-request-method": "POST",
        "access-control-request-headers": "content-type"})
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
    assert "POST" in r.headers["access-control-allow-methods"]
    assert r.headers["access-control-allow-headers"] == "content-type"
    assert upstream.requests == []


def test_null_origin_cannot_reach_zellij(client, upstream):
    assert client.get("/app-proxy/9097/", headers={"origin": "null"}).status_code == 403
    assert upstream.requests == []


# --------------------------------------------------------------- WebSockets

class _FakeConnect:
    """Records what the WS proxy would send upstream, then fails the dial."""

    def __init__(self):
        self.calls = []

    def __call__(self, uri, **kwargs):
        self.calls.append((uri, kwargs))
        return self

    async def __aenter__(self):
        raise OSError("upstream down (test)")

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def fake_ws(monkeypatch):
    fake = _FakeConnect()
    monkeypatch.setattr(agent_routes.websockets, "connect", fake)
    monkeypatch.setattr(zellij_client, "get_master_session_cookie", lambda: MASTER)
    return fake


def _dial(client, path, headers=None):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(path, headers=headers or {}) as ws:
            ws.receive_text()
    return exc.value


@pytest.mark.parametrize("port", [9222, 9093, 9091, 8065])
def test_ws_unregistered_port_closed_1008_without_dialing(client, fake_ws, port):
    assert _dial(client, f"/app-proxy/{port}/devtools/browser/x").code == 1008
    assert fake_ws.calls == []


def test_ws_zellij_gets_master_cookie_only(client, fake_ws):
    _dial(client, "/app-proxy/9097/ws/terminal", {"cookie": "mine=1"})
    uri, kwargs = fake_ws.calls[0]
    assert uri == "ws://127.0.0.1:9097/ws/terminal"
    assert kwargs["additional_headers"] == {"cookie": f"session_token={MASTER}"}


def test_ws_user_app_gets_client_cookie_not_master(client, fake_ws, register):
    register(8065)
    _dial(client, "/app-proxy/8065/socket", {"cookie": "mine=1"})
    uri, kwargs = fake_ws.calls[0]
    assert uri == "ws://127.0.0.1:8065/socket"
    assert kwargs["additional_headers"] == {"cookie": "mine=1"}


def test_ws_user_app_without_cookie_sends_none(client, fake_ws, register):
    register(8065)
    _dial(client, "/app-proxy/8065/socket")
    assert not fake_ws.calls[0][1].get("additional_headers")


@pytest.mark.parametrize("cookie,expected", [
    ("mine=1; session_token=ZELLIJ-SESSION", {"cookie": "mine=1"}),
    ("session_token=ZELLIJ-SESSION", None),
])
def test_ws_user_app_never_receives_the_zellij_session_cookie(client, fake_ws, register,
                                                            cookie, expected):
    register(8065)
    _dial(client, "/app-proxy/8065/socket", {"cookie": cookie})
    assert (fake_ws.calls[0][1].get("additional_headers") or None) == expected


@pytest.mark.parametrize("port", [8088, 5999, 9098])
def test_ws_reserved_port_closed_1008_even_when_registered(client, fake_ws, register, port):
    register(port)
    assert _dial(client, f"/app-proxy/{port}/x").code == 1008
    assert fake_ws.calls == []
