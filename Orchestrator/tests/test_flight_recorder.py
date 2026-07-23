"""Flight Recorder operator (design 2026-07-23) — M1-M6 contracts.

Covers: the widened read gate's golden equivalence (invariant #2), reserved-
name add/delete guards, reserved-job protection, the Oracle persona branch,
watchtower resilience, the flight-report pin filter, and the /operators
additive `reserved` key. Seeding is tested against a tmp cwd so the real
config.ini is never touched (BLACKBOX_SKIP_FR_SEED guards the app-import path).
"""
import os
os.environ.setdefault("BLACKBOX_SKIP_FR_SEED", "1")  # before any app import

import json
import pytest
from starlette.testclient import TestClient

from Orchestrator.config import (
    FLIGHT_RECORDER_OPERATOR, RESERVED_OPERATORS, reads_all_operators,
)
import Orchestrator.oversight as oversight


# ── M2: the widened gate (invariant #2 — golden equivalence) ───────────────

def test_reads_all_operators_reproduces_historical_conditionals():
    # Every pre-existing operator value behaves EXACTLY as before:
    assert reads_all_operators(None) is True         # None == see everything
    assert reads_all_operators("") is True           # empty == see everything
    assert reads_all_operators("system") is True     # magic all-read string
    assert reads_all_operators("Brandon") is False   # named operators scoped
    assert reads_all_operators("Anna 2") is False
    # ONLY the Flight Recorder is newly widened:
    assert reads_all_operators(FLIGHT_RECORDER_OPERATOR) is True
    # Case-variants are NOT widened (gating is exact-string throughout):
    assert reads_all_operators("flight recorder") is False


# ── M1: reserved names + guards ────────────────────────────────────────────

def test_reserved_set_contents():
    assert FLIGHT_RECORDER_OPERATOR == "Flight Recorder"
    assert RESERVED_OPERATORS == {"Flight Recorder", "system"}


@pytest.fixture()
def client():
    import Orchestrator.app  # noqa: F401 — registers all routes
    from Orchestrator.checkpoint import app
    return TestClient(app)


def test_delete_reserved_operator_refused(client):
    r = client.delete(f"/operator/{FLIGHT_RECORDER_OPERATOR}")
    assert r.status_code == 400
    assert "permanent" in r.json()["detail"]


def test_add_reserved_shadow_names_refused(client):
    # Case-shadowing a reserved name is refused…
    r = client.post("/operator/add", json={"name": "flight recorder"})
    assert r.status_code == 400
    assert "shadows" in r.json()["detail"]
    # …and claiming the exact reserved name is refused when not yet present
    # (seeding owns its creation), unless it already exists (→ "exists").
    r2 = client.post("/operator/add", json={"name": "system"})
    assert r2.status_code == 400


def test_operators_route_exposes_additive_reserved_key(client):
    data = client.get("/operators").json()
    assert "operators" in data and "default" in data   # existing keys intact
    assert isinstance(data.get("reserved"), list)      # additive key present
    # reserved lists only operators actually present in the list
    for name in data["reserved"]:
        assert name in data["operators"]


# ── M1: seeding — tmp cwd, real config never touched ───────────────────────

@pytest.fixture()
def restore_cfg_users():
    """The seeding tests CFG.read() a tmp config into the PROCESS-GLOBAL
    parser (review 2026-07-23: without restore, later tests in the same run
    see the tmp [users] section). Snapshot + restore around each test."""
    from Orchestrator.config import CFG
    saved = dict(CFG.items("users")) if CFG.has_section("users") else None
    yield
    if saved is not None:
        if CFG.has_section("users"):
            CFG.remove_section("users")
        CFG.add_section("users")
        for k, v in saved.items():
            CFG.set("users", k, v)


def _seed_env(tmp_path, monkeypatch, users="Default, bbx1, Brandon",
              default="Default"):
    monkeypatch.delenv("BLACKBOX_SKIP_FR_SEED", raising=False)
    monkeypatch.chdir(tmp_path)
    import Orchestrator.config as _cfg
    (tmp_path / "config.ini").write_text(
        f"[users]\nlist = {users}\ndefault = {default}\n")
    _cfg.CFG.read(tmp_path / "config.ini")
    monkeypatch.setattr(_cfg, "USERS_LIST",
                        [u.strip() for u in users.split(",") if u.strip()])
    monkeypatch.setattr(_cfg, "USERS_DEFAULT", default)
    # oversight reads config module-globals via `import Orchestrator.config`,
    # so patching the module attributes above covers it. Cron seeding is
    # stubbed out — the scheduler needs a live DB.
    monkeypatch.setattr(oversight, "retire_flight_report_cron_job", lambda: None)
    monkeypatch.setattr(oversight, "STATE_PATH", tmp_path / "fr_state.json")
    return _cfg


