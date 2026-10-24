"""The in-app updater must never destroy local work.

Audit 2026-10-09 rank 43 (O16-01, O16-08, O16-09):
  * the runner ran `git reset --hard` with no dirty-tree check, so one tap on
    Install discarded every uncommitted change to a tracked file. It now refuses
    unless the caller sets discard_local_changes, and then stashes first, on
    every reset including the rollbacks;
  * /update/rollback ignored the update lock, never restored the pip freeze and
    wrote a phase that was not terminal (so the box reported an interrupted
    update forever after);
  * git fetch (120 s) and pip freeze ran on the event loop and froze the box;
  * a mutex probe that lost the race released the holder's lock.
(The /health boot_id half of the item is in test_health_boot_id.py.)

Real git throughout: a bare origin plus a clone stand in for GitHub and the box,
so stash, tag and reset are the real commands. The subprocess boundary the
runner shells out to (pip, apt, sudo) is faked, and the restart is recorded,
never fired.
"""
from __future__ import annotations

import asyncio
import json
import textwrap
import time
from types import SimpleNamespace

import pytest

from Orchestrator.update import git_ops
from Orchestrator.update import manager as manager_mod
from Orchestrator.update import runner as runner_mod
from Orchestrator.update.manager import UpdateInProgressError, UpdateManager
from Orchestrator.update.runner import UpdateRunner

from ._gitbox import git, head, make_box, make_zip_box, push_upstream, stash_entries, tags


@pytest.fixture
def box(tmp_path):
    b = make_box(tmp_path)
    b.v2 = push_upstream(b)
    return b


@pytest.fixture(autouse=True)
def restarts(monkeypatch):
    """Record restart scheduling; never bounce a real service."""
    scheduled = []
    monkeypatch.setattr(runner_mod, "_fire_detached_restart", lambda: None)
    monkeypatch.setattr(runner_mod, "_schedule_restart",
                        lambda delay_s=2.0: scheduled.append(delay_s))
    return scheduled


class FakeRunner(UpdateRunner):
    """Real git, fake pip/apt/sudo: every _run command is recorded."""

    def __init__(self, root, mgr=None, responder=None, **kw):
        super().__init__(root, mgr or UpdateManager(root), **kw)
        self.calls: list[list[str]] = []
        self._responder = responder or (lambda runner, cmd: (0, ""))

    async def _run(self, cmd, timeout=120.0):
        cmd = list(cmd)
        self.calls.append(cmd)
        return self._responder(self, cmd)


def collect(agen):
    async def _go():
        return [ev async for ev in agen]
    return asyncio.run(_go())


# ── git_ops.tracked_changes ─────────────────────────────────────────────

def test_tracked_changes_lists_paths_of_tracked_files_only(box):
    root = box.root
    assert git_ops.tracked_changes(root) == []
    (root / "app.py").write_text("edited\n")                       # unstaged
    git(root, "mv", "notes.txt", "my notes.txt")                   # staged rename, space
    (root / "untracked.txt").write_text("runtime\n")               # never listed
    assert sorted(git_ops.tracked_changes(root)) == ["app.py", "my notes.txt"]


# ── the runner: dirty tree ──────────────────────────────────────────────

def test_dirty_tracked_file_refuses_the_update_and_keeps_the_edit(box, restarts):
    (box.root / "app.py").write_text("VERSION = 1  # hand fix on the box\n")
    r = FakeRunner(box.root)
    done = collect(r.run())[-1]

    assert done["type"] == "complete" and done["succeeded"] is False
    assert done["refused"] == "dirty_tree"
    assert done["dirty_files"] == ["app.py"]
    assert "app.py" in done["error"] and "nothing was changed" in done["error"]
    assert done["restart_scheduled"] is False
    # Nothing moved: the edit, HEAD, the tags and the stash are all as they were.
    assert (box.root / "app.py").read_text() == "VERSION = 1  # hand fix on the box\n"
    assert head(box.root) == box.v1
    assert tags(box.root) == [] and stash_entries(box.root) == []
    assert restarts == []
    # A refusal is not a failed update: no state that would gate the panel.
    assert UpdateManager(box.root).read_state() is None


def test_staged_changes_count_as_dirty(box):
    (box.root / "notes.txt").write_text("staged edit\n")
    git(box.root, "add", "notes.txt")
    done = collect(FakeRunner(box.root).run())[-1]
    assert done.get("refused") == "dirty_tree"
    assert done["dirty_files"] == ["notes.txt"]


