"""Unit tests for Orchestrator.cli_agent.terminal_uploads (plan Task 5:
terminal upload folders die with their session).

Everything runs against tmp_path with an explicit ``now`` / os.utime mtime
control — no test may ever touch the real Portal/uploads/terminal.
"""
import logging
import os

from Orchestrator.cli_agent import terminal_uploads

_LOGGER_NAME = terminal_uploads.logger.name


# --- remove_for_session ------------------------------------------------


def test_remove_for_session_removes_existing_folder(tmp_path):
    folder = tmp_path / "Brandon__claude__root"
    folder.mkdir()
    (folder / "shot.png").write_bytes(b"\x89PNG fake")

    terminal_uploads.remove_for_session("Brandon__claude__root", base_dir=tmp_path)

    assert not folder.exists()


def test_remove_for_session_noop_on_missing_folder(tmp_path):
    """Idempotent: no folder for the session -> silent no-op, base dir
    untouched (the DELETE endpoint calls this unconditionally)."""
    terminal_uploads.remove_for_session("Brandon__claude__root", base_dir=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_remove_for_session_rejects_traversal_and_invalid_names(tmp_path):
    """Defense-in-depth: only a plain session name may select the folder.
    zellij's charset allows dots, so ".." passes is_valid_session_name —
    without an explicit guard, base/".." rmtrees the PARENT of the base."""
    base = tmp_path / "terminal"
    base.mkdir()
    sibling = tmp_path / "sibling"
    sibling.mkdir()

    for bad in ("..", ".", "a/b", "x;y", ""):
        terminal_uploads.remove_for_session(bad, base_dir=base)

    assert base.exists()
    assert sibling.exists()
    assert tmp_path.exists()


def test_remove_for_session_residue_logs_warning_not_success(
    tmp_path, monkeypatch, caplog,
):
    """Honest reporting: rmtree(ignore_errors=True) can silently leave the
    folder (or part of it) behind. Residue must emit a WARNING naming the
    folder — never the INFO "removed" success line."""
    folder = tmp_path / "Brandon__claude__root"
    folder.mkdir()
    monkeypatch.setattr(
        terminal_uploads.shutil, "rmtree", lambda *a, **k: None,
    )

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        terminal_uploads.remove_for_session(
            "Brandon__claude__root", base_dir=tmp_path,
        )

    assert folder.exists()
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any(
        "Brandon__claude__root" in r.getMessage()
        and "residue" in r.getMessage()
        for r in warnings
    ), f"expected a residue WARNING, got: {[r.getMessage() for r in caplog.records]}"
    assert not any(
        r.levelno == logging.INFO and "removed" in r.getMessage()
        for r in caplog.records
    ), "residue must not log the INFO success line"


# --- sweep_orphans -----------------------------------------------------


def test_sweep_orphans_keeps_live_removes_only_old_orphans(tmp_path):
    now = 1_800_000_000.0
    week = 7 * 86400

    live = tmp_path / "Brandon__claude__root"
    live.mkdir()
    os.utime(live, (now - 2 * week, now - 2 * week))  # ancient but LIVE -> kept

    old_orphan = tmp_path / "Brandon__terminal__dead"
    old_orphan.mkdir()
    (old_orphan / "f.txt").write_text("x")
    os.utime(old_orphan, (now - week - 60, now - week - 60))

    young_orphan = tmp_path / "Brandon__codex__fresh"
    young_orphan.mkdir()
    os.utime(young_orphan, (now - 3600, now - 3600))  # grace window -> kept

    stray_file = tmp_path / "notes.txt"  # non-directory entries are never touched
    stray_file.write_text("keep")

    removed = terminal_uploads.sweep_orphans(
        {"Brandon__claude__root"}, week, base_dir=tmp_path, now=now,
    )

    assert removed == ["Brandon__terminal__dead"]
    assert not old_orphan.exists()
    assert live.exists()
    assert young_orphan.exists()
    assert stray_file.exists()


def test_sweep_orphans_residue_excluded_from_removed(
    tmp_path, monkeypatch, caplog,
):
    """A permanently-stuck folder must NOT be reported as removed —
    otherwise every hourly pass logs a fresh "swept" success for a folder
    that never went away (inverted signal). Residue → WARNING, and the
    returned list excludes the folder."""
    now = 1_800_000_000.0
    week = 7 * 86400

    stuck = tmp_path / "Brandon__terminal__stuck"
    stuck.mkdir()
    os.utime(stuck, (now - 2 * week, now - 2 * week))

    monkeypatch.setattr(
        terminal_uploads.shutil, "rmtree", lambda *a, **k: None,
    )

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        removed = terminal_uploads.sweep_orphans(
            set(), week, base_dir=tmp_path, now=now,
        )

    assert removed == []
    assert stuck.exists()
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any(
        "Brandon__terminal__stuck" in r.getMessage()
        and "residue" in r.getMessage()
        for r in warnings
    ), f"expected a residue WARNING, got: {[r.getMessage() for r in caplog.records]}"
    assert not any(
        r.levelno == logging.INFO and "swept" in r.getMessage()
        for r in caplog.records
    ), "residue must not log the INFO sweep-success line"


def test_sweep_orphans_missing_base_dir_noop(tmp_path):
    removed = terminal_uploads.sweep_orphans(
        set(), 60.0, base_dir=tmp_path / "does-not-exist", now=0.0,
    )
    assert removed == []
