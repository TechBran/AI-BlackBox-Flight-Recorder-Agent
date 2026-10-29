"""The shared ledger reader (audit rank 77: O04-tasks-retrieval-02,
O05-ledger-persistence-18).

The ledger is append-only, so a memory map of its first N bytes stays exact
for as long as the file holds N bytes. Orchestrator/ledger.py keeps one map
per ledger file keyed by its (device, inode, size, mtime), and every reader
on the hot paths slices snapshots out of it: the per-turn sections, the
keyword scan, the Timeline, the rerank passages, the mint's tail read and the
embedding passes. These tests run on ledgers written under tmp_path.
"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import threading
import tracemalloc
from pathlib import Path

import pytest

from Orchestrator import ledger, volume


def _snap(n: int, body: str = "", op: str = "Ana", nl: str = "\n", version: str = "7.1.0") -> str:
    sid = f"SNAP-20261001-{n}"
    utc = f"2026-10-01T00:{n % 60:02d}:00Z"
    return (f"=== START SNAPSHOT — UTC {utc} — {sid} ({version}) ==={nl}"
            f"OPERATOR: {op}{nl}Raw Session Log{nl}- [1] {utc} operator={op} user: "
            f"{body or f'turn {n} déjà vu 🙂'}{nl}"
            f"=== END SNAPSHOT — {sid} — UTC {utc} ==={nl}")


# -- views: exact ranges, safe across a mint, one map per file version -----------

def test_a_view_reads_exactly_the_files_bytes(tmp_path):
    p = tmp_path / "SNAPSHOT_VOLUME.txt"
    data = "".join(_snap(n) for n in range(1, 30)).encode()
    p.write_bytes(data)
    v = ledger.view(p)
    assert v.size == len(data) and len(v) == len(data)
    for a, b in ((0, 10), (5, 777), (len(data) - 9, len(data)), (100, 100), (50, 10)):
        assert v.read(a, b) == data[a:b] == bytes(v.slice(a, b)) == v[a:b]
    assert v.read(-5, 3) == data[:3] and v.read(len(data) - 2, len(data) + 50) == data[-2:]
    assert v.text(0, len(data)) == data.decode()
    assert bytes(v.buffer()) == data


def test_a_missing_or_empty_ledger_reads_as_empty(tmp_path):
    missing = ledger.view(tmp_path / "nope" / "SNAPSHOT_VOLUME.txt")
    assert missing.size == 0 and missing.read(0, 10) == b"" and missing.buffer() == b""
    empty = tmp_path / "SNAPSHOT_VOLUME.txt"
    empty.write_bytes(b"")
    assert ledger.view(empty).size == 0 and ledger.view(empty)[0:5] == b""


def test_a_view_taken_before_a_mint_keeps_its_bytes_and_a_new_view_sees_the_append(tmp_path):
    p = tmp_path / "SNAPSHOT_VOLUME.txt"
    p.write_bytes(_snap(1).encode())
    before = ledger.view(p)
    size = before.size
    with open(p, "ab") as f:                       # a mint appends
        f.write(_snap(2).encode())
    assert before.size == size and before.read(0, size + 10_000) == _snap(1).encode()
    after = ledger.view(p)
    assert after is not before and after.size == size + len(_snap(2).encode())
    assert after.read(size, after.size) == _snap(2).encode()


def test_an_unchanged_ledger_is_mapped_once(tmp_path, monkeypatch):
    p = tmp_path / "SNAPSHOT_VOLUME.txt"
    p.write_bytes(_snap(1).encode())
    first = ledger.view(p)
    opens = []
    real_map = ledger._map
    monkeypatch.setattr(ledger, "_map", lambda path: opens.append(path) or real_map(path))
    assert all(ledger.view(p) is first for _ in range(50))
    assert opens == []                              # 50 looks, no open, no map
    os.replace(_write(tmp_path / "fresh.txt", _snap(9)), p)
    assert ledger.view(p).read(0, 200) == _snap(9).encode()[:200] and len(opens) == 1


def _write(p: Path, text: str) -> Path:
    p.write_text(text, encoding="utf-8")
    return p


def test_reads_racing_appends_are_never_torn(tmp_path):
    """Threads read every snapshot of the view they took while another thread
    keeps appending: each read is exactly the bytes that were there."""
    p = tmp_path / "SNAPSHOT_VOLUME.txt"
    blocks = [_snap(n, body="x" * (50 + n % 400)).encode() for n in range(1, 400)]
    p.write_bytes(blocks[0])
    stop, errors = threading.Event(), []

    def writer():
        for b in blocks[1:]:
            with open(p, "ab") as f:
                f.write(b)
        stop.set()

    def reader():
        while not stop.is_set():
            v = ledger.view(p)
            pos = 0
            for b in blocks:
                if pos + len(b) > v.size:
                    break
                if v.read(pos, pos + len(b)) != b:
                    errors.append(pos)
                pos += len(b)

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert ledger.view(p).size == sum(map(len, blocks))


def test_a_shrunk_ledger_is_remapped_and_reported(tmp_path, capsys):
    p = tmp_path / "SNAPSHOT_VOLUME.txt"
    p.write_bytes((_snap(1) + _snap(2)).encode())
    ledger.view(p)
    p.write_bytes(_snap(1).encode())               # never done to a real ledger
    v = ledger.view(p)
    assert v.size == len(_snap(1).encode()) and v.read(0, v.size) == _snap(1).encode()
    assert "shrank" in capsys.readouterr().out


# -- the tail a mint reads -----------------------------------------------------------

def _tail_cases():
    big = "y" * 300_000                             # past the first 64 KB window
    return {
        "plain": "".join(_snap(n) for n in range(1, 50)),
        "crlf": "".join(_snap(n, nl="\r\n") for n in range(1, 50)),
        "one": _snap(1),
        "big_last": "".join(_snap(n) for n in range(1, 5)) + _snap(5, body=big),
        "big_middle": _snap(1, body=big) + _snap(2),
        "torn_start_after_end": "".join(_snap(n) for n in range(1, 9)) + _snap(9)[:60],
        "junk_after_end": "".join(_snap(n) for n in range(1, 9)) + "garbage line\n" * 9000,
        "quoted_end_in_last": "".join(_snap(n) for n in range(1, 4)) + _snap(
            4, body="see\n=== END SNAPSHOT — SNAP-20261001-2 — UTC 2026-10-01T00:02:00Z ===\nok"),
        "multibyte_everywhere": "".join(_snap(n, body="🙂é" * (n * 700)) for n in range(1, 12)),
        "no_end": "header line\n" * 50,
        "empty": "",
        "blank_lines_before_end": "".join(_snap(n) for n in range(1, 4)).replace(
            "=== END SNAPSHOT — SNAP-20261001-3", "\n\n   \n=== END SNAPSHOT — SNAP-20261001-3"),
    }


@pytest.mark.parametrize("name", sorted(_tail_cases()))
def test_the_tail_answers_exactly_what_the_whole_ledger_does(tmp_path, name):
    p = tmp_path / "SNAPSHOT_VOLUME.txt"
    p.write_bytes(_tail_cases()[name].encode())
    whole = volume.read_text_safe(p)
    assert volume.parse_tail(volume.read_tail_text(p)) == volume.parse_tail(whole)


def test_a_torn_tail_still_raises(tmp_path):
    """An END whose START is gone: the whole ledger is read, and parse_tail
    raises as it did."""
    p = tmp_path / "SNAPSHOT_VOLUME.txt"
    p.write_bytes(("x" * 200_000 + "\n=== END SNAPSHOT — SNAP-20261001-7 — UTC "
                   "2026-10-01T00:07:00Z ===\n").encode())
    with pytest.raises(volume.LedgerError, match="PARTIAL-SNAPSHOT"):
        volume.parse_tail(volume.read_tail_text(p))


def test_the_tail_reads_a_few_kb_of_a_large_ledger(tmp_path, monkeypatch):
    p = tmp_path / "SNAPSHOT_VOLUME.txt"
    p.write_bytes("".join(_snap(n, body="history " * 500) for n in range(1, 2000)).encode())
    served = []
    real = ledger.LedgerView.read
    monkeypatch.setattr(ledger.LedgerView, "read",
                        lambda self, a, b: served.append(b - a) or real(self, a, b))
    assert volume.parse_tail(volume.read_tail_text(p))["tail_id"] == "SNAP-20261001-1999"
    assert sum(served) <= volume._TAIL_WINDOW < p.stat().st_size // 50


# -- the prefix hash every append records -------------------------------------------

def test_the_prefix_hash_hashes_only_the_appended_bytes(tmp_path, monkeypatch):
    p = tmp_path / "SNAPSHOT_VOLUME.txt"
    h = ledger.PrefixHasher()
    data = b""
    fed = []
    real_slice = ledger.LedgerView.slice
    monkeypatch.setattr(ledger.LedgerView, "slice",
                        lambda self, a, b: fed.append(b - a) or real_slice(self, a, b))
    for n in range(1, 30):
        data += _snap(n).encode()
        p.write_bytes(data)
        assert h.digest_upto(ledger.view(p), len(data)) == hashlib.sha256(data).hexdigest()
    assert sum(fed) == len(data)                   # every byte hashed once
    # Asked for an older prefix, or a replaced file: right, from byte 0.
    assert h.digest_upto(ledger.view(p), 100) == hashlib.sha256(data[:100]).hexdigest()
    os.replace(_write(tmp_path / "other.txt", _snap(77)), p)
    assert h.digest_upto(ledger.view(p), 50) == hashlib.sha256(_snap(77).encode()[:50]).hexdigest()
    with pytest.raises(ValueError):
        h.digest_upto(ledger.view(p), 10 ** 9)


@pytest.fixture
def ledger_box(tmp_path, monkeypatch):
    vol = tmp_path / "Volumes" / "SNAPSHOT_VOLUME.txt"
    vol.parent.mkdir()
    man = tmp_path / "Manifest" / "manifest.json"
    man.parent.mkdir()
    man.write_text(json.dumps({"archive": []}))
    arc = tmp_path / "Archive"
    arc.mkdir()
    for name, value in (("VOL_PATH", vol), ("MANIFEST", man), ("ARC_DIR", arc)):
        monkeypatch.setattr(volume, name, value)
    monkeypatch.setattr(volume, "_latest_copy", None)
    monkeypatch.setattr(volume, "_latest_published", None)
    monkeypatch.setattr(volume, "_PREFIX", ledger.PrefixHasher())
    monkeypatch.setattr(volume, "archive_min_free_gb", lambda: 0.0)
    return type("LedgerBox", (), {"vol": vol, "man": man, "arc": arc})


def _append(box, n):
    start = box.vol.stat().st_size if box.vol.exists() else 0
    volume.append_snapshot_text(_snap(n))
    return start, box.vol.stat().st_size


def _records():
    return [json.loads(x) for x in volume.ledger_hashes_path().read_text().splitlines()]


def test_each_append_records_the_hash_of_the_ledger_up_to_its_end(ledger_box):
    for n in range(1, 6):
        start, end = _append(ledger_box, n)
        rec = volume.record_append(f"SNAP-20261001-{n}", start, end, "2026-10-01T00:00:00Z")
        assert rec["sha256"] == hashlib.sha256(ledger_box.vol.read_bytes()).hexdigest()
    recs = _records()
    assert [r["byte_start"] for r in recs] == [0] + [r["byte_end"] for r in recs[:-1]]
    assert set(recs[0]) == {"snap_id", "byte_start", "byte_end", "sha256", "utc"}


def test_nothing_appended_records_nothing(ledger_box):
    assert volume.record_append("SNAP-20261001-1", 0, 0) is None       # no ledger
    start, end = _append(ledger_box, 1)
    assert volume.record_append("SNAP-20261001-1", end, end) is None   # no growth
    assert not volume.ledger_hashes_path().exists()
    with pytest.raises(volume.LedgerError, match="RECORD-PAST-EOF"):
        volume.record_append("SNAP-20261001-1", 0, end + 10)


def test_a_torn_record_line_never_swallows_the_next_record(ledger_box):
    start, end = _append(ledger_box, 1)
    volume.record_append("SNAP-20261001-1", start, end)
    with open(volume.ledger_hashes_path(), "ab") as f:
        f.write(b'{"snap_id": "SNAP-2026')            # a crash mid-write
    start, end = _append(ledger_box, 2)
    volume.record_append("SNAP-20261001-2", start, end)
    lines = volume.ledger_hashes_path().read_text().splitlines()
    assert json.loads(lines[-1])["snap_id"] == "SNAP-20261001-2" and len(lines) == 3


def test_publish_latest_moves_forward_only_and_writes_iso_time(ledger_box):
    recs = []
    for n in (1, 2):
        start, end = _append(ledger_box, n)
        recs.append(volume.record_append(f"SNAP-20261001-{n}", start, end, f"2026-10-01T00:0{n}:00Z"))
    assert volume.publish_latest(recs[1]) is True
    assert volume.publish_latest(recs[0]) is False   # an older mint finishing late
    man = json.loads(ledger_box.man.read_text())
    assert man["latest_sha256"] == recs[1]["sha256"] and man["latest_bytes"] == recs[1]["byte_end"]
    assert man["latest_utc"] == "2026-10-01T00:02:00Z" and man["latest_snap_id"] == "SNAP-20261001-2"
    assert volume.publish_latest(None) is False


# -- the per-turn readers -------------------------------------------------------------

@pytest.fixture
def big_ledger(isolated_stores):
    """A ~6 MB ledger for three operators plus its index."""
    from Orchestrator import fossils
    from Orchestrator.config import VOL_PATH
    parts = []
    for n in range(1, 1201):
        op = ("Ana", "Ben", "Cy")[n % 3]
        parts.append(_snap(n, op=op, body=f"turn {n} reranker latency déjà " + "filler words " * 380))
    VOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    VOL_PATH.write_bytes("".join(parts).encode())
    return fossils.rebuild_snapshot_index()


def test_the_keyword_scan_holds_no_copy_of_the_corpus(big_ledger):
    """The scan used to copy the operator's whole share of the ledger (all of
    it for "system": 54 MB on the dev box) before scoring; it now slices the
    map, so its peak is a few snapshots' worth."""
    from Orchestrator import fossils
    from Orchestrator.config import VOL_PATH
    share = VOL_PATH.stat().st_size
    fossils.keyword_retrieve_ids_for_operator("", "reranker latency", 20, "system")   # warm
    gc.collect()
    tracemalloc.start()
    ids = fossils.keyword_retrieve_ids_for_operator("", "reranker latency", 20, "system")
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(ids) == 20
    # Per-snapshot bookkeeping and the top-k texts remain; a copy of the
    # corpus would be at least the whole share.
    assert peak < share // 4, f"peak {peak:,} bytes for a {share:,}-byte corpus"


