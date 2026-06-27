"""Tests for the on-device (local Gemma) model MIRROR catalog (Task 1.1).

The BlackBox hub mirrors the Gemma 4 LiteRT ``.litertlm`` bundles so each
user's phone downloads them from the hub over Tailscale (no per-user Hugging
Face friction). This task is ONLY the catalog: it tells the app what is
downloadable + its metadata. The actual fetch-once + ranged download endpoint
is Task 1.2.

The mirror catalog is DISTINCT from the picker catalog (``LOCAL_MODELS`` in
local_routes): the picker entries are id/name/provider descriptors for the
model selector, whereas these carry DOWNLOAD/METADATA fields (hf_repo,
filename, size_bytes, sha256, min_ram_gb, recommended_for). The slugs are kept
identical across both so a downloaded bundle maps to a picker entry.

Hermetic: NO network. The mirror module is pure config + a getter; the
endpoint just serializes it. The startup embedding-sync hook is mocked (it
spawns a daemon thread calling sync_embeddings, which would hit the network).
"""

import hashlib

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from Orchestrator.local_provider import catalog as mirror


@pytest.fixture
def client():
    """TestClient with the startup embedding-sync hook mocked (it spawns a
    daemon thread calling sync_embeddings, which would hit the network)."""
    with patch("Orchestrator.toolvault.embeddings.sync_embeddings") as m_src:
        m_src.return_value = {"x": {"vector": [0.1]}}
        from Orchestrator.app import app
        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# GET /local/models/catalog
# ---------------------------------------------------------------------------

