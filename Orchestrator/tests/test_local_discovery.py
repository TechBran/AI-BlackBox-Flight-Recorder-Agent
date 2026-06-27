"""Tests for the on-device (local Gemma) model DISCOVERY + CURATION catalog.

The hub no longer moves model bytes: phones download the Gemma 4 LiteRT
``.litertlm`` bundles DIRECTLY from the Hugging Face CDN. ``catalog.py`` is the
discovery + curation layer — it queries the HF Hub API for ``litert-community``
gemma repos, merges real ``size_bytes`` / ``sha256`` (from ``lfs.oid``) /
``gated`` / ``download_url`` onto a curated config floor, and serves
``GET /local/models/catalog``.

The catalog is DISTINCT from the picker catalog (``LOCAL_MODELS`` in
local_routes): the picker entries are id/name/provider descriptors for the
model selector, whereas these carry DOWNLOAD/METADATA fields (hf_repo,
filename, size_bytes, sha256, min_ram_gb, recommended_for, download_url, gated).
The slugs are kept identical across both so a downloaded bundle maps to a
picker entry.

Hermetic: NO network. The two HF fetchers (``_fetch_hf_models`` /
``_fetch_hf_tree``) are monkeypatched — the module-wide ``autouse`` fixture
below patches both to deterministic E2B/E4B fakes (and invalidates the TTL
cache) so EVERY test, including the ``/local/models/catalog`` endpoint tests
that route through ``build_catalog()``, runs offline. The startup
embedding-sync hook is mocked (it spawns a daemon thread calling
sync_embeddings, which would hit the network).
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from Orchestrator.local_provider import catalog as mirror


# E2B/E4B fakes the autouse fixture feeds the HF fetchers (no real HF call).
_FAKE_HF_MODELS = [
    {"id": "litert-community/gemma-4-E2B-it-litert-lm", "gated": False},
    {"id": "litert-community/gemma-4-E4B-it-litert-lm", "gated": False},
]
_FAKE_HF_TREES = {
    "litert-community/gemma-4-E2B-it-litert-lm": [
        {"path": "gemma-4-E2B-it.litertlm", "size": 2588147712, "lfs": {"oid": "b" * 64}},
    ],
    "litert-community/gemma-4-E4B-it-litert-lm": [
        {"path": "gemma-4-E4B-it.litertlm", "size": 3659530240, "lfs": {"oid": "c" * 64}},
    ],
}


@pytest.fixture(autouse=True)
def patch_hf_fetchers(monkeypatch):
    """Hermetic default for EVERY test in this file: patch the two HF fetchers to
    deterministic E2B/E4B fakes and invalidate the TTL cache, so no test (incl.
    the endpoint tests that route through ``build_catalog()``) ever hits the
    network. Tests needing other shapes simply re-patch + ``_invalidate_cache``."""
    monkeypatch.setattr(mirror, "_fetch_hf_models", lambda: [dict(m) for m in _FAKE_HF_MODELS])
    monkeypatch.setattr(mirror, "_fetch_hf_tree", lambda repo: [dict(f) for f in _FAKE_HF_TREES[repo]])
    mirror._invalidate_cache()
    yield
    mirror._invalidate_cache()


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
        # Direct-from-HF download fields (the phone streams from download_url).
        "download_url", "gated",
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


# ---------------------------------------------------------------------------
# catalog.build_catalog() — HF-enriched curated floor (TTL-cached)
#
# These tests re-patch the HF fetchers (on top of the autouse default) to drive
# specific discovery shapes, then ``_invalidate_cache()`` to force a rebuild.
# ---------------------------------------------------------------------------

def _patch_hf(monkeypatch, models, trees):
    monkeypatch.setattr(mirror, "_fetch_hf_models", lambda: models)
    monkeypatch.setattr(mirror, "_fetch_hf_tree", lambda repo: trees[repo])
    mirror._invalidate_cache()  # force a rebuild


def test_build_catalog_enriches_curated_with_hf_facts(monkeypatch):
    models = [{"id": "litert-community/gemma-4-E2B-it-litert-lm", "gated": False},
              {"id": "litert-community/gemma-4-E4B-it-litert-lm", "gated": False}]
    trees = {
        "litert-community/gemma-4-E2B-it-litert-lm": [
            {"path": "gemma-4-E2B-it.litertlm", "size": 2588147712, "lfs": {"oid": "b" * 64}}],
        "litert-community/gemma-4-E4B-it-litert-lm": [
            {"path": "gemma-4-E4B-it.litertlm", "size": 3659530240, "lfs": {"oid": "c" * 64}}],
    }
    _patch_hf(monkeypatch, models, trees)
    bundles = {b["slug"]: b for b in mirror.list_bundles()}
    e4b = bundles["gemma-4-e4b"]
    assert e4b["size_bytes"] == 3659530240
    assert e4b["sha256"] == "c" * 64                 # pinned from lfs.oid
    assert e4b["gated"] is False
    assert e4b["download_url"].endswith("/gemma-4-E4B-it.litertlm")
    assert e4b["recommended"] is True              # curated config preserved
    assert e4b["max_tokens"] == 16384
    assert e4b["support_image"] is True


def test_build_catalog_adds_uncurated_repo_with_defaults(monkeypatch):
    models = [{"id": "litert-community/gemma-4-12B-it-litert-lm", "gated": False}]
    trees = {"litert-community/gemma-4-12B-it-litert-lm":
             [{"path": "gemma-4-12B-it.litertlm", "size": 8_000_000_000, "lfs": {"oid": "d" * 64}}]}
    _patch_hf(monkeypatch, models, trees)
    bundles = {b["slug"]: b for b in mirror.list_bundles()}
    assert "gemma-4-12b" in bundles
    assert bundles["gemma-4-12b"]["recommended"] is False      # default
    assert bundles["gemma-4-12b"]["min_ram_gb"] > 4.5          # inferred from 8GB size


def test_build_catalog_falls_back_to_curated_when_hf_down(monkeypatch):
    def _boom():
        raise RuntimeError("HF unreachable")
    monkeypatch.setattr(mirror, "_fetch_hf_models", _boom)
    mirror._invalidate_cache()
    bundles = {b["slug"]: b for b in mirror.list_bundles()}
    # The curated floor (E2B/E4B) is still served, with constructible download_url.
    assert {"gemma-4-e2b", "gemma-4-e4b"} <= set(bundles)
    assert bundles["gemma-4-e2b"]["download_url"].endswith(".litertlm")


def test_list_bundles_is_cached(monkeypatch):
    calls = {"n": 0}

    def _models():
        calls["n"] += 1
        return []

    monkeypatch.setattr(mirror, "_fetch_hf_models", _models)
    mirror._invalidate_cache()
    mirror.list_bundles()
    mirror.list_bundles()
    assert calls["n"] == 1  # second call is a cache hit within TTL


# ---------------------------------------------------------------------------
# catalog.get_bundle(slug)
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
        "download_url", "gated",
    }
    assert required.issubset(bundle.keys()), f"missing fields: {required - bundle.keys()}"


def test_get_bundle_unknown_returns_none():
    """``mirror.get_bundle()`` for an unknown slug → None (no KeyError)."""
    assert mirror.get_bundle("does-not-exist") is None


# --- slug parity (deferred from Task 1.1 review) ---------------------------

def test_slug_parity():
    """The discovery catalog (build_catalog) and the picker catalog
    (LOCAL_MODELS) MUST keep their slugs coupled — a downloaded bundle must map
    to a picker entry. Under the autouse E2B/E4B fakes the curated-floor slugs
    equal the picker ids."""
    from Orchestrator.routes.local_routes import LOCAL_MODELS

    bundle_slugs = {b["slug"] for b in mirror.list_bundles()}
    picker_ids = {m["id"] for m in LOCAL_MODELS}
    assert bundle_slugs == picker_ids


# ---------------------------------------------------------------------------
# Task A2 — HF Hub API fetchers
#
# Hermetic: these monkeypatch ``mirror._http_get_json`` so no real Hugging Face
# request ever runs (``mirror`` is the ``catalog`` module — aliased above).
# ---------------------------------------------------------------------------

def test_fetch_hf_models_shape(monkeypatch):
    # _fetch_hf_models returns a list of {"id","gated"} for litert-community gemma repos.
    fake = [
        {"id": "litert-community/gemma-4-E2B-it-litert-lm", "gated": False},
        {"id": "litert-community/gemma-4-E4B-it-litert-lm", "gated": False},
        {"id": "litert-community/not-a-litertlm-repo", "gated": False},
    ]
    monkeypatch.setattr(mirror, "_http_get_json", lambda url, **kw: fake)
    out = mirror._fetch_hf_models()
    ids = {m["id"] for m in out}
    assert "litert-community/gemma-4-E2B-it-litert-lm" in ids


def test_fetch_hf_tree_shape(monkeypatch):
    fake_tree = [
        {"path": "gemma-4-E2B-it-web.litertlm", "size": 2008432640,
         "lfs": {"oid": "a" * 64}},
        {"path": "gemma-4-E2B-it.litertlm", "size": 2588147712,
         "lfs": {"oid": "b" * 64}},
        {"path": "README.md", "size": 100},
    ]
    monkeypatch.setattr(mirror, "_http_get_json", lambda url, **kw: fake_tree)
    files = mirror._fetch_hf_tree("litert-community/gemma-4-E2B-it-litert-lm")
    paths = {f["path"] for f in files}
    assert "gemma-4-E2B-it.litertlm" in paths


# ---------------------------------------------------------------------------
# Task A3 — file selection + slug derivation + download URL helpers
# ---------------------------------------------------------------------------

def test_select_litertlm_prefers_curated_filename():
    tree = [
        {"path": "gemma-4-E2B-it-web.litertlm", "size": 1, "lfs": {"oid": "a" * 64}},
        {"path": "gemma-4-E2B-it.litertlm", "size": 2588147712, "lfs": {"oid": "b" * 64}},
    ]
    f = mirror._select_litertlm(tree, preferred="gemma-4-E2B-it.litertlm")
    assert f["path"] == "gemma-4-E2B-it.litertlm"
    assert f["sha256"] == "b" * 64
    assert f["size_bytes"] == 2588147712


def test_select_litertlm_heuristic_excludes_web():
    tree = [
        {"path": "gemma-4-E4B-it-web.litertlm", "size": 1, "lfs": {"oid": "a" * 64}},
        {"path": "gemma-4-E4B-it.litertlm", "size": 5, "lfs": {"oid": "b" * 64}},
    ]
    f = mirror._select_litertlm(tree, preferred=None)
    assert f["path"] == "gemma-4-E4B-it.litertlm"  # the non-web -it build


def test_slug_for_repo():
    assert mirror._slug_for_repo("litert-community/gemma-4-E2B-it-litert-lm") == "gemma-4-e2b"
    assert mirror._slug_for_repo("litert-community/gemma-4-12B-it-litert-lm") == "gemma-4-12b"


def test_download_url():
    assert mirror._download_url("litert-community/gemma-4-E2B-it-litert-lm",
                                "gemma-4-E2B-it.litertlm") == \
        "https://huggingface.co/litert-community/gemma-4-E2B-it-litert-lm/resolve/main/gemma-4-E2B-it.litertlm"
