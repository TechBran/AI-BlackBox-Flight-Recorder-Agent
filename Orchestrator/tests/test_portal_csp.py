"""The Portal's Content-Security-Policy (audit rank 75, P02-voice-web-12).

/ui responses carry a policy that lets the page run only its own script
files (no inline <script>, no on*= handler, no eval) while styles, media,
same-origin API calls and same-host WebSockets keep working; /ui/uploads/
responses are made inert instead (a script there would otherwise be trusted
as the Portal's own). The Portal document at "/" carries the same policy.

An upload is recognised by the file the static mount actually opens, never by
the URL's spelling: /ui//uploads/x, /ui/%2Fuploads/x and /ui/./uploads/x all
serve Portal/uploads/x (StaticFiles normalises the path), so each must come
back inert too. The middleware runs against a throwaway static root, both
behind plain StaticFiles and behind the production GuardedStaticFiles, and
the shared app is checked to install it.
"""
import asyncio
import os

import pytest
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient

# At import (collection) time, like test_static_guard: the shared app cannot
# take middleware once another test has started it.
import Orchestrator.app  # noqa: F401 — registers every route and middleware
from Orchestrator.checkpoint import app as shared_app
from Orchestrator.utils.portal_csp import (
    PortalCSPMiddleware, harden_upload_headers, is_upload_path, portal_csp)
from Orchestrator.utils.static_guard import PORTAL_MEDIA_DIRS, GuardedStaticFiles

HOST = "box.tail1234.ts.net"


def _directives(policy: str) -> dict:
    out = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


def _portal_root(tmp_path):
    root = tmp_path / "Portal"
    (root / "uploads").mkdir(parents=True)
    (root / "modules").mkdir()
    (root / "index.html").write_text("<!doctype html><title>Portal</title>")
    (root / "app.js").write_text("export {}")
    (root / "uploads" / "evil.js").write_text("alert(1)")
    (root / "uploads" / "page.html").write_text("<script>alert(1)</script>")
    (root / "uploads" / "pic.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (root / "uploads" / "voice.mp3").write_bytes(b"ID3")
    return root


def _static(kind, root):
    if kind == "plain":
        return StaticFiles(directory=str(root), html=True)
    # Wired exactly as startup.py mounts /ui.
    return GuardedStaticFiles(directory=str(root), html=True, refused_dirs=("artifacts", "dev"),
                              media_dirs=PORTAL_MEDIA_DIRS, media_headers=harden_upload_headers)


@pytest.fixture(params=["plain", "guarded"])
def portal_app(request, tmp_path):
    root = _portal_root(tmp_path)
    api = Route("/health", lambda request: JSONResponse({"ok": True}))
    home = Route("/", lambda request: HTMLResponse("<!doctype html><title>Portal</title>"))
    app = Starlette(routes=[api, home, Mount("/ui", app=_static(request.param, root))])
    app.add_middleware(PortalCSPMiddleware)
    return app


@pytest.fixture
def client(portal_app):
    return TestClient(portal_app, base_url=f"http://{HOST}")


def _raw_get(app, path: str):
    """GET `path` exactly as spelled: httpx would resolve dot segments before
    sending, a browser keeps '//' and a raw client sends anything. Returns
    (status, headers)."""
    sent = []
    finished = None
    requested = False

    async def receive():
        # The request once, then a disconnect only after the response ends
        # (FileResponse listens for it while streaming the body).
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            finished.set()

    async def run():
        nonlocal finished
        finished = asyncio.Event()
        await app(scope, receive, send)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "headers": [(b"host", HOST.encode())],
        "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 9091),
    }
    asyncio.run(run())
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], Headers(raw=start["headers"])


def test_portal_pages_carry_the_policy(client):
    for path in ("/", "/ui/", "/ui/index.html", "/ui/app.js"):
        r = client.get(path)
        assert r.status_code == 200, path
        d = _directives(r.headers["content-security-policy"])
        assert d["script-src"] == [f"{HOST}/ui/"], "own files only: no inline, no eval"
        assert d["worker-src"] == [f"{HOST}/ui/"]
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


def test_scripts_come_only_from_the_portal_tree():
    # 'self' would also trust /app-proxy/{port}/*.js (agent-written apps, served
    # with their own script types) and every other same-origin route; the
    # Portal loads script only from /ui/ (index.html, its modules, the
    # vendored libraries and the capture worklet).
    for host in ("localhost:9091", "192.168.1.20:9091", HOST):
        d = _directives(portal_csp(host))
        assert d["script-src"] == [f"{host}/ui/"]
        assert d["worker-src"] == [f"{host}/ui/"]


@pytest.mark.parametrize("host", ['evil.com" ; script-src *', "a b", "x;y", "", None])
def test_a_strange_host_is_never_echoed(host):
    d = _directives(portal_csp(host))
    assert d["connect-src"] == ["'self'", "data:", "blob:"]
    assert d["script-src"] == ["'self'"]
    assert d["worker-src"] == ["'self'"]


