"""The mint and checkpoint path (audit O05-ledger-persistence-04/-05/-15/-16).

The ledger append is the commit point: under mint_lock a mint only renders,
appends and writes the index entry; the embed (bounded by a ceiling), the
vector append, the archive copy and the state save run after the lock is
released, best-effort. A checkpoint is attempted at most once per interval
per operator, never twice at once, through the configured provider.

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


def test_archive_failure_after_append_never_remints_the_same_turns(box, monkeypatch):
    box.provider(FakeProvider())

    def disk_full():
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ckpt, "archive_volume", disk_full)
    _turn(OP, 1)
    first = ckpt.perform_mint(OP, "TEST")                  # used to raise here
    assert first["archived"] is None and first["warnings"]
    _turn(OP, 2)
    second = ckpt.perform_mint(OP, "TEST")
    assert _snapshots(box.vol) == [first["snap_id"], second["snap_id"]]
    assert "question 1" in _body(box.vol, first["snap_id"])
    assert "question 1" not in _body(box.vol, second["snap_id"])
    assert "question 2" in _body(box.vol, second["snap_id"])


def test_mint_with_content_reports_a_failed_archive_instead_of_raising(box, monkeypatch):
    box.provider(FakeProvider())
    monkeypatch.setattr(ckpt, "archive_volume", lambda: (_ for _ in ()).throw(OSError(28, "full")))
    result = ckpt.mint_with_content(OP, "direct content", reason="TEST")
    assert _snapshots(box.vol) == [result["snap_id"]]
    assert result["archived"] is None and result["warnings"]
    assert result["snap_id"] in fossils.load_snapshot_index()


def test_the_embed_runs_after_mint_lock_is_released(box):
    held = []
    box.provider(FakeProvider(hook=lambda: held.append(state.mint_lock.locked())))
    _turn(OP, 1)
    ckpt.perform_mint(OP, "TEST")
    ckpt.mint_with_content(OP, "direct content", reason="TEST")
    assert held == [False, False]


def test_a_successful_mint_still_embeds_and_archives(box):
    box.provider(FakeProvider())
    _turn(OP, 1)
    result = ckpt.perform_mint(OP, "TEST")
    assert result["dims"] == DIMS and result["archived"] and "warnings" not in result
    assert get_store(SLUG, base_dir=box.stores).missing([result["snap_id"]]) == []
    assert len(list(box.arc.glob("Volume_*.txt"))) == 1


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