def test_untracked_runtime_files_never_block_an_update(box, restarts):
    (box.root / "Manifest" / "update_state.json.tmp").write_text("{}")
    (box.root / "scratch.log").write_text("runtime noise\n")
    done = collect(FakeRunner(box.root).run())[-1]

    assert done["succeeded"] is True, done
    assert head(box.root) == box.v2
    assert (box.root / "scratch.log").read_text() == "runtime noise\n"
    assert (box.root / "Volumes" / "SNAPSHOT_VOLUME.txt").read_text() == "ledger\n"
    assert done["restart_scheduled"] is True
    assert restarts == [2.0]


def test_discard_local_changes_stashes_before_the_reset(box):
    (box.root / "app.py").write_text("VERSION = 1  # hand fix on the box\n")
    r = FakeRunner(box.root, discard_local_changes=True)
    events = collect(r.run())
    done = events[-1]

    assert done["succeeded"] is True, done
    assert head(box.root) == box.v2
    assert (box.root / "app.py").read_text() == "VERSION = 2\n"
    entries = stash_entries(box.root)
    assert len(entries) == 1 and entries[0].endswith(f"auto-stash-{r.pre_update_tag}")
    # The stash really holds the operator's edit, and the event names it.
    assert "hand fix on the box" in git(box.root, "stash", "show", "-p", "stash@{0}")
    assert done["stash_ref"] == git(box.root, "rev-parse", "stash@{0}")
    assert any("git stash" in e.get("text", "") for e in events if e["type"] == "log")


def test_failed_phase_rollback_stashes_what_changed_since_the_reset(box):
    """The rollback after a failed phase is a reset --hard too: whatever the
    failed phase left in tracked files is stashed, then the code goes back."""
    (box.seed / "Scripts" / "onboarding").mkdir(parents=True)
    (box.seed / "Scripts" / "onboarding" / "system-packages.txt").write_text(
        "newpkg                # MUST_HAVE # needed\n")
    git(box.seed, "add", "-A")
    git(box.seed, "commit", "-q", "-m", "v3 needs a package")
    git(box.seed, "push", "-q", "origin", "main")

    def responder(runner, cmd):
        if cmd[-1] == runner_mod.APT_PREFLIGHT_PROBE_PKG:
            return runner_mod.APT_HELPER_RC_NOT_ALLOWLISTED, ""
        if cmd[-1] == "newpkg":
            # Something touched a tracked file mid-update, then the phase failed.
            (runner.root / "app.py").write_text("VERSION = 2  # touched mid-update\n")
            return 1, "E: unable to locate package"
        return 0, ""

    r = FakeRunner(box.root, responder=responder)
    done = collect(r.run())[-1]

    assert done["succeeded"] is False and done["failed_phase"] == "apt_install"
    assert head(box.root) == box.v1, "code must be rolled back"
    entries = stash_entries(box.root)
    assert entries and entries[0].endswith(f"auto-stash-rollback-{r.pre_update_tag}")
    assert "touched mid-update" in git(box.root, "stash", "show", "-p", "stash@{0}")
    # The rollback runs before the complete event, which reports it.
    assert "The update was rolled back." in done["error"]
    assert done["stash_ref"] == git(box.root, "rev-parse", "stash@{0}")


# ── the runner: event loop (O16-09) ─────────────────────────────────────