def test_an_ipv6_host_keeps_self_for_scripts():
    # A CSP host-source cannot name an IPv6 literal; a source the browser
    # cannot parse would leave script-src empty and the Portal dead.
    d = _directives(portal_csp("[fd7a:115c::1]:9091"))
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


@pytest.mark.parametrize("name", ["feed.atom", "feed.rss", "style.xsl", "data.rdf", "map.kml",
                                  "page.xhtml", "doc.xml"])
def test_an_uploaded_xml_document_renders_sandboxed(portal_app, tmp_path, name):
    # Any XML type renders as a document in which XHTML-namespaced <script>
    # runs (Firefox renders every +xml type), not only the few named ones.
    root = tmp_path / "Portal"
    (root / "uploads" / name).write_text(
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        '<script xmlns="http://www.w3.org/1999/xhtml">alert(1)</script></feed>')
    r = TestClient(portal_app).get(f"/ui/uploads/{name}")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == "sandbox", r.headers["content-type"]


def test_uploaded_media_is_served_as_itself(client):
    r = client.get("/ui/uploads/voice.mp3")
    assert r.headers["content-type"] == "audio/mpeg"
    assert "content-security-policy" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"


# Spellings StaticFiles resolves to Portal/uploads/<name>. A browser keeps '//'
# (and turns '\\' into '/'); %2F arrives decoded in scope["path"].
UPLOAD_SPELLINGS = [
    "/ui//uploads/{}", "/ui///uploads//{}", "/ui/./uploads/{}",
    "/ui/modules/../uploads/{}", "/ui/uploads/./{}", "/ui/.//uploads/{}",
]


@pytest.mark.parametrize("spelling", UPLOAD_SPELLINGS)
def test_an_uploaded_script_stays_inert_however_the_path_is_spelled(portal_app, spelling):
    status, headers = _raw_get(portal_app, spelling.format("evil.js"))
    assert status == 200, spelling
    assert headers["content-type"].startswith("text/plain"), spelling
    assert headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("spelling", UPLOAD_SPELLINGS)
@pytest.mark.parametrize("name", ["page.html", "pic.svg"])
def test_an_uploaded_document_stays_sandboxed_however_the_path_is_spelled(portal_app, spelling, name):
    status, headers = _raw_get(portal_app, spelling.format(name))
    assert status == 200, spelling
    assert headers["content-security-policy"] == "sandbox", spelling
    assert headers["x-content-type-options"] == "nosniff"


def test_an_encoded_slash_is_an_upload_too(client):
    # httpx sends %2F as is; the server decodes it into scope["path"].
    r = client.get("/ui/%2Fuploads/page.html")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == "sandbox"


@pytest.mark.parametrize("path,upload", [
    ("/ui/uploads/a.js", True), ("/ui//uploads/a.js", True), ("/ui/./uploads/a", True),
    ("/ui/x/../uploads/a", True), ("/ui/UPLOADS/a.html", True), ("/ui/uploads", True),
    ("/ui/", False), ("/ui", False), ("/ui/index.html", False), ("/ui/modules/x.js", False),
    ("/ui/uploads-old/a.js", False), ("/ui/../uploads/a", False), ("/", False),
    ("/upload", False), ("/uploads/a.js", False),
])
def test_is_upload_path(path, upload):
    assert is_upload_path(path) is upload


def test_a_symlink_into_uploads_is_served_inert(tmp_path):
    # The guard hardens by the file it actually opens: a code-tree name that
    # resolves into Portal/uploads/ (follow_symlink stays off, so StaticFiles
    # hands file_response the real path) is an upload, whatever the URL says.
    root = _portal_root(tmp_path)
    os.symlink(root / "uploads" / "page.html", root / "innocent.html")
    app = Starlette(routes=[Mount("/ui", app=_static("guarded", root))])
    r = TestClient(app).get("/ui/innocent.html")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == "sandbox"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_the_shared_app_installs_the_policy():
    assert any(m.cls is PortalCSPMiddleware for m in shared_app.user_middleware)


def _shared_ui_mount():
    return next(r for r in shared_app.routes if isinstance(r, Mount) and r.path == "/ui")


def test_the_shared_ui_mount_hardens_uploads_itself():
    static = _shared_ui_mount().app
    assert isinstance(static, GuardedStaticFiles)
    assert static.media_headers is harden_upload_headers
    assert "uploads" in static.media_dirs


def test_the_shared_app_serves_the_root_document_under_the_policy():
    # "/" is the documented entry point (README, tailscale serve, the wizard's
    # hand-off) and serves the same Portal/index.html as /ui/.
    r = TestClient(shared_app, base_url=f"http://{HOST}").get("/")
    assert r.status_code == 200
    d = _directives(r.headers["content-security-policy"])
    assert d["script-src"] == [f"{HOST}/ui/"]
    assert d["object-src"] == ["'none'"]
