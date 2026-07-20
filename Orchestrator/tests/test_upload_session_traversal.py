# Orchestrator/tests/test_upload_session_traversal.py
"""Path-traversal hardening for the agent-mode session upload endpoints.

The three /upload/session endpoints in admin_routes.py take a session_id and
use it in filesystem paths under SESSION_UPLOADS_DIR. Pre-fix, a hostile id
escaped the sessions root:
- POST /upload/session with session_id="../../.." wrote anywhere the service
  can write (ProtectHome=no);
- DELETE /upload/session/{sid} with sid=".." (reachable over HTTP as %2E%2E —
  Starlette decodes path params AFTER route matching) rmtree'd
  Portal/uploads/ wholesale.

Legitimate ids are plain tokens (crypto.randomUUID() or
"session-{Date.now()}-{alnum}"), so _validate_session_id pins
^[A-Za-z0-9._-]{1,128}$ and explicitly rejects "." / ".." (the charset admits
dots). 400 on violation, before ANY filesystem use.

House pattern: TestClient over the shared checkpoint app — importing
admin_routes registers its routes on it (precedent:
test_cu_preflight.py::test_preflight_route) — plus monkeypatched module
constant SESSION_UPLOADS_DIR -> tmp_path (precedent:
test_models_custom.py's REGISTRY_PATH redirect). GET/DELETE traversal rides
%2E%2E because httpx normalizes literal dot-segments away ("/.." never
reaches the app) and %2F ids 404 at routing — the slash vector only transits
the POST form field. The pure-function tier covers the full id matrix
transport-independently (raw clients CAN deliver what httpx won't).
"""
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Must import before the first TestClient request or the shared app's
# middleware stack freezes early and later `import Orchestrator.app` crashes.
import Orchestrator.app  # noqa: F401
from Orchestrator.checkpoint import app
from Orchestrator.routes import admin_routes


TRAVERSAL_IDS = ["../x", "..", ".", "a/b", "a" * 200]
VALID_IDS = ["session-1734012345678-abc123def", "9b2f7c1e-4d3a-4f2b-9c8d-1a2b3c4d5e6f"]


@pytest.fixture
def sessions_root(tmp_path, monkeypatch):
    """Redirect SESSION_UPLOADS_DIR into tmp so traversal ids stay inside
    tmp_path: root is tmp/uploads/sessions, so even ".." resolves to
    tmp/uploads — never outside the test sandbox."""
    root = tmp_path / "uploads" / "sessions"
    root.mkdir(parents=True)
    monkeypatch.setattr(admin_routes, "SESSION_UPLOADS_DIR", root)
    return root


@pytest.fixture
def client():
    return TestClient(app)


def _post(client, session_id):
    return client.post(
        "/upload/session",
        files={"file": ("hello.txt", b"hello world")},
        data={"session_id": session_id},
    )


# ------------------------------------------------------- pure-function tier

@pytest.mark.parametrize(
    "bad",
    TRAVERSAL_IDS
    + ["", "a" * 129, "a\\b", "a b", "a\x00b", "..%2Fx"]
    # Trailing-newline ids: `$` + .match() accepts "x\n" (and "..\n" also
    # slips the literal dot-set check) — fullmatch is the anchor that holds.
    + ["abc\n", "..\n"],
)
def test_validate_rejects(bad):
    with pytest.raises(HTTPException) as exc:
        admin_routes._validate_session_id(bad)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("good", VALID_IDS + ["a", "a" * 128, "...", "with.dots-and_underscores"])
def test_validate_accepts(good):
    assert admin_routes._validate_session_id(good) == good


# ------------------------------------------------------------ POST endpoint

@pytest.mark.parametrize("bad", TRAVERSAL_IDS)
def test_post_traversal_400_nothing_written(client, sessions_root, bad):
    before = sorted(p for p in sessions_root.parent.parent.rglob("*"))
    r = _post(client, bad)
    assert r.status_code == 400
    assert "Invalid session_id" in r.json()["detail"]
    # Nothing created anywhere under tmp — not in sessions/, not escaped above it.
    assert sorted(p for p in sessions_root.parent.parent.rglob("*")) == before


@pytest.mark.parametrize("good", VALID_IDS)
def test_post_valid_id_works(client, sessions_root, good):
    r = _post(client, good)
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == good
    assert body["filename"] == "hello.txt"
    saved = sessions_root / good / "hello.txt"
    assert saved.read_bytes() == b"hello world"
    assert body["path"] == str(saved.resolve())


# ------------------------------------------------------------- GET endpoint

def test_get_traversal_400(client, sessions_root):
    # %2E%2E survives httpx and decodes to ".." AFTER route matching.
    r = client.get("/upload/session/%2E%2E")
    assert r.status_code == 400
    assert "Invalid session_id" in r.json()["detail"]


def test_get_valid_id_lists_files(client, sessions_root):
    sid = VALID_IDS[0]
    assert _post(client, sid).status_code == 200
    r = client.get(f"/upload/session/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["files"][0]["filename"] == "hello.txt"


# ---------------------------------------------------------- DELETE endpoint

def test_delete_traversal_400_parent_survives(client, sessions_root):
    # Pre-fix this rmtree'd SESSION_UPLOADS_DIR/".." — i.e. Portal/uploads/
    # itself. The parent (and the sessions root) must survive.
    r = client.delete("/upload/session/%2E%2E")
    assert r.status_code == 400
    assert "Invalid session_id" in r.json()["detail"]
    assert sessions_root.exists()
    assert sessions_root.parent.exists()


def test_delete_valid_id_removes_folder(client, sessions_root):
    sid = str(uuid.uuid4())
    assert _post(client, sid).status_code == 200
    assert (sessions_root / sid).exists()
    r = client.delete(f"/upload/session/{sid}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert not (sessions_root / sid).exists()
    assert sessions_root.exists()