def test_the_index_is_not_reread_per_request_after_a_mint(isolated_stores, monkeypatch):
    """A mint puts its entry in the in-memory index itself; requests after it
    never parse snapshot_index.json again."""
    import builtins
    from Orchestrator import fossils
    from Orchestrator.config import VOL_PATH
    VOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    VOL_PATH.write_bytes("".join(_snap(n) for n in range(1, 10)).encode())
    fossils.rebuild_snapshot_index()
    fossils.update_snapshot_index("SNAP-20261001-99", 0, 10, "Ana", "2026-10-01T00:00:00Z")
    loads = []
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (
        loads.append(f) if str(f).endswith("snapshot_index.json") else None) or real_open(f, *a, **k))
    for _ in range(20):
        assert "SNAP-20261001-99" in fossils.load_snapshot_index()
        fossils.get_recent_fossils_for_operator("", "Ana", 3, None)
    assert loads == []


def test_every_hot_reader_goes_through_the_shared_map(big_ledger, monkeypatch):
    """No open() of the ledger on a turn's sections, a Timeline page, a
    snapshot fetch or a rerank passage once the ledger is mapped."""
    import builtins
    from Orchestrator import context_builder, fossils, retrieval
    from Orchestrator.config import VOL_PATH
    from Orchestrator.routes import task_routes
    ledger.view(VOL_PATH)                          # mapped once
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (
        opened.append(f) if str(f).endswith("SNAPSHOT_VOLUME.txt") else None) or real_open(f, *a, **k))
    ids = sorted(big_ledger, key=lambda s: int(s.rsplit("-", 1)[1]))
    monkeypatch.setattr(retrieval, "retrieve", lambda q, operator="", k=10, **kw: [
        (s, 0.5, None) if kw.get("return_provenance") else (s, 0.5) for s in ids[-k:]])
    context_builder.build_fossil_context("reranker latency", "Ana", provider="anthropic",
                                         include_media=False)
    task_routes.api_snapshots_recent(limit=20)
    fossils.get_snapshot_by_id(ids[7])
    assert retrieval._decode_snapshot_text(big_ledger[ids[3]]).startswith("=== START SNAPSHOT")
    assert opened == []


