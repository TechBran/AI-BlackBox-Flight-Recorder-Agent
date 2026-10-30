"""Hygiene (spec W26, W27, W32, A11, A11b): after a full simulated phone run the
wellness files are 0700/0600 (and re-secured at boot), nothing was written
under Portal/, Volumes/, Archive/ or Manifest/, no sentinel health value
appears in any other store or in anything logged or printed, the device key
never reaches a log, and the redaction backstop masks the new secret shapes.

Then the ledger rule's half (wave 2): a streamed chat turn quoting the
sentinels is saved through POST /chat/save and minted, and a non-stream
worker turn (the cron and /chat path) is recorded in tasks.db and minted;
afterwards no sentinel is in data/ (outside data/wellness/), Manifest/,
Volumes/, Archive/, Orchestrator/*.db, Portal/, the embedder's input, the
journal or anything printed. Writes into the checkout itself are refused by
the root conftest's write guard for every test, these included."""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import stat
from collections import defaultdict
from pathlib import Path

import pytest

from Orchestrator.tests.wellness_support import (OPERATOR, SIM_IP, ClientTransport, fixtures,
                                                 free_port, load_sim, roster, simulator_mode)
from Orchestrator.wellness import api, paths

FX = fixtures()
SIM = load_sim()
WATCHED = ("Portal", "Volumes", "Archive", "Manifest")


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def _tree(root: Path) -> dict:
    out = {}
    for base in WATCHED:
        for p in sorted((root / base).rglob("*")) if (root / base).exists() else ():
            if p.is_file():
                s = p.stat()
                out[str(p.relative_to(root))] = (s.st_size, s.st_mtime_ns)
    return out


def _files_to_scan(root: Path):
    wellness = paths.data_dir().resolve()
    for base in ("data", "Manifest", "Volumes", "Archive", "Portal", "credentials"):
        for p in sorted((root / base).rglob("*")) if (root / base).exists() else ():
            if p.is_file() and wellness not in p.resolve().parents:
                yield p
    for p in sorted(root.glob("*.db")) + sorted((root / "Orchestrator").glob("*.db")):
        yield p


def _hits(text: str):
    return [s for s in FX.SENTINEL_STRINGS if s in text]


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    """One full run: register, enroll, push the 120-day fixture, read every
    tool answer once with a turn (so delivered payloads exist), status."""
    from fastapi.testclient import TestClient

    from Orchestrator.tests import box_stores
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("hygiene-box")
    box_stores.point_at(root, mp)
    for d in ("Volumes", "Manifest", "Archive", "Portal/uploads", "Portal/artifacts"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "config.ini").write_text(Path("config.ini").read_text())
    mp.chdir(root)
    roster(mp, OPERATOR, "other-human", "Flight Recorder")
    port = free_port(SIM_IP)
    simulator_mode(port)
    # Imported first: its setup_logging() replaces the root handlers.
    from Orchestrator.app import app
    records = []
    handler = logging.Handler(level=logging.DEBUG)
    handler.emit = lambda rec: records.append(rec)
    logging.getLogger().addHandler(handler)
    old_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.DEBUG)
    from unittest.mock import patch
    try:
        with patch("Orchestrator.toolvault.embeddings.sync_embeddings", return_value={}):
            before = _tree(root)
            with TestClient(app, client=(SIM_IP, 50000)) as client:
                t = ClientTransport(client)
                state = SIM.State(root / "sim-state")
                SIM.register(t, state, operator=OPERATOR, ip=SIM_IP, port=port,
                             pairing_file=paths.sim_pairing_path())
                out = SIM.enroll(t, state, operator=OPERATOR, ip=SIM_IP, port=port,
                                 capability_report=FX.load("continuous_120")["capabilities"])
                assert out["status"] == 200, out
                assert SIM.push(t, state, fixture="continuous_120",
                                log=lambda m: None)["quarantined"] == 0
                d = api.Delivery("wt_hyg", "android", "local")
                now = FX.now_ms()
                envs = [api.summary(OPERATOR, "custom", start=FX.day_of(-6), end=FX.day_of(-2),
                                    now_ms=now, delivery=d),
                        api.history(OPERATOR, FX.day_of(-10), FX.day_of(0), now_ms=now,
                                    delivery=d),
                        api.series(OPERATOR, "steps", "last_7_days", now_ms=now, delivery=d)]
                SIM.status(t, state)
                key = state.load()["key"]
                after = _tree(root)
                printed = io.StringIO()
                with contextlib.redirect_stdout(printed):
                    turns = _chat_turns(mp, root, client, now)
        yield {"root": root, "records": records, "before": before, "after": after,
               "key": key, "envs": envs, "printed": printed.getvalue(), **turns}
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger().setLevel(old_level)
        mp.undo()


