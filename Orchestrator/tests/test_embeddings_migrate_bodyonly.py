"""Migrate/rebuild can build a BODY-mode chunk store (M14.3d).

run_rebuild(slug, content_mode="body") builds a schema-2 candidate whose chunk
vectors are envelope-free (extract_snapshot_content) AND stamps
content_mode="body" on the new store's meta. Mint and migrate produce IDENTICAL
chunks for the same (text, mode) because both go through the ONE shared helper
chunker.chunks_for_snapshot — a future edit cannot desync new-mint from
re-embedded snapshots. Default "full" leaves the rebuild byte-identical to today.

Isolation recipe mirrors test_embeddings_migrate_v2.py: tmp_path index/stores/
volume, faked provider, migrate singletons reset. BUILD-ONLY: never cuts over.
"""
import asyncio
import json
import threading

import numpy as np

from Orchestrator import config, fossils
from Orchestrator.embeddings import migrate, ollama_io, providers, search
from Orchestrator.embeddings.chunker import chunk_snapshot, chunks_for_snapshot
from Orchestrator.embeddings.registry import EMBEDDING_MODELS

TARGET = "qwen3-embedding-0.6b"
TARGET_DIMS = EMBEDDING_MODELS[TARGET]["dims"]

_ENVELOPE = (
    "=== START SNAPSHOT — UTC 2026-07-04T22:37:21Z — SNAP-{i} (7.1.0) ===\n"
    "CROSS-FILE BEACON\nTail lock confirmed\n"
    "VOLUME TRACKER\nTail: SNAP-x\nGAUGES\nOPERATOR: Anna\n\n"
    "SNAPSHOT BODY\n\nKernel Index\n- Current: SNAP-{i}\n\nRaw Session Log\n"
)


def _envelope_body(i: int, n_lines: int = 400) -> str:
    body = "\n".join(
        f"snap{i:02d} line {j:05d} " + "abcdefghij" * 3 for j in range(n_lines)
    )
    return _ENVELOPE.format(i=i) + body + f"\n=== END SNAPSHOT — SNAP-{i} ===\n"


class FakeProvider:
    def __init__(self, dims):
        self.dims = dims
        self.calls = []

    @property
    def embedded_texts(self):
        return [t for texts, _ in self.calls for t in texts]

    async def embed(self, texts, purpose):
        self.calls.append((list(texts), purpose))
        return [self._vec(t) for t in texts]

    def _vec(self, text):
        rng = np.random.default_rng(sum(text.encode()) % (2**32))
        return [float(x) for x in rng.standard_normal(self.dims)]


def _build_volume(index_path, volume_path, n=3):
    index, blob = {}, b""
    for i in range(n):
        sid = f"SNAP-{i}"
        raw = _envelope_body(i).encode("utf-8")
        index[sid] = {
            "byte_start": len(blob), "byte_end": len(blob) + len(raw),
            "operator": "Brandon", "timestamp": "2026-07-01T00:00:00Z",
            "type": "normal",
        }
        blob += raw
    volume_path.write_bytes(blob)
    index_path.write_text(json.dumps(index), encoding="utf-8")
    fossils._index_cache = None


def _env(tmp_path, monkeypatch):
    index_path = tmp_path / "snapshot_index.json"
    stores_dir = tmp_path / "embeddings"
    volume_path = tmp_path / "volume.txt"
    monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", index_path)
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)
    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(stores_dir))
    monkeypatch.setattr(config, "VOL_PATH", volume_path)
    monkeypatch.setattr(migrate, "_JOB", None)
    monkeypatch.setattr(migrate, "_JOB_TASK", None)
    monkeypatch.setattr(migrate, "_CANCEL", threading.Event())
    monkeypatch.setattr(migrate, "BATCH_SLEEP_S", 0.0)
    monkeypatch.setattr(search, "_active_store", None)
    fake = FakeProvider(TARGET_DIMS)
    monkeypatch.setitem(providers._instances, TARGET, fake)
    _build_volume(index_path, volume_path)
    return index_path, stores_dir, volume_path, fake


def _build_store_dir(stores_dir):
    return stores_dir / migrate.BUILD_DIR_NAME / TARGET


def test_body_mode_rebuild_stamps_meta_and_drops_envelope(tmp_path, monkeypatch):
    _, stores_dir, _, fake = _env(tmp_path, monkeypatch)

    result = asyncio.run(migrate.run_rebuild(TARGET, content_mode="body"))
    assert result["state"] == "done"

    meta = json.loads(
        (_build_store_dir(stores_dir) / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["schema"] == 2
    assert meta["content_mode"] == "body"

    # Every embedded text is envelope-free (body-only ranking vectors).
    assert fake.embedded_texts
    for text in fake.embedded_texts:
        assert "CROSS-FILE BEACON" not in text
        assert "START SNAPSHOT" not in text
        assert "VOLUME TRACKER" not in text
        assert "GAUGES" not in text


def test_full_mode_rebuild_unchanged_keeps_envelope(tmp_path, monkeypatch):
    _, stores_dir, _, fake = _env(tmp_path, monkeypatch)

    result = asyncio.run(migrate.run_rebuild(TARGET))  # default content_mode
    assert result["state"] == "done"

    meta = json.loads(
        (_build_store_dir(stores_dir) / "meta.json").read_text(encoding="utf-8")
    )
    assert meta.get("content_mode", "full") == "full"
    # Full-mode still embeds the whole envelope-inclusive text (ordinal 0).
    assert any("CROSS-FILE BEACON" in t for t in fake.embedded_texts)


def test_chunk_group_batches_matches_mint_helper_body_mode():
    """Parity guard: chunk_group_batches(body) produces EXACTLY the chunks
    chunks_for_snapshot(body) yields — the shared helper makes mint == migrate."""
    text = _envelope_body(0)
    batches, empty = migrate.chunk_group_batches(
        [("SNAP-0", text)], TARGET, content_mode="body"
    )
    assert empty == []
    # Flatten migrate's chunks for SNAP-0.
    migrate_chunks = [c for batch in batches for _, chunks in batch for c in chunks]
    expected = chunks_for_snapshot(text, model_key=TARGET, content_mode="body")
    assert migrate_chunks == expected
    # And it is genuinely the body cut (multi-chunk, envelope-free).
    body_chunks = chunk_snapshot(fossils.extract_snapshot_content(text), TARGET)
    assert expected == [fossils.extract_snapshot_content(text)] + body_chunks
