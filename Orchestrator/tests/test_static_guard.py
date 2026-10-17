"""U10 (audit rank 24: X03-security-backend-07, P07-html-css-01, P07-html-css-05):
the /ui and /onboarding static mounts refuse runtime data, server-side source,
key/config material and dot-files, and stop serving dev pages.

The hostile requests run against the REAL mounted StaticFiles objects (taken from
the shared app's routes) re-pointed at a temp static root, so the test exercises
exactly the class and configuration production serves, without touching the
checkout's Portal/ tree.
"""
import copy
import os
import subprocess
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

import Orchestrator.app  # noqa: F401 — registers every mount on the shared app
from Orchestrator.checkpoint import app

REPO = Path(__file__).resolve().parents[2]


def _mounted(path: str):
    for route in app.routes:
        if isinstance(route, Mount) and route.path == path:
            return route.app
    raise AssertionError(f"no mount at {path}")


def _repointed(static_app, root: Path):
    """The production StaticFiles object, serving `root` instead of Portal/."""
    clone = copy.copy(static_app)
    clone.directory = str(root)
    clone.all_directories = clone.get_directories(str(root), None)
    clone.config_checked = False
    return clone


def _client(static_app, mount_at: str = "/ui") -> TestClient:
    return TestClient(Starlette(routes=[Mount(mount_at, app=static_app)]))