def test_git_fetch_and_pip_freeze_run_off_the_event_loop(box, monkeypatch):
    real_fetch = git_ops.fetch_origin_main

    def slow_fetch(root):
        time.sleep(0.6)
        real_fetch(root)

    monkeypatch.setattr(runner_mod.git_ops, "fetch_origin_main", slow_fetch)
    pip = box.root / "Orchestrator" / "venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text(textwrap.dedent("""\
        #!/bin/sh
        if [ "$1" = "freeze" ]; then
            sleep 0.6
            echo "aiohttp==3.13.2"
            echo "WARNING: not a requirement" >&2
        fi
        exit 0
    """))
    pip.chmod(0o755)

    r = FakeRunner(box.root)
    ticks: list[float] = []

    async def main():
        stop = asyncio.Event()

        async def ticker():
            while not stop.is_set():
                ticks.append(time.monotonic())
                await asyncio.sleep(0.02)

        t = asyncio.create_task(ticker())
        events = [ev async for ev in r.run()]
        stop.set()
        await t
        return events

    events = asyncio.run(main())
    assert events[-1]["succeeded"] is True, events[-1]
    assert len(ticks) > 20, "the loop never got to run while the runner worked"
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.3, f"the loop stalled for {max(gaps):.2f}s"
    # The freeze is pip's stdout only: a warning on stderr must not land in the
    # file a rollback reinstalls from.
    # Its first line names the update it belongs to (see the rollback tests).
    assert (box.root / "Manifest" / "pre_update_pip_freeze.txt").read_text() == (
        f"# blackbox-freeze-for: {r.pre_update_tag}\naiohttp==3.13.2\n")


def test_a_failed_freeze_never_leaves_a_stale_one_behind(box):
    """A rollback reinstalls Manifest/pre_update_pip_freeze.txt. If this update
    could not freeze, an older update's file must not stand in for it."""
    (box.root / "Manifest" / "pre_update_pip_freeze.txt").write_text("ancient==0.1\n")
    pip = box.root / "Orchestrator" / "venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text("#!/bin/sh\nexit 3\n")
    pip.chmod(0o755)
    collect(FakeRunner(box.root).run())
    assert not (box.root / "Manifest" / "pre_update_pip_freeze.txt").exists()


# ── the mutex ───────────────────────────────────────────────────────────

def test_a_losing_acquire_never_releases_the_holders_lock(tmp_path):
    root = tmp_path / "box"
    root.mkdir()
    mgr = UpdateManager(root)            # the routes share one instance
    with mgr.acquire_or_raise():
        with pytest.raises(UpdateInProgressError):
            with mgr.acquire_or_raise():
                pass
        assert UpdateManager(root).is_locked(), "the holder lost its lock"
    assert not UpdateManager(root).is_locked()


def test_rolled_back_is_terminal(tmp_path):
    root = tmp_path / "box"
    root.mkdir()
    mgr = UpdateManager(root)
    mgr.write_state(task_id="rollback-1", phase=manager_mod.PHASE_ROLLED_BACK,
                    target_sha="a" * 40, from_sha="b" * 40,
                    pre_update_tag="pre-update-1")
    assert manager_mod.PHASE_ROLLED_BACK in manager_mod.TERMINAL_PHASES
    assert mgr.is_interrupted() is False


# ── routes ──────────────────────────────────────────────────────────────

def wire_routes(box, monkeypatch, client):
    """Point the update routes at `box` and record every runner they make."""
    from Orchestrator.routes import update_routes
    mgr = UpdateManager(box.root)
    monkeypatch.setattr(update_routes, "_ROOT", box.root)
    monkeypatch.setattr(update_routes, "_MGR", mgr)
    monkeypatch.setattr(update_routes, "_STATUS_CACHE", {})
    monkeypatch.setattr(update_routes, "_STATUS_CACHE_TS", 0.0)
    monkeypatch.setattr(update_routes, "count_active_cli_sessions", lambda: 0)
    monkeypatch.setattr(update_routes, "list_active_cli_sessions", lambda: [])
    made: list[FakeRunner] = []

    class RecordingRunner(FakeRunner):
        def __init__(self, root, manager, **kw):
            super().__init__(root, manager, **kw)
            made.append(self)

    monkeypatch.setattr(update_routes, "UpdateRunner", RecordingRunner)
    return SimpleNamespace(client=client, box=box, mgr=mgr, runners=made)


@pytest.fixture
def routes(box, monkeypatch, app_client):
    return wire_routes(box, monkeypatch, app_client)


def sse_events(client, task_id) -> list[dict]:
    r = client.get("/update/log/stream", params={"task_id": task_id})
    assert r.status_code == 200
    out = []
    for line in r.text.splitlines():
        if line.startswith("data: "):
            ev = json.loads(line[len("data: "):])
            if ev.get("type") != "heartbeat":
                out.append(ev)
    return out


def run_update(rt, **body):
    r = rt.client.post("/update/start", json=body)
    assert r.status_code == 200, r.text
    return sse_events(rt.client, r.json()["task_id"])


