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
    }
    assert set(row.keys()) == expected_fields
    assert row["token_name"] == "token_test"


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


# --- audit C3: reconcile_or_wipe 4-case coverage ----------------------


def test_reconcile_case1_both_clean_is_noop(isolated_state):
    """Case 1: empty state + empty tokens.db → no-op (no wipe)."""
    assert not isolated_state.exists()
    with patch.object(zellij_state.zellij_client, "list_tokens", return_value=[]):
        zellij_state.reconcile_or_wipe()
    # Still no state file (no-op).
    assert not isolated_state.exists()


def test_reconcile_case2_both_populated_match_is_noop(isolated_state):
    """Case 2: state and tokens.db have the same token_names → no-op."""
    zellij_state.add_session(
        "Brandon", "claude", None,
        "Brandon__claude__a", "token_A", None,
    )
    zellij_state.add_session(
        "Brandon", "claude", None,
        "Brandon__claude__b", "token_B", None,
    )
    rows_before = zellij_state.load()

    with patch.object(
        zellij_state.zellij_client,
        "list_tokens",
        return_value=[
            {"name": "token_A", "created_at": "2026-01-01"},
            {"name": "token_B", "created_at": "2026-01-02"},
        ],
    ):
        zellij_state.reconcile_or_wipe()

    rows_after = zellij_state.load()
    assert rows_after == rows_before, "no-op case must not modify state"


def test_reconcile_case3_state_populated_tokens_empty_wipes(isolated_state, monkeypatch, tmp_path):
    """Case 3: state has rows, tokens.db empty → WIPE both."""
    # Point _ZELLIJ_TOKENS_DB at a fake file we can verify is unlinked.
    fake_tokens_db = tmp_path / "fake_tokens.db"
    fake_tokens_db.write_text("not-a-real-db")
    monkeypatch.setattr(zellij_state, "_ZELLIJ_TOKENS_DB", fake_tokens_db)

    zellij_state.add_session(
        "Brandon", "claude", None,
        "Brandon__claude__a", "token_A", None,
    )
    assert isolated_state.exists()

    with patch.object(zellij_state.zellij_client, "list_tokens", return_value=[]):
        zellij_state.reconcile_or_wipe()

    assert not isolated_state.exists(), "case 3 must wipe state file"
    assert not fake_tokens_db.exists(), "case 3 must wipe tokens.db"


def test_reconcile_case4_state_empty_tokens_populated_wipes(isolated_state, monkeypatch, tmp_path):
    """Case 4: state empty, tokens.db populated → WIPE both."""
    fake_tokens_db = tmp_path / "fake_tokens.db"
    fake_tokens_db.write_text("not-a-real-db")
    monkeypatch.setattr(zellij_state, "_ZELLIJ_TOKENS_DB", fake_tokens_db)

    assert not isolated_state.exists()
    assert fake_tokens_db.exists()

    with patch.object(
        zellij_state.zellij_client,
        "list_tokens",
        return_value=[{"name": "token_orphan", "created_at": "2026-01-01"}],
    ):
        zellij_state.reconcile_or_wipe()

    # State file still absent (no-op for that side).
    assert not isolated_state.exists()
    # tokens.db was wiped.
    assert not fake_tokens_db.exists(), "case 4 must wipe orphaned tokens.db"


def test_reconcile_mismatch_wipes(isolated_state, monkeypatch, tmp_path):
    """Bonus 5th case: both populated but token_names disagree → wipe."""
    fake_tokens_db = tmp_path / "fake_tokens.db"
    fake_tokens_db.write_text("not-a-real-db")
    monkeypatch.setattr(zellij_state, "_ZELLIJ_TOKENS_DB", fake_tokens_db)

    zellij_state.add_session(
        "Brandon", "claude", None,
        "Brandon__claude__a", "token_A", None,
    )
    with patch.object(
        zellij_state.zellij_client,
        "list_tokens",
        return_value=[{"name": "token_Z", "created_at": "2026-01-01"}],
    ):
        zellij_state.reconcile_or_wipe()

    assert not isolated_state.exists(), "mismatch must wipe state"
    assert not fake_tokens_db.exists(), "mismatch must wipe tokens.db"
