"""Unit tests for Orchestrator.cli_agent.zellij_state.

Acceptance criteria covered:
- I7: post-launch state file contains zero UUID-shaped strings.
- polish #2: add_session ↔ save concurrency lock — 5 threads, no lost updates.
- C3: reconcile_or_wipe 4-case coverage.
- General: atomic save (.tmp + os.replace), idempotent remove.
"""
from __future__ import annotations

import inspect
import json
import re
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from Orchestrator.cli_agent import zellij_state


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Redirect _STATE_DIR + _STATE_PATH onto tmp_path so the test never
    touches the real state file."""
    state_dir = tmp_path / "state"
    state_path = state_dir / "zellij_sessions.json"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_path)
    return state_path


# --- audit I7: zero UUIDs on disk --------------------------------------


def test_add_session_schema_has_no_token_value_uuid(isolated_state):
    """Acceptance criterion (audit I7): after add_session(), grep the
    state file — there must be NO UUID-shaped string. Schema persists
    only token_name (e.g., 'token_3'), never the raw UUID value."""
    zellij_state.add_session(
        operator="Brandon",
        provider="claude",
        app=None,
        session_name="Brandon__claude__test",
        token_name="token_test",
        expires_at=None,
    )

    raw = isolated_state.read_text(encoding="utf-8")
    assert not _UUID_RE.search(raw), (
        f"UUID-shaped string found in state file (audit I7 violation): "
        f"{raw!r}"
    )

    rows = json.loads(raw)
    assert len(rows) == 1
    row = rows[0]
    expected_fields = {
        "operator",
        "provider",
        "app",
        "session_name",
        "token_name",
        "created_at",
        "expires_at",
        "yolo",
    }
    assert set(row.keys()) == expected_fields
    assert row["token_name"] == "token_test"


# --- yolo round-trip -----------------------------------------------------


def test_add_session_persists_yolo_true(isolated_state):
    """add_session(yolo=True) round-trips: the persisted row carries
    yolo=True and it survives load()."""
    zellij_state.add_session(
        operator="Brandon",
        provider="claude",
        app=None,
        session_name="Brandon__claude__root__yolo",
        token_name="master",
        expires_at=None,
        yolo=True,
    )
    rows = zellij_state.load()
    assert len(rows) == 1
    assert rows[0]["yolo"] is True


def test_add_session_yolo_defaults_false(isolated_state):
    """Omitted yolo keeps every existing caller working: persisted rows
    default to yolo=False."""
    zellij_state.add_session(
        "Brandon", "claude", None,
        "Brandon__claude__root", "master", None,
    )
    rows = zellij_state.load()
    assert len(rows) == 1
    assert rows[0]["yolo"] is False


def test_add_session_upsert_refreshes_yolo(isolated_state):
    """Idempotent upsert on (operator, session_name) refreshes yolo in
    place (like token_name/expires_at) rather than duplicating the row —
    pins the UPDATE branch by flipping the value on the second call."""
    zellij_state.add_session(
        "Brandon", "claude", None,
        "Brandon__claude__root__yolo", "master", None, yolo=True,
    )
    # Second call with the SAME session name but yolo=False must hit the
    # update branch and refresh the field, not duplicate the row.
    zellij_state.add_session(
        "Brandon", "claude", None,
        "Brandon__claude__root__yolo", "master", None, yolo=False,
    )
    rows = zellij_state.load()
    assert len(rows) == 1
    assert rows[0]["yolo"] is False


# --- atomic save pattern (audit polish) --------------------------------


def test_save_uses_tmp_plus_os_replace_atomic_pattern():
    """Verify atomic-write pattern is present in save() source — explicit
    contract that we don't ship a half-written file on crash."""
    src = inspect.getsource(zellij_state.save)
    assert ".tmp" in src, "save() must write to a .tmp suffix first"
    assert "os.replace" in src, "save() must call os.replace for atomic swap"


# --- audit polish #2: concurrency lock ---------------------------------


def test_add_session_concurrency_no_lost_updates(isolated_state):
    """5 threads each call add_session with distinct session_names. After
    all join, load() must return all 5 rows (the _STATE_LOCK serializes
    read-modify-write so no thread's write clobbers another's)."""
    errors: list[Exception] = []

    def worker(idx: int):
        try:
            zellij_state.add_session(
                operator="Brandon",
                provider="claude",
                app=f"app{idx}",
                session_name=f"Brandon__claude__app{idx}__t{idx}",
                token_name=f"token_{idx}",
                expires_at=None,
            )
        except Exception as exc:  # pragma: no cover — defensive
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive(), "thread did not join in time"

    assert not errors, f"worker threads raised: {errors}"

    rows = zellij_state.load()
    names = sorted(r["session_name"] for r in rows)
    expected = sorted(
        f"Brandon__claude__app{i}__t{i}" for i in range(5)
    )
    assert names == expected, (
        f"Lost updates! Expected 5 rows, got {len(rows)}: {names}"
    )


# --- remove_session idempotency ---------------------------------------


def test_remove_session_on_missing_name_is_noop(isolated_state):
    # Empty file, then remove — should not raise.
    zellij_state.remove_session("doesnt-exist")
    # Still empty.
    assert zellij_state.load() == []


def test_remove_session_removes_matching_row(isolated_state):
    zellij_state.add_session(
        "Brandon", "claude", None,
        "Brandon__claude__a", "token_1", None,
    )
    zellij_state.add_session(
        "Brandon", "claude", None,
        "Brandon__claude__b", "token_2", None,
    )
    zellij_state.remove_session("Brandon__claude__a")
    rows = zellij_state.load()
    names = [r["session_name"] for r in rows]
    assert names == ["Brandon__claude__b"]


