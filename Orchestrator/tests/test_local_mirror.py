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

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from Orchestrator.local_provider import mirror


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
    """Each bundle carries the full download/metadata field set."""
    resp = client.get("/local/models/catalog")
    assert resp.status_code == 200
    required = {
        "slug", "display_name", "hf_repo", "filename",
        "size_bytes", "sha256", "min_ram_gb", "recommended_for",
    }
    for b in resp.json()["bundles"]:
        assert required.issubset(b.keys()), f"missing fields: {required - b.keys()}"


def test_catalog_ram_ordering(client):
    """Sanity: the lighter E2B model needs less RAM than the heavier E4B."""
    resp = client.get("/local/models/catalog")
    assert resp.status_code == 200
    by_slug = {b["slug"]: b for b in resp.json()["bundles"]}
    assert by_slug["gemma-4-e2b"]["min_ram_gb"] < by_slug["gemma-4-e4b"]["min_ram_gb"]


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
