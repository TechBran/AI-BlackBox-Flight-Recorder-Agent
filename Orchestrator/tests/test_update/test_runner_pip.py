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
    # A clean tree (the dirty-tree gate has its own tests in test_updater_safety).
    monkeypatch.setattr(runner_mod.git_ops, "tracked_changes", lambda p: [])
    monkeypatch.setattr(runner_mod.git_ops, "paths_in_the_way", lambda p, ref: [])
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
    """An install FROM requirements.txt / requirements.in (`-r`). Naming
    requirements.txt as constraints (`-c`) installs nothing by itself."""
    return any(a.endswith(("requirements.txt", "requirements.in"))
               and i > 0 and cmd[i - 1] == "-r"
               for i, a in enumerate(cmd))


def pip_checks(runner):
    return [c for c in runner.calls if c[:1] and c[0].endswith("/pip") and c[1:2] == ["check"]]


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


# ── installer-added packages on top of the lock (U14 review) ───────────────
# installer/templates/blackbox-install-reranker-cpu.sh adds the CPU reranker
# (torch + sentence-transformers) to the Orchestrator venv on MID-tier boxes.
# A lock install moves any pin they depend on back to the lock's version and
# pip still exits 0 (it only prints the conflict), so before this the first
# lock-changing update left transformers unimportable and reranking silently
# off. The pip phase now reinstalls them under the lock's pins afterwards.

MID_TIER_FREEZE = (
    "aiohttp==3.13.2\n"
    "huggingface-hub==1.33.0\n"
    "Sentence_Transformers==6.2.0\n"
    "torch==2.11.0+cpu\n"
    "transformers==5.20.2\n"
)


def test_lock_install_reapplies_the_cpu_reranker_under_the_lock_pins(
        monkeypatch, root, resets):
    (root / "Manifest" / "pre_update_pip_freeze.txt").write_text(MID_TIER_FREEZE)
    set_changed(monkeypatch, ["requirements.lock.txt"])
    r = FakeRunner(root, lambda cmd: (0, ""))
    events = drive(r)

    assert events[-1]["succeeded"] is True
    installs = pip_installs(r)
    assert len(installs) == 2
    lock_cmd, extras_cmd = installs
    assert "--require-hashes" in lock_cmd
    assert extras_cmd[extras_cmd.index("-c") + 1] == str(root / "requirements.txt")
    assert [a for a in extras_cmd if not a.startswith("-") and a not in (
        extras_cmd[0], "install", str(root / "requirements.txt"))] == list(
            runner_mod.LOCK_EXTRAS)
    assert "--upgrade" not in extras_cmd and "-U" not in extras_cmd
    assert not any("download.pytorch.org" in a for a in extras_cmd), \
        "torch is not touched by the lock; only the PyPI packages are reinstalled"
    assert r.calls.index(extras_cmd) > r.calls.index(lock_cmd)
    checks = pip_checks(r)
    assert checks and r.calls.index(checks[0]) > r.calls.index(extras_cmd)


def test_no_reranker_reinstall_on_a_box_without_it(monkeypatch, root, resets):
    (root / "Manifest" / "pre_update_pip_freeze.txt").write_text("aiohttp==3.13.2\n")
    set_changed(monkeypatch, ["requirements.lock.txt"])
    r = FakeRunner(root, lambda cmd: (0, ""))
    events = drive(r)

    assert events[-1]["succeeded"] is True
    assert len(pip_installs(r)) == 1
    assert len(pip_checks(r)) == 1


def test_reranker_reinstall_failure_rolls_back(monkeypatch, root, resets):
    """No version of the reranker stack fits the new pins (or PyPI is down):
    the box would keep the lock and lose reranking, so the update fails and
    the venv goes back to its pre-update freeze."""
    (root / "Manifest" / "pre_update_pip_freeze.txt").write_text(MID_TIER_FREEZE)
    set_changed(monkeypatch, ["requirements.lock.txt"])

    def responder(cmd):
        if "-c" in cmd:
            return 1, "ERROR: ResolutionImpossible: transformers needs huggingface-hub>=1.31.0"
        return 0, ""

    r = FakeRunner(root, responder)
    events = drive(r)
    done = events[-1]

    assert done["succeeded"] is False
    assert done["failed_phase"] == runner_mod.PHASE_PIP_INSTALL
    assert "sentence-transformers" in done["error"]
    assert "ResolutionImpossible" in done["error"]
    assert "blackbox-install-reranker-cpu.sh" in done["error"]
    assert [c for c in pip_installs(r) if "--force-reinstall" in c], "venv must be restored"
    assert resets[-1] == r.pre_update_tag


def test_rollback_skips_pins_from_another_index(monkeypatch, root, resets):
    """`torch==2.11.0+cpu` came from the PyTorch CPU index; PyPI cannot serve
    it, and one unservable line makes pip refuse the whole restore. The lock
    never touches torch, so the restore leaves it where it is."""
    (root / "Manifest" / "pre_update_pip_freeze.txt").write_text(MID_TIER_FREEZE)
    set_changed(monkeypatch, ["requirements.lock.txt"])

    def responder(cmd):
        if "--require-hashes" in cmd:
            return 1, "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE."
        return 0, ""

    r = FakeRunner(root, responder)
    drive(r)
    restore = [c for c in pip_installs(r) if "--force-reinstall" in c]
    assert len(restore) == 1
    restored = (root / restore[0][restore[0].index("-r") + 1]).read_text().splitlines()
    assert "torch==2.11.0+cpu" not in restored
    assert "huggingface-hub==1.33.0" in restored
    assert "transformers==5.20.2" in restored
    assert "aiohttp==3.13.2" in restored


def test_pip_check_conflicts_are_reported_not_fatal(monkeypatch, root, resets):
    """A package nothing in the lock asked for (a hand-installed sse-starlette
    on MS-02 wants starlette>=0.49.1) must not block every update, but the
    update log has to say which requirements the venv no longer meets."""
    set_changed(monkeypatch, ["requirements.lock.txt"])
    conflict = ("sse-starlette 3.4.4 has requirement starlette>=0.49.1, "
                "but you have starlette 0.48.0.")

    def responder(cmd):
        if cmd[1:2] == ["check"]:
            return 1, conflict
        return 0, ""

    r = FakeRunner(root, responder)
    events = drive(r)

    assert events[-1]["succeeded"] is True
    logs = " ".join(e.get("text", "") for e in events if e.get("type") == "log")
    assert "WARNING" in logs and conflict in logs


def test_cpu_reranker_installer_installs_under_the_lock_pins():
    """The installer side of the same contract: torch and sentence-transformers
    resolve inside the lock's pins (-c requirements.txt), so the reranker it
    installs is one the next lock install does not break, and the runner
    reinstalls the same PyPI packages it installs."""
    from pathlib import Path
    sh = (Path(__file__).resolve().parents[3] / "installer" / "templates"
          / "blackbox-install-reranker-cpu.sh").read_text().replace("\\\n", " ")
    cmds = [line for line in sh.splitlines() if '"$ORCH_PIP" install' in line]
    assert len(cmds) == 2, cmds
    for cmd in cmds:
        assert '-c "$BLACKBOX_ROOT/requirements.txt"' in cmd, cmd
    pypi = [c for c in cmds if "--index-url" not in c]
    for name in runner_mod.LOCK_EXTRAS:
        assert any(name in c for c in pypi), f"{name} is not what the installer installs"