def test_status_and_preflight_report_dirty_files(routes):
    (routes.box.root / "app.py").write_text("hand edit\n")
    pre = routes.client.post("/update/preflight").json()
    assert pre["dirty_files"] == ["app.py"]
    assert pre["commits_behind"] == 1
    status = routes.client.get("/update/status").json()
    assert status["dirty_files"] == ["app.py"]
    (routes.box.root / "app.py").write_text("VERSION = 1\n")
    # Live, not cached: the cached status still answers the current tree.
    assert routes.client.get("/update/status").json()["dirty_files"] == []


def test_start_refuses_a_dirty_tree_with_the_file_list(routes):
    (routes.box.root / "app.py").write_text("hand edit\n")
    r = routes.client.post("/update/start", json={})
    assert r.status_code == 409
    body = r.json()
    assert body["refused"] == "dirty_tree" and body["dirty_files"] == ["app.py"]
    assert isinstance(body["detail"], str) and "app.py" in body["detail"]
    assert routes.runners == []
    assert (routes.box.root / "app.py").read_text() == "hand edit\n"
    assert head(routes.box.root) == routes.box.v1


def test_start_with_consent_stashes_and_updates(routes):
    (routes.box.root / "app.py").write_text("hand edit\n")
    done = run_update(routes, discard_local_changes=True)[-1]
    assert done["succeeded"] is True, done
    assert head(routes.box.root) == routes.box.v2
    assert len(stash_entries(routes.box.root)) == 1


def test_rollback_refuses_while_the_update_lock_is_held(routes):
    run_update(routes)
    other = UpdateManager(routes.box.root)   # another process's view
    with other.acquire_or_raise():
        r = routes.client.post("/update/rollback")
    assert r.status_code == 409
    assert head(routes.box.root) == routes.box.v2, "nothing may move under a held lock"


def test_rollback_runs_under_the_lock_and_ends_terminal(routes, restarts):
    run_update(routes)
    tag = routes.mgr.read_state()["pre_update_tag"]
    restarts.clear()
    r = routes.client.post("/update/rollback")      # the Portal posts no body
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reverted_to"] == tag and body["task_id"].startswith("rollback-")
    events = sse_events(routes.client, body["task_id"])
    done = events[-1]

    assert done["succeeded"] is True, done
    assert done["restart_scheduled"] is True and restarts == [2.0]
    assert head(routes.box.root) == routes.box.v1
    state = routes.mgr.read_state()
    assert state["phase"] == "rolled_back" and state["pre_update_tag"] == tag
    assert routes.client.get("/update/state").json()["is_interrupted"] is False


def test_rollback_restores_the_pip_freeze_when_requirements_moved(tmp_path, monkeypatch, app_client):
    b = make_box(tmp_path, {"requirements.txt": "aiohttp==3.13.2\n"})
    b.v2 = push_upstream(b, requirements=True)
    rt = wire_routes(b, monkeypatch, app_client)
    (b.root / "requirements.lock.txt").write_text("aiohttp==3.14.5 \\\n    --hash=sha256:" + "0" * 64 + "\n")
    (b.root / "Manifest" / "pre_update_pip_freeze.txt").write_text("aiohttp==3.13.2\n")
    run_update(rt)
    r = rt.client.post("/update/rollback", json={})
    assert r.status_code == 200, r.text
    done = sse_events(rt.client, r.json()["task_id"])[-1]
    assert done["succeeded"] is True, done

    rollback_runner = rt.runners[-1]
    restores = [c for c in rollback_runner.calls if "--force-reinstall" in c]
    assert restores, rollback_runner.calls
    assert restores[0][restores[0].index("-r") + 1].endswith("Manifest/pre_update_pip_freeze.txt")
    assert head(b.root) == b.v1


def test_rollback_refuses_a_dirty_tree_unless_told_to_stash(routes):
    run_update(routes)
    (routes.box.root / "app.py").write_text("VERSION = 2  # hot fix\n")
    r = routes.client.post("/update/rollback")
    assert r.status_code == 409
    assert r.json()["dirty_files"] == ["app.py"]
    assert (routes.box.root / "app.py").read_text() == "VERSION = 2  # hot fix\n"

    r = routes.client.post("/update/rollback", json={"discard_local_changes": True})
    assert r.status_code == 200, r.text
    done = sse_events(routes.client, r.json()["task_id"])[-1]
    assert done["succeeded"] is True, done
    assert head(routes.box.root) == routes.box.v1
    assert "hot fix" in git(routes.box.root, "stash", "show", "-p", "stash@{0}")