def test_catalog_lists_both_bundles(client):
    """GET /local/models/catalog → 200; two bundles whose slugs are exactly
    the two on-device gemma models."""
    resp = client.get("/local/models/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert "bundles" in body
    assert len(body["bundles"]) == 2
    slugs = {b["slug"] for b in body["bundles"]}
    assert slugs == {"gemma-4-e2b", "gemma-4-e4b"}


def test_catalog_entries_have_required_fields(client):
    """Each bundle carries the full download/metadata field set, now including
    the per-model config fields (Task W6) the picker + sidecar consume."""
    resp = client.get("/local/models/catalog")
    assert resp.status_code == 200
    required = {
        "slug", "display_name", "hf_repo", "filename",
        "size_bytes", "sha256", "min_ram_gb", "recommended_for",
        # Per-model config (Task W6) -- snake_case to match the sidecar/API JSON.
        "recommended", "context_note", "max_tokens", "support_image",
    }
    for b in resp.json()["bundles"]:
        assert required.issubset(b.keys()), f"missing fields: {required - b.keys()}"


def test_catalog_per_model_config_e4b_recommended(client):
    """Task W6: E4B is the recommended default -- carries recommended True, a
    "Recommended" context note, a real 16K max_tokens window, and is multimodal
    (support_image True)."""
    resp = client.get("/local/models/catalog")
    assert resp.status_code == 200
    by_slug = {b["slug"]: b for b in resp.json()["bundles"]}
    e4b = by_slug["gemma-4-e4b"]
    assert e4b["recommended"] is True
    assert "Recommended" in e4b["context_note"]
    assert e4b["max_tokens"] == 16384
    assert e4b["support_image"] is True


def test_catalog_per_model_config_e2b_experimental(client):
    """Task W6: E2B is labeled experimental/weaker at multi-step agent loops --
    recommended False with an "Experimental" context note. Still a real 16K
    window + multimodal."""
    resp = client.get("/local/models/catalog")
    assert resp.status_code == 200
    by_slug = {b["slug"]: b for b in resp.json()["bundles"]}
    e2b = by_slug["gemma-4-e2b"]
    assert e2b["recommended"] is False
    assert "Experimental" in e2b["context_note"]
    assert e2b["max_tokens"] == 16384
    assert e2b["support_image"] is True


def test_catalog_exactly_one_recommended(client):
    """Exactly one bundle is flagged recommended (E4B) -- the picker shows a
    single default."""
    resp = client.get("/local/models/catalog")
    assert resp.status_code == 200
    recommended = [b for b in resp.json()["bundles"] if b.get("recommended")]
    assert len(recommended) == 1
    assert recommended[0]["slug"] == "gemma-4-e4b"


def test_catalog_ram_ordering(client):
    """Sanity: the lighter E2B model needs less RAM than the heavier E4B."""
    resp = client.get("/local/models/catalog")
    assert resp.status_code == 200
    by_slug = {b["slug"]: b for b in resp.json()["bundles"]}
    assert by_slug["gemma-4-e2b"]["min_ram_gb"] < by_slug["gemma-4-e4b"]["min_ram_gb"]


def test_bundles_pinned_to_real_litert_community_repos():
    """The bundles are PINNED to the real ungated litert-community repos/files
    (Task 2.6a): uppercase E2B/E4B casing, the portable ``.litertlm`` CPU/GPU
    build, a populated size_bytes, and sha256 still None (verify skipped until
    the real digest is pinned in 2.6b)."""
    e2b = mirror.BUNDLES["gemma-4-e2b"]
    assert e2b["hf_repo"] == "litert-community/gemma-4-E2B-it-litert-lm"
    assert e2b["filename"] == "gemma-4-E2B-it.litertlm"
    assert isinstance(e2b["size_bytes"], int) and e2b["size_bytes"] > 0
    assert e2b["sha256"] is None  # TODO(2.6b): pinned to the real digest

    e4b = mirror.BUNDLES["gemma-4-e4b"]
    assert e4b["hf_repo"] == "litert-community/gemma-4-E4B-it-litert-lm"
    assert e4b["filename"] == "gemma-4-E4B-it.litertlm"
    assert isinstance(e4b["size_bytes"], int) and e4b["size_bytes"] > 0
    assert e4b["sha256"] is None  # TODO(2.6b): pinned to the real digest

    # The heavier E4B bundle is larger than the lighter E2B bundle.
    assert e4b["size_bytes"] > e2b["size_bytes"]


def test_list_bundles_is_isolated():
    """``mirror.list_bundles()`` returns copies — mutating a returned dict must
    NOT corrupt the module-level ``BUNDLES``; a second call is unaffected."""
    first = mirror.list_bundles()
    # Tamper with a returned dict.
    first[0]["slug"] = "tampered"
    first[0]["min_ram_gb"] = -999

    second = mirror.list_bundles()
    second_slugs = {b["slug"] for b in second}
    assert "tampered" not in second_slugs
    assert second_slugs == {"gemma-4-e2b", "gemma-4-e4b"}

    # And the module-level source dict itself is uncorrupted.
    assert set(mirror.BUNDLES.keys()) == {"gemma-4-e2b", "gemma-4-e4b"}
    assert all(b["min_ram_gb"] > 0 for b in mirror.BUNDLES.values())


# ---------------------------------------------------------------------------
# mirror.get_bundle(slug)
# ---------------------------------------------------------------------------

def test_get_bundle_returns_known():
    """``mirror.get_bundle()`` for a known slug → a dict carrying that slug and
    the full download/metadata field set."""
    bundle = mirror.get_bundle("gemma-4-e2b")
    assert isinstance(bundle, dict)
    assert bundle["slug"] == "gemma-4-e2b"
    required = {
        "slug", "display_name", "hf_repo", "filename",
        "size_bytes", "sha256", "min_ram_gb", "recommended_for",
    }
    assert required.issubset(bundle.keys()), f"missing fields: {required - bundle.keys()}"


def test_get_bundle_unknown_returns_none():
    """``mirror.get_bundle()`` for an unknown slug → None (no KeyError)."""
    assert mirror.get_bundle("does-not-exist") is None


def test_get_bundle_is_isolated():
    """``mirror.get_bundle()`` returns a copy — mutating it must NOT corrupt the
    module-level ``BUNDLES``; a second call for the same slug is unaffected."""
    original_filename = mirror.BUNDLES["gemma-4-e2b"]["filename"]

    first = mirror.get_bundle("gemma-4-e2b")
    # Tamper with the returned dict.
    first["filename"] = "tampered"

    second = mirror.get_bundle("gemma-4-e2b")
    assert second["filename"] == original_filename
    assert mirror.BUNDLES["gemma-4-e2b"]["filename"] == original_filename


# ---------------------------------------------------------------------------
# Task 1.2 — fetch-once + ranged download
#
# Hermetic: every download test monkeypatches mirror.MIRROR_DIR to a tmp dir
# AND monkeypatches mirror._download_bundle with a fake that writes a small
# known fixture to the dest, so NO real Hugging Face request is ever made.
# ---------------------------------------------------------------------------

# Known fixture bytes the fake _download_bundle writes (29 bytes).
FIXTURE_BYTES = b"GEMMA-BUNDLE-BYTES-0123456789"


@pytest.fixture
def fake_mirror(monkeypatch, tmp_path):
    """Point mirror.MIRROR_DIR at a tmp dir and replace the network fetch with a
    fake that writes FIXTURE_BYTES to the destination. Returns the tmp dir."""
    monkeypatch.setattr(mirror, "MIRROR_DIR", tmp_path)

    def _fake_download(bundle, dest_path):
        dest_path.write_bytes(FIXTURE_BYTES)

    monkeypatch.setattr(mirror, "_download_bundle", _fake_download)
    return tmp_path


# --- GET /local/models/download/{slug} -------------------------------------

def test_download_full_returns_200_and_bytes(client, fake_mirror):
    """GET with no Range header → 200, whole file, Accept-Ranges advertised."""
    resp = client.get("/local/models/download/gemma-4-e2b")
    assert resp.status_code == 200
    assert resp.content == FIXTURE_BYTES
    assert resp.headers.get("accept-ranges") == "bytes"


def test_download_range_returns_206_partial(client, fake_mirror):
    """GET with Range: bytes=0-9 → 206, first 10 bytes, correct Content-Range."""
    resp = client.get(
        "/local/models/download/gemma-4-e2b",
        headers={"Range": "bytes=0-9"},
    )
    assert resp.status_code == 206
    assert resp.content == FIXTURE_BYTES[0:10]
    total = len(FIXTURE_BYTES)
    assert resp.headers.get("content-range") == f"bytes 0-9/{total}"
    assert resp.headers.get("accept-ranges") == "bytes"


def test_download_range_mid_slice(client, fake_mirror):
    """GET with Range: bytes=5-9 → 206, bytes[5:10]."""
    resp = client.get(
        "/local/models/download/gemma-4-e2b",
        headers={"Range": "bytes=5-9"},
    )
    assert resp.status_code == 206
    assert resp.content == FIXTURE_BYTES[5:10]
    total = len(FIXTURE_BYTES)
    assert resp.headers.get("content-range") == f"bytes 5-9/{total}"


def test_download_unknown_slug_404(client, fake_mirror):
    """GET an unknown slug → 404 with a clear error (no fetch attempted)."""
    resp = client.get("/local/models/download/nope")
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert "nope" in body["error"]


def test_download_malformed_range_400(client, fake_mirror):
    """GET with a malformed Range (``bytes=abc``) → 400.

    FileResponse delegates Range parsing to Starlette, which rejects a
    syntactically-invalid range with 400 Bad Request (verified empirically
    against Starlette 0.48.0). Pinned to that actual behaviour.
    """
    resp = client.get(
        "/local/models/download/gemma-4-e2b",
        headers={"Range": "bytes=abc"},
    )
    assert resp.status_code == 400


def test_download_unsatisfiable_range_416(client, fake_mirror):
    """GET with a Range past EOF (fixture is 29 bytes; ``bytes=1000-2000``) → 416.

    Starlette returns 416 Range Not Satisfiable with ``Content-Range: */<size>``
    for a syntactically-valid but out-of-bounds range (verified empirically
    against Starlette 0.48.0).
    """
    resp = client.get(
        "/local/models/download/gemma-4-e2b",
        headers={"Range": "bytes=1000-2000"},
    )
    assert resp.status_code == 416
    # RFC 7233: an unsatisfiable response carries Content-Range: */<total>.
    assert resp.headers.get("content-range") == f"*/{len(FIXTURE_BYTES)}"


# --- mirror.ensure_present -------------------------------------------------

def test_ensure_present_fetches_once(monkeypatch, tmp_path):
    """ensure_present downloads on the first call and is a pure cache hit on the
    second — the network fetch runs exactly ONCE."""
    monkeypatch.setattr(mirror, "MIRROR_DIR", tmp_path)

    calls = {"n": 0}

    def _counting_download(bundle, dest_path):
        calls["n"] += 1
        dest_path.write_bytes(FIXTURE_BYTES)

    monkeypatch.setattr(mirror, "_download_bundle", _counting_download)

    path1 = mirror.ensure_present("gemma-4-e2b")
    assert path1.exists()
    assert path1.read_bytes() == FIXTURE_BYTES
    assert calls["n"] == 1

    path2 = mirror.ensure_present("gemma-4-e2b")
    assert path2 == path1
    assert path2.read_bytes() == FIXTURE_BYTES
    assert calls["n"] == 1  # second call is a cache hit — no re-download


def test_ensure_present_unknown_slug_raises(monkeypatch, tmp_path):
    """ensure_present on an unknown slug raises (no silent download)."""
    monkeypatch.setattr(mirror, "MIRROR_DIR", tmp_path)
    with pytest.raises((KeyError, ValueError)):
        mirror.ensure_present("does-not-exist")


def test_ensure_present_cleans_up_partial_on_failure(monkeypatch, tmp_path):
    """A mid-download failure must NOT cache a truncated bundle: the final
    target must not exist and no ``.part`` temp file may be left behind. A
    subsequent successful fetch then works (a failed attempt didn't poison the
    cache)."""
    monkeypatch.setattr(mirror, "MIRROR_DIR", tmp_path)

    bundle = mirror.get_bundle("gemma-4-e2b")
    target = tmp_path / bundle["filename"]

    def _partial_then_fail(bundle, dest_path):
        # Write partial bytes to the temp dest, then blow up mid-stream.
        dest_path.write_bytes(b"PARTIAL")
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(mirror, "_download_bundle", _partial_then_fail)

    with pytest.raises(RuntimeError):
        mirror.ensure_present("gemma-4-e2b")

    # No truncated bundle cached, and no leftover .part temp file.
    assert not target.exists()
    assert list(tmp_path.glob("*.part")) == []

    # A clean retry succeeds — the earlier failure did not poison the cache.
    def _working_download(bundle, dest_path):
        dest_path.write_bytes(FIXTURE_BYTES)

    monkeypatch.setattr(mirror, "_download_bundle", _working_download)
    path = mirror.ensure_present("gemma-4-e2b")
    assert path.exists()
    assert path.read_bytes() == FIXTURE_BYTES
    assert list(tmp_path.glob("*.part")) == []


def test_ensure_present_verifies_sha256_when_set(monkeypatch, tmp_path):
    """When the bundle carries a sha256, ensure_present verifies the downloaded
    bytes against it: a mismatch raises ValueError and leaves NO cached target.
    A correct sha256 succeeds."""
    monkeypatch.setattr(mirror, "MIRROR_DIR", tmp_path)

    bundle = mirror.get_bundle("gemma-4-e2b")
    target = tmp_path / bundle["filename"]

    def _working_download(bundle, dest_path):
        dest_path.write_bytes(FIXTURE_BYTES)

    monkeypatch.setattr(mirror, "_download_bundle", _working_download)

    # --- wrong hash → ValueError + nothing cached ---
    monkeypatch.setitem(mirror.BUNDLES["gemma-4-e2b"], "sha256", "deadbeef" * 8)
    with pytest.raises(ValueError):
        mirror.ensure_present("gemma-4-e2b")
    assert not target.exists()
    assert list(tmp_path.glob("*.part")) == []

    # --- correct hash → success ---
    correct = hashlib.sha256(FIXTURE_BYTES).hexdigest()
    monkeypatch.setitem(mirror.BUNDLES["gemma-4-e2b"], "sha256", correct)
    path = mirror.ensure_present("gemma-4-e2b")
    assert path.exists()
    assert path.read_bytes() == FIXTURE_BYTES


# --- mirror.bundle_sha256 --------------------------------------------------

def test_bundle_sha256(tmp_path):
    """bundle_sha256 streams the file and matches hashlib's digest."""
    p = tmp_path / "blob.bin"
    p.write_bytes(FIXTURE_BYTES)
    expected = hashlib.sha256(FIXTURE_BYTES).hexdigest()
    assert mirror.bundle_sha256(p) == expected


# --- slug parity (deferred from Task 1.1 review) ---------------------------

def test_slug_parity():
    """The mirror catalog (BUNDLES) and the picker catalog (LOCAL_MODELS) MUST
    carry identical slug sets — the download path makes this coupling
    load-bearing (a downloaded bundle must map to a picker entry)."""
    from Orchestrator.routes.local_routes import LOCAL_MODELS

    bundle_slugs = set(mirror.BUNDLES.keys())
    picker_ids = {m["id"] for m in LOCAL_MODELS}
    assert bundle_slugs == picker_ids
