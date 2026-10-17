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
- the master cookie goes ONLY to 9097; user apps get the client's own cookies;
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


@pytest.mark.parametrize("port", [9091, 9093, 9222])
def test_reserved_ports_refused_even_when_registered(client, upstream, register, port):
    """detect_app_startup registers whatever port it reads in agent output;
    the Orchestrator (guard bypass), MCP and Chrome DevTools stay off-limits."""
    register(port)
    assert client.get(f"/app-proxy/{port}/").status_code == 403
    assert upstream.requests == []


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


def test_sandboxed_app_fetch_gets_cors(client, upstream, register):
    """The sandboxed app's own fetch() arrives with Origin: null."""
    register(8065)
    r = client.get("/app-proxy/8065/api/stats", headers={"origin": "null"})
    assert r.status_code == 200
    assert r.headers.get_list("access-control-allow-origin") == ["*"]


def test_upstream_cors_not_duplicated(client, upstream, register):
    register(8065)
    upstream.reply = lambda req: httpx.Response(
        200, text="{}", headers={"content-type": "application/json",
                                 "access-control-allow-origin": "*"})
    r = client.get("/app-proxy/8065/api/stats", headers={"origin": "null"})
    assert r.headers.get_list("access-control-allow-origin") == ["*"]


def test_sandboxed_app_preflight_answered_locally(client, upstream, register):
    register(8065)
    r = client.options("/app-proxy/8065/api/items", headers={
        "origin": "null", "access-control-request-method": "POST",
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
