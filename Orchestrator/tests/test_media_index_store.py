"""Manifest/media_index.json: a torn write or concurrent adds must never
reset the media catalog (audit O05-ledger-persistence-12)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from Orchestrator import media_index


@pytest.fixture
def index_file(isolated_stores, monkeypatch):
    uploads = isolated_stores.root / "Portal" / "uploads"
    monkeypatch.setattr(media_index, "UPLOADS_DIR", uploads)
    return Path(media_index.MEDIA_INDEX_FILE), uploads


def test_corrupt_index_is_quarantined_and_rebuilt_from_uploads_not_reset(index_file):
    path, uploads = index_file
    (uploads / "sunset-over-hills_abcdef12.png").write_bytes(b"png")
    media_index.add_media_entry("/ui/uploads/sunset-over-hills_abcdef12.png", "image",
                                prompt="a sunset over hills", task_id="abcdef12")
    path.write_text(path.read_text()[:40])          # a torn write

    media_index.add_media_entry("/ui/uploads/new.mp3", "audio", prompt="new song")

    index = json.loads(path.read_text())
    assert "/ui/uploads/new.mp3" in index
    # The file still on disk is back in the catalog, not silently dropped.
    assert "/ui/uploads/sunset-over-hills_abcdef12.png" in index
    (moved,) = sorted(path.parent.glob("media_index.json.corrupt-*"))
    assert len(moved.read_text()) == 40


def test_concurrent_adds_keep_every_entry(index_file):
    path, _ = index_file

    def worker(n):
        for i in range(15):
            media_index.add_media_entry(f"/ui/uploads/t{n}-{i}.png", "image", file_size=1)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(json.loads(path.read_text())) == 90


def test_failed_save_leaves_the_previous_index(index_file):
    path, _ = index_file
    media_index.add_media_entry("/ui/uploads/a.png", "image", file_size=1)
    good = path.read_text()
    # Indexing is best-effort (the media itself is already saved), so the
    # add logs instead of raising; the catalog on disk must be untouched.
    media_index.add_media_entry("/ui/uploads/b.png", "image", file_size=1,
                                extra_metadata={"bad": object()})
    assert path.read_text() == good


def test_readers_never_rewrite_and_survive_a_corrupt_index(index_file):
    path, _ = index_file
    path.write_text("{oops")
    assert media_index.list_media() == []
    assert media_index.get_index_stats()["total_files"] == 0
    assert list(path.parent.glob("media_index.json.corrupt-*"))