def test_seed_appends_without_touching_default_or_order(tmp_path, monkeypatch, restore_cfg_users):
    _cfg = _seed_env(tmp_path, monkeypatch)
    oversight.ensure_flight_recorder()
    assert _cfg.USERS_LIST[:3] == ["Default", "bbx1", "Brandon"]  # order kept
    assert _cfg.USERS_LIST[-1] == FLIGHT_RECORDER_OPERATOR        # appended
    assert _cfg.USERS_DEFAULT == "Default"                        # untouched
    # Persisted: a re-read of the written config contains the FR
    assert FLIGHT_RECORDER_OPERATOR in (tmp_path / "config.ini").read_text()


def test_seed_is_idempotent(tmp_path, monkeypatch, restore_cfg_users):
    _cfg = _seed_env(tmp_path, monkeypatch)
    oversight.ensure_flight_recorder()
    once = list(_cfg.USERS_LIST)
    oversight.ensure_flight_recorder()
    assert _cfg.USERS_LIST == once        # no duplicate append


def test_seed_adopts_exact_collision(tmp_path, monkeypatch, restore_cfg_users):
    _cfg = _seed_env(tmp_path, monkeypatch,
                     users=f"Brandon, {FLIGHT_RECORDER_OPERATOR}")
    oversight.ensure_flight_recorder()
    assert _cfg.USERS_LIST.count(FLIGHT_RECORDER_OPERATOR) == 1
    assert oversight.get_state_snapshot()["adopted_preexisting"] is True


def test_seed_leaves_case_variant_alone(tmp_path, monkeypatch, restore_cfg_users):
    _cfg = _seed_env(tmp_path, monkeypatch, users="Brandon, flight recorder")
    oversight.ensure_flight_recorder()
    assert "flight recorder" in _cfg.USERS_LIST          # variant untouched
    assert FLIGHT_RECORDER_OPERATOR in _cfg.USERS_LIST   # canonical seeded


# ── M6: natural mint trigger (Brandon 2026-07-23 — no scheduler) ───────────

def _fresh_counter(monkeypatch, tmp_path, n=3):
    monkeypatch.delenv("BLACKBOX_SKIP_FR_SEED", raising=False)
    monkeypatch.setattr(oversight, "STATE_PATH", tmp_path / "fr_state.json")
    monkeypatch.setattr(oversight, "FR_REPORT_EVERY_N", n)
    oversight.load_flight_recorder_state()
    with oversight._state_lock:
        oversight._state["mints_since_report"] = 0


def test_mint_trigger_fires_after_n_snapshots(monkeypatch, tmp_path):
    _fresh_counter(monkeypatch, tmp_path, n=3)
    fired = []
    monkeypatch.setattr(oversight, "create_flight_report_async",
                        lambda manual=False: fired.append(manual) or "SNAP-X")
    for i in range(3):
        oversight.on_snapshot_minted(f"SNAP-20260723-000{i}", "normal", "Brandon")
    # The report runs on a daemon thread — join it via the in-flight lock.
    import time
    for _ in range(100):
        if fired:
            break
        time.sleep(0.02)
    assert fired == [False]           # exactly one auto report
    assert oversight.get_state_snapshot()["mints_since_report"] >= 3


def test_flight_reports_never_count_or_retrigger(monkeypatch, tmp_path):
    _fresh_counter(monkeypatch, tmp_path, n=1)
    fired = []
    monkeypatch.setattr(oversight, "create_flight_report_async",
                        lambda manual=False: fired.append(manual))
    oversight.on_snapshot_minted("SNAP-20260723-0001", "flight_report",
                                 FLIGHT_RECORDER_OPERATOR)
    import time; time.sleep(0.05)
    assert fired == []                # own output never triggers (no recursion)
    assert oversight.get_state_snapshot()["mints_since_report"] == 0


def test_mint_trigger_never_raises_into_the_minting_caller(monkeypatch, tmp_path):
    _fresh_counter(monkeypatch, tmp_path, n=1)
    monkeypatch.setattr(oversight, "create_flight_report_async",
                        lambda manual=False: (_ for _ in ()).throw(RuntimeError("boom")))
    # Must not propagate — minting is sacred.
    oversight.on_snapshot_minted("SNAP-20260723-0001", "normal", "Anna")
    import time; time.sleep(0.05)     # thread exception stays on the thread