@pytest.mark.parametrize("tag", ["", "pre-update-404", "--orphan"])
def test_rollback_needs_a_real_anchor(routes, tag):
    """A failed fetch persists pre_update_tag "" and a hand-edited state can
    carry anything: neither may reach `git reset --hard`."""
    routes.mgr.write_state(task_id="update-1", phase="failed", target_sha="",
                           from_sha="", pre_update_tag=tag)
    r = routes.client.post("/update/rollback")
    assert r.status_code == 404
    assert head(routes.box.root) == routes.box.v1



# ── review follow-ups: nothing slips in between the check and the reset ──
#
# The dirty check used to run once, seconds (the update: tag, freezes, diff)
# to minutes (the rollback: a pip --force-reinstall of each venv) before the
# `git reset --hard`. An edit saved in that window, by a CLI agent in a
# terminal or over SSH, was reset over with no stash. Every reset now stashes
# what changed since the check, and moves aside the untracked files it would
# overwrite, in the same worker-thread step as the reset itself.

class EditDuringFreeze(FakeRunner):
    """A tracked file is edited while the runner freezes the venvs."""

    def _freeze_venvs(self):
        (self.root / "notes.txt").write_text("agent edit made during the update\n")


def test_an_edit_made_after_the_check_is_stashed_not_reset_over(box):
    r = EditDuringFreeze(box.root)
    events = collect(r.run())
    done = events[-1]

    assert done["succeeded"] is True, done
    assert head(box.root) == box.v2
    entries = stash_entries(box.root)
    assert len(entries) == 1 and entries[0].endswith(f"auto-stash-during-{r.pre_update_tag}")
    assert "agent edit made during the update" in git(box.root, "stash", "show", "-p", "stash@{0}")
    assert done["stash_ref"] == git(box.root, "rev-parse", "stash@{0}")
    logs = [e["text"] for e in events if e["type"] == "log"]
    assert any("notes.txt" in t and "stash" in t for t in logs), logs


def _updated_box(tmp_path, *, requirements=False):
    """A box after a successful in-app update v1 -> v2 (tag pre-update-1)."""
    extra = {"requirements.txt": "aiohttp==3.13.2\n"} if requirements else None
    b = make_box(tmp_path, extra)
    b.v2 = push_upstream(b, requirements=requirements)
    git(b.root, "tag", "pre-update-1", "HEAD")
    git(b.root, "fetch", "-q", "origin")
    git(b.root, "reset", "-q", "--hard", "origin/main")
    b.mgr = UpdateManager(b.root)
    b.mgr.write_state(task_id="update-1", phase="complete", target_sha=b.v2,
                      from_sha=b.v1, pre_update_tag="pre-update-1")
    return b


def test_an_edit_made_during_the_rollbacks_venv_restore_survives(tmp_path):
    """The rollback reinstalled the venvs (minutes) and only then reset the
    code, so an edit saved during the pip run was destroyed. The code now goes
    back first, and the venv restore can no longer race a reset."""
    b = _updated_box(tmp_path, requirements=True)
    (b.root / "Manifest" / "pre_update_pip_freeze.txt").write_text("aiohttp==3.13.2\n")

    def responder(runner, cmd):
        if "--force-reinstall" in cmd:
            (runner.root / "notes.txt").write_text("edit saved during the pip restore\n")
        return 0, ""

    rb = FakeRunner(b.root, b.mgr, responder=responder, kind="rollback")
    done = collect(rb.rollback("pre-update-1"))[-1]

    assert done["succeeded"] is True, done
    assert any("--force-reinstall" in c for c in rb.calls), rb.calls
    assert head(b.root) == b.v1
    assert (b.root / "notes.txt").read_text() == "edit saved during the pip restore\n"


class EditAfterRollbackCheck(FakeRunner):
    """A tracked file is edited right after the rollback's dirty check."""

    def _write_state(self, phase):
        super()._write_state(phase)
        if phase == manager_mod.PHASE_ROLLBACK:
            (self.root / "notes.txt").write_text("edit saved as the rollback started\n")