def test_the_embedding_passes_slice_a_view_like_the_bytes(tmp_path):
    from Orchestrator.embeddings.migrate import slice_snapshot_text
    p = tmp_path / "SNAPSHOT_VOLUME.txt"
    data = (_snap(1) + _snap(2)).encode()
    p.write_bytes(data)
    index = {"a": {"byte_start": 0, "byte_end": len(_snap(1).encode())},
             "b": {"byte_start": len(_snap(1).encode()), "byte_end": len(data)},
             "bad": {"byte_start": 10, "byte_end": len(data) + 1}}
    v = ledger.view(p)
    for sid in index:
        assert slice_snapshot_text(sid, index, v) == slice_snapshot_text(sid, index, data)
    assert slice_snapshot_text("bad", index, v) is None


def test_startup_indexes_the_new_tail_from_the_map(isolated_stores):
    from Orchestrator import fossils, startup
    from Orchestrator.config import VOL_PATH
    VOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    VOL_PATH.write_bytes("".join(_snap(n) for n in range(1, 6)).encode())
    index = dict(fossils.rebuild_snapshot_index())
    max_end = max(m["byte_end"] for m in index.values())
    volume.append_snapshot_text(_snap(6) + _snap(7))
    expected = {k: dict(v) for k, v in fossils.rebuild_snapshot_index().items()}
    startup._incremental_index_update(index, ledger.view(VOL_PATH).buffer(), max_end)
    assert {k: dict(v) for k, v in index.items()} == expected


def test_last_response_reads_exactly_its_snapshot(isolated_stores):
    """It read byte_end - byte_start CHARACTERS from a text-mode seek, past the
    snapshot on any multi-byte text."""
    from Orchestrator import fossils
    from Orchestrator.config import VOL_PATH
    from Orchestrator.routes import tts_routes
    VOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    first = ("=== START SNAPSHOT — UTC 2026-10-01T00:01:00Z — SNAP-20261001-1 (7.1.0) ===\n"
             "OPERATOR: Ana\nRaw Session Log\n"
             "- [1] 2026-10-01T00:01:00Z operator=Ana user: héllo 🙂🙂🙂\n"
             "- [2] 2026-10-01T00:01:00Z operator=Ana assistant: the answer 🙂\n"
             "=== END SNAPSHOT — SNAP-20261001-1 — UTC 2026-10-01T00:01:00Z ===\n")
    VOL_PATH.write_bytes((first + _snap(2, op="Ben")).encode())
    fossils.rebuild_snapshot_index()
    out = tts_routes._last_response_for("Ana")
    assert "the answer 🙂" in json.dumps(out, ensure_ascii=False)
