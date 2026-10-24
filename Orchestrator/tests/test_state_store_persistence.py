"""Operator preferences, operator state and the app registry persist
atomically, survive a corrupt file, and the Portal's preference sync reaches
disk (audit X05-architecture-05/-06, O01-core-runtime-03)."""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

import pytest

from Orchestrator import state


def _quarantined(path: Path):
    return sorted(path.parent.glob(f"{path.name}.corrupt-*"))


@pytest.fixture
def prefs(isolated_stores, monkeypatch):
    """The live preferences dict, emptied for the test and restored after.
    setattr first, so a loader that rebinds the global is undone too."""
    original = state.OPERATOR_PREFERENCES
    saved = copy.deepcopy(original)
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", original)
    original.clear()
    yield Path(state.OPERATOR_PREFS_FILE)
    original.clear()
    original.update(saved)


@pytest.fixture
def op_state(isolated_stores, monkeypatch):
    monkeypatch.setattr(state, "state_by_op", state.defaultdict(state.OpState))
    monkeypatch.setattr(state, "AGENT_SESSIONS", {})
    monkeypatch.setattr(state, "PERSISTED_AGENT_SESSIONS", {})
    return Path(state.OPERATOR_STATE_FILE)


# ── the stale-import bug: Portal syncs never reached disk ───────────────────

def test_portal_preference_sync_persists_after_boot_load(prefs, app_client):
    """POST /operator/preferences/{op} (Portal voice + CU sync) after the boot
    loader ran: the value is on disk and visible to get_operator_preference.
    tts_routes imported OPERATOR_PREFERENCES by value, and the loader rebound
    the name, so the POST wrote into an orphaned dict."""
    prefs.write_text(json.dumps({"Ann": {"tts_voice": "openai:alloy", "persona": "terse"}}))
    state.load_operator_preferences()

    r = app_client.post("/operator/preferences/Ann", json={"tts_voice": "openai:nova"})
    assert r.status_code == 200
    assert r.json()["preferences"]["tts_voice"] == "openai:nova"

    on_disk = json.loads(prefs.read_text())
    assert on_disk["Ann"] == {"tts_voice": "openai:nova", "persona": "terse"}
    assert state.get_operator_preference("Ann", "tts_voice") == "openai:nova"
    got = app_client.get("/operator/preferences/Ann").json()["preferences"]
    assert got == {"tts_voice": "openai:nova", "persona": "terse"}


def test_loader_keeps_the_shared_dict_object(prefs):
    before = state.OPERATOR_PREFERENCES
    prefs.write_text(json.dumps({"Ann": {"k": 1}}))
    state.load_operator_preferences()
    assert state.OPERATOR_PREFERENCES is before
    assert before == {"Ann": {"k": 1}}


def test_persona_reset_persists(prefs, app_client):
    state.set_operator_preference("Ann", "persona", "pirate")
    assert app_client.delete("/operator/persona/Ann").status_code == 200
    assert "persona" not in json.loads(prefs.read_text()).get("Ann", {})


# ── corrupt files are quarantined, never overwritten ─────────────────────────

def test_corrupt_preferences_are_quarantined_not_overwritten(prefs):
    prefs.write_text('{"Ann": {"persona": "a long persona the operator wrote')
    state.load_operator_preferences()
    assert state.OPERATOR_PREFERENCES == {}
    state.set_operator_preference("Bob", "tts_voice", "openai:nova")
    (moved,) = _quarantined(prefs)
    assert moved.read_text().startswith('{"Ann": {"persona"')
    assert json.loads(prefs.read_text()) == {"Bob": {"tts_voice": "openai:nova"}}


def test_corrupt_operator_state_is_quarantined_not_overwritten(op_state):
    op_state.write_text('{"Ann": {"total_turns": 41, "last_chec')
    state.load_operator_state()
    state.get_state("Bob").total_turns = 3
    state.save_operator_state()
    (moved,) = _quarantined(op_state)
    assert moved.read_text().startswith('{"Ann": {"total_turns": 41')
    assert json.loads(op_state.read_text())["Bob"]["total_turns"] == 3


def test_corrupt_app_registry_is_quarantined(isolated_stores, monkeypatch):
    monkeypatch.setattr(state, "APP_REGISTRY", {})
    path = Path(state.APPS_REGISTRY_FILE)
    path.write_text('[{"app_id": "x", "name": ')
    state.load_app_registry()
    state.save_app_registry()
    (moved,) = _quarantined(path)
    assert moved.read_text() == '[{"app_id": "x", "name": '
    assert json.loads(path.read_text()) == []


# ── a save that fails midway leaves the previous file intact ────────────────

def test_failed_preference_save_leaves_the_previous_file(prefs):
    state.set_operator_preference("Ann", "tts_voice", "openai:alloy")
    good = prefs.read_text()
    state.OPERATOR_PREFERENCES["Ann"]["broken"] = object()   # not JSON-serializable
    state.save_operator_preferences()
    assert prefs.read_text() == good


def test_failed_state_save_leaves_the_previous_file(op_state, monkeypatch):
    state.get_state("Ann").total_turns = 7
    state.save_operator_state()
    good = op_state.read_text()

    class Session:   # an agent session whose model is not serializable
        operator, claude_session_id = "Ann", "abc"
        message_count, model, working_directory, last_activity = 1, object(), "", ""

    monkeypatch.setattr(state, "AGENT_SESSIONS", {"s": Session()})
    state.save_operator_state()
    assert op_state.read_text() == good


def test_files_are_owner_only(prefs, op_state):
    state.set_operator_preference("Ann", "persona", "x")
    state.save_operator_state()
    for path in (prefs, op_state):
        assert path.stat().st_mode & 0o077 == 0, path


# ── concurrency ──────────────────────────────────────────────────────────────

def test_concurrent_preference_writes_keep_every_key_and_a_parseable_file(prefs):
    def worker(n):
        for i in range(20):
            state.set_operator_preference(f"op{n}", f"k{i}", i)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    on_disk = json.loads(prefs.read_text())
    assert {op: len(v) for op, v in on_disk.items()} == {f"op{n}": 20 for n in range(6)}


def test_agent_session_only_state_entry_loads(op_state):
    """An entry carrying only an agent session (no turn counters) must load:
    the summary print used data['total_turns'] and aborted the loader."""
    op_state.write_text(json.dumps({
        "Ann": {"agent_session": {"claude_session_id": "abcdef0123", "message_count": 2}},
        "Bob": {"total_turns": 5, "last_checkpoint_turn": 2},
    }))
    state.load_operator_state()
    assert state.get_state("Bob").total_turns == 5
    assert state.PERSISTED_AGENT_SESSIONS["Ann"]["message_count"] == 2


def test_corrupt_flight_recorder_state_is_quarantined_not_overwritten(tmp_path, monkeypatch):
    from Orchestrator import oversight
    path = tmp_path / "flight_recorder_state.json"
    monkeypatch.setattr(oversight, "STATE_PATH", path)
    path.write_text('{"seeded_at": "2026-07-23T00:00:00+00:00", "last_report_id": "SNAP-2026')
    snapshot = oversight.load_flight_recorder_state()
    assert snapshot["last_report_id"] is None
    oversight._save_state()
    (moved,) = _quarantined(path)
    assert moved.read_text().startswith('{"seeded_at"')
    assert json.loads(path.read_text())["mints_since_report"] == 0
    assert path.stat().st_mode & 0o077 == 0