# --- Phase 2: reconcile_or_wipe PRESERVES live-backed terminal rows -----
#
# The master-token model made every state row carry token_name="master",
# so the OLD token-set comparison wiped all terminal rows on every restart.
# reconcile_or_wipe now drives off LIVE zellij SESSIONS: a row is preserved
# iff its session still exists (running OR exited-resurrectable) and it is
# not an expired short-lived row; genuinely-orphaned rows are dropped.


def test_reconcile_no_state_rows_is_noop(isolated_state):
    """No state rows -> nothing to reconcile (and the persistent master
    token in tokens.db is NOT touched)."""
    assert not isolated_state.exists()
    with patch.object(zellij_state.zellij_client, "list_sessions", return_value=[]):
        zellij_state.reconcile_or_wipe()
    assert not isolated_state.exists()


def test_reconcile_preserves_row_with_live_session(isolated_state):
    """A terminal row (expires_at=None) whose zellij session STILL EXISTS
    is PRESERVED across a simulated restart (the survive-restart change)."""
    zellij_state.add_session(
        "Brandon", "terminal", None,
        "Brandon__terminal__root", "master", None,
    )
    rows_before = zellij_state.load()
    assert len(rows_before) == 1

    with patch.object(
        zellij_state.zellij_client, "list_sessions",
        return_value=[{"name": "Brandon__terminal__root", "created_at": "1h ago", "exited": False}],
    ):
        zellij_state.reconcile_or_wipe()

    rows_after = zellij_state.load()
    assert rows_after == rows_before, "live-backed terminal row must be preserved"


def test_reconcile_preserves_row_with_exited_resurrectable_session(isolated_state):
    """An EXITED-but-resurrectable session is still a valid resume target;
    its row must be PRESERVED (the zellij-web client resurrects on attach)."""
    zellij_state.add_session(
        "Brandon", "claude", "grocery-store",
        "Brandon__claude__grocery-store", "master", None,
    )
    with patch.object(
        zellij_state.zellij_client, "list_sessions",
        return_value=[{"name": "Brandon__claude__grocery-store", "created_at": "2days ago", "exited": True}],
    ):
        zellij_state.reconcile_or_wipe()
    rows = zellij_state.load()
    names = [r["session_name"] for r in rows]
    assert names == ["Brandon__claude__grocery-store"], "exited-resurrectable row must be preserved"


def test_reconcile_drops_orphaned_row_no_live_session(isolated_state):
    """A row whose zellij session is GONE (killed out-of-band) is dropped
    — genuinely orphaned, not resumable."""
    zellij_state.add_session(
        "Brandon", "terminal", None,
        "Brandon__terminal__root", "master", None,
    )
    # zellij has a DIFFERENT session, not ours -> ours is orphaned.
    with patch.object(
        zellij_state.zellij_client, "list_sessions",
        return_value=[{"name": "SomeoneElse__terminal__root", "created_at": "1h ago", "exited": False}],
    ):
        zellij_state.reconcile_or_wipe()
    rows = zellij_state.load()
    assert rows == [], "orphaned row (no live session) must be dropped"


def test_reconcile_preserves_live_drops_orphan_in_same_pass(isolated_state):
    """Mixed: one row live (preserve), one row orphaned (drop)."""
    zellij_state.add_session(
        "Brandon", "terminal", None,
        "Brandon__terminal__root", "master", None,
    )
    zellij_state.add_session(
        "Brandon", "claude", None,
        "Brandon__claude__root", "master", None,
    )
    with patch.object(
        zellij_state.zellij_client, "list_sessions",
        return_value=[{"name": "Brandon__terminal__root", "created_at": "1h ago", "exited": False}],
    ):
        zellij_state.reconcile_or_wipe()
    names = sorted(r["session_name"] for r in zellij_state.load())
    assert names == ["Brandon__terminal__root"], "live preserved, orphan dropped"


def test_reconcile_drops_expired_short_lived_row_even_if_session_present(isolated_state):
    """A row with a PAST expires_at is stale and dropped even if a session
    of that name happens to exist (legacy short-lived-token hygiene)."""
    past = "2000-01-01T00:00:00+00:00"
    zellij_state.add_session(
        "Brandon", "claude", None,
        "Brandon__claude__root", "token_old", past,
    )
    with patch.object(
        zellij_state.zellij_client, "list_sessions",
        return_value=[{"name": "Brandon__claude__root", "created_at": "1h ago", "exited": False}],
    ):
        zellij_state.reconcile_or_wipe()
    assert zellij_state.load() == [], "expired row must be dropped"


def test_reconcile_skips_when_zellij_unreachable_preserves_state(isolated_state):
    """If zellij is down at boot, reconcile makes NO destructive change —
    state is preserved (orchestrator retries next start). This protects a
    live terminal during a transient zellij outage."""
    zellij_state.add_session(
        "Brandon", "terminal", None,
        "Brandon__terminal__root", "master", None,
    )
    rows_before = zellij_state.load()
    with patch.object(
        zellij_state.zellij_client, "list_sessions",
        side_effect=RuntimeError("zellij daemon down"),
    ):
        zellij_state.reconcile_or_wipe()
    assert zellij_state.load() == rows_before, "must not wipe on zellij-unreachable"