REPLY = ("Your sleeping heart rate was 47.3 bpm (47 bpm), forty-seven beats per minute. "
         "You walked 12,345 steps (12345); your blood pressure was 118/79.")


def _chat_turns(mp, root: Path, client, now: int) -> dict:
    """A streamed turn saved through POST /chat/save (it echoes the turn key
    of the deliveries above) and a non-stream worker turn whose tool delivers
    mid-turn, both minting into a real ledger under `root`."""
    import hashlib

    import Orchestrator.routes.chat_routes as cr
    import Orchestrator.tasks as tasks
    from Orchestrator import fossils, state
    from Orchestrator.embeddings import search as search_mod
    from Orchestrator.models import Task, TaskStatus, TaskType, task_db
    from Orchestrator.routes.chat import save as save_mod
    from Orchestrator.volume import now_utc_iso
    from Orchestrator.wellness import enroll, turn as turn_mod
    gm = root / "GoldMaster"
    gm.mkdir(exist_ok=True)
    (gm / "v7.0.txt").write_bytes(b"gold")
    (gm / "v7.0.sha256").write_text(hashlib.sha256(b"gold").hexdigest())
    if not (root / "Manifest" / "manifest.json").exists():
        (root / "Manifest" / "manifest.json").write_text(json.dumps({"archive": []}))
    mp.setattr(fossils, "_index_cache", None)
    mp.setattr(fossils, "_index_cache_mtime", 0.0)
    mp.setattr(state, "state_by_op", defaultdict(state.OpState))
    mp.setattr(tasks, "collect_pending_media_artifacts", lambda *a, **k: [])
    mp.setenv("BLACKBOX_SKIP_FR_SEED", "1")
    embedded = []
    mp.setattr(search_mod, "embed_snapshot_for_index", lambda text: embedded.append(text) or {})
    for mod in (save_mod, tasks):
        mp.setattr(mod, "AUTO_ENABLE", True)
        mp.setattr(mod, "TURNS_THRESHOLD", 1)
        mp.setattr(mod, "DEBOUNCE_MS", 0)
        mp.setattr(mod, "should_create_checkpoint", lambda *a, **k: False)

    saved = client.post("/chat/save", json={
        "operator": OPERATOR, "user_message": "how did I sleep last night?",
        "assistant_response": REPLY, "reasoning": "The tool said 47.3 and 12345 and 118/79.",
        "model": "model-test", "wellness_turn": "wt_hyg"}).json()

    st = enroll.store_for(enroll.get_enrollment(OPERATOR), create=False)
    mp.setattr(tasks, "build_fossil_context", lambda *a, **k: (
        "", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []}))
    mp.setattr(tasks, "TOOLVAULT_ENABLED", False)

    def fake_call(messages, model, operator="x"):
        env = api.summary(OPERATOR, "custom", start=FX.day_of(-5), end=FX.day_of(-5), now_ms=now,
                          delivery=api.Delivery("wt_hyg_worker", "cron", "anthropic"))
        assert env["ok"]
        return REPLY, {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}, \
            "Thinking about 47.3 and 12345."

    mp.setattr(cr, "call_anthropic", fake_call)
    task = Task(task_id="hygiene-worker-1", task_type=TaskType.CHAT, status=TaskStatus.PENDING,
                created_at=now_utc_iso(), updated_at=now_utc_iso(), operator=OPERATOR,
                result_data={"messages": [{"role": "user", "content": "morning check-in"}],
                             "operator": OPERATOR, "provider": "anthropic", "model": "m"})
    task_db.save_task(task)
    token = turn_mod.stamp(turn_mod.WellnessTurn(OPERATOR, turn_mod.SURFACE_CRON, True,
                                                 provider="anthropic", turn_key="wt_hyg_worker"))
    try:
        tasks.process_chat_task(task)
    finally:
        turn_mod.clear(token)
    return {"saved": saved, "embedded": embedded, "worker": task_db.get_task(task.task_id),
            "store": st}


