"""Smoke tests for the backfill CLI wrapper (Task 12).

These are WIRING tests: the migration engine itself is covered by
test_embeddings_migrate.py, so nothing here runs a real migration (no
provider, no network). Each test invokes the script as a real subprocess —
exactly how an operator would — from the project root (config.ini lives
there), pointed at tmp fixtures via the --stores-dir / --index overrides.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from Orchestrator.embeddings.migrate import STATE_FILE
from Orchestrator.embeddings.registry import EMBEDDING_MODELS

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "Orchestrator" / "backfill_embeddings.py"


def run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run the script as an operator would: subprocess from the project root."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture
def fixture_paths(tmp_path):
    """A tmp stores dir + a 2-snapshot index file, isolated from the real box."""
    stores = tmp_path / "stores"
    stores.mkdir()
    index = tmp_path / "index.json"
    index.write_text(json.dumps({
        "SNAP-20260101-0001": {"byte_start": 0, "byte_end": 10},
        "SNAP-20260101-0002": {"byte_start": 10, "byte_end": 20},
    }), encoding="utf-8")
    return stores, index


def overrides(stores: Path, index: Path) -> list:
    return ["--stores-dir", str(stores), "--index", str(index)]


# ── --help ───────────────────────────────────────────────────────────────────

def test_help_exits_zero_with_usage():
    result = run_cli("--help")
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()
    for flag in ("--target", "--force", "--list", "--stores-dir", "--index"):
        assert flag in result.stdout


# ── --list ───────────────────────────────────────────────────────────────────

def test_list_shows_stores_active_and_missing(fixture_paths):
    stores, index = fixture_paths
    # active.json in the OVERRIDE dir — proves --stores-dir is honored end to
    # end (the config default would report gemini-embedding-001).
    (stores / "active.json").write_text(
        json.dumps({"active": "qwen3-embedding-0.6b"}), encoding="utf-8"
    )

    result = run_cli("--list", *overrides(stores, index))
    assert result.returncode == 0, result.stdout + result.stderr

    out = result.stdout
    active_lines = [l for l in out.splitlines() if "active:" in l]
    assert active_lines and "qwen3-embedding-0.6b" in active_lines[0]
    # Every registry model is listed, with no stores on disk: count 0,
    # missing = both index snapshots.
    for slug, spec in EMBEDDING_MODELS.items():
        assert re.search(
            rf"{re.escape(slug)}\s+{spec['dims']}\s+0\s+2", out
        ), f"missing row for {slug} in:\n{out}"
    assert "2 snapshots" in out  # index size echoed


def test_list_is_side_effect_free(fixture_paths):
    stores, index = fixture_paths
    result = run_cli("--list", *overrides(stores, index))
    assert result.returncode == 0
    # Probing stores must create no store dirs/files (open() is read-only).
    assert list(stores.iterdir()) == []


def test_list_shows_schema_and_rows_columns(fixture_paths):
    """M6e ops currency: --list displays schema + rows after count/missing.
    A chunked (schema-2) store reports count in SNAPSHOT currency with the
    raw chunk-row count in the rows column; storeless models show 1 / 0."""
    import numpy as np

    from Orchestrator.embeddings.store import VectorStore

    stores, index = fixture_paths
    # Build a real v2 store on disk (the CLI subprocess re-reads it fresh);
    # direct VectorStore construction is the documented test-only path.
    store = VectorStore("gemini-embedding-001", 3072, stores, schema=2).open()
    rng = np.random.default_rng(5)
    store.append_group(
        "SNAP-20260101-0001", [rng.standard_normal(3072) for _ in range(3)]
    )

    result = run_cli("--list", *overrides(stores, index))
    assert result.returncode == 0, result.stdout + result.stderr

    out = result.stdout
    assert "schema" in out and "rows" in out  # header carries the new columns
    # v2 row: dims 3072, count 1 (SNAPSHOT currency), missing 1, schema 2, rows 3
    assert re.search(r"gemini-embedding-001\s+3072\s+1\s+1\s+2\s+3", out), out
    # a storeless model reports schema 1 / rows 0 (v1 default, nothing on disk)
    assert re.search(r"qwen3-embedding-0\.6b\s+1024\s+0\s+2\s+1\s+0", out), out


# ── invalid slug ─────────────────────────────────────────────────────────────

def test_invalid_slug_exits_2_and_lists_valid_slugs(fixture_paths):
    stores, index = fixture_paths
    result = run_cli("--target", "not-a-real-model", *overrides(stores, index))
    assert result.returncode == 2
    assert "not-a-real-model" in result.stdout
    for slug in EMBEDDING_MODELS:
        assert slug in result.stdout


# ── state-file guard ─────────────────────────────────────────────────────────
# These two run IN-PROCESS (backfill.main + a monkeypatched liveness probe),
# not as subprocesses: the M6d liveness guard fires BEFORE the state-file
# guard, so a subprocess run would exit 5 whenever the real orchestrator is
# up — the suite must be runnable with the box live. The probe is pinned
# False so the state-file guard itself is what's under test; main() rebinds
# config/fossils module globals from argv, so those are monkeypatch-pinned
# for restore (in-process run, shared interpreter).

def _pin_main_globals(monkeypatch):
    from Orchestrator import backfill_embeddings as backfill
    from Orchestrator import config, fossils

    monkeypatch.setattr(backfill, "_service_alive", lambda *a, **k: False)
    monkeypatch.setattr(backfill, "_install_sigint_cancel", lambda: None)
    monkeypatch.setattr(
        config, "EMBEDDINGS_STORES_DIR", config.EMBEDDINGS_STORES_DIR
    )
    monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", fossils.SNAPSHOT_INDEX)
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)
    return backfill


def test_running_state_file_blocks_without_force(fixture_paths, monkeypatch,
                                                 capsys):
    backfill = _pin_main_globals(monkeypatch)
    stores, index = fixture_paths
    (stores / STATE_FILE).write_text(json.dumps({
        "target": "qwen3-embedding-0.6b",
        "state": "running",
        "done": 1,
        "total": 5,
        "started_at": "2026-06-12T00:00:00+00:00",
    }), encoding="utf-8")

    rc = backfill.main(
        ["--target", "gemini-embedding-001", *overrides(stores, index)]
    )
    out = capsys.readouterr().out
    assert rc == 3
    assert "RUNNING" in out
    assert "--force" in out  # remediation is spelled out


def test_non_running_state_file_does_not_block(tmp_path, monkeypatch, capsys):
    """A finished job's state file must not trip the guard; with an empty
    index and target == active the run short-circuits before any provider
    work — proving the guard checks state, not mere file existence."""
    backfill = _pin_main_globals(monkeypatch)
    stores = tmp_path / "stores"
    stores.mkdir()
    index = tmp_path / "index.json"
    index.write_text("{}", encoding="utf-8")
    (stores / STATE_FILE).write_text(json.dumps({
        "target": "gemini-embedding-001",
        "state": "done",
        "done": 5,
        "total": 5,
    }), encoding="utf-8")

    rc = backfill.main(overrides(stores, index))  # default target = active
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Nothing to do" in out