def _write(root: Path, rel: str, data: bytes = b"x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


SERVED = {
    "index.html": b"<html>portal</html>",
    "app-modular.js": b"export {}",
    "modules/chat.js": b"export const a = 1;",
    "styles/main.css": b"body{}",
    "onboarding/fonts/Fraunces.ttf": b"\x00\x01font",
    "vendor/novnc/LICENSE.txt": b"license",
    "uploads/song.mp3": b"ID3mp3",
    "uploads/BlackBox-v37.apk": b"PK\x03\x04apk",
    "uploads/clip.mp4": b"\x00\x00\x00\x18ftypmp4",
    "uploads/voice.wav": b"RIFFwav",
    "uploads/pic.png": b"\x89PNG",
    "uploads/doc.pdf": b"%PDF-1.7",
    "uploads/report.docx": b"PK\x03\x04docx",
    "uploads/notes.txt": b"hello",
    "uploads/sessions/session-1-abc/photo.jpg": b"\xff\xd8\xff",
}

REFUSED = [
    "tasks.db",
    "x.db-wal",
    "x.db-shm",
    "tasks.db-journal",
    "tasks.db.bak",
    "store.sqlite3",
    "cache.sqlite-wal",
    ".env",
    ".git/config",
    "modules/__pycache__/x.pyc",
    "some.py",
    "styles/build_manifest.py",
    "deploy.sh",
    "orchestrator.log",
    "config.ini",
    "prod.env",
    "server.pem",
    "server.key",
    "package.json",
    "styles/css_manifest.json",
    "node_modules/pkg/index.js",
    "uploads/sessions/session-1-abc/.env",
    "uploads/x.db",
    "uploads/.hidden.mp3",
    "artifacts/op_1234abcd_report.txt",
    "dev/fx-lab.html",
    "BACKUP.DB",
]


@pytest.fixture
def portal_root(tmp_path):
    root = tmp_path / "Portal"
    for rel, data in SERVED.items():
        _write(root, rel, data)
    for rel in REFUSED:
        _write(root, rel, b"SECRET-" + rel.encode())
    return root


@pytest.fixture
def ui(portal_root):
    return _client(_repointed(_mounted("/ui"), portal_root))


@pytest.mark.parametrize("rel", REFUSED)
def test_ui_refuses_runtime_source_and_dot_files(ui, rel):
    for method in ("GET", "HEAD"):
        r = ui.request(method, f"/ui/{rel}")
        assert r.status_code == 404, (method, rel, r.status_code)
        assert b"SECRET" not in r.content


def test_ui_refuses_a_range_request_for_the_task_db(ui):
    """The audit's probe: `curl -r 0-99 /ui/tasks.db` returned 206."""
    r = ui.get("/ui/tasks.db", headers={"Range": "bytes=0-99"})
    assert r.status_code == 404


@pytest.mark.parametrize("url", [
    "/ui/modules/../tasks.db",
    "/ui/tasks%2Edb",
    "/ui/uploads/..%2Ftasks.db",
    "/ui/%2Eenv",
    "/ui/modules/__pycache__/",
])
def test_ui_refuses_encoded_and_traversal_spellings(ui, url):
    r = ui.get(url)
    assert r.status_code == 404
    assert b"SECRET" not in r.content


def test_ui_refuses_a_served_name_that_symlinks_to_a_refused_file(ui, portal_root):
    os.symlink(portal_root / "tasks.db", portal_root / "uploads" / "innocent.mp3")
    r = ui.get("/ui/uploads/innocent.mp3")
    assert r.status_code == 404
    assert b"SECRET" not in r.content


@pytest.mark.parametrize("rel", sorted(SERVED))
def test_ui_still_serves_portal_assets_and_uploads(ui, rel):
    r = ui.get(f"/ui/{rel}")
    assert r.status_code == 200, (rel, r.status_code)
    assert r.content == SERVED[rel]


def test_ui_directory_index_still_works(ui):
    r = ui.get("/ui/")
    assert r.status_code == 200
    assert r.content == SERVED["index.html"]


def test_ui_serves_a_range_request_for_an_upload(ui):
    r = ui.get("/ui/uploads/song.mp3", headers={"Range": "bytes=0-2"})
    assert r.status_code == 206
    assert r.content == b"ID3"


def test_dev_pages_are_served_only_with_the_debug_flag(portal_root):
    from Orchestrator.utils.static_guard import GuardedStaticFiles, portal_refused_dirs

    off = _client(GuardedStaticFiles(directory=str(portal_root), html=True,
                                     refused_dirs=portal_refused_dirs(False)))
    on = _client(GuardedStaticFiles(directory=str(portal_root), html=True,
                                    refused_dirs=portal_refused_dirs(True)))
    assert off.get("/ui/dev/fx-lab.html").status_code == 404
    assert on.get("/ui/dev/fx-lab.html").status_code == 200
    # The flag never re-opens runtime data or source.
    for rel in ("tasks.db", "artifacts/op_1234abcd_report.txt", "some.py", ".env"):
        assert on.get(f"/ui/{rel}").status_code == 404, rel


def test_onboarding_mount_is_guarded_and_still_no_store(tmp_path):
    root = tmp_path / "onboarding"
    _write(root, "index.html", b"<html>wizard</html>")
    _write(root, "steps/welcome.js", b"export {}")
    _write(root, ".env", b"SECRET")
    _write(root, "state.json", b"SECRET")
    client = _client(_repointed(_mounted("/onboarding"), root), "/onboarding")

    r = client.get("/onboarding/")
    assert r.status_code == 200 and r.content == b"<html>wizard</html>"
    assert "no-store" in r.headers["cache-control"]
    assert client.get("/onboarding/steps/welcome.js").status_code == 200
    assert client.get("/onboarding/.env").status_code == 404
    assert client.get("/onboarding/state.json").status_code == 404


def _tracked_portal_files():
    try:
        out = subprocess.run(["git", "ls-files", "Portal"], cwd=REPO,
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git checkout not available")
    return [line[len("Portal/"):] for line in out.splitlines() if line]


def test_no_tracked_portal_asset_is_refused():
    """The deny-list must not break the Portal: every tracked file is served
    except the Node-only package.json marker and the dev pages."""
    ui_static = _mounted("/ui")
    refused = {rel for rel in _tracked_portal_files() if ui_static.is_refused(rel)}
    assert refused - {"package.json"} == {r for r in refused if r.startswith("dev/")}
    assert "index.html" not in refused and "app-modular.js" not in refused


def test_the_only_tracked_pages_outside_dev_are_the_real_entries():
    """Dead and dev-only pages (index-modular.html, fx-lab.html, the onboarding
    mocks) must not sit in the served tree; dev pages belong under Portal/dev/."""
    pages = {rel for rel in _tracked_portal_files()
             if rel.endswith(".html") and not rel.startswith("dev/")}
    assert pages == {"index.html", "cu-view/index.html", "onboarding/index.html"}
