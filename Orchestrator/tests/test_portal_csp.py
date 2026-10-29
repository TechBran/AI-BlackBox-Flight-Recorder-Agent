"""The Portal's Content-Security-Policy (audit rank 75, P02-voice-web-12).

/ui responses carry a policy that lets the page run only its own script
files (no inline <script>, no on*= handler, no eval) while styles, media,
same-origin API calls and same-host WebSockets keep working; /ui/uploads/
responses are made inert instead (a script there would otherwise be trusted
as 'self'). The middleware runs against a throwaway static root, and the
shared app is checked to install it.
"""
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient

# At import (collection) time, like test_static_guard: the shared app cannot
# take middleware once another test has started it.
import Orchestrator.app  # noqa: F401 — registers every route and middleware
from Orchestrator.checkpoint import app as shared_app
from Orchestrator.utils.portal_csp import PortalCSPMiddleware, portal_csp


def _directives(policy: str) -> dict:
    out = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


@pytest.fixture
def client(tmp_path):
    root = tmp_path / "Portal"
    (root / "uploads").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Portal</title>")
    (root / "app.js").write_text("export {}")
    (root / "uploads" / "evil.js").write_text("alert(1)")
    (root / "uploads" / "page.html").write_text("<script>alert(1)</script>")
    (root / "uploads" / "pic.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (root / "uploads" / "voice.mp3").write_bytes(b"ID3")
    api = Route("/health", lambda request: JSONResponse({"ok": True}))
    app = Starlette(routes=[api, Mount("/ui", app=StaticFiles(directory=str(root), html=True))])
    app.add_middleware(PortalCSPMiddleware)
    return TestClient(app, base_url="http://box.tail1234.ts.net")


def test_portal_pages_carry_the_policy(client):
    for path in ("/ui/", "/ui/index.html", "/ui/app.js"):
        r = client.get(path)
        assert r.status_code == 200, path
        d = _directives(r.headers["content-security-policy"])
        assert d["script-src"] == ["'self'"], "own files only: no inline, no eval"
        assert "'unsafe-inline'" in d["style-src"], "inline styles keep working"
        assert d["object-src"] == ["'none'"]
        assert d["base-uri"] == ["'self'"]
        assert "ws://box.tail1234.ts.net" in d["connect-src"]
        assert "wss://box.tail1234.ts.net" in d["connect-src"]
        assert {"data:", "blob:"} <= set(d["media-src"])


def test_no_inline_or_eval_anywhere_in_the_policy():
    policy = portal_csp("localhost:9091")
    d = _directives(policy)
    assert "'unsafe-inline'" not in d["script-src"] and "'unsafe-eval'" not in policy
    assert "'unsafe-inline'" not in d["default-src"]


@pytest.mark.parametrize("host", ['evil.com" ; script-src *', "a b", "x;y", "", None])
def test_a_strange_host_is_never_echoed(host):
    d = _directives(portal_csp(host))
    assert d["connect-src"] == ["'self'", "data:", "blob:"]
    assert d["script-src"] == ["'self'"]


def test_ipv6_and_ports_are_echoed():
    assert "ws://[fd7a:115c::1]:9091" in portal_csp("[fd7a:115c::1]:9091")
    assert "wss://192.168.1.20:9091" in portal_csp("192.168.1.20:9091")


def test_api_routes_are_untouched(client):
    r = client.get("/health")
    assert r.json() == {"ok": True}
    assert "content-security-policy" not in r.headers


def test_an_uploaded_script_is_not_a_script(client):
    r = client.get("/ui/uploads/evil.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in r.headers or r.headers["content-security-policy"] == "sandbox"


@pytest.mark.parametrize("name", ["page.html", "pic.svg"])
def test_an_uploaded_document_renders_sandboxed(client, name):
    r = client.get(f"/ui/uploads/{name}")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == "sandbox"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_uploaded_media_is_served_as_itself(client):
    r = client.get("/ui/uploads/voice.mp3")
    assert r.headers["content-type"] == "audio/mpeg"
    assert "content-security-policy" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"


def test_the_shared_app_installs_the_policy():
    assert any(m.cls is PortalCSPMiddleware for m in shared_app.user_middleware)