def test_a_rollback_stashes_an_edit_made_after_its_check(tmp_path):
    b = _updated_box(tmp_path)
    rb = EditAfterRollbackCheck(b.root, b.mgr, kind="rollback")
    done = collect(rb.rollback("pre-update-1"))[-1]

    assert done["succeeded"] is True, done
    assert head(b.root) == b.v1
    entries = stash_entries(b.root)
    assert entries and entries[0].endswith("auto-stash-during-rollback-pre-update-1")
    assert "edit saved as the rollback started" in git(b.root, "stash", "show", "-p", "stash@{0}")


# ── untracked files where the target has one ─────────────────────────────

def _push_adds(box, files):
    for rel, body in files.items():
        p = box.seed / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    git(box.seed, "add", "-A")
    git(box.seed, "commit", "-q", "-m", "upstream adds files")
    git(box.seed, "push", "-q", "origin", "main")
    return git(box.seed, "rev-parse", "HEAD")


def test_paths_in_the_way_lists_untracked_files_a_reset_would_overwrite(tmp_path):
    b = make_box(tmp_path)
    target = _push_adds(b, {"docs/plan.md": "upstream\n", "top.md": "upstream\n"})
    git(b.root, "fetch", "-q", "origin")
    (b.root / "docs").write_text("a file where the update needs a directory\n")
    (b.root / "top.md").write_text("operator's copy\n")
    (b.root / "scratch.log").write_text("unrelated runtime file\n")
    assert git_ops.paths_in_the_way(b.root, target) == ["docs", "top.md"]
    assert git_ops.paths_in_the_way(b.root, "HEAD") == []


def test_an_untracked_file_where_the_update_adds_one_is_moved_aside(tmp_path):
    """`git reset --hard` overwrites an untracked file at a path the target
    adds, and neither a stash nor a tag holds it: the operator's only copy was
    gone. It is moved under Manifest/displaced/<tag>/ first."""
    b = make_box(tmp_path)
    _push_adds(b, {"docs/audit.md": "upstream version\n"})
    (b.root / "docs").mkdir()
    (b.root / "docs" / "audit.md").write_text("operator's only copy\n")
    assert git_ops.tracked_changes(b.root) == []

    r = FakeRunner(b.root)
    events = collect(r.run())
    done = events[-1]

    assert done["succeeded"] is True, done
    assert (b.root / "docs" / "audit.md").read_text() == "upstream version\n"
    kept = b.root / "Manifest" / "displaced" / r.pre_update_tag / "docs" / "audit.md"
    assert kept.read_text() == "operator's only copy\n"
    logs = [e["text"] for e in events if e["type"] == "log"]
    assert any("docs/audit.md" in t and "Manifest/displaced" in t for t in logs), logs


# ── a failed update never resets when the stash fails ───────────────────

def test_a_failed_update_keeps_the_new_code_when_the_stash_fails(box, monkeypatch):
    """The rollback after a failed phase reset even when the stash before it
    failed, destroying what changed in tracked files during the update."""
    (box.seed / "Scripts" / "onboarding").mkdir(parents=True)
    (box.seed / "Scripts" / "onboarding" / "system-packages.txt").write_text(
        "newpkg                # MUST_HAVE # needed\n")
    git(box.seed, "add", "-A")
    git(box.seed, "commit", "-q", "-m", "v3 needs a package")
    git(box.seed, "push", "-q", "origin", "main")
    target = git(box.seed, "rev-parse", "HEAD")

    def responder(runner, cmd):
        if cmd[-1] == runner_mod.APT_PREFLIGHT_PROBE_PKG:
            return runner_mod.APT_HELPER_RC_NOT_ALLOWLISTED, ""
        if cmd[-1] == "newpkg":
            (runner.root / "notes.txt").write_text("operator edit during the apt phase\n")
            return 1, "E: unable to locate package"
        return 0, ""

    def failing_stash(root, message):
        raise runner_mod.subprocess.CalledProcessError(
            1, ["git", "stash"], "", "Cannot save the current worktree state")

    monkeypatch.setattr(runner_mod.git_ops, "stash_push", failing_stash)
    done = collect(FakeRunner(box.root, responder=responder).run())[-1]

    assert done["succeeded"] is False and done["failed_phase"] == "apt_install"
    assert head(box.root) == target, "no reset may run once the stash failed"
    assert (box.root / "notes.txt").read_text() == "operator edit during the apt phase\n"
    assert "NOT rolled back" in done["error"]
    assert "Cannot save the current worktree state" in done["error"]


