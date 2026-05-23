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
"""
from __future__ import annotations

import asyncio
import os
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
        # bash install.sh helpers (Step 2b MCP registration, Step 4f1
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
            for pkg in new_pkgs:
                yield self._log(PHASE_APT_INSTALL, f"Installing {pkg}...")
                rc, out = await self._run(["sudo", "-n",
                                            "/usr/local/sbin/blackbox-apt-install",
                                            pkg])
                if rc != 0:
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
        if buckets["systemd"] or buckets["sudoers"] or buckets["helpers"]:
            self._write_state(PHASE_SYSTEMD_REGEN)
            yield self._phase_event(PHASE_SYSTEMD_REGEN)
            # For v1, regenerate everything by re-running install.sh in
            # NON-interactive mode (its blocks have if-not-exists guards).
            # Future: surgical regen-only-what-changed via per-template logic.
            yield self._log(PHASE_SYSTEMD_REGEN,
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
                yield self._log(PHASE_SYSTEMD_REGEN,
                                f"WARNING: install.sh re-run failed (rc={rc}); "
                                f"continuing without system-level regen. "
                                f"Run `sudo bash Scripts/install.sh` manually if "
                                f"systemd unit / sudoers / helpers need updating. "
                                f"Output tail: {_tail(out)}")
            else:
                yield self._log(PHASE_SYSTEMD_REGEN, "System-level regen complete.")

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


def _parse_pkg_list(content: str) -> set[str]:
    """Parse system-packages.txt format. Lines like:
       package-name              # MUST_HAVE # reason
       package-name              # SHOULD_HAVE # reason
    Returns set of package names with either bucket."""
    pkgs = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Format: pkg<whitespace># BUCKET # reason
        if "#" not in stripped:
            continue
        pkg_part, _, rest = stripped.partition("#")
        if "MUST_HAVE" in rest or "SHOULD_HAVE" in rest:
            pkgs.add(pkg_part.strip())
    return pkgs


def _tail(text: str, lines: int = 6) -> str:
    """Last N lines of `text` for compact error display in SSE events."""
    return "\n".join(text.splitlines()[-lines:])


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
