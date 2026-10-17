"""A model-named artifact must never become a live page on the Portal origin.

ARTIFACTS_DIR is Portal/artifacts, which the /ui StaticFiles mount also serves
INLINE, typing each file by its extension. The filename comes from the model's
[ARTIFACT:<filename>:<type>] tag (a prompt-injected page or email can choose
it), so "[ARTIFACT:report.html:text]<script>...</script>[/ARTIFACT]" used to
land at /ui/artifacts/<id> as text/html and run with full Portal privileges
(bypass review of U2). The file on disk must get an inert name, while the
download keeps the name the model gave it.

Hermetic: artifacts go to tmp_path; the static mount is a local Starlette app
built exactly like startup.py's; the download route runs on the shared app via
TestClient without its lifespan (precedent: test_upload_session_traversal.py).
"""

import re

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.staticfiles import StaticFiles

# Must import before the first TestClient request (see test_upload_session_traversal.py).
import Orchestrator.app  # noqa: F401
import Orchestrator.artifacts as artifacts_mod
from Orchestrator.artifacts import parse_and_process_artifacts_with_meta
from Orchestrator.checkpoint import app
from Orchestrator.routes import admin_routes

PAYLOAD = "<script>fetch('/agent/apps').then(r => r.text()).then(alert)</script>"

# Every name a browser would render as an active document when served by type.
ACTIVE_NAMES = [
    "report.html", "REPORT.HTML", "page.htm", "page.shtml", "doc.xhtml", "doc.xht",
    "logo.svg", "logo.svgz", "feed.xml", "style.xsl", "style.xslt", "feed.rss",
    "atom.atom", "math.mml", "data.rdf", "archive.mht", "mail.eml", "page.html.gz",
    "app.js", "mod.mjs",
]
# Media types that run script in the serving origin when navigated to.
ACTIVE_TYPE = re.compile(r"text/html|xml|message/|multipart/|javascript|ecmascript", re.I)


@pytest.fixture
def portal_root(tmp_path, monkeypatch):
    """A tmp stand-in for Portal/ with its artifacts/ subdirectory wired into both modules."""
    root = tmp_path / "Portal"
    (root / "artifacts").mkdir(parents=True)
    monkeypatch.setattr(artifacts_mod, "ARTIFACTS_DIR", root / "artifacts")
    monkeypatch.setattr(admin_routes, "ARTIFACTS_DIR", root / "artifacts")
    return root


@pytest.fixture
def ui_client(portal_root):
    """The /ui mount exactly as startup.py builds it, over the tmp Portal/."""
    ui = Starlette()
    ui.mount("/ui", StaticFiles(directory=str(portal_root), html=True), name="ui")
    return TestClient(ui)


def _artifact_id(url):
    assert url.startswith("/artifacts/")
    return url[len("/artifacts/"):]


@pytest.mark.parametrize("name", ACTIVE_NAMES)
@pytest.mark.parametrize("kind", ["text", "csv"])
def test_model_named_active_file_is_not_served_as_a_page(name, kind, ui_client):
    reply = f"[ARTIFACT:{name}:{kind}]{PAYLOAD}[/ARTIFACT]"
    _html, artifacts = parse_and_process_artifacts_with_meta(reply, "tester")
    assert len(artifacts) == 1
    artifact_id = _artifact_id(artifacts[0]["url"])

    resp = ui_client.get(f"/ui/artifacts/{artifact_id}")
    assert resp.status_code == 200
    media_type = resp.headers["content-type"]
    assert not ACTIVE_TYPE.search(media_type), f"{name} served inline as {media_type}"
    assert media_type.startswith("text/plain")
    # The structured metadata still reports the name the model chose.
    assert artifacts[0]["filename"] == name


def test_download_link_keeps_the_original_name(portal_root):
    html, artifacts = parse_and_process_artifacts_with_meta(
        f"[ARTIFACT:report.html:text]{PAYLOAD}[/ARTIFACT]", "tester")
    assert 'download="report.html"' in html
    assert "Download report.html" in html
    assert f'href="{artifacts[0]["url"]}"' in html


def test_download_route_serves_an_attachment_under_the_original_name(portal_root):
    _html, artifacts = parse_and_process_artifacts_with_meta(
        f"[ARTIFACT:report.html:text]{PAYLOAD}[/ARTIFACT]", "tester")
    resp = TestClient(app).get(artifacts[0]["url"])
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert 'filename="report.html"' in disposition
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert "sandbox" in resp.headers.get("content-security-policy", "")
    assert not ACTIVE_TYPE.search(resp.headers["content-type"])
    assert resp.text == PAYLOAD


@pytest.mark.parametrize("name", ["notes.txt", "README.md", "data.json", "script.py", "table.csv", "notes.html.txt"])
def test_inert_names_are_stored_and_downloaded_unchanged(name, portal_root):
    _html, artifacts = parse_and_process_artifacts_with_meta(f"[ARTIFACT:{name}:text]hello[/ARTIFACT]", "tester")
    artifact_id = _artifact_id(artifacts[0]["url"])
    assert re.fullmatch(rf"tester_[0-9a-f]{{8}}_{re.escape(name)}", artifact_id), artifact_id
    assert (portal_root / "artifacts" / artifact_id).read_text() == "hello"
    resp = TestClient(app).get(artifacts[0]["url"])
    assert resp.status_code == 200
    assert f'filename="{name}"' in resp.headers["content-disposition"]


def test_a_name_that_already_ends_in_the_inert_suffix_round_trips(portal_root, ui_client):
    """The on-disk rename is reversible for EVERY name, so the download name is exact."""
    name = "report.html" + getattr(artifacts_mod, "INERT_SUFFIX", ".inert.txt")
    _html, artifacts = parse_and_process_artifacts_with_meta(f"[ARTIFACT:{name}:text]{PAYLOAD}[/ARTIFACT]", "tester")
    resp = TestClient(app).get(artifacts[0]["url"])
    assert f'filename="{name}"' in resp.headers["content-disposition"]
    inline = ui_client.get(f"/ui/artifacts/{_artifact_id(artifacts[0]['url'])}")
    assert inline.headers["content-type"].startswith("text/plain")