def test_retire_migration_deletes_seeded_job(monkeypatch, tmp_path):
    monkeypatch.setattr(oversight, "STATE_PATH", tmp_path / "fr_state.json")
    oversight.load_flight_recorder_state()
    with oversight._state_lock:
        oversight._state["report_job_id"] = "cron_seeded"
    deleted = []
    class FakeMgr:
        def list_jobs(self):
            return [{"id": "cron_byname", "operator": FLIGHT_RECORDER_OPERATOR,
                     "name": oversight.FR_REPORT_JOB_NAME},
                    {"id": "cron_user", "operator": "Brandon", "name": "Other"}]
        def delete_job(self, jid):
            deleted.append(jid); return True
    import Orchestrator.scheduler as sched
    monkeypatch.setattr(sched, "get_scheduler_manager", lambda: FakeMgr())
    oversight.retire_flight_report_cron_job()
    assert set(deleted) == {"cron_seeded", "cron_byname"}   # user job untouched
    assert oversight.get_state_snapshot()["report_job_id"] is None


# ── M3: the Oracle persona ─────────────────────────────────────────────────

def test_flight_recorder_persona_is_the_oracle(monkeypatch):
    import Orchestrator.behavioral_core as bc
    import Orchestrator.state as state
    monkeypatch.setattr(state, "get_operator_preference",
                        lambda op, key, default=None: None)
    p = bc.get_persona(FLIGHT_RECORDER_OPERATOR, "chat")
    assert p == bc.DEFAULT_PERSONA_FLIGHT_RECORDER
    assert "permanent overseer" in p
    # Other operators keep the generic default (invariant: unchanged behavior)
    assert bc.get_persona("Brandon", "chat") == bc.DEFAULT_PERSONA_CHAT


def test_flight_recorder_persona_override_and_revert(monkeypatch):
    import Orchestrator.behavioral_core as bc
    import Orchestrator.state as state
    monkeypatch.setattr(state, "get_operator_preference",
                        lambda op, key, default=None: "Custom oracle voice")
    assert bc.get_persona(FLIGHT_RECORDER_OPERATOR, "chat") == "Custom oracle voice"
    monkeypatch.setattr(state, "get_operator_preference",
                        lambda op, key, default=None: None)   # DELETE persona →
    assert bc.get_persona(FLIGHT_RECORDER_OPERATOR, "chat") == \
        bc.DEFAULT_PERSONA_FLIGHT_RECORDER                    # reverts to Oracle


# ── M4: watchtower resilience ──────────────────────────────────────────────

def test_collector_survives_broken_sources(monkeypatch):
    # Break the task DB: the collector must record the error, not raise.
    import Orchestrator.models as models
    monkeypatch.setattr(models.task_db, "get_task_list",
                        lambda op=None: (_ for _ in ()).throw(RuntimeError("db gone")),
                        raising=False)
    signals = oversight.collect_oversight_signals()
    assert "collected_at" in signals
    assert any("tasks:" in e for e in signals["errors"])
    # Digest renders whatever survived, never raises
    assert isinstance(oversight.format_signals_digest(signals), str)


# ── M5: flight-report pin filter + min-activity skip ───────────────────────

def test_flight_report_pin_filters_type_and_operator(monkeypatch):
    from Orchestrator import fossils, volume
    fake_index = {
        "SNAP-20260723-0001": {"operator": "Brandon", "type": "checkpoint",
                               "byte_start": 0, "byte_end": 5},
        "SNAP-20260723-0002": {"operator": FLIGHT_RECORDER_OPERATOR,
                               "type": "flight_report",
                               "byte_start": 5, "byte_end": 10},
        "SNAP-20260723-0003": {"operator": FLIGHT_RECORDER_OPERATOR,
                               "type": "normal",
                               "byte_start": 10, "byte_end": 15},
    }
    monkeypatch.setattr(fossils, "load_snapshot_index", lambda: fake_index)
    monkeypatch.setattr(volume, "read_volume_bytes", lambda p: b"AAAAABBBBBCCCCC")
    reports = oversight.get_recent_flight_reports(count=5)
    assert reports == ["BBBBB"]     # only the FR flight_report block


def test_report_skips_on_insufficient_activity(tmp_path, monkeypatch):
    from Orchestrator import fossils, volume
    monkeypatch.setattr(oversight, "STATE_PATH", tmp_path / "fr_state.json")
    oversight.load_flight_recorder_state()
    with oversight._state_lock:
        oversight._state["last_report_id"] = "SNAP-20260723-0002"
    idx = {"SNAP-20260723-0001": {"operator": "B"},
           "SNAP-20260723-0002": {"operator": "B"},
           "SNAP-20260723-0003": {"operator": "B"}}   # only 1 new since last
    monkeypatch.setattr(fossils, "load_snapshot_index", lambda: idx)
    monkeypatch.setattr(fossils, "get_recent_fossils_for_operator",
                        lambda *a, **k: ["snap text"])
    monkeypatch.setattr(volume, "read_text_safe", lambda p: "")
    called = {"synth": False}
    monkeypatch.setattr(oversight, "_synthesize",
                        lambda prompt: called.__setitem__("synth", True) or "x")
    assert oversight.create_flight_report_async(manual=False) is None
    assert called["synth"] is False   # skipped BEFORE any LLM call
