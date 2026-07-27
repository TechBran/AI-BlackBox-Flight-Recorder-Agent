"""UpdateRunner — orchestrates the full update flow with SSE-friendly events.

Consumed by Orchestrator/routes/update_routes.py:/update/log/stream which
relays events to the Portal as SSE messages. The runner itself is async
so each phase yields control between subprocess calls (keeps uvicorn
responsive for /update/status polling during a long pip install).

EVENT SHAPES (each yielded as a dict; route serializes to SSE 'data: {json}'):
  {type: "phase", phase: "apt_install", started_iso: "..."}
  {type: "log", text: "...", phase: "apt_install"}
  {type: "heartbeat", phase: "...", elapsed_s: 42}  # every 2s if no other events
  {type: "complete", succeeded: true|false, sha_before, sha_after, error?}

DESIGN NOTES (audit-driven):
  - audit C2: code reset --hard runs INSIDE the runner's critical section
    AFTER all preconditions (pip install, etc.) have validated against
    a worktree-staging copy. Restart is scheduled via call_later 2s AFTER
    the SSE complete event flushes (audit M4) so the browser receives
    "done" before the service dies.
  - audit M1: pre-update pip freeze captured. On failure, pip-sync-style
    rollback uses the captured freeze.
  - audit M5: state machine writes happen at every phase boundary so a
    crash mid-update leaves enough breadcrumb for the startup banner.
  - customer 2026-07-27 (stale helper after commit 5eabbe45): the apt phase
    preflights the installed helper before mutating anything, and a SHOULD_HAVE
    package failure warns instead of discarding the whole update. See
    _apt_preflight. Phase order stays monotonic — the one privileged install.sh
    re-run happens in systemd_regen, AFTER apt/pip/mcp, never before.

SELF-UPDATE CEILING: update_routes.py imports UpdateRunner at module load and
iterates one instance for the whole update — there is no reload and no re-exec,
so the OLD runner.py runs everything after `git reset --hard`, including the
phases below. Any fix in THIS file first takes effect on the NEXT update. That
is inherent, not a bug: it is why the durable fixes for the stale-helper class
live in install.sh + the helper template (which the reset DOES refresh) and in
the startup hooks (startup_assert_helpers_current / _sudoers_current, which
re-template the root-owned helpers on every service start).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional

from Orchestrator.update import changes as changes_mod
from Orchestrator.update import git_ops
from Orchestrator.update.manager import (
    UpdateManager, UpdateInProgressError,
    PHASE_STAGING, PHASE_APT_INSTALL, PHASE_PIP_INSTALL, PHASE_MCP_INSTALL,
    PHASE_SYSTEMD_REGEN, PHASE_RESET_HARD, PHASE_RESTART_PENDING,
    PHASE_COMPLETE, PHASE_FAILED,
)


# ── apt allowlist buckets (Scripts/onboarding/system-packages.txt) ──────
# MUST_HAVE failures abort + roll back; SHOULD_HAVE failures warn + continue.
# Anything we cannot classify is treated as MUST_HAVE — fail safe, not open.
BUCKET_MUST_HAVE = "MUST_HAVE"
BUCKET_SHOULD_HAVE = "SHOULD_HAVE"

APT_HELPER = "/usr/local/sbin/blackbox-apt-install"

# blackbox-apt-install exit codes we react to specifically (its header is the
# contract): 4 = allowlist file unreadable, 5 = package not in the allowlist.
APT_HELPER_RC_ALLOWLIST_UNREADABLE = 4
APT_HELPER_RC_NOT_ALLOWLISTED = 5

# Preflight probe package. Passes the helper's ^[a-z0-9.+-]+$ regex and can
# never be in the allowlist, so a healthy helper reads the allowlist, refuses
# the package and exits 5 — proving readability WITHOUT invoking apt at all.
APT_PREFLIGHT_PROBE_PKG = "blackbox-preflight-probe-not-a-real-package"


class UpdateRunner:
    """One runner instance per update attempt. Disposable — instantiate,
    iterate run(), discard. The UpdateManager is shared singleton-style."""

    def __init__(self, blackbox_root: Path, manager: UpdateManager):
        self.root = Path(blackbox_root)
        self.mgr = manager
        self.task_id = f"update-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        self.started_at = time.time()
        # Captured at run() entry, used everywhere downstream
        self.from_sha: str = ""
        self.target_sha: str = ""
        self.pre_update_tag: str = ""
        # Path to the staging worktree (audit C2) — None until staging phase.
        self.staging_path: Optional[Path] = None

    # ── Public entry point ──────────────────────────────────────────────

    async def run(self) -> AsyncIterator[dict]:
        """Async generator. Yields event dicts. Caller (route handler)
        relays each yielded dict to the SSE connection.

        Acquires the mutex on entry. Releases on exit (even on exception)
        via the UpdateManager.acquire_or_raise context manager.
        """
        try:
            with self.mgr.acquire_or_raise():
                async for event in self._run_locked():
                    yield event
        except UpdateInProgressError as e:
            yield {"type": "complete", "succeeded": False,
                   "error": str(e), "phase": None}

    # ── Inner flow (mutex held throughout) ──────────────────────────────

    async def _run_locked(self) -> AsyncIterator[dict]:
        # PHASE: staging — capture rollback anchors + worktree-stage
        self.from_sha = git_ops.current_sha(self.root)
        try:
            git_ops.fetch_origin_main(self.root)
        except subprocess.CalledProcessError as e:
            yield self._fail("staging", f"git fetch failed: {_tail(e.stderr)}")
            return
        self.target_sha = git_ops.latest_origin_sha(self.root)

        if self.from_sha == self.target_sha:
            yield {"type": "complete", "succeeded": True,
                   "sha_before": self.from_sha, "sha_after": self.target_sha,
                   "phase": PHASE_COMPLETE,
                   "message": "Already up to date"}
            return

        self.pre_update_tag = f"pre-update-{int(self.started_at)}"
        try:
            git_ops.tag(self.root, self.pre_update_tag)
        except subprocess.CalledProcessError as e:
            yield self._fail("staging", f"git tag failed: {_tail(e.stderr)}")
            return

        self._write_state(PHASE_STAGING)
        yield self._phase_event(PHASE_STAGING)
        yield self._log(PHASE_STAGING, f"Tagged {self.pre_update_tag} as rollback anchor")
        await asyncio.sleep(0)  # yield control briefly

        # Pre-update venv freezes (audit M1)
        try:
            self._freeze_venvs()
            yield self._log(PHASE_STAGING, "Captured pre-update pip freeze for both venvs")
        except Exception as e:
            yield self._log(PHASE_STAGING, f"Warning: venv freeze partial: {e}")
        await asyncio.sleep(0)

        # Categorize changes
        changed = git_ops.diff_files(self.root, self.from_sha, self.target_sha)
        buckets = changes_mod.categorize(changed)
        yield self._log(PHASE_STAGING,
                        f"Diff: {len(changed)} files changed. Buckets: "
                        f"{', '.join(b for b, on in buckets.items() if on and b != 'code_only')}")

        # PHASE: reset_hard — atomic file swap (audit C2)
        # NOTE: We do reset_hard FIRST then re-run the install.sh-equivalent
        # bits via subprocess, instead of worktree-staging. Reason: the
        # bash install.sh helpers (Step 2b MCP registration, Step 0b
        # helper install) write to /usr/local/sbin/ and /etc/systemd/
        # system/ — they can't easily run against a worktree path. The
        # worktree pattern is reserved for code-only validation (pip install
        # against the new code's requirements.txt before swapping).
        #
        # Trade-off: brief window where new Python files are on disk but
        # service hasn't restarted yet. uvicorn workers re-importing a
        # module mid-window would crash. We mitigate by scheduling the
        # restart immediately after reset_hard (no async/await between).
        self._write_state(PHASE_RESET_HARD)
        yield self._phase_event(PHASE_RESET_HARD)
        try:
            git_ops.reset_hard(self.root, self.target_sha)
            yield self._log(PHASE_RESET_HARD,
                            f"git reset --hard {git_ops.current_short(self.root)} OK")
        except subprocess.CalledProcessError as e:
            yield self._fail(PHASE_RESET_HARD,
                             f"git reset --hard failed: {_tail(e.stderr)}")
            return

        # PHASE: apt_install (conditional)
        if buckets["apt"]:
            self._write_state(PHASE_APT_INSTALL)
            yield self._phase_event(PHASE_APT_INSTALL)
            new_pkgs = self._new_apt_packages_since(self.from_sha)
            if not new_pkgs:
                yield self._log(PHASE_APT_INSTALL, "No new packages to install.")
            pkg_buckets = self._apt_package_buckets()

            if new_pkgs:
                # Preflight BEFORE the first install: prove the installed
                # helper can read its allowlist, so a stale helper produces a
                # sentence the customer can act on instead of an `rc=4` after
                # the update has already started mutating the box. This only
                # REPORTS — it does not re-run install.sh here (that would be an
                # early privileged re-run, the exact phase-ordering hazard we
                # reverted; the single re-run stays at systemd_regen). A stale
                # helper blocking only optional packages self-heals there: the
                # SHOULD_HAVE-only branch below skips them, the update reaches
                # systemd_regen, and install.sh re-templates the helper.
                ok, msg = await self._apt_preflight()
                if not ok:
                    required = [p for p in new_pkgs
                                if pkg_buckets.get(p, BUCKET_MUST_HAVE)
                                == BUCKET_MUST_HAVE]
                    if required:
                        yield self._fail(PHASE_APT_INSTALL, msg)
                        await self._rollback_code()
                        return
                    # Only optional packages are pending — same call as a
                    # SHOULD_HAVE install failure below: warn, keep the update.
                    yield self._log(PHASE_APT_INSTALL,
                                    f"WARNING: {msg} Skipping optional packages "
                                    f"({', '.join(new_pkgs)}) and continuing.")
                    new_pkgs = []

            for pkg in new_pkgs:
                yield self._log(PHASE_APT_INSTALL, f"Installing {pkg}...")
                rc, out = await self._run(["sudo", "-n", APT_HELPER, pkg])
                if rc != 0:
                    # SHOULD_HAVE is "degraded but functional" by definition
                    # (see the allowlist's own bucket headings). Throwing away
                    # a landed code + dependency update because an optional
                    # convenience package would not install is what turned a
                    # cosmetic problem into a customer stuck on old code
                    # (2026-07-27: novnc, SHOULD_HAVE, rolled the lot back).
                    # Same precedent as the systemd_regen warning below.
                    bucket = pkg_buckets.get(pkg, BUCKET_MUST_HAVE)
                    if bucket == BUCKET_SHOULD_HAVE:
                        yield self._log(PHASE_APT_INSTALL,
                                        f"WARNING: optional package {pkg} failed to "
                                        f"install (rc={rc}); continuing without it. "
                                        f"{pkg} is SHOULD_HAVE in "
                                        f"Scripts/onboarding/system-packages.txt — the "
                                        f"feature that uses it stays unavailable until "
                                        f"you run `sudo apt-get install -y {pkg}` "
                                        f"manually. Output tail: {_tail(out)}")
                        continue
                    yield self._fail(PHASE_APT_INSTALL,
                                     f"apt install {pkg} failed (rc={rc}): {_tail(out)}")
                    await self._rollback_code()
                    return
                yield self._log(PHASE_APT_INSTALL, f"{pkg} OK")

        # PHASE: pip_install (conditional)
        if buckets["pip"]:
            self._write_state(PHASE_PIP_INSTALL)
            yield self._phase_event(PHASE_PIP_INSTALL)
            rc, out = await self._run([
                str(self.root / "Orchestrator/venv/bin/pip"),
                "install", "-r", str(self.root / "requirements.txt"),
                "--quiet", "--disable-pip-version-check",
            ], timeout=600.0)
            if rc != 0:
                yield self._fail(PHASE_PIP_INSTALL,
                                 f"pip install failed (rc={rc}): {_tail(out)}")
                await self._rollback_pip_and_code()
                return
            yield self._log(PHASE_PIP_INSTALL, "Orchestrator venv updated.")

        # PHASE: mcp_install (conditional)
        if buckets["mcp_pip"]:
            self._write_state(PHASE_MCP_INSTALL)
            yield self._phase_event(PHASE_MCP_INSTALL)
            rc, out = await self._run([
                str(self.root / "MCP/venv/bin/pip"),
                "install", "-r", str(self.root / "MCP/requirements.txt"),
                "--quiet", "--disable-pip-version-check",
            ], timeout=300.0)
            if rc != 0:
                yield self._fail(PHASE_MCP_INSTALL,
                                 f"MCP pip install failed (rc={rc}): {_tail(out)}")
                await self._rollback_mcp_pip_and_code()
                return
            yield self._log(PHASE_MCP_INSTALL, "MCP venv updated.")

        # PHASE: systemd_regen + helpers + sudoers (conditional)
        # This is the ONE privileged install.sh re-run per update, and it runs
        # HERE — after apt/pip/mcp — on purpose (customer, 2026-07-27, stale
        # helper after commit 5eabbe45). An earlier re-run was tried and
        # reverted: the unit's KillMode=process means install.sh's own Step 7
        # restart would SIGTERM the uvicorn process iterating THIS update. It is
        # also where a stale helper heals — install.sh Step 0b re-templates the
        # root-owned /usr/local/sbin helpers whenever the helpers/systemd/sudoers
        # buckets are lit.
        if buckets["systemd"] or buckets["sudoers"] or buckets["helpers"]:
            self._write_state(PHASE_SYSTEMD_REGEN)
            yield self._phase_event(PHASE_SYSTEMD_REGEN)
            async for ev in self._privileged_regen(PHASE_SYSTEMD_REGEN):
                yield ev

        # PHASE: restart_pending — emit complete event, schedule restart
        self._write_state(PHASE_RESTART_PENDING)
        yield self._phase_event(PHASE_RESTART_PENDING)
        yield self._log(PHASE_RESTART_PENDING,
                        "Scheduling detached service restart in 2s...")

        # Final complete event BEFORE restart fires (audit M4)
        sha_after = git_ops.current_short(self.root)
        self._write_state(PHASE_COMPLETE)
        yield {
            "type": "complete",
            "succeeded": True,
            "sha_before": self.from_sha[:7],
            "sha_after": sha_after,
            "phase": PHASE_COMPLETE,
            "task_id": self.task_id,
            "pre_update_tag": self.pre_update_tag,
        }

        # Schedule restart 2s after the SSE flush. asyncio.get_running_loop
        # is safe inside an async generator (we're in event loop context).
        loop = asyncio.get_running_loop()
        loop.call_later(2.0, _fire_detached_restart)

    # ── Privileged regen (shared by the helper refresh + systemd phases) ─

    async def _privileged_regen(self, phase: str) -> AsyncIterator[dict]:
        """Re-run `sudo -n bash Scripts/install.sh` once per update.

        Called only from the systemd_regen phase (after apt/pip/mcp). Yields
        log events under `phase` (the caller owns the phase event). Never raises
        and never rolls back: every outcome here is degrade-and-continue.
        """
        # Probe non-interactive sudo BEFORE attempting install.sh. If
        # we know `sudo -n` will fail (no NOPASSWD grant for bash
        # install.sh — see installer/templates/sudoers-blackbox-system),
        # skip the phase entirely with a friendly message rather than
        # surfacing a scary "install.sh failed" / "password required"
        # error to the user. Brandon's customer hit this 2026-05-23:
        # cecc3c1's graceful-degrade still showed an alarming WARNING
        # line that read like a failure in the UI even though the
        # update actually completed.
        probe_rc, _ = await self._run(["sudo", "-n", "true"], timeout=5.0)
        if probe_rc != 0:
            yield self._log(phase,
                            "Skipped privileged regen (no passwordless sudo). "
                            "Code + dependencies updated successfully. If a "
                            "systemd unit, sudoers, or helper script change "
                            "needs to apply, run `sudo bash Scripts/install.sh` "
                            "once manually.")
            rc = 0  # treat as success for downstream phases
            out = ""
        else:
            # For v1, regenerate everything by re-running install.sh in
            # NON-interactive mode (its blocks have if-not-exists guards).
            # Future: surgical regen-only-what-changed via per-template logic.
            yield self._log(phase,
                            "Re-running sudo install.sh for system-level regen...")
            rc, out = await self._run(
                ["sudo", "-n", "bash", str(self.root / "Scripts/install.sh")],
                timeout=600.0,
            )
        if rc != 0:
            # Common failure mode: sudoers doesn't grant non-interactive
            # `bash install.sh` (only specific systemctl/journalctl/helper
            # paths per installer/templates/sudoers-blackbox-system). Don't
            # roll back the entire update for this — the code/dependency
            # changes (git reset, pip install, etc) already applied
            # successfully. System-level regen (systemd unit / sudoers
            # rewrites) is deferred; the customer can run install.sh
            # manually via SSH if they need those changes immediately.
            # Most install.sh edits (Step 4g GNOME tweaks, Step 4i browser
            # defaults, etc) are user-session config that doesn't need a
            # privileged re-run anyway. Hit by Brandon on MSO2 2026-05-22
            # after commit 236ea70 (Step 4i browser default) triggered
            # the systemd bucket without needing actual systemd work.
            # Future: refine changes.py categorization to only trigger
            # systemd bucket when install.sh changes ACTUALLY touch
            # systemd-relevant heredocs (Steps 4 / 4b / 4b1 / 4e).
            yield self._log(phase,
                            f"WARNING: install.sh re-run failed (rc={rc}); "
                            f"continuing without system-level regen. "
                            f"Run `sudo bash Scripts/install.sh` manually if "
                            f"systemd unit / sudoers / helpers need updating. "
                            f"Output tail: {_tail(out)}")
        else:
            yield self._log(phase, "System-level regen complete.")

    # ── apt allowlist plumbing (customer, 2026-07-27) ────────────────────

    async def _apt_preflight(self) -> tuple[bool, str]:
        """Can the INSTALLED apt helper actually read its allowlist?

        Returns (ok, message). `message` is empty when ok, and otherwise a
        complete customer-facing sentence naming the exact remedy command —
        the whole point of the check is that `rc=4` is not something anyone
        can act on. Runs before the first package install, so the answer
        arrives before the apt phase mutates the box.

        Costs one no-op helper invocation: the probe package passes the
        regex gate and is guaranteed absent from the allowlist, so a healthy
        helper reads the file, refuses the package and exits 5 without ever
        reaching apt-get.
        """
        rc, out = await self._run(
            ["sudo", "-n", APT_HELPER, APT_PREFLIGHT_PROBE_PKG], timeout=30.0)

        if rc == APT_HELPER_RC_NOT_ALLOWLISTED:
            return True, ""

        if rc == APT_HELPER_RC_ALLOWLIST_UNREADABLE:
            stale = _extract_allowlist_path(out)
            where = f"at {stale}" if stale else "at a path that no longer exists"
            return False, (
                f"The system package helper on this machine is out of date: "
                f"{APT_HELPER} is still looking for its package list {where}, "
                f"but this BlackBox is installed at {self.root}. That helper is "
                f"a root-owned file, so updates cannot replace it on their own. "
                f"Fix it with one command over SSH or in a terminal on the box: "
                f"sudo bash {self.root}/Scripts/install.sh"
            )

        # Anything else: helper missing, sudo grant missing, timeout. Same
        # remedy — install.sh reinstalls the helper AND rewrites the sudoers
        # grant that reaches it.
        return False, (
            f"The system package helper {APT_HELPER} could not be run "
            f"(exit {rc}). It is installed and granted by the installer, so it "
            f"is either missing or no longer permitted. Fix it with one command "
            f"over SSH or in a terminal on the box: "
            f"sudo bash {self.root}/Scripts/install.sh"
        )

    def _apt_package_buckets(self) -> dict[str, str]:
        """Map package → MUST_HAVE|SHOULD_HAVE from the post-reset allowlist
        (HEAD is already the target commit by the time the apt phase runs, so
        this is the NEW allowlist — the one the new packages come from).

        Returns {} if the file cannot be read; callers must then treat every
        package as MUST_HAVE. Fail safe, not fail open.
        """
        path = self.root / "Scripts/onboarding/system-packages.txt"
        try:
            return _parse_pkg_buckets(path.read_text())
        except OSError:
            return {}

    # ── Helpers ─────────────────────────────────────────────────────────

    def _write_state(self, phase: str) -> None:
        self.mgr.write_state(
            task_id=self.task_id,
            phase=phase,
            target_sha=self.target_sha,
            from_sha=self.from_sha,
            pre_update_tag=self.pre_update_tag,
        )

    def _phase_event(self, phase: str) -> dict:
        return {"type": "phase", "phase": phase,
                "started_iso": _now_iso(),
                "elapsed_s": int(time.time() - self.started_at)}

    def _log(self, phase: str, text: str) -> dict:
        return {"type": "log", "phase": phase, "text": text,
                "iso": _now_iso()}

    def _fail(self, phase: str, error: str) -> dict:
        self.mgr.write_state(
            task_id=self.task_id, phase=PHASE_FAILED,
            target_sha=self.target_sha, from_sha=self.from_sha,
            pre_update_tag=self.pre_update_tag,
            extra={"error": error, "failed_phase": phase},
        )
        return {
            "type": "complete",
            "succeeded": False,
            "phase": PHASE_FAILED,
            "failed_phase": phase,
            "error": error,
            "sha_before": self.from_sha[:7],
            "pre_update_tag": self.pre_update_tag,
        }

    async def _run(self, cmd: list[str],
                    timeout: Optional[float] = 120.0) -> tuple[int, str]:
        """Run a subprocess, return (returncode, combined-output).
        Uses asyncio.create_subprocess_exec so the event loop stays
        responsive during long-running commands."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.root),
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, f"TIMEOUT after {timeout}s: {' '.join(cmd[:3])}..."
        return proc.returncode or 0, (stdout or b"").decode("utf-8", "replace")

    def _freeze_venvs(self) -> None:
        """Capture pip freeze for Orchestrator + MCP venvs (audit M1).
        Pre-rollback restore happens via pip-sync-equivalent in _rollback_pip*."""
        orch_pip = self.root / "Orchestrator/venv/bin/pip"
        mcp_pip = self.root / "MCP/venv/bin/pip"
        if orch_pip.is_file():
            out = subprocess.run([str(orch_pip), "freeze"],
                                  capture_output=True, text=True, timeout=30)
            (self.root / "Manifest/pre_update_pip_freeze.txt").write_text(out.stdout)
        if mcp_pip.is_file():
            out = subprocess.run([str(mcp_pip), "freeze"],
                                  capture_output=True, text=True, timeout=30)
            (self.root / "Manifest/pre_update_mcp_freeze.txt").write_text(out.stdout)

    def _new_apt_packages_since(self, from_sha: str) -> list[str]:
        """Compare system-packages.txt between from_sha and HEAD (target),
        return packages present in HEAD's MUST_HAVE+SHOULD_HAVE but not in
        from_sha's. Only NEW ones need installing — existing ones are
        already installed."""
        path = "Scripts/onboarding/system-packages.txt"
        try:
            old = subprocess.run(
                ["git", "show", f"{from_sha}:{path}"],
                cwd=str(self.root), capture_output=True, text=True, timeout=10,
            ).stdout
        except Exception:
            old = ""
        new = (self.root / path).read_text() if (self.root / path).exists() else ""
        return sorted(_parse_pkg_list(new) - _parse_pkg_list(old))

    async def _rollback_code(self) -> None:
        """git reset --hard pre-update-tag. Used after non-venv failure."""
        try:
            git_ops.reset_hard(self.root, self.pre_update_tag)
        except Exception:
            pass

    async def _rollback_pip_and_code(self) -> None:
        """Restore Orchestrator venv to pre-update freeze, then reset code."""
        freeze = self.root / "Manifest/pre_update_pip_freeze.txt"
        if freeze.is_file():
            await self._run([
                str(self.root / "Orchestrator/venv/bin/pip"),
                "install", "-r", str(freeze),
                "--quiet", "--force-reinstall",
            ], timeout=600.0)
        await self._rollback_code()

    async def _rollback_mcp_pip_and_code(self) -> None:
        freeze = self.root / "Manifest/pre_update_mcp_freeze.txt"
        if freeze.is_file():
            await self._run([
                str(self.root / "MCP/venv/bin/pip"),
                "install", "-r", str(freeze),
                "--quiet", "--force-reinstall",
            ], timeout=300.0)
        await self._rollback_code()


