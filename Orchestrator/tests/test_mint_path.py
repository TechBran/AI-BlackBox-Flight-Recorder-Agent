"""The mint and checkpoint path (audit O05-ledger-persistence-04/-05/-15/-16,
rank 77).

The ledger append is the commit point: under mint_lock a mint only reads the
ledger's tail, renders, appends, writes the index entry and records the
ledger's prefix hash; the embed (bounded by a ceiling), the vector append,
the manifest update and the state save run after the lock is released,
best-effort. Mints make no archive copy (the maintenance pass does, daily).
A checkpoint is attempted at most once per interval per operator, never twice
at once, through the configured provider.

Every test runs on a real ledger, GoldMaster, manifest, index and vector
store under tmp_path, with fake embedding providers and fake chat calls.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from Orchestrator import checkpoint as ckpt
from Orchestrator import config, fossils, state, volume
from Orchestrator.embeddings import providers, search as search_mod
from Orchestrator.embeddings.providers import EmbeddingProviderError
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import get_store, set_active_slug

SLUG = "gemini-embedding-001"
DIMS = EMBEDDING_MODELS[SLUG]["dims"]
OP = "Ann"


class FakeProvider:
    def __init__(self, fail=False, hook=None):
        self.fail, self.hook, self.calls = fail, hook, 0

    async def embed(self, texts, purpose):
        self.calls += 1
        if self.hook:
            self.hook()
        if self.fail:
            raise EmbeddingProviderError("fake provider: down")
        return [[1.0] + [0.0] * (DIMS - 1) for _ in texts]


@pytest.fixture
def box(isolated_stores, tmp_path, monkeypatch):
    root = tmp_path / "ledger"
    vol = root / "SNAPSHOT_VOLUME.txt"
    arc = root / "Archive"
    manifest = root / "manifest.json"
    gm = root / "GoldMaster.txt"
    gm_hash = root / "GoldMaster.sha256"
    arc.mkdir(parents=True)
    manifest.write_text(json.dumps({"archive": []}))
    gm.write_bytes(b"gold")
    gm_hash.write_text(hashlib.sha256(b"gold").hexdigest())
    for name, value in (("VOL_PATH", vol), ("ARC_DIR", arc), ("MANIFEST", manifest),
                        ("GM_PATH", gm), ("GM_HASH", gm_hash)):
        monkeypatch.setattr(volume, name, value)
    monkeypatch.setattr(volume, "archive_min_free_gb", lambda: 0.0)
    monkeypatch.setattr(volume, "_latest_copy", None)
    monkeypatch.setattr(ckpt, "VOL_PATH", vol)
    index_path = root / "snapshot_index.json"
    stores_dir = root / "embeddings"
    monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", index_path)
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)
    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(stores_dir))
    monkeypatch.setattr(search_mod, "_active_store", None)
    providers._instances.clear()
    set_active_slug(SLUG, base_dir=stores_dir)
    monkeypatch.setattr(state, "state_by_op", defaultdict(state.OpState))
    from Orchestrator import tasks
    monkeypatch.setattr(tasks, "collect_pending_media_artifacts", lambda *a, **k: [])
    monkeypatch.setenv("BLACKBOX_SKIP_FR_SEED", "1")   # no background flight reports
    ns = type("Box", (), {})()
    ns.vol, ns.arc, ns.index, ns.stores = vol, arc, index_path, stores_dir
    ns.provider = lambda p: providers._instances.__setitem__(SLUG, p)
    yield ns
    providers._instances.clear()
    search_mod._active_store = None


def _turn(op, n):
    s = state.get_state(op)
    utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s.add_conversation_turn({"role": "user", "utc": utc, "text": f"question {n}"})
    s.add_conversation_turn({"role": "assistant", "utc": utc, "text": f"answer {n}"})


def _snapshots(vol: Path):
    return re.findall(r"^=== START SNAPSHOT — UTC \S+ — (SNAP-\d{8}-\d+)", vol.read_text(), re.M)


def _body(vol: Path, snap_id: str) -> str:
    text = vol.read_text()
    start = text.index(f"— {snap_id} (7.1.0) ===")
    return text[start:text.index(f"=== END SNAPSHOT — {snap_id}")]


# ── the append is the commit point ──────────────────────────────────────────

def test_embed_failure_after_append_leaves_the_ledger_consistent_and_reembeddable(box):
    box.provider(FakeProvider(fail=True))
    _turn(OP, 1)
    result = ckpt.perform_mint(OP, "TEST")
    snap_id = result["snap_id"]
    assert _snapshots(box.vol) == [snap_id]
    assert snap_id in fossils.load_snapshot_index()
    store = get_store(SLUG, base_dir=box.stores)
    assert store.missing([snap_id]) == [snap_id]          # the catch-up loop re-embeds it
    assert state.get_state(OP).last_mint_index == len(state.get_state(OP).conversation_log)
    # ...and once the provider is back, the snapshot embeds like any other.
    box.provider(FakeProvider())
    payload = ckpt._embed_for_index(snap_id, _body(box.vol, snap_id))
    fossils.append_snapshot_vectors(snap_id, **payload)
    assert store.missing([snap_id]) == []


def test_a_failed_hash_record_after_append_never_remints_the_same_turns(box, monkeypatch):
    box.provider(FakeProvider())

    def disk_full(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ckpt, "record_append", disk_full)
    _turn(OP, 1)
    first = ckpt.perform_mint(OP, "TEST")                  # an archive failure used to raise here
    assert first["assert_sha256"] is None and first["warnings"]
    _turn(OP, 2)
    second = ckpt.perform_mint(OP, "TEST")
    assert _snapshots(box.vol) == [first["snap_id"], second["snap_id"]]
    assert "question 1" in _body(box.vol, first["snap_id"])
    assert "question 1" not in _body(box.vol, second["snap_id"])
    assert "question 2" in _body(box.vol, second["snap_id"])


def test_mint_with_content_reports_a_failed_hash_record_instead_of_raising(box, monkeypatch):
    box.provider(FakeProvider())
    monkeypatch.setattr(ckpt, "record_append", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "full")))
    result = ckpt.mint_with_content(OP, "direct content", reason="TEST")
    assert _snapshots(box.vol) == [result["snap_id"]]
    assert result["assert_sha256"] is None and result["warnings"]
    assert result["snap_id"] in fossils.load_snapshot_index()


def test_the_embed_runs_after_mint_lock_is_released(box):
    held = []
    box.provider(FakeProvider(hook=lambda: held.append(state.mint_lock.locked())))
    _turn(OP, 1)
    ckpt.perform_mint(OP, "TEST")
    ckpt.mint_with_content(OP, "direct content", reason="TEST")
    assert held == [False, False]


def _records(box):
    path = volume.ledger_hashes_path()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_successful_mint_embeds_and_records_its_prefix_hash_without_a_copy(box):
    """A mint no longer copies the whole ledger into Archive/ (audit
    O05-ledger-persistence-02): it records the ledger's sha256 at its own end,
    which is the digest the copy used to carry."""
    box.provider(FakeProvider())
    _turn(OP, 1)
    result = ckpt.perform_mint(OP, "TEST")
    ledger = box.vol.read_bytes()
    assert result["dims"] == DIMS and "warnings" not in result
    assert result["assert_sha256"] == hashlib.sha256(ledger).hexdigest()
    assert result["archived"] is None and result["ledger_bytes"] == len(ledger)
    assert get_store(SLUG, base_dir=box.stores).missing([result["snap_id"]]) == []
    assert list(box.arc.glob("Volume_*.txt")) == []
    [rec] = _records(box)
    assert rec == {"snap_id": result["snap_id"], "byte_start": 0, "byte_end": len(ledger),
                   "sha256": result["assert_sha256"], "utc": rec["utc"]}
    man = json.loads(volume.MANIFEST.read_text())
    assert man["latest_sha256"] == result["assert_sha256"] and man["latest_bytes"] == len(ledger)
    assert man["latest_snap_id"] == result["snap_id"] and man["archive"] == []
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", man["latest_utc"])


def test_concurrent_mints_each_land_once_in_ledger_and_index(box):
    box.provider(FakeProvider())
    results = []
    threads = [threading.Thread(target=lambda n=n: results.append(
        ckpt.mint_with_content(OP, f"content {n}", reason="TEST"))) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [r["snap_id"] for r in results]
    assert sorted(_snapshots(box.vol)) == sorted(ids) and len(set(ids)) == 6
    assert set(ids) <= set(fossils.load_snapshot_index())
    # One record per append, in append order, each the hash of its prefix,
    # each starting where the one before it ended: the chain.
    ledger = box.vol.read_bytes()
    records = _records(box)
    assert sorted(r["snap_id"] for r in records) == sorted(ids)
    assert [r["byte_start"] for r in records] == [0] + [r["byte_end"] for r in records[:-1]]
    for rec in records:
        assert rec["sha256"] == hashlib.sha256(ledger[:rec["byte_end"]]).hexdigest()
    man = json.loads(volume.MANIFEST.read_text())
    assert man["latest_sha256"] == hashlib.sha256(ledger).hexdigest()


def test_a_hash_record_never_captures_a_concurrent_append(box, monkeypatch):
    """Mint A updates the manifest after releasing mint_lock while mint B is
    half way through its append: the manifest describes the ledger A
    committed (never B's torn half), and ends on the newest ledger."""
    box.provider(FakeProvider())
    real_append = ckpt.append_snapshot_text
    real_publish = ckpt.publish_latest
    b_half_written, a_published, a_unlocked = threading.Event(), threading.Event(), threading.Event()
    seen = {}

    def slow_append(text):
        if "OPERATOR: Bob" not in text:
            return real_append(text)
        half = len(text) // 2
        real_append(text[:half])
        b_half_written.set()
        a_published.wait(5)
        with open(volume.VOL_PATH, "ab") as f:
            f.write(text[half:].encode())

    def publish(record):
        if record and record["snap_id"] not in seen and "A" not in seen:
            assert b_half_written.wait(5)
            out = real_publish(record)
            seen["A"] = json.loads(volume.MANIFEST.read_text())
            a_published.set()
            return out
        return real_publish(record)

    real_embed_store = ckpt._embed_and_store

    def embed_store(warnings, snap_id, body):
        if "OPERATOR: Ann" in body:
            a_unlocked.set()
        return real_embed_store(warnings, snap_id, body)

    monkeypatch.setattr(ckpt, "append_snapshot_text", slow_append)
    monkeypatch.setattr(ckpt, "publish_latest", publish)
    monkeypatch.setattr(ckpt, "_embed_and_store", embed_store)
    _turn("Ann", 1)
    _turn("Bob", 1)
    results = {}
    ta = threading.Thread(target=lambda: results.__setitem__("A", ckpt.perform_mint("Ann", "T")))
    ta.start()
    assert a_unlocked.wait(5)
    tb = threading.Thread(target=lambda: results.__setitem__("B", ckpt.perform_mint("Bob", "T")))
    tb.start()
    ta.join(10)
    tb.join(10)
    ledger = box.vol.read_bytes()
    a_end = results["A"]["ledger_bytes"]
    committed_a = ledger[:a_end].decode()
    assert committed_a.count("=== START SNAPSHOT") == committed_a.count("=== END SNAPSHOT") == 1
    assert seen["A"]["latest_bytes"] == a_end
    assert seen["A"]["latest_sha256"] == hashlib.sha256(ledger[:a_end]).hexdigest()
    volume.parse_tail(committed_a)                         # a whole ledger, not torn
    man = json.loads(volume.MANIFEST.read_text())
    assert man["latest_sha256"] == hashlib.sha256(ledger).hexdigest()
    assert results["B"]["assert_sha256"] == man["latest_sha256"]


def test_every_writer_reads_only_the_ledger_tail(box, monkeypatch):
    """A mint used to read and decode the whole ledger twice under mint_lock
    just to find its tail (audit O05-ledger-persistence-18). On a large
    ledger, a mint now reads a few KB of it."""
    from Orchestrator.tests.test_ledger_reads_per_turn import count_volume_reads
    filler = []
    for n in range(1, 401):
        sid = f"SNAP-20260101-{n:03d}"
        filler.append(f"=== START SNAPSHOT — UTC 2026-01-01T00:00:00Z — {sid} (7.1.0) ===\n"
                      f"OPERATOR: Old\n{'history ' * 600}\n"
                      f"=== END SNAPSHOT — {sid} — UTC 2026-01-01T00:00:00Z ===\n")
    box.vol.write_text("".join(filler))
    size = box.vol.stat().st_size
    box.provider(FakeProvider())
    _turn(OP, 1)
    ckpt.perform_mint(OP, "TEST")          # the first mint after boot hashes the ledger once
    # Bytes read through open() or served from the ledger map, counted as
    # the per-turn harness counts them.
    served = count_volume_reads(monkeypatch)
    _turn(OP, 2)
    result = ckpt.perform_mint(OP, "TEST")
    assert result["snap_id"] == "SNAP-" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-402"
    assert served["bytes"] < size // 10, f"a mint read {served['bytes']:,} of {size:,} bytes"


# ── the embed ceiling ───────────────────────────────────────────────────────

def test_a_hung_embedding_provider_is_cut_off_at_the_ceiling(box, monkeypatch):
    class Hung:
        async def embed(self, texts, purpose):
            await asyncio.sleep(8)
            return [[1.0] * DIMS for _ in texts]

    box.provider(Hung())
    monkeypatch.setattr(search_mod, "EMBED_CEILING_S", 0.3)
    started = time.monotonic()
    assert search_mod.generate_embedding_sync("query") is None
    assert search_mod.embed_snapshot_for_index("snapshot body") == {}
    assert time.monotonic() - started < 4


# ── the index cache is copy-on-write ────────────────────────────────────────

def test_index_updates_never_mutate_a_dict_a_reader_holds(box):
    fossils.update_snapshot_index("SNAP-20260101-001", 0, 10, OP, "2026-01-01T00:00:00Z")
    held = fossils.load_snapshot_index()
    before = dict(held)
    fossils.update_snapshot_index("SNAP-20260101-002", 10, 20, OP, "2026-01-01T00:00:00Z")
    assert held == before                                  # a reader iterating it is safe
    assert "SNAP-20260101-002" in fossils.load_snapshot_index()


# ── checkpoints: one attempt per interval, configured provider ──────────────

@pytest.fixture
def checkpoint_env(box, monkeypatch):
    box.provider(FakeProvider())
    monkeypatch.setattr(ckpt, "CHECKPOINT_AUTO_CREATE_INTERVAL", 4)
    monkeypatch.setattr(ckpt, "CHECKPOINT_MIN_SNAPSHOTS", 1)
    monkeypatch.setattr(ckpt, "get_recent_fossils_for_operator",
                        lambda *a, **k: ["=== START SNAPSHOT — SNAP-1 ===\nold turn\n"] * 3)
    for name in ("GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"):
        monkeypatch.setattr(config, name, "")
    from Orchestrator.routes import chat_routes as cr

    def no_gemini(*a, **k):
        raise AssertionError("checkpoint compression called the pinned Gemini model")

    monkeypatch.setattr(cr, "call_gemini", no_gemini)
    return cr


def test_a_due_checkpoint_is_claimed_once_per_interval(checkpoint_env):
    s = state.get_state(OP)
    answers = {}
    for turn in range(1, 13):
        s.total_turns = turn
        answers[turn] = ckpt.should_create_checkpoint(OP)
    # Due at 4; the attempt (still running, or failed) holds off 5-7; the
    # next try is a full interval later.
    assert [t for t, due in answers.items() if due] == [4, 8, 12]


def test_a_successful_checkpoint_restarts_the_count(checkpoint_env):
    s = state.get_state(OP)
    s.total_turns = 4
    assert ckpt.should_create_checkpoint(OP) is True
    s.last_checkpoint_turn = 5                     # the attempt succeeded at turn 5
    s.total_turns = 8
    assert ckpt.should_create_checkpoint(OP) is False
    s.total_turns = 9
    assert ckpt.should_create_checkpoint(OP) is True


def test_compression_uses_the_configured_provider(checkpoint_env, monkeypatch):
    cr = checkpoint_env
    monkeypatch.setattr(config, "DEFAULT_PROVIDER", "anthropic")
    calls = []

    def fake_anthropic(messages, model, operator):
        calls.append(model)
        return ("compressed summary of the turns", {}, "", [])

    monkeypatch.setattr(cr, "call_anthropic", fake_anthropic)
    result = ckpt.create_checkpoint_async(OP)
    assert result["status"] == "created"
    assert calls == [config.resolve_default_model("anthropic")]
    body = _body(volume.VOL_PATH, result["snap_id"])
    assert "TYPE: checkpoint" in body and "compressed summary" in body
    assert config.resolve_default_model("anthropic") in body
    assert fossils.load_snapshot_index()[result["snap_id"]]["type"] == "checkpoint"
    assert state.get_state(OP).last_checkpoint_id == result["snap_id"]


def test_a_checkpoint_records_its_prefix_hash_and_refreshes_the_manifest(checkpoint_env, monkeypatch):
    """Checkpoints appended to the ledger without touching latest_sha256, so
    the manifest went stale after every one (audit O05-ledger-persistence-03)."""
    cr = checkpoint_env
    monkeypatch.setattr(config, "DEFAULT_PROVIDER", "anthropic")
    monkeypatch.setattr(cr, "call_anthropic",
                        lambda messages, model, operator: ("compressed summary", {}, "", []))
    _turn(OP, 1)
    minted = ckpt.perform_mint(OP, "TEST")
    result = ckpt.create_checkpoint_async(OP)
    assert result["status"] == "created" and "warnings" not in result
    ledger = volume.VOL_PATH.read_bytes()
    records = _records(box=None)
    assert [r["snap_id"] for r in records] == [minted["snap_id"], result["snap_id"]]
    assert records[1]["byte_start"] == records[0]["byte_end"]
    assert records[1]["sha256"] == hashlib.sha256(ledger).hexdigest()
    man = json.loads(volume.MANIFEST.read_text())
    assert man["latest_sha256"] == hashlib.sha256(ledger).hexdigest()
    assert man["latest_snap_id"] == result["snap_id"]


def test_a_flight_report_records_its_prefix_hash_and_refreshes_the_manifest(box, monkeypatch, tmp_path):
    from Orchestrator import oversight
    box.provider(FakeProvider())
    _turn(OP, 1)
    minted = ckpt.perform_mint(OP, "TEST")
    monkeypatch.setattr(oversight, "VOL_PATH", box.vol)
    monkeypatch.setattr(oversight, "STATE_PATH", tmp_path / "fr_state.json")
    oversight.load_flight_recorder_state()
    monkeypatch.setattr(fossils, "get_recent_fossils_for_operator", lambda *a, **k: ["a snapshot"])
    monkeypatch.setattr(oversight, "collect_oversight_signals", lambda: {})
    monkeypatch.setattr(oversight, "format_signals_digest", lambda signals: "no signals")
    monkeypatch.setattr(oversight, "_synthesize", lambda prompt: "all quiet")
    monkeypatch.setattr(oversight, "_maybe_notify", lambda *a: None)
    snap_id = oversight.create_flight_report_async(manual=True)
    assert snap_id and snap_id != minted["snap_id"]
    ledger = box.vol.read_bytes()
    assert [r["snap_id"] for r in _records(box)] == [minted["snap_id"], snap_id]
    man = json.loads(volume.MANIFEST.read_text())
    assert man["latest_sha256"] == hashlib.sha256(ledger).hexdigest()
    assert man["latest_snap_id"] == snap_id


def test_a_failed_checkpoint_is_reported_as_failed(checkpoint_env, monkeypatch):
    cr = checkpoint_env
    monkeypatch.setattr(config, "DEFAULT_PROVIDER", "anthropic")

    def no_key(*a, **k):
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    monkeypatch.setattr(cr, "call_anthropic", no_key)
    from Orchestrator import models, tasks
    from Orchestrator.models import Task, TaskStatus, TaskType
    task = Task(task_id="ckpt-test-1", task_type=TaskType.CHECKPOINT, status=TaskStatus.PENDING,
                created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
                operator=OP, result_data={"manual": False})
    models.task_db.save_task(task)
    tasks.process_checkpoint_task(task)
    done = models.task_db.get_task("ckpt-test-1")
    assert done.status == TaskStatus.FAILED
    assert "ANTHROPIC_API_KEY" in (done.error_message or "")
    assert not volume.VOL_PATH.exists() or _snapshots(volume.VOL_PATH) == []


def test_only_one_checkpoint_runs_per_operator_at_a_time(checkpoint_env, monkeypatch):
    cr = checkpoint_env
    monkeypatch.setattr(config, "DEFAULT_PROVIDER", "anthropic")
    entered, release = threading.Event(), threading.Event()

    def slow_anthropic(messages, model, operator):
        entered.set()
        release.wait(5)
        return ("summary", {}, "", [])

    monkeypatch.setattr(cr, "call_anthropic", slow_anthropic)
    results = {}
    first = threading.Thread(target=lambda: results.__setitem__("first", ckpt.create_checkpoint_async(OP)))
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=lambda: results.__setitem__("second", ckpt.create_checkpoint_async(OP, True)))
    second.start()
    second.join(2)
    release.set()
    first.join(5)
    second.join(5)
    assert results["second"]["status"] == "skipped"
    assert results["first"]["status"] == "created"
    assert len(_snapshots(volume.VOL_PATH)) == 1
