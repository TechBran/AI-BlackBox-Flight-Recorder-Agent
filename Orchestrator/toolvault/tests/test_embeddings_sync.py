"""Tests for the ToolVault v2 hash-keyed embedding sync (Task 2.1).

The sync re-embeds a tool's DESCRIPTION only when its sha256 hash changes,
keeping ``ToolVault/embeddings.json`` as the only cached artifact. Stale
tools (no longer in the canonical list) are pruned.

Tests are hermetic: ``embeddings.embed_tool_description`` is monkeypatched to
a deterministic fake (never hits the network), and the store lives in
``tmp_path`` via ``embeddings.EMBEDDINGS_PATH``.
"""

import json

import pytest

from Orchestrator.toolvault import embeddings
from Orchestrator.toolvault.config import EMBEDDING_MODEL


FAKE_VECTOR = [0.1, 0.2, 0.3]


def _canonical(n):
    """Build a canonical list of n tools with distinct descriptions."""
    return [
        {"name": f"tool_{i}", "description": f"description for tool {i}"}
        for i in range(n)
    ]


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "embeddings.json"


@pytest.fixture
def patched_embed(monkeypatch):
    """Monkeypatch embed_tool_description with a call counter."""
    calls = {"count": 0, "texts": []}

    def fake_embed(text):
        calls["count"] += 1
        calls["texts"].append(text)
        return list(FAKE_VECTOR)

    monkeypatch.setattr(embeddings, "embed_tool_description", fake_embed)
    return calls


# ---------------------------------------------------------------------------
# Store load/save
# ---------------------------------------------------------------------------

def test_load_missing_file_returns_empty(store_path):
    assert embeddings.load_embeddings_store(store_path) == {}


def test_load_corrupt_json_returns_empty(store_path):
    store_path.write_text("{ this is not valid json ]")
    assert embeddings.load_embeddings_store(store_path) == {}


def test_save_then_load_roundtrip(store_path):
    store = {"tool_0": {"hash": "abc", "model": EMBEDDING_MODEL, "vector": [1.0]}}
    embeddings.save_embeddings_store(store, store_path)
    assert embeddings.load_embeddings_store(store_path) == store


def test_emb_hash_stable_and_text_sensitive():
    h1 = embeddings._emb_hash("hello")
    h2 = embeddings._emb_hash("hello")
    h3 = embeddings._emb_hash("world")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# sync_embeddings
# ---------------------------------------------------------------------------

def test_first_sync_embeds_all(store_path, patched_embed):
    canon = _canonical(3)
    store = embeddings.sync_embeddings(canon, store_path)

    assert patched_embed["count"] == 3
    assert set(store.keys()) == {"tool_0", "tool_1", "tool_2"}
    for name, entry in store.items():
        assert entry["hash"] == embeddings._emb_hash(
            next(t["description"] for t in canon if t["name"] == name)
        )
        assert entry["model"] == EMBEDDING_MODEL
        assert entry["vector"] == FAKE_VECTOR

    # Persisted to disk
    on_disk = json.loads(store_path.read_text())
    assert set(on_disk.keys()) == {"tool_0", "tool_1", "tool_2"}


def test_second_sync_no_reembed(store_path, patched_embed):
    canon = _canonical(3)
    embeddings.sync_embeddings(canon, store_path)
    assert patched_embed["count"] == 3

    store = embeddings.sync_embeddings(canon, store_path)
    assert patched_embed["count"] == 3  # no additional embeds
    assert set(store.keys()) == {"tool_0", "tool_1", "tool_2"}


def test_changed_description_reembeds_only_one(store_path, patched_embed):
    canon = _canonical(3)
    embeddings.sync_embeddings(canon, store_path)
    assert patched_embed["count"] == 3

    canon[1]["description"] = "a brand new description"
    store = embeddings.sync_embeddings(canon, store_path)

    assert patched_embed["count"] == 4  # exactly one more
    assert store["tool_1"]["hash"] == embeddings._emb_hash("a brand new description")
    # Last embed call was for the changed tool only
    assert patched_embed["texts"][-1] == "a brand new description"


def test_force_reembeds_all(store_path, patched_embed):
    canon = _canonical(3)
    embeddings.sync_embeddings(canon, store_path)
    assert patched_embed["count"] == 3

    embeddings.sync_embeddings(canon, store_path, force=True)
    assert patched_embed["count"] == 6  # all 3 again


def test_prune_removes_stale(store_path, patched_embed):
    canon = _canonical(3)
    embeddings.sync_embeddings(canon, store_path)

    # Drop tool_2
    canon = canon[:2]
    store = embeddings.sync_embeddings(canon, store_path)

    assert set(store.keys()) == {"tool_0", "tool_1"}
    on_disk = json.loads(store_path.read_text())
    assert "tool_2" not in on_disk


def test_embed_failure_keeps_prior_entry(store_path, monkeypatch):
    canon = _canonical(2)

    # First sync: both succeed.
    monkeypatch.setattr(embeddings, "embed_tool_description", lambda t: list(FAKE_VECTOR))
    embeddings.sync_embeddings(canon, store_path)

    # Change tool_0's description, but embed now fails for the changed one.
    canon[0]["description"] = "changed description that fails"

    def flaky_embed(text):
        if text == "changed description that fails":
            return None
        return list(FAKE_VECTOR)

    monkeypatch.setattr(embeddings, "embed_tool_description", flaky_embed)
    store = embeddings.sync_embeddings(canon, store_path)

    # tool_0 keeps its prior (old) entry intact — no crash.
    assert store["tool_0"]["vector"] == FAKE_VECTOR
    assert store["tool_0"]["hash"] == embeddings._emb_hash("description for tool 0")
    assert store["tool_1"]["vector"] == FAKE_VECTOR


def test_embed_failure_new_tool_skipped(store_path, monkeypatch):
    canon = _canonical(1)
    monkeypatch.setattr(embeddings, "embed_tool_description", lambda t: None)
    store = embeddings.sync_embeddings(canon, store_path)
    # New tool that failed to embed is simply absent — no crash.
    assert "tool_0" not in store