def test_modes_are_private(ran):
    assert _mode(paths.data_dir()) == 0o700 if paths.data_dir().exists() else True
    root = ran["root"]
    wdir = root / "data" / "wellness"
    assert _mode(wdir) == 0o700
    for p in wdir.iterdir():
        assert _mode(p) == 0o600, p.name
    for name in ("wellness_device_keys.json", "wellness_sim_pairing.json"):
        assert _mode(root / "credentials" / name) == 0o600


def test_nothing_written_under_the_served_and_ledger_trees(ran):
    assert ran["after"] == ran["before"]


def test_the_sentinels_are_really_in_the_answers(ran):
    """The scan below would catch a leak: the tool answers DO carry them."""
    text = " ".join(str(e) for e in ran["envs"])
    assert "47.3" in text and "12345" in text


def test_no_sentinel_outside_the_wellness_store(ran):
    for p in _files_to_scan(ran["root"]):
        text = p.read_bytes().decode("utf-8", "replace")
        assert not _hits(text), (str(p.relative_to(ran["root"])), _hits(text))


def test_no_sentinel_or_key_in_the_logs(ran):
    lines = [r.getMessage() for r in ran["records"]]
    assert any("[WELLNESS] ingest op=" in line for line in lines)
    for line in lines:
        assert not _hits(line), line
        assert ran["key"] not in line
    ingest = [line for line in lines if "[WELLNESS] ingest" in line]
    assert all("op=" in line and "rows=" in line for line in ingest)
    assert not any(OPERATOR in line for line in ingest)       # the op_key prefix, not the name


def test_no_sentinel_in_anything_printed(ran, capsys):
    out, err = capsys.readouterr()
    assert not _hits(out + err)
    assert "[CHAT/SAVE] Wellness turn: preview skipped" in ran["printed"]
    assert not _hits(ran["printed"])


def test_the_chat_turns_were_wellness_turns_and_minted(ran):
    """The scan below would catch a leak: both turns really reached the ledger,
    tagged, with their rows stamped masked."""
    assert ran["saved"]["minted"] is True
    body = (ran["root"] / "Volumes" / "SNAPSHOT_VOLUME.txt").read_text(encoding="utf-8")
    assert body.count("WELLNESS: ") >= 2 and body.count(" · values not recorded") >= 2
    assert "[REASONING omitted: wellness turn]" in body
    assert "how did I sleep last night?" in body                       # the user's own words
    worker = ran["worker"]
    assert worker.status.value == "completed" and "[·]" in worker.result_data["reply"]
    assert "hygiene-worker-1" in ran["store"].task_refs()
    assert all(r["masked_at"] for r in ran["store"].deliveries_for_turn("wt_hyg"))


def test_no_sentinel_in_what_the_embedder_received(ran):
    assert ran["embedded"] and not any(_hits(text) for text in ran["embedded"])


def test_no_sentinel_in_the_worker_task_row(ran):
    worker = ran["worker"]
    assert not _hits(json.dumps(worker.result_data) + str(worker.reasoning_text))


def test_boot_hook_resecures(ran):
    root = ran["root"]
    wdir = root / "data" / "wellness"
    mp = pytest.MonkeyPatch()
    mp.setattr(paths, "DATA_DIR", wdir)
    mp.setattr(paths, "KEYS_PATH", root / "credentials" / "wellness_device_keys.json")
    try:
        os.chmod(wdir, 0o755)
        db = next(wdir.glob("*.db"))
        os.chmod(db, 0o644)
        from Orchestrator import startup
        startup.startup_wellness_store()
        assert _mode(wdir) == 0o700 and _mode(db) == 0o600
    finally:
        mp.undo()


def test_redaction_masks_the_wellness_secrets():
    from Orchestrator.secret_redaction import redact
    key = "Ab3_" * 10 + "xyz"
    assert len(key) == 43
    out = redact(f'{{"k": "{key}", "epoch": 2}}')
    assert key not in out and '"epoch": 2' in out
    sig = "a1" * 32
    header = f"v1;enr=en_0a1b2c3d;ts=1;nonce={'b' * 32};sig={sig}"
    assert sig not in redact(header) and "enr=en_0a1b2c3d" in redact(header)
    assert "wbx_s_" not in redact("token wbx_s_abcdefghijklmnop0123")
    assert redact("sig=short and k=1") == "sig=short and k=1"
    # The same key inside an escaped JSON string, as a repr, and as k=.
    assert key not in redact(json.dumps({"payload": json.dumps({"k": key})}))
    assert key not in redact(repr({"k": key}))
    assert key not in redact(f"bind k={key} epoch=2")
    assert redact(f"kk={key}") == f"kk={key}"