def _fire_detached_restart() -> None:
    """Spawn `sudo systemctl restart blackbox.service` as a detached
    process. Uses start_new_session=True so it survives our own SIGTERM.
    Called via asyncio.call_later(2.0, ...) so the SSE complete event has
    already flushed to the browser (audit M4)."""
    subprocess.Popen(
        ["sudo", "-n", "systemctl", "restart", "blackbox.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# EXACT parity with the grep blackbox-apt-install (and install.sh Step 1) use
# to build the allowlist: `^[a-zA-Z0-9._+-]+\s+#\s+(MUST_HAVE|SHOULD_HAVE)`.
# Anchored at column 0 (a leading-indented line the helper's grep would reject
# must not be accepted here) with whitespace required on BOTH sides of the
# first `#`. If this parser accepted a line the helper rejects, the runner
# would try to install a package the helper refuses at rc=5 and — default
# MUST_HAVE — roll the whole update back over a formatting typo.
_PKG_LINE_RE = re.compile(r'^([a-zA-Z0-9._+-]+)\s+#\s+(MUST_HAVE|SHOULD_HAVE)')


def _parse_pkg_buckets(content: str) -> dict[str, str]:
    """Parse system-packages.txt into package → bucket. Lines like:
       package-name              # MUST_HAVE # reason
       package-name              # SHOULD_HAVE # reason

    Matches the helper's allowlist grep EXACTLY (see _PKG_LINE_RE) so the
    runner and the helper never disagree about a package.

    MUST_HAVE is STICKY: if a package appears in both buckets (the file keeps
    `xvfb` as MUST_HAVE sitting inside the SHOULD_HAVE section — the exact
    editing pattern that produces a duplicate), it resolves to MUST_HAVE and
    is never downgraded, regardless of line order. Fail safe, not open.

    Other buckets (FEATURE_OPTIONAL, HARDWARE_OPTIONAL, DEV_ONLY) are
    excluded entirely: the update pipeline never installs them.
    """
    buckets: dict[str, str] = {}
    for line in content.splitlines():
        m = _PKG_LINE_RE.match(line)
        if not m:
            continue
        pkg, bucket = m.group(1), m.group(2)
        if buckets.get(pkg) == BUCKET_MUST_HAVE:
            continue  # already MUST_HAVE — never downgrade to SHOULD_HAVE
        buckets[pkg] = bucket
    return buckets


def _parse_pkg_list(content: str) -> set[str]:
    """Set of package names in the MUST_HAVE+SHOULD_HAVE allowlist."""
    return set(_parse_pkg_buckets(content))


def _extract_allowlist_path(helper_output: str) -> str:
    """Pull the path out of blackbox-apt-install's exit-4 stderr line:
       `[blackbox-apt-install] ERROR: allowlist file not readable: /some/path`
    Returns "" if the line isn't there — the caller degrades to a generic
    phrasing rather than printing a half-parsed path at a customer."""
    marker = "allowlist file not readable:"
    for line in helper_output.splitlines():
        if marker in line:
            return line.split(marker, 1)[1].strip()
    return ""


def _tail(text: str, lines: int = 6) -> str:
    """Last N lines of `text` for compact error display in SSE events."""
    return "\n".join(text.splitlines()[-lines:])


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