# ── tracked_changes: a worktree rename's source path ────────────────────

def test_tracked_changes_reads_a_worktree_rename_as_one_path(box):
    """An intent-to-add file can pair with a deleted one as a worktree rename
    (" R new\\0old\\0"): the source path was parsed as an entry of its own."""
    root = box.root
    (root / "brand-new-file.txt").write_text("operator notes\n")
    git(root, "add", "-N", "brand-new-file.txt")
    (root / "notes.txt").unlink()
    assert git_ops.tracked_changes(root) == ["brand-new-file.txt"]


# ── the pip freeze belongs to one update ────────────────────────────────

def test_a_rollback_skips_a_freeze_taken_for_another_update(tmp_path):
    """Scripts/update.sh writes the same freeze file but not update_state.json:
    after a manual run, the in-app Rollback reset the code to the in-app tag
    and reinstalled the manual run's packages. A freeze now names the update
    it belongs to, and a rollback to another tag leaves the venv alone."""
    b = _updated_box(tmp_path, requirements=True)
    (b.root / "Manifest" / "pre_update_pip_freeze.txt").write_text(
        "# blackbox-freeze-for: pre-update-999\naiohttp==3.12.0\n")
    rb = FakeRunner(b.root, b.mgr, kind="rollback")
    events = collect(rb.rollback("pre-update-1"))
    done = events[-1]

    assert done["succeeded"] is True, done
    assert not [c for c in rb.calls if "--force-reinstall" in c], rb.calls
    assert head(b.root) == b.v1
    logs = [e["text"] for e in events if e["type"] == "log"]
    assert any("pre-update-999" in t for t in logs), logs


def test_a_rollback_restores_a_freeze_taken_for_its_own_update(tmp_path):
    b = _updated_box(tmp_path, requirements=True)
    (b.root / "Manifest" / "pre_update_pip_freeze.txt").write_text(
        "# blackbox-freeze-for: pre-update-1\naiohttp==3.13.2\n")
    rb = FakeRunner(b.root, b.mgr, kind="rollback")
    done = collect(rb.rollback("pre-update-1"))[-1]
    assert done["succeeded"] is True, done
    assert [c for c in rb.calls if "--force-reinstall" in c], rb.calls


# ── a ZIP-installed box (install.sh Step 0a) ─────────────────────────────
#
# Step 0a and git_ops.lazy_init point HEAD at origin/main over the ZIP's files
# and never fill the index, so `git status` lists EVERY tracked file as a
# staged deletion: the panel said thousands of files had local changes and
# every Install was refused until the operator agreed to stash them all.

def test_a_zip_install_box_lists_only_files_that_differ_from_head(box):
    root = make_zip_box(box, box.v2)
    assert git_ops.tracked_changes(root) == []
    (root / "notes.txt").write_text("customer edit\n")
    assert git_ops.tracked_changes(root) == ["notes.txt"]
    # The probe read a throwaway index: the box's own is still unfilled, and
    # the throwaway one is gone.
    assert git(root, "ls-files") == ""
    assert not list((root / ".git").glob("blackbox-status-*"))


def _push_v3(box):
    (box.seed / "app.py").write_text("VERSION = 3\n")
    git(box.seed, "commit", "-q", "-am", "v3")
    git(box.seed, "push", "-q", "origin", "main")


def test_a_zip_install_box_updates_without_asking_when_nothing_differs(box):
    root = make_zip_box(box, box.v2)       # the ZIP is the commit HEAD names
    _push_v3(box)
    done = collect(FakeRunner(root).run())[-1]
    assert done["succeeded"] is True, done
    assert (root / "app.py").read_text() == "VERSION = 3\n"
    assert stash_entries(root) == []


def test_a_zip_install_box_refuses_and_stashes_only_the_real_edit(box):
    root = make_zip_box(box, box.v2)
    _push_v3(box)
    (root / "notes.txt").write_text("customer edit\n")

    done = collect(FakeRunner(root).run())[-1]
    assert done.get("refused") == "dirty_tree" and done["dirty_files"] == ["notes.txt"]

    done = collect(FakeRunner(root, discard_local_changes=True).run())[-1]
    assert done["succeeded"] is True, done
    assert (root / "app.py").read_text() == "VERSION = 3\n"
    assert "customer edit" in git(root, "show", "stash@{0}:notes.txt")
