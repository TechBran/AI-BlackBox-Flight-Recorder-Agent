"""Tests for the pip phase of Orchestrator/update/runner.py.

Regression cover for X01-deps-python-02/-05 (audit rank 35): the phase ran a
plain `pip install -r requirements.txt`, so a satisfied floor never moved, the
transitive closure was whatever pip resolved that day, and nothing checked what
was downloaded. It now installs requirements.lock.txt with --require-hashes,
and a failure stops the update loudly: there is no quiet retry from an
unhashed or unpinned file.

Same style as test_runner_apt.py: a real UpdateManager over a tmp_path root,
fakes only at the git and subprocess boundary.
"""
import asyncio

import pytest

from Orchestrator.update import runner as runner_mod
from Orchestrator.update.manager import UpdateManager
from Orchestrator.update.runner import PIP_LOCK_FILE, UpdateRunner

FROM_SHA = "a" * 40
TARGET_SHA = "b" * 40
LOCK_BODY = (
    "aiohttp==3.14.5 \\\n"
    "    --hash=sha256:" + "0" * 64 + "\n"
)


@pytest.fixture(autouse=True)
def no_real_restart(monkeypatch):
    """Never one scheduling change away from bouncing the dev box's service."""
    monkeypatch.setattr(runner_mod, "_fire_detached_restart", lambda: None)


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "blackbox"
    (r / "Manifest").mkdir(parents=True)
    (r / "Scripts" / "onboarding").mkdir(parents=True)
    (r / PIP_LOCK_FILE).write_text(LOCK_BODY)
    return r


@pytest.fixture
def resets(monkeypatch):
    recorded = []
    monkeypatch.setattr(runner_mod.git_ops, "current_sha", lambda p: FROM_SHA)
    monkeypatch.setattr(runner_mod.git_ops, "fetch_origin_main", lambda p: None)
    monkeypatch.setattr(runner_mod.git_ops, "latest_origin_sha", lambda p: TARGET_SHA)
    monkeypatch.setattr(runner_mod.git_ops, "tag", lambda p, t: None)
    monkeypatch.setattr(runner_mod.git_ops, "current_short", lambda p: TARGET_SHA[:7])
    monkeypatch.setattr(runner_mod.git_ops, "reset_hard",
                        lambda p, ref: recorded.append(ref))
    return recorded


def set_changed(monkeypatch, files):
    monkeypatch.setattr(runner_mod.git_ops, "diff_files", lambda p, a, b: list(files))


class FakeRunner(UpdateRunner):
    def __init__(self, root, responder):
        super().__init__(root, UpdateManager(root))
        self.calls = []
        self._responder = responder

    async def _run(self, cmd, timeout=120.0):
        cmd = list(cmd)
        self.calls.append(cmd)
        return self._responder(cmd)


def drive(runner):
    async def _collect():
        return [ev async for ev in runner.run()]
    return asyncio.run(_collect())


def pip_installs(runner):
    return [c for c in runner.calls if c[:1] and c[0].endswith("/pip") and "install" in c]


def names_unhashed_manifest(cmd):
    return any(a.endswith(("requirements.txt", "requirements.in")) for a in cmd)


@pytest.mark.parametrize("changed", [["requirements.lock.txt"], ["requirements.txt"]])
def test_pip_phase_installs_the_hashed_lock(monkeypatch, root, resets, changed):
    set_changed(monkeypatch, changed)
    r = FakeRunner(root, lambda cmd: (0, ""))
    events = drive(r)

    assert events[-1]["succeeded"] is True
    installs = pip_installs(r)
    assert len(installs) == 1
    cmd = installs[0]
    assert cmd[0] == str(root / "Orchestrator/venv/bin/pip")
    assert "--require-hashes" in cmd
    assert cmd[cmd.index("-r") + 1] == str(root / PIP_LOCK_FILE)
    assert not names_unhashed_manifest(cmd)


def test_lock_install_failure_rolls_back_and_never_falls_back(monkeypatch, root, resets):
    """A hash mismatch or a missing wheel is exactly when a quiet retry from an
    unhashed file would install something nobody vetted. Fail, restore the
    pre-update freeze, put the code back, and say how to diagnose it."""
    (root / "Manifest" / "pre_update_pip_freeze.txt").write_text("aiohttp==3.13.2\n")
    set_changed(monkeypatch, ["requirements.lock.txt"])

    def responder(cmd):
        if "--require-hashes" in cmd:
            return 1, "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE."
        return 0, ""

    r = FakeRunner(root, responder)
    events = drive(r)
    done = events[-1]

    assert done["succeeded"] is False
    assert done["failed_phase"] == runner_mod.PHASE_PIP_INSTALL
    assert "requirements.lock.txt" in done["error"]
    assert "--require-hashes" in done["error"]
    assert "DO NOT MATCH THE HASHES" in done["error"]
    installs = pip_installs(r)
    assert sum("--require-hashes" in c for c in installs) == 1, "no retry of the lock"
    assert not any(names_unhashed_manifest(c) for c in installs), \
        "must never fall back to requirements.txt / requirements.in"
    restore = [c for c in installs if "--force-reinstall" in c]
    assert restore and restore[0][restore[0].index("-r") + 1].endswith(
        "Manifest/pre_update_pip_freeze.txt")
    assert resets[-1] == r.pre_update_tag, "code must be rolled back"


def test_missing_lock_fails_before_touching_the_venv(monkeypatch, root, resets):
    """A tree without its lock (a half-applied update, a hand edit) must stop
    the update, not install from whatever manifest happens to be there."""
    (root / PIP_LOCK_FILE).unlink()
    (root / "requirements.txt").write_text("aiohttp>=3.9\n")
    set_changed(monkeypatch, ["requirements.txt"])
    r = FakeRunner(root, lambda cmd: (0, ""))
    events = drive(r)
    done = events[-1]

    assert done["succeeded"] is False
    assert done["failed_phase"] == runner_mod.PHASE_PIP_INSTALL
    assert PIP_LOCK_FILE in done["error"]
    assert pip_installs(r) == []
    assert resets[-1] == r.pre_update_tag


def test_install_sh_installs_the_hashed_lock():
    """The other install path: a fresh box (and Scripts/update.sh, which
    re-runs install.sh) must build the Orchestrator venv from the hashed lock
    and stop on failure, not resolve requirements.txt fresh."""
    from pathlib import Path
    sh = (Path(__file__).resolve().parents[3] / "Scripts" / "install.sh").read_text()
    assert ('"$BLACKBOX_ROOT/Orchestrator/venv/bin/pip" install \\\n'
            '        --require-hashes -r "$BLACKBOX_ROOT/requirements.lock.txt"; then') in sh
    assert 'venv/bin/pip" install -r "$BLACKBOX_ROOT/requirements.txt"' not in sh


def test_code_only_update_skips_pip(monkeypatch, root, resets):
    set_changed(monkeypatch, ["Orchestrator/web_tools.py"])
    r = FakeRunner(root, lambda cmd: (0, ""))
    events = drive(r)
    assert events[-1]["succeeded"] is True
    assert pip_installs(r) == []
