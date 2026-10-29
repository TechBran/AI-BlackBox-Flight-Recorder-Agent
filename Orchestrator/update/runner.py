"""UpdateRunner — orchestrates the full update flow with SSE-friendly events.

Consumed by Orchestrator/routes/update_routes.py:/update/log/stream which
relays events to the Portal as SSE messages. The runner itself is async
so each phase yields control between subprocess calls (keeps uvicorn
responsive for /update/status polling during a long pip install).

EVENT SHAPES (each yielded as a dict; route serializes to SSE 'data: {json}'):
  {type: "phase", phase: "apt_install", started_iso: "..."}
  {type: "log", text: "...", phase: "apt_install"}
  {type: "heartbeat", phase: "...", elapsed_s: 42}  # every 2s if no other events
  {type: "complete", succeeded: true|false, sha_before, sha_after, error?,
   restart_scheduled: bool, stash_ref?, stash_refs?: [...],
   refused?: "dirty_tree", dirty_files?: [...]}  # refused = nothing changed

DIRTY TREE (audit O16-01): the update and the rollback refuse to run while a
tracked file has local changes, unless the caller passed discard_local_changes;
then the changes are stashed (`auto-stash-<tag>`) before anything is changed.
Untracked runtime files never block. Every `git reset --hard` then runs in one
worker-thread step with a second look (_keep_then_reset): tracked edits saved
since the check are stashed (`auto-stash-during-<tag>`) and untracked files the
reset would overwrite are moved under Manifest/displaced/<tag>/. A reset that
cannot keep them does not run. Scripts/update.sh applies the same rules.

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
    _apt_preflight. Phase order stays monotonic — the system-level regen runs in
    systemd_regen, AFTER apt/pip/mcp, never before.
  - install.sh-grant removal, 2026-07-27: systemd_regen NO LONGER re-runs
    `sudo -n bash install.sh` (its NOPASSWD grant was the in-service→root
    escalation this change closes). Instead _privileged_regen, for each CHANGED
    git-tracked template that maps to a bounded blackbox-write-systemd
    target_kind, compares the repo render against the ROOT-OWNED trusted store
    entry /etc/blackbox/templates/<kind> and dispatches `blackbox-write-systemd
    <kind>` (NO source) only when they match — the helper then copies the store
    entry to its hardcoded dest. A change the human has not yet PUBLISHED to the
    store (store absent, root-unreadable, or differing) is NOT applied and NOT
    claimed as success: the runner surfaces a "run sudo bash install.sh once"
    remedy instead of dispatching a stale store back over the live config (the
    store-staleness trap — the store is human-published only, by design).
    New system packages become an operator step for the same reason (see the apt
    phase): the apt helper too trusts only the root-owned /etc allowlist, so a
    commit adding a new MUST_HAVE dependency surfaces a "run sudo bash install.sh
    once" remedy.

TRUSTED ORIGIN + SIGNED RELEASES (2026-10 audit rank 79,
X04-security-clients-infra-11): before the fetch the checkout's origin must be
one /etc/blackbox/update-origin pins, and once /etc/blackbox/update-allowed-
signers holds a key the commit installed must be SSH-signed by it (verify.py
has the rules; Scripts/update.sh applies the same ones). A refusal changes
nothing and is not persisted, like a dirty-tree refusal. The reset installs the
SHA that was verified, never whatever origin/main names by then. A failed
update that is rolled back puts the local changes it stashed back once the
code is at the pre-update tag (_restore_stashes).

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
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

from Orchestrator.update import changes as changes_mod
from Orchestrator.update import git_ops
from Orchestrator.update import verify
from Orchestrator.update.manager import (
    UpdateManager, UpdateInProgressError,
    PHASE_STAGING, PHASE_APT_INSTALL, PHASE_PIP_INSTALL, PHASE_MCP_INSTALL,
    PHASE_SYSTEMD_REGEN, PHASE_RESET_HARD, PHASE_RESTART_PENDING,
    PHASE_COMPLETE, PHASE_FAILED, PHASE_ROLLBACK, PHASE_ROLLED_BACK,
)

logger = logging.getLogger("blackbox.update")

# What a git_ops call raises: a non-zero exit, its timeout (fetch is 120 s), or
# git itself missing.
_GIT_ERRORS = (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError)


# ── apt allowlist buckets (Scripts/onboarding/system-packages.txt) ──────
# MUST_HAVE failures abort + roll back; SHOULD_HAVE failures warn + continue.
# Anything we cannot classify is treated as MUST_HAVE — fail safe, not open.
BUCKET_MUST_HAVE = "MUST_HAVE"
BUCKET_SHOULD_HAVE = "SHOULD_HAVE"

APT_HELPER = "/usr/local/sbin/blackbox-apt-install"

# The Orchestrator venv installs from this hashed lock (compiled from
# requirements.in by scripts/compile_python_lock.sh) with --require-hashes:
# every package pinned and its download checked (X01-deps-python-02/-05). A
# failed lock install fails the update; there is no fallback to an unhashed or
# unpinned file — that is a manual, operator-run step (see requirements.in).
PIP_LOCK_FILE = "requirements.lock.txt"

# The lock's pins without hashes, used as constraints (-c) for the packages an
# installer adds on top of the lock.
PIP_CONSTRAINTS_FILE = "requirements.txt"

# Packages an installer adds to the Orchestrator venv on top of the lock:
# installer/templates/blackbox-install-reranker-cpu.sh installs the in-process
# CPU reranker on MID-tier boxes (torch from the PyTorch CPU index, then these
# from PyPI), under the lock's pins. A lock install moves a pin they depend on
# to the lock's version and pip only warns (exit 0): before this, hub went back
# under transformers and the reranker stopped importing, silently. So after
# the lock, the pip phase reinstalls them under the new pins, and pip picks
# versions that work with them. torch is not reinstalled: the lock never moves
# it, and PyPI does not serve the +cpu build.
LOCK_EXTRAS = ("sentence-transformers",)

# First line of a pre-update pip freeze: the tag of the update it was taken
# for. Scripts/update.sh writes the same files with its own tag and no
# update_state.json, so a rollback reinstalls a freeze only when it names the
# tag being rolled back to (a freeze without the line predates it: trusted).
FREEZE_HEADER = "# blackbox-freeze-for: "

# blackbox-apt-install exit codes we react to specifically (its header is the
# contract): 4 = allowlist file unreadable, 5 = package not in the allowlist.
APT_HELPER_RC_ALLOWLIST_UNREADABLE = 4
APT_HELPER_RC_NOT_ALLOWLISTED = 5

# Preflight probe package. Passes the helper's ^[a-z0-9.+-]+$ regex and can
# never be in the allowlist, so a healthy helper reads the allowlist, refuses
# the package and exits 5 — proving readability WITHOUT invoking apt at all.
APT_PREFLIGHT_PROBE_PKG = "blackbox-preflight-probe-not-a-real-package"


# ── System-level regen dispatch table (install.sh-grant removal, 2026-07-27) ──
# The bounded root helper the decomposed regen dispatches through. HARDCODES
# every destination per target_kind AND reads its content from the root-owned
# store — the caller supplies ONLY a kind, never a path OR a source (M0). See
# installer/templates/blackbox-write-systemd.sh.
WRITE_SYSTEMD_HELPER = "/usr/local/sbin/blackbox-write-systemd"

# Root-owned trusted template store the helper copies FROM (per kind). Written
# ONLY by the human `sudo bash install.sh` publish step; the running service has
# no write path to /etc/blackbox (ProtectSystem=strict + the perimeter invariant
# test). _privileged_regen reads it here to decide whether a repo template change
# has already been PUBLISHED (store == repo render → safe to dispatch a store→dest
# drift-heal) or is still unpublished (→ surface the operator remedy, never
# dispatch a stale store and log false success). The BLACKBOX_WRITE_SYSTEMD_DEST_ROOT
# prefix is the SAME test-only sandbox rebase the helper uses (empty in production,
# where sudo's env_reset scrubs it), so a test populates one fake store both read.
TEMPLATE_STORE_DIR = "/etc/blackbox/templates"


@dataclass(frozen=True)
class _RegenTarget:
    """One git-tracked template the update regen can re-apply WITHOUT re-running
    install.sh. `template` is the repo-relative path (so it can be matched
    against the same `git diff` changes.py reads); `deployed` is the hardcoded
    destination — used ONLY in log lines now (the gating compare is repo-render
    vs the root-owned STORE entry, not vs `deployed`); `kind` is the
    blackbox-write-systemd target_kind whose own hardcoded destination MUST
    equal `deployed` and whose store entry is /etc/blackbox/templates/<kind>.

    Only templates that render from (BLACKBOX_ROOT, REAL_USER) alone belong here
    — exactly what startup_assert_sudoers_current / _helpers_current already
    prove is reproducible on any box. blackbox-models.service is deliberately
    ABSENT: it needs REAL_HOME / LOCALSTACK_* substitutions the runner does not
    have, so rendering it here would dispatch a unit full of literal
    PLACEHOLDER strings. The inline-heredoc units in install.sh (blackbox.service,
    override, cli-agent-overrides, mcp, ydotoold, time-sync) are the
    documented residual — an operator step (`sudo bash install.sh`), never a
    fragile heredoc reimplementation here.
    """
    template: str          # repo-relative path (matches git diff / changes.py)
    deployed: str          # hardcoded deployed destination (log lines only now)
    kind: str              # blackbox-write-systemd target_kind
    restart_service: str   # aux service to restart after write; "" = none


# Templates rendered from (BLACKBOX_ROOT, REAL_USER) only. Substituting a
# placeholder a template does not contain is a harmless no-op, so both are always
# applied — yielding bytes identical to install.sh's per-file deploy AND to the
# store entry install.sh publishes (which is why the repo-render-vs-store compare
# in _privileged_regen is meaningful). NEVER includes blackbox.service or its
# drop-ins: restarting the running unit here would SIGTERM the uvicorn process
# iterating this very update (the reverted hazard).
_REGEN_TARGETS: tuple[_RegenTarget, ...] = (
    _RegenTarget("installer/templates/sudoers-blackbox-system",
                 "/etc/sudoers.d/blackbox-system", "sudoers-system", ""),
    _RegenTarget("installer/templates/blackbox-apt-install.sh",
                 "/usr/local/sbin/blackbox-apt-install", "apt-install-helper",
                 ""),
    _RegenTarget("installer/templates/blackbox-write-systemd.sh",
                 "/usr/local/sbin/blackbox-write-systemd", "write-systemd-helper",
                 ""),
    _RegenTarget("installer/templates/zellij-web.service",
                 "/etc/systemd/system/zellij-web.service", "zellij-web-unit",
                 "zellij-web.service"),
)


class UpdateRunner:
    """One runner instance per update attempt. Disposable — instantiate,
    iterate run(), discard. The UpdateManager is shared singleton-style."""

    def __init__(self, blackbox_root: Path, manager: UpdateManager, *,
                 discard_local_changes: bool = False, kind: str = "update"):
        self.root = Path(blackbox_root)
        self.mgr = manager
        self.task_id = f"{kind}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        self.started_at = time.time()
        # The operator's explicit consent to take local changes to tracked
        # files out of the working tree. Without it a dirty tree is refused;
        # with it they are stashed (never just discarded) before any reset.
        self.discard_local_changes = discard_local_changes
        # Captured at run() entry, used everywhere downstream
        self.from_sha: str = ""
        self.target_sha: str = ""
        self.pre_update_tag: str = ""
        # SHAs of the stashes this run made, oldest first (see stash_ref).
        self.stash_refs: list[str] = []
        # Path to the staging worktree (audit C2) — None until staging phase.
        self.staging_path: Optional[Path] = None

    @property
    def stash_ref(self) -> str:
        """SHA of the first stash this run made ("" = none): the operator's
        consented one when there was one. stash_refs lists them all."""
        return self.stash_refs[0] if self.stash_refs else ""

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
                   "error": str(e), "phase": None, "restart_scheduled": False}

    async def rollback(self, tag: str) -> AsyncIterator[dict]:
        """The in-app Rollback (POST /update/rollback): put the venvs and the
        code back at the pre-update tag `tag`, then restart. Same event shapes
        and the same lock as run() (O16-08: the rollback used to reset the code
        with no lock, keep the new packages and leave a non-terminal phase)."""
        try:
            with self.mgr.acquire_or_raise():
                async for event in self._rollback_locked(tag):
                    yield event
        except UpdateInProgressError as e:
            yield {"type": "complete", "succeeded": False,
                   "error": str(e), "phase": None, "restart_scheduled": False}

    # ── Inner flow (mutex held throughout) ──────────────────────────────

    async def _run_locked(self) -> AsyncIterator[dict]:
        # PHASE: staging — capture rollback anchors + worktree-stage. Every git
        # call runs in a worker thread (self._git): a fetch can take 120 s and
        # used to stall every request on the box (O16-09).
        try:
            self.from_sha = await self._git(git_ops.current_sha)
        except _GIT_ERRORS as e:
            yield self._fail("staging", f"could not read the current commit: {_git_error(e)}")
            return
        # The pinned origin, before anything is fetched from it (verify.py).
        try:
            trust_notes = await asyncio.to_thread(verify.check_origin, self.root)
        except verify.UpdateRefused as e:
            yield update_refusal(str(e))
            return
        try:
            await self._git(git_ops.fetch_origin_main)
            self.target_sha = await self._git(git_ops.latest_origin_sha)
        except _GIT_ERRORS as e:
            yield self._fail("staging", f"git fetch failed: {_git_error(e)}")
            return

        if self.from_sha == self.target_sha:
            yield {"type": "complete", "succeeded": True,
                   "sha_before": self.from_sha, "sha_after": self.target_sha,
                   "phase": PHASE_COMPLETE, "restart_scheduled": False,
                   "message": "Already up to date"}
            return

        # The release signature of the exact commit the reset below installs.
        try:
            trust_notes += await asyncio.to_thread(verify.check_commit, self.root,
                                                   self.target_sha)
        except verify.UpdateRefused as e:
            yield update_refusal(str(e))
            return

        # Local changes to tracked files (O16-01): the reset below destroys
        # them and the pre-update tag only protects committed work. Refused
        # unless the operator agreed to discard them, and stashed even then.
        # Checked before anything is tagged, frozen or reset.
        try:
            dirty = await self._git(git_ops.tracked_changes)
        except _GIT_ERRORS as e:
            yield self._fail("staging", f"git status failed: {_git_error(e)}")
            return
        if dirty and not self.discard_local_changes:
            yield dirty_tree_refusal(dirty, "Installing the update")
            return

        self.pre_update_tag = f"pre-update-{int(self.started_at)}"
        stash_note = ""
        if dirty:
            try:
                stash_note = await self._stash(f"auto-stash-{self.pre_update_tag}", dirty)
            except _GIT_ERRORS as e:
                yield self._fail("staging", f"Could not stash the local changes, so "
                                            f"nothing was reset: {_git_error(e)}")
                return
        try:
            await self._git(git_ops.tag, self.pre_update_tag)
        except _GIT_ERRORS as e:
            yield self._fail("staging", f"git tag failed: {_git_error(e)}")
            return

        self._write_state(PHASE_STAGING)
        yield self._phase_event(PHASE_STAGING)
        yield self._log(PHASE_STAGING, f"Tagged {self.pre_update_tag} as rollback anchor")
        for note in trust_notes:
            yield self._log(PHASE_STAGING, note)
        if stash_note:
            yield self._log(PHASE_STAGING, stash_note)

        # Pre-update venv freezes (audit M1)
        try:
            await asyncio.to_thread(self._freeze_venvs)
            yield self._log(PHASE_STAGING, "Captured pre-update pip freeze for both venvs")
        except Exception as e:
            yield self._log(PHASE_STAGING, f"Warning: venv freeze partial: {e}")

        # Categorize changes
        try:
            changed = await self._git(git_ops.diff_files, self.from_sha, self.target_sha)
        except _GIT_ERRORS as e:
            yield self._fail("staging", f"git diff failed: {_git_error(e)}")
            return
        buckets = changes_mod.categorize(changed)
        yield self._log(PHASE_STAGING,
                        f"Diff: {len(changed)} files changed. Buckets: "
                        f"{', '.join(b for b, on in buckets.items() if on and b != 'code_only')}")
        pin_sources = [f for f in verify.PIN_SOURCES if f in changed]
        if pin_sources:
            # Only the human install.sh run publishes them (publish_update_pins).
            yield self._log(PHASE_STAGING,
                            f"Note: this update changes the update trust pins "
                            f"({', '.join(pin_sources)}). This box keeps checking "
                            f"updates against its current pins until `sudo bash "
                            f"{self.root}/Scripts/install.sh` publishes the new ones.")

        # PHASE: reset_hard — atomic file swap (audit C2)
        # NOTE: We do reset_hard FIRST then re-run the install.sh-equivalent
        # bits via subprocess, instead of worktree-staging. Reason: the
        # bash install.sh helpers (Step 2b MCP registration, Step 0b
        # helper install) write to /usr/local/sbin/ and /etc/systemd/
        # system/ — they can't easily run against a worktree path. The
        # worktree pattern is reserved for code-only validation (pip install
        # against the new code's requirements.lock.txt before swapping).
        #
        # Trade-off: a window where new Python files are on disk but the
        # service hasn't restarted yet (it lasts through the apt/pip phases
        # below). A module imported lazily inside that window loads the new
        # code; the restart at the end ends the window.
        self._write_state(PHASE_RESET_HARD)
        yield self._phase_event(PHASE_RESET_HARD)
        try:
            kept = await self._reset_keeping(
                self.target_sha, f"auto-stash-during-{self.pre_update_tag}",
                self.pre_update_tag)
        except _KeepFailed as e:
            yield self._fail(PHASE_RESET_HARD, f"{e} Nothing was reset.")
            return
        except _GIT_ERRORS as e:
            yield self._fail(PHASE_RESET_HARD,
                             f"git reset --hard failed: {_git_error(e)}")
            return
        for note in kept:
            yield self._log(PHASE_RESET_HARD, note)
        try:
            short = await self._git(git_ops.current_short)
        except _GIT_ERRORS:
            short = self.target_sha[:7]
        yield self._log(PHASE_RESET_HARD, f"git reset --hard {short} OK")

        # PHASE: apt_install (conditional)
        if buckets["apt"]:
            self._write_state(PHASE_APT_INSTALL)
            yield self._phase_event(PHASE_APT_INSTALL)
            new_pkgs = await asyncio.to_thread(self._new_apt_packages_since,
                                               self.from_sha)
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
                        async for ev in self._fail_rolled_back(PHASE_APT_INSTALL, msg):
                            yield ev
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
                    # MUST_HAVE failure → fatal + rollback. install.sh-grant
                    # removal, 2026-07-27: the apt helper now trusts ONLY the
                    # root-owned /etc/blackbox/system-packages.txt, which only a
                    # human `sudo bash install.sh` writes. So a commit that adds a
                    # NEW MUST_HAVE dependency reaches the helper as rc=5 (not
                    # allowlisted) until the operator refreshes the /etc copy —
                    # that is the accepted trade for removing the install.sh grant.
                    # Give that specific case an actionable remedy instead of a
                    # bare rc; any other MUST_HAVE failure keeps the generic
                    # sentence.
                    if rc == APT_HELPER_RC_NOT_ALLOWLISTED:
                        err = (
                            f"Required package {pkg} is not in the root-owned "
                            f"allowlist /etc/blackbox/system-packages.txt yet. "
                            f"Automated updates install only packages already "
                            f"approved there; adding a new system dependency is a "
                            f"one-time operator step. Run "
                            f"`sudo bash {self.root}/Scripts/install.sh` once (it "
                            f"rewrites the /etc allowlist from this commit), then "
                            f"retry the update.")
                    else:
                        err = f"apt install {pkg} failed (rc={rc}): {_tail(out)}"
                    async for ev in self._fail_rolled_back(PHASE_APT_INSTALL, err):
                        yield ev
                    return
                yield self._log(PHASE_APT_INSTALL, f"{pkg} OK")

        # PHASE: pip_install (conditional)
        # Installs the hashed lock, not requirements.txt: a plain `pip install
        # -r requirements.txt` never moved a satisfied floor and resolved the
        # transitive closure fresh on every box (X01-deps-python-02/-05).
        if buckets["pip"]:
            self._write_state(PHASE_PIP_INSTALL)
            yield self._phase_event(PHASE_PIP_INSTALL)
            pip = self.root / "Orchestrator/venv/bin/pip"
            lock = self.root / PIP_LOCK_FILE
            if not lock.is_file():
                # Nothing touched the venv yet: put the code back only.
                async for ev in self._fail_rolled_back(
                        PHASE_PIP_INSTALL,
                        f"{PIP_LOCK_FILE} is missing from {self.root}; "
                        f"refusing to install Python packages without it."):
                    yield ev
                return
            cmd = [str(pip), "install", "--require-hashes", "-r", str(lock),
                   "--quiet", "--disable-pip-version-check"]
            rc, out = await self._run(cmd, timeout=600.0)
            if rc != 0:
                async for ev in self._fail_rolled_back(
                        PHASE_PIP_INSTALL,
                        f"pip install --require-hashes -r {PIP_LOCK_FILE} "
                        f"failed (rc={rc}): {_tail(out)}\n"
                        f"Nothing was installed unpinned. Re-run `{pip} install "
                        f"--require-hashes -r {lock}` to see the full error "
                        f"(requirements.in documents the manual fallback).",
                        "orch"):
                    yield ev
                return
            yield self._log(PHASE_PIP_INSTALL,
                            f"Orchestrator venv updated from {PIP_LOCK_FILE} (hash-checked).")

            extras = self._installed_lock_extras()
            if extras:
                constraints = self.root / PIP_CONSTRAINTS_FILE
                rc, out = await self._run(
                    [str(pip), "install", "-c", str(constraints), *extras,
                     "--quiet", "--disable-pip-version-check"], timeout=600.0)
                if rc != 0:
                    async for ev in self._fail_rolled_back(
                            PHASE_PIP_INSTALL,
                            f"Reinstalling {', '.join(extras)} under the new "
                            f"pins failed (rc={rc}): {_tail(out)}\n"
                            f"Without it the CPU reranker would not import. Re-run "
                            f"`{pip} install -c {constraints} {' '.join(extras)}` to see "
                            f"the full error, or remove the reranker (installer/"
                            f"templates/blackbox-install-reranker-cpu.sh installed it).",
                            "orch"):
                        yield ev
                    return
                yield self._log(PHASE_PIP_INSTALL,
                                f"Reinstalled {', '.join(extras)} under the new pins "
                                f"(-c {PIP_CONSTRAINTS_FILE}).")

            # pip install exits 0 when it leaves an installed package's
            # requirement unmet, so ask. Not fatal: a package nothing here
            # installs (hand-added, or another venv's) would block every update.
            rc, out = await self._run([str(pip), "check"], timeout=120.0)
            if rc != 0:
                yield self._log(PHASE_PIP_INSTALL,
                                f"WARNING: pip check found installed packages whose "
                                f"requirements the venv no longer meets; they may fail "
                                f"to import:\n{_tail(out, 12)}")

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
                async for ev in self._fail_rolled_back(
                        PHASE_MCP_INSTALL,
                        f"MCP pip install failed (rc={rc}): {_tail(out)}", "mcp"):
                    yield ev
                return
            yield self._log(PHASE_MCP_INSTALL, "MCP venv updated.")

        # PHASE: systemd_regen + helpers + sudoers (conditional)
        # System-level regen runs HERE — after apt/pip/mcp — on purpose
        # (customer, 2026-07-27, stale helper after commit 5eabbe45). An earlier
        # attempt was reverted: the unit's KillMode=process means a blackbox.service
        # restart during regen would SIGTERM the uvicorn process iterating THIS
        # update. install.sh-grant removal, 2026-07-27: this no longer re-runs
        # install.sh; it dispatches the changed bounded templates (see
        # _privileged_regen). Enter the phase when a dispatchable template moved
        # OR a bucket the runner still reports (systemd via install.sh, sudoers,
        # helpers) is lit — the latter keeps the residual install.sh-only note.
        regen_changed = [t for t in _REGEN_TARGETS if t.template in set(changed)]
        if (regen_changed or buckets["systemd"]
                or buckets["sudoers"] or buckets["helpers"]):
            self._write_state(PHASE_SYSTEMD_REGEN)
            yield self._phase_event(PHASE_SYSTEMD_REGEN)
            async for ev in self._privileged_regen(
                    PHASE_SYSTEMD_REGEN, changed, buckets):
                yield ev

        # PHASE: restart_pending — emit complete event, schedule restart
        self._write_state(PHASE_RESTART_PENDING)
        yield self._phase_event(PHASE_RESTART_PENDING)
        yield self._log(PHASE_RESTART_PENDING,
                        "Scheduling detached service restart in 2s...")

        # Final complete event BEFORE restart fires (audit M4)
        try:
            sha_after = await self._git(git_ops.current_short)
        except _GIT_ERRORS:
            sha_after = self.target_sha[:7]
        self._write_state(PHASE_COMPLETE)
        yield {
            "type": "complete",
            "succeeded": True,
            "sha_before": self.from_sha[:7],
            "sha_after": sha_after,
            "phase": PHASE_COMPLETE,
            "task_id": self.task_id,
            "pre_update_tag": self.pre_update_tag,
            "stash_ref": self.stash_ref,
            "stash_refs": list(self.stash_refs),
            # Clients wait for /health's boot_id to change only when a restart
            # is actually coming (A13-ui-ops-01).
            "restart_scheduled": True,
        }

        # Restart 2s after the SSE flush.
        _schedule_restart()

    async def _rollback_locked(self, tag: str) -> AsyncIterator[dict]:
        """Rollback under the lock. Order: refuse or stash a dirty tree, reset
        the code to the tag (keeping what changed since the check), restore
        the venvs the reverted commits changed (from the freeze taken before
        that update), persist the TERMINAL rolled_back phase, then schedule the
        restart. The code goes back before the venvs: the restore runs pip for
        minutes, and a reset after it overwrote edits saved meanwhile."""
        self.pre_update_tag = tag
        try:
            self.from_sha = await self._git(git_ops.current_sha)
            self.target_sha = await self._git(git_ops.tag_commit, tag)
        except _GIT_ERRORS as e:
            yield self._fail(PHASE_ROLLBACK, f"could not read {tag}: {_git_error(e)}")
            return
        if not self.target_sha:
            yield self._fail(PHASE_ROLLBACK,
                             f"The rollback anchor {tag} no longer exists in this "
                             f"checkout, so there is nothing safe to roll back to.")
            return

        try:
            dirty = await self._git(git_ops.tracked_changes)
        except _GIT_ERRORS as e:
            yield self._fail(PHASE_ROLLBACK, f"git status failed: {_git_error(e)}")
            return
        if dirty and not self.discard_local_changes:
            yield dirty_tree_refusal(dirty, "Rolling back")
            return

        self._write_state(PHASE_ROLLBACK)
        yield self._phase_event(PHASE_ROLLBACK)
        if dirty:
            try:
                note = await self._stash(f"auto-stash-rollback-{tag}", dirty)
            except _GIT_ERRORS as e:
                yield self._fail(PHASE_ROLLBACK, f"Could not stash the local changes, "
                                                 f"so nothing was reset: {_git_error(e)}")
                return
            yield self._log(PHASE_ROLLBACK, note)

        # Which venvs the reverted commits changed, read before HEAD moves: a
        # --force-reinstall of everything takes minutes for nothing otherwise.
        try:
            changed = await self._git(git_ops.diff_files, self.target_sha, self.from_sha)
        except _GIT_ERRORS as e:
            yield self._fail(PHASE_ROLLBACK, f"git diff failed: {_git_error(e)}")
            return
        buckets = changes_mod.categorize(changed)

        try:
            kept = await self._reset_keeping(
                self.target_sha, f"auto-stash-during-rollback-{tag}", f"rollback-{tag}")
        except _KeepFailed as e:
            yield self._fail(PHASE_ROLLBACK, f"{e} Nothing was rolled back.")
            return
        except _GIT_ERRORS as e:
            yield self._fail(PHASE_ROLLBACK, f"git reset --hard {tag} failed: {_git_error(e)}")
            return
        for note in kept:
            yield self._log(PHASE_ROLLBACK, note)
        yield self._log(PHASE_ROLLBACK, f"git reset --hard {tag} ({self.target_sha[:7]}) OK")

        # The freeze files hold the venvs as they were before the update being
        # reverted. Restore only the venvs that update's commits changed.
        for bucket, label, restore in (
                ("pip", "Orchestrator", self._restore_orch_venv),
                ("mcp_pip", "MCP", self._restore_mcp_venv)):
            if not buckets[bucket]:
                continue
            yield self._log(PHASE_ROLLBACK,
                            f"Restoring the {label} venv from its pre-update pip freeze...")
            rc, out = await restore(tag)
            if rc is None:
                yield self._log(PHASE_ROLLBACK,
                                f"WARNING: the {label} venv keeps its current packages: "
                                f"{out}.")
            elif rc != 0:
                yield self._log(PHASE_ROLLBACK,
                                f"WARNING: restoring the {label} venv failed "
                                f"(rc={rc}); the code is back at {tag} anyway. "
                                f"Output tail: {_tail(out)}")
            else:
                yield self._log(PHASE_ROLLBACK, f"{label} venv restored.")

        self._write_state(PHASE_ROLLED_BACK)
        yield {
            "type": "complete",
            "succeeded": True,
            "sha_before": self.from_sha[:7],
            "sha_after": self.target_sha[:7],
            "phase": PHASE_ROLLED_BACK,
            "task_id": self.task_id,
            "reverted_to": tag,
            "pre_update_tag": tag,
            "stash_ref": self.stash_ref,
            "stash_refs": list(self.stash_refs),
            "restart_scheduled": True,
        }
        _schedule_restart()

    # ── Privileged regen (shared by the helper refresh + systemd phases) ─

    async def _privileged_regen(self, phase: str, changed: list[str],
                                buckets: dict[str, bool]) -> AsyncIterator[dict]:
        """Apply system-level template changes without re-running install.sh.

        install.sh-grant removal, 2026-07-27: the former `sudo -n bash install.sh`
        re-run is gone — its `NOPASSWD: /usr/bin/bash <root>/Scripts/install.sh`
        grant was the in-service→root escalation this change closes. Instead, for
        each CHANGED git-tracked template that maps to a bounded
        blackbox-write-systemd target_kind (see _REGEN_TARGETS), render it with
        THIS box's real root/user and compare against the ROOT-OWNED trusted
        store entry /etc/blackbox/templates/<kind>:

          • store == repo render → the human HAS published this change. Dispatch
            `blackbox-write-systemd <kind>` (NO source) — the helper copies the
            store entry to its hardcoded dest — then restart the affected aux
            service via its OWN existing grant (never blackbox.service; the
            detached restart after the complete event owns that — restarting it
            here would SIGTERM the uvicorn process iterating this update, the
            reverted KillMode=process hazard).

          • store missing / root-unreadable / differing → the change is NOT
            published to the store yet (the store is human-`install.sh`-only, by
            design). Dispatching would copy the STALE store back and log a FALSE
            "Regenerated" success (the store-staleness trap the M3 review flags).
            So the runner does NOT dispatch and does NOT claim success — it
            surfaces the "run sudo bash install.sh once" operator remedy and lets
            the rest of the update complete. A store the human LATER publishes is
            still drift-healed onto the deployed copy at the next restart by the
            startup hooks (M3b).

        Called only from the systemd_regen phase (after apt/pip/mcp). Yields log
        events under `phase` (the caller owns the phase event). Never raises and
        never rolls back: every outcome here is degrade-and-continue.
        """
        # Probe non-interactive sudo BEFORE attempting any dispatch. If `sudo -n`
        # fails, none of the bounded helper grants are reachable — skip with a
        # friendly message rather than surfacing per-dispatch failures. Brandon's
        # customer hit the alarming-WARNING version of this 2026-05-23.
        probe_rc, _ = await self._run(["sudo", "-n", "true"], timeout=5.0)
        if probe_rc != 0:
            yield self._log(
                phase,
                "Skipped system-level regen: passwordless sudo is unavailable, "
                f"so the bounded {WRITE_SYSTEMD_HELPER} helper cannot be reached. "
                "Code and dependencies updated successfully. If a systemd unit, "
                "sudoers, or helper change needs to apply, run "
                f"`sudo bash {self.root}/Scripts/install.sh` once manually.")
            return

        changed_set = set(changed)
        regen_changed = [t for t in _REGEN_TARGETS if t.template in changed_set]
        restarts: list[str] = []

        for target in regen_changed:
            rendered = self._render_template(target)
            if rendered is None:
                yield self._log(
                    phase, f"Skipped {target.kind}: template "
                           f"{target.template} unreadable in this checkout.")
                continue
            # Gate on repo-render vs the ROOT-OWNED trusted STORE entry (NOT the
            # deployed copy). The store is human-`install.sh`-only; write-systemd
            # copies the store, not this render. So only a store that ALREADY
            # equals this render can be dispatched — otherwise write-systemd would
            # re-assert the stale store and we'd log a false success.
            store_content = self._read_store(target.kind)
            if store_content is None or store_content != rendered:
                # Unpublished change: store absent (fresh/legacy box), root-
                # unreadable (the 0440 sudoers the service user cannot read — we
                # cannot confirm it carries this change), or byte-differing (the
                # human has not re-run install.sh since this commit). Do NOT
                # dispatch and do NOT claim success — surface the operator remedy,
                # same shape as the new-apt-package case, and continue. The rest
                # of the update (code, pip) already landed.
                reason = ("its root-owned trusted store copy could not be read "
                          "to confirm it carries this change"
                          if store_content is None
                          else "this change is not yet in the root-owned trusted "
                               "template store")
                yield self._log(
                    phase,
                    f"Privileged config change to {target.deployed} was NOT "
                    f"applied: {reason}. Automated updates cannot write the "
                    f"root-owned template store by design; run `sudo bash "
                    f"{self.root}/Scripts/install.sh` once to publish and apply "
                    f"it. Code and dependencies updated successfully.")
                continue

            # store == repo render: the human HAS published this change. Safe to
            # drift-heal the deployed copy FROM the trusted store — write-systemd
            # copies /etc/blackbox/templates/<kind> to the hardcoded dest with NO
            # caller source.
            rc, out = await self._dispatch_write_systemd(target)
            if rc == 0:
                yield self._log(
                    phase, f"Regenerated {target.deployed} from the trusted "
                           f"store (blackbox-write-systemd {target.kind}).")
                if target.restart_service:
                    restarts.append(target.restart_service)
            else:
                # Non-fatal: the code + dependency update already landed. Name the
                # one-command remedy rather than roll everything back over a
                # system-config write (same precedent as the SHOULD_HAVE apt path).
                yield self._log(
                    phase,
                    f"WARNING: could not regenerate {target.deployed} from the "
                    f"trusted store via blackbox-write-systemd {target.kind} "
                    f"(rc={rc}); continuing. Run `sudo bash "
                    f"{self.root}/Scripts/install.sh` once if this change must "
                    f"apply now. Output tail: {_tail(out)}")

        # Restart affected aux services via their existing granted verbs. The
        # write-systemd helper already ran `daemon-reload` after each systemd-kind
        # write, so the new unit is loaded; this just bounces it. NEVER
        # blackbox.service — the detached restart after the complete event owns it.
        for svc in dict.fromkeys(restarts):  # dedup, order-preserving
            rc, out = await self._run(
                ["sudo", "-n", "systemctl", "restart", svc], timeout=30.0)
            if rc == 0:
                yield self._log(phase, f"Restarted {svc}.")
            else:
                yield self._log(
                    phase,
                    f"WARNING: {svc} restart failed (rc={rc}); it will pick up "
                    f"the new unit on its next start. Output tail: {_tail(out)}")

        # Residual (install.sh-grant removal, 2026-07-27): the always-on units are
        # inline heredocs in install.sh (blackbox.service, override,
        # cli-agent-overrides, mcp, ydotoold, time-sync), not standalone
        # templates — the runner cannot re-template them without a fragile
        # reimplementation. Most install.sh edits are user-session config, not unit
        # changes, so this is an informational note (not a failure) whenever the
        # install.sh-triggered systemd bucket is lit.
        if buckets["systemd"]:
            yield self._log(
                phase,
                "Note: this update changed Scripts/install.sh. Sudoers, helper, "
                "and template-backed units were applied above; if it also altered "
                "an inline systemd unit heredoc (rare), run "
                f"`sudo bash {self.root}/Scripts/install.sh` once to apply it.")

    def _render_template(self, target: "_RegenTarget") -> Optional[str]:
        """Render a regen template with THIS box's real root + user. Substituting
        a placeholder the template lacks is a no-op, so both are always applied —
        producing bytes identical to install.sh's per-file deploy. Returns None
        if the template is unreadable (the caller degrades, non-fatal)."""
        real_user = (os.environ.get("USER")
                     or os.environ.get("LOGNAME") or "bbx")
        try:
            text = (self.root / target.template).read_text(encoding="utf-8")
        except OSError:
            return None
        return (text.replace("BLACKBOX_ROOT_PLACEHOLDER", str(self.root))
                    .replace("REAL_USER_PLACEHOLDER", real_user))

    def _read_store(self, kind: str) -> Optional[str]:
        """Read the ROOT-OWNED trusted store entry /etc/blackbox/templates/<kind>.

        Returns None if the entry is ABSENT (fresh/legacy box, store not yet
        published) OR UNREADABLE (a 0440 root-owned sudoers the service user
        cannot read). In BOTH cases the runner cannot confirm the store carries a
        given repo change, so it must decline to dispatch (which would copy a
        possibly-stale store and log false success) — see _privileged_regen.

        The path mirrors blackbox-write-systemd's STORE_FILE exactly, including
        the TEST-ONLY $BLACKBOX_WRITE_SYSTEMD_DEST_ROOT sandbox rebase (empty in
        production, where sudo's env_reset scrubs it) so a test populates one fake
        store both the runner and the helper read."""
        root_prefix = os.environ.get("BLACKBOX_WRITE_SYSTEMD_DEST_ROOT", "")
        store_path = f"{root_prefix}{TEMPLATE_STORE_DIR}/{kind}"
        try:
            with open(store_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except (OSError, PermissionError):
            return None

    async def _dispatch_write_systemd(self,
                                      target: "_RegenTarget") -> tuple[int, str]:
        """Dispatch the bounded blackbox-write-systemd helper for `target.kind`
        with NO source argument (install.sh-grant removal, 2026-07-27). The
        helper copies the ROOT-OWNED store entry /etc/blackbox/templates/<kind>
        to the kind's hardcoded destination, validates sudoers via `visudo -c`,
        sets the mode, and daemon-reloads for systemd kinds — the caller supplies
        ONLY the kind, never a path OR content. Reached only after the caller has
        confirmed the store entry equals the repo render, so this is a pure
        store→dest drift-heal. Mirrors the M3b startup hooks' dispatch exactly."""
        return await self._run(
            ["sudo", "-n", WRITE_SYSTEMD_HELPER, target.kind], timeout=30.0)

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
        if self.stash_refs:
            shas = ", ".join(sha[:7] for sha in self.stash_refs)
            error += (f"\nLocal changes are saved in stash {shas}; `git stash list` "
                      f"shows it and `git stash apply <sha>` restores it.")
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
            "stash_ref": self.stash_ref,
            "stash_refs": list(self.stash_refs),
            "restart_scheduled": False,
        }

    async def _git(self, fn, *args):
        """Run a git_ops call against this root in a worker thread, so a slow
        fetch or a big reset never stalls the event loop (O16-09)."""
        return await asyncio.to_thread(fn, self.root, *args)

    async def _stash(self, message: str, dirty: list[str]) -> str:
        """Stash the local changes to tracked files under `message` and return
        the log line naming the stash. Raises a _GIT_ERRORS member when git
        cannot stash; the caller must not reset in that case."""
        return await asyncio.to_thread(self._stash_sync, message, dirty)

    def _stash_sync(self, message: str, dirty: list[str], *,
                    unasked: bool = False) -> str:
        """Blocking body of _stash. `unasked`: the changes appeared after the
        dirty check, so the operator never saw them (the log line says so)."""
        git_ops.stash_push(self.root, message)
        sha = git_ops.latest_stash(self.root)
        self.stash_refs.append(sha)
        noun = "file" if len(dirty) == 1 else "files"
        how = f"as {message} ({sha[:7]}); `git stash list` shows it and `git stash apply` restores it."
        if unasked:
            return (f"{len(dirty)} tracked {noun} changed after the check for local "
                    f"changes ({_shown(dirty)}). Stashed them instead of overwriting "
                    f"them, {how}")
        return f"Stashed local changes to {len(dirty)} tracked {noun} {how}"

    async def _reset_keeping(self, ref: str, stash_message: str,
                             aside: str) -> list[str]:
        """`git reset --hard ref` via _keep_then_reset, in a worker thread."""
        return await asyncio.to_thread(self._keep_then_reset, ref, stash_message, aside)

    def _keep_then_reset(self, ref: str, stash_message: str,
                         aside: str) -> list[str]:
        """`git reset --hard ref`, first keeping what it would destroy (O16-01).

        Blocking, and run as ONE worker-thread step so nothing of the runner's
        runs between this second look and the reset. The dirty check at the top
        of the run comes before the tag, the venv freezes or the diff, and an
        edit saved since then (a CLI agent in a terminal, an SSH session) used
        to be reset over. Here, tracked files changed since that check are
        stashed as `stash_message`, then untracked files the reset would
        overwrite (git_ops.paths_in_the_way) are moved under
        Manifest/displaced/<aside>/. Neither asks again: a stash or a move
        loses nothing, and refusing this late would leave the tag or the
        freezes out of step with the code.

        Returns the log lines naming what was kept. Raises _KeepFailed before
        the reset when something could not be kept, and the reset then does
        not run; a failing reset raises its own _GIT_ERRORS member.
        """
        notes: list[str] = []
        try:
            dirty = git_ops.tracked_changes(self.root)
            if dirty:
                notes.append(self._stash_sync(stash_message, dirty, unasked=True))
        except _GIT_ERRORS as e:
            raise _KeepFailed(f"Could not stash the local changes to tracked files: "
                              f"{_git_error(e)}.") from e
        try:
            in_way = git_ops.paths_in_the_way(self.root, ref)
            dest = self.root / "Manifest" / "displaced" / aside
            for rel in in_way:
                moved_to = dest / rel
                if os.path.lexists(moved_to):
                    raise FileExistsError(f"{moved_to} already exists")
                moved_to.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(self.root / rel), str(moved_to))
        except _GIT_ERRORS as e:
            raise _KeepFailed(f"Could not move aside the untracked files the reset "
                              f"would overwrite: {_git_error(e)}.") from e
        if in_way:
            noun = "file" if len(in_way) == 1 else "files"
            notes.append(f"Moved {len(in_way)} untracked {noun} the reset would "
                         f"overwrite ({_shown(in_way)}) to Manifest/displaced/{aside}/.")
        git_ops.reset_hard(self.root, ref)
        return notes

    def _restore_stashes(self, shas: list[str]) -> tuple[list[str], str]:
        """Put back the stashes `shas` (oldest first) after a rollback reset,
        dropping each one that applied: its changes are in the working tree
        again. Stops at the first that does not apply and leaves it, and the
        rest, in the stash list (still named in stash_refs). Blocking; returns
        (SHAs put back, "" or the problem sentence)."""
        put_back: list[str] = []
        for sha in shas:
            try:
                git_ops.stash_apply(self.root, sha)
            except _GIT_ERRORS as e:
                return put_back, (f"the local changes in stash {sha[:7]} could not be put "
                                  f"back ({_git_error(e)}); `git stash apply {sha[:7]}` "
                                  f"restores them")
            try:
                git_ops.drop_stash(self.root, sha)
            except _GIT_ERRORS:
                pass  # applied: the entry left behind is a spare copy
            self.stash_refs.remove(sha)
            put_back.append(sha)
        return put_back, ""

    async def _fail_rolled_back(self, phase: str, error: str,
                                *venvs: str) -> AsyncIterator[dict]:
        """Roll a failed update back, then report the failure: restore the
        venvs named in `venvs` ("orch", "mcp") from this update's freezes, reset
        the code to the pre-update tag (keeping what the reset would destroy),
        and yield the log lines and then the failed complete event. The
        rollback runs BEFORE the event, and the event's error ends with what
        really happened: rolled back, a venv that could not be restored, or
        code left in place because what the reset would destroy could not be
        kept (the reset then never runs). Once the code is back, the local
        changes this run stashed are put back too (X04-security-clients-infra-11:
        the rollback used to leave them in the stash list)."""
        restored: list[str] = []
        problems: list[str] = []
        for which in venvs:
            label, restore = (("Orchestrator", self._restore_orch_venv) if which == "orch"
                              else ("MCP", self._restore_mcp_venv))
            rc, out = await restore(self.pre_update_tag)
            if rc is None:
                problems.append(f"the {label} venv was not restored: {out}")
            elif rc != 0:
                problems.append(f"restoring the {label} venv failed (rc={rc}): {_tail(out)}")
            else:
                restored.append(label)
        # Made on the pre-update code, so they apply cleanly once it is back;
        # what the rollback's own reset stashes (the failed run's leftovers) is
        # not among them and stays in the stash list.
        stashed_before = list(self.stash_refs)
        try:
            kept = await self._reset_keeping(
                self.pre_update_tag, f"auto-stash-rollback-{self.pre_update_tag}",
                f"rollback-{self.pre_update_tag}")
        except (_KeepFailed, *_GIT_ERRORS) as e:
            why = str(e) if isinstance(e, _KeepFailed) else _git_error(e)
            outcome = (f"The code was NOT rolled back to {self.pre_update_tag}: "
                       f"{why.rstrip('.')}. The new code is still on disk, so do not "
                       f"restart the service yet: commit or stash the changed files, "
                       f"then run `git reset --hard {self.pre_update_tag}` in {self.root}.")
        else:
            for note in kept:
                yield self._log(phase, note)
            outcome = "The update was rolled back"
            if restored:
                outcome += (f" and the {' and '.join(restored)} venv restored from its "
                            f"pre-update pip freeze (Manifest/)")
            outcome += "."
            put_back, stash_problem = await asyncio.to_thread(self._restore_stashes,
                                                              stashed_before)
            if put_back:
                outcome += (" The local changes it stashed were put back in the "
                            "working tree.")
            if stash_problem:
                problems.append(stash_problem)
        error += "\n" + outcome
        for problem in problems:
            error += f"\nWARNING: {problem}."
        yield self._fail(phase, error)

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
        Pre-rollback restore happens via pip-sync-equivalent in _restore_*.

        Blocking: the runner calls it through asyncio.to_thread (O16-09). An
        older update's freeze is removed first and a failed freeze writes
        nothing, so a rollback never reinstalls packages from the wrong update;
        the first line names this update's tag (FREEZE_HEADER) for the same
        reason. Raises with every venv's problem once both have been tried."""
        problems = []
        for pip_rel, out_rel in (
                ("Orchestrator/venv/bin/pip", "Manifest/pre_update_pip_freeze.txt"),
                ("MCP/venv/bin/pip", "Manifest/pre_update_mcp_freeze.txt")):
            pip = self.root / pip_rel
            if not pip.is_file():
                continue
            freeze = self.root / out_rel
            freeze.unlink(missing_ok=True)
            try:
                out = subprocess.run([str(pip), "freeze"],
                                     capture_output=True, text=True, timeout=30)
            except (subprocess.TimeoutExpired, OSError) as e:
                problems.append(f"{pip_rel} freeze: {e}")
                continue
            if out.returncode != 0:
                problems.append(f"{pip_rel} freeze exited {out.returncode}: "
                                f"{_tail(out.stderr, 2)}")
                continue
            freeze.write_text(f"{FREEZE_HEADER}{self.pre_update_tag}\n{out.stdout}")
        if problems:
            raise RuntimeError("; ".join(problems))

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

    def _installed_lock_extras(self) -> list[str]:
        """The LOCK_EXTRAS present in the pre-update freeze. The lock install
        removes nothing, so they are still installed."""
        freeze = self.root / "Manifest/pre_update_pip_freeze.txt"
        try:
            installed = {_canonical_name(line) for line in freeze.read_text().splitlines()}
        except OSError:
            return []
        return [name for name in LOCK_EXTRAS if name in installed]

    async def _restore_orch_venv(self, tag: str) -> tuple[Optional[int], str]:
        """Reinstall the pre-update freeze into the Orchestrator venv. Returns
        pip's (returncode, output), or (None, why) when nothing was reinstalled:
        no freeze on disk, or one taken for another update than `tag`."""
        freeze = self.root / "Manifest/pre_update_pip_freeze.txt"
        problem = _freeze_problem(freeze, tag)
        if problem:
            return None, problem
        # Leave out pins with a local version label (torch==2.11.0+cpu):
        # they came from another index, PyPI cannot serve them, and one
        # unservable line makes pip refuse the whole restore. Nothing in
        # the update installs them, so they are still in place.
        lines = freeze.read_text().splitlines()
        kept = [line for line in lines
                if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*==\S*\+", line)]
        restore = freeze
        if len(kept) != len(lines):
            restore = self.root / "Manifest/pre_update_pip_restore.txt"
            restore.write_text("".join(line + "\n" for line in kept))
        return await self._run([
            str(self.root / "Orchestrator/venv/bin/pip"),
            "install", "-r", str(restore),
            "--quiet", "--force-reinstall",
        ], timeout=600.0)

    async def _restore_mcp_venv(self, tag: str) -> tuple[Optional[int], str]:
        """Reinstall the pre-update freeze into the MCP venv; (None, why) when
        nothing was reinstalled (see _restore_orch_venv)."""
        freeze = self.root / "Manifest/pre_update_mcp_freeze.txt"
        problem = _freeze_problem(freeze, tag)
        if problem:
            return None, problem
        return await self._run([
            str(self.root / "MCP/venv/bin/pip"),
            "install", "-r", str(freeze),
            "--quiet", "--force-reinstall",
        ], timeout=300.0)


class _KeepFailed(Exception):
    """_keep_then_reset could not keep what a reset would destroy, so it did
    not reset. The message is a sentence for the operator."""


def _freeze_problem(freeze: Path, tag: str) -> str:
    """Why `freeze` must not be reinstalled for a rollback to `tag`, or ""."""
    try:
        first = freeze.read_text().split("\n", 1)[0]
    except OSError:
        return "no pre-update pip freeze for it is on disk"
    if first.startswith(FREEZE_HEADER):
        owner = first[len(FREEZE_HEADER):].strip()
        if owner != tag:
            return (f"its pip freeze on disk was taken before {owner} (Scripts/update.sh "
                    f"writes the same file), not before {tag}")
    return ""


def _shown(paths: list[str]) -> str:
    """Up to five paths for a log line."""
    shown = ", ".join(paths[:5])
    return shown + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")


def _schedule_restart(delay_s: float = 2.0) -> None:
    """Fire the detached restart `delay_s` after the complete event, so the
    client has received it before the service dies (audit M4). Called from
    inside the runner's async generators, where a loop is always running."""
    asyncio.get_running_loop().call_later(delay_s, _fire_detached_restart)


def dirty_tree_message(dirty: list[str], action: str) -> str:
    """The sentence an operator sees when local changes block `action`
    ("Installing the update", "Rolling back")."""
    noun = "file" if len(dirty) == 1 else "files"
    return (f"This box has local changes to {len(dirty)} tracked {noun} ({_shown(dirty)}). "
            f"{action} would overwrite them, so nothing was changed. Commit or "
            f"stash them yourself, or confirm again and let the updater stash "
            f"them (`git stash list` shows them later, `git stash apply` "
            f"restores them).")


def dirty_tree_refusal(dirty: list[str], action: str) -> dict:
    """The complete event for a refused dirty tree. Nothing was changed, so
    no state is persisted: a refusal is not a failed update."""
    return {"type": "complete", "succeeded": False, "phase": None,
            "refused": "dirty_tree", "dirty_files": list(dirty),
            "error": dirty_tree_message(dirty, action),
            "restart_scheduled": False}


def update_refusal(error: str) -> dict:
    """The complete event for an update the trust pins refuse (verify.py): an
    origin they do not list, or a commit without a pinned release signature.
    Nothing was fetched from an unpinned origin, and nothing was tagged,
    stashed or reset, so no state is persisted; the journal gets a warning."""
    logger.warning("[UPDATE] refused: %s", error)
    return {"type": "complete", "succeeded": False, "phase": None,
            "refused": "unverified_update", "error": error,
            "restart_scheduled": False}


def _git_error(e: BaseException) -> str:
    """Short operator-facing text for a _GIT_ERRORS exception."""
    if isinstance(e, subprocess.TimeoutExpired):
        return f"git timed out after {e.timeout:.0f}s"
    if isinstance(e, subprocess.CalledProcessError):
        return _tail(e.stderr or e.stdout or "") or f"git exited {e.returncode}"
    return str(e)


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


def _canonical_name(requirement_line: str) -> str:
    """PEP 503 name of a `pip freeze` line ("Sentence_Transformers==6.2.0" ->
    "sentence-transformers"); "" for anything else."""
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==", requirement_line)
    return re.sub(r"[-_.]+", "-", m.group(1)).lower() if m else ""


def _tail(text: str, lines: int = 6) -> str:
    """Last N lines of `text` for compact error display in SSE events."""
    return "\n".join(text.splitlines()[-lines:])


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
