# Orchestrator/tests/test_perimeter_real_app.py
"""The perimeter guard as INSTALLED on the real app (audit rank 13).

test_origin_guard.py pins the guard's contract on a minimal app; this file
proves the shared checkpoint app actually carries it, for EVERY WebSocket
route the app registers (enumerated from app.router.routes, so a route added
next month is covered without editing this file) and for the HTTP API.

Foreign-origin cases run against the real app: the guard closes the socket
with 1008 (or answers 403) before routing, so no handler runs and nothing
touches a live service. Positive cases (same-origin / no Origin) are checked
against the SAME installed policy wrapped around a recording stub, because
running every real handler would start voice bridges and terminals.

House pattern: TestClient over the shared checkpoint app, importing
Orchestrator.app first so every route module registers before the middleware
stack freezes (precedent: test_upload_session_traversal.py).
"""
import re

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

import Orchestrator.app  # noqa: F401 — registers every route on the shared app
from Orchestrator.checkpoint import app
from Orchestrator.security.origin_guard import (
    REFUSED_HOST,
    REFUSED_ORIGIN,
    OriginGuardMiddleware,
)

EVIL = "https://evil.example"
_PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::[a-z]+)?\}")
# Sample values for path params. {port}=1 keeps the pre-fix (red) run inert:
# the old app-proxy WS handler rejects ports < 1024 without dialing anything.
_SAMPLES = {"port": "1", "path": "guard-probe", "session_id": "guard-probe",
            "cli_session_id": "guard-probe"}


def _ws_paths():
    out = []

    def walk(routes, prefix=""):
        for r in routes:
            if isinstance(r, WebSocketRoute):      # FastAPI's APIWebSocketRoute subclasses it
                out.append(prefix + r.path)
            elif isinstance(r, Mount) and getattr(r, "routes", None):
                walk(r.routes, prefix + r.path)

    walk(app.router.routes)
    return sorted(set(out))


WS_PATHS = _ws_paths()


def _concrete(path):
    return _PARAM.sub(lambda m: _SAMPLES.get(m.group(1), "guard-probe"), path)


def _installed_policy():
    for m in app.user_middleware:
        if m.cls is OriginGuardMiddleware:
            return m.kwargs["policy"]
    raise AssertionError("OriginGuardMiddleware is not installed on the shared app")


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def test_route_enumeration_finds_every_known_socket():
    """Sanity floor so a broken walker can't make the parametrized test vacuous."""
    must = {
        "/ws/agent/{session_id}", "/app-proxy/{port}/{path:path}",
        "/cu/view/{session_id}/ws", "/cli-agent/ws/{session_id}", "/ws/stt",
        "/ws/phone/{session_id}", "/ws/realtime/{session_id}",
        "/ws/gemini-live/{session_id}", "/ws/grok-live/{session_id}",
        "/ws/gemini-agent/{session_id}", "/twilio/media/{session_id}",
        "/twilio/cli/media/{session_id}/{cli_session_id}",
    }
    assert must <= set(WS_PATHS), sorted(must - set(WS_PATHS))


@pytest.mark.parametrize("route", WS_PATHS)
def test_every_websocket_refuses_foreign_origin(client, route):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_concrete(route), headers={"origin": EVIL}):
            pass
    assert exc.value.code == 1008
    # The guard's own reason — not a handler that happened to close 1008
    # after accepting (cu_view_ws, the old app_proxy_websocket, FastAPI's
    # missing-query-param close on /cli-agent/ws all use 1008 too).
    assert exc.value.reason == REFUSED_ORIGIN


@pytest.mark.parametrize("route", WS_PATHS)
def test_every_websocket_refuses_null_origin(client, route):
    path = _concrete(route)       # {port}=1 is never a registered app
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(path, headers={"origin": "null"}):
            pass
    assert exc.value.code == 1008
    assert exc.value.reason == REFUSED_ORIGIN


class _Stub:
    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append(scope["path"])
        await receive()
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": "reached"})
        await send({"type": "websocket.close", "code": 1000})


@pytest.mark.parametrize("route", WS_PATHS)
@pytest.mark.parametrize("headers", [{}, {"origin": "http://testserver"}],
                         ids=["no-origin", "same-origin"])
def test_every_websocket_admits_same_origin_and_native_clients(route, headers):
    stub = _Stub()
    guarded = TestClient(OriginGuardMiddleware(stub, policy=_installed_policy()))
    path = _concrete(route)
    with guarded.websocket_connect(path, headers=headers) as ws:
        assert ws.receive_text() == "reached"
    assert stub.calls == [path]


# --------------------------------------------------------------------- HTTP

def test_wildcard_cors_is_gone():
    from starlette.middleware.cors import CORSMiddleware
    assert not [m for m in app.user_middleware if m.cls is CORSMiddleware], \
        "allow_origins=['*'] + allow_credentials=True must not come back"


def test_foreign_origin_post_refused_without_cors(client):
    # The no-cors text/plain "simple" POST (X05-architecture-03); empty
    # messages keep a regressed handler inert (400 before any provider call).
    r = client.post("/chat/stream", content=b'{"messages":[]}',
                    headers={"origin": EVIL, "content-type": "text/plain"})
    assert r.status_code == 403
    assert r.json() == {"detail": REFUSED_ORIGIN}
    assert "access-control-allow-origin" not in r.headers


def test_null_origin_refused(client):
    # Empty messages: even if a regression let this through, the handler
    # answers 400 before any provider call (keeps the red run inert).
    r = client.post("/chat/stream", json={"messages": []}, headers={"origin": "null"})
    assert r.status_code == 403


def test_foreign_origin_read_gets_no_cors(client):
    r = client.get("/agent/apps", headers={"origin": EVIL})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers


def test_same_origin_through_tailscale_serve(client):
    """tailscale serve preserves Host, connects from 127.0.0.1 over http and
    says https in X-Forwarded-Proto."""
    host = "box.tail1234.ts.net"
    r = client.get("/agent/apps", headers={
        "host": host, "origin": f"https://{host}", "x-forwarded-proto": "https",
        "x-forwarded-for": "100.64.0.7"})
    assert r.status_code == 200
    assert "apps" in r.json()


def test_tailnet_host_without_origin_ok(client):
    assert client.get("/agent/apps", headers={"host": "box.tail1234.ts.net"}).status_code == 200


def test_rebinding_host_refused(client):
    r = client.get("/agent/apps", headers={"host": "attacker.example"})
    assert r.status_code == 400
    assert r.json() == {"detail": REFUSED_HOST}


def test_user_app_origin_allowed_with_cors(client):
    origin = "http://testserver:8065"
    r = client.get("/agent/apps", headers={"origin": origin})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-credentials" not in r.headers


def test_cors_preflight_for_allowed_origin(client):
    origin = "http://localhost:8065"
    r = client.options("/chat/stream", headers={
        "origin": origin, "access-control-request-method": "POST",
        "access-control-request-headers": "content-type"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == origin
    assert "POST" in r.headers["access-control-allow-methods"]


def test_cors_preflight_for_foreign_origin_refused(client):
    r = client.options("/chat/stream", headers={
        "origin": EVIL, "access-control-request-method": "POST"})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers
