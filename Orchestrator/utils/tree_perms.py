"""Who else can write into the checkout: the startup warning (2026-10 audit
rank 79, X04-security-clients-infra-13).

The service runs `python -m uvicorn` with the checkout as its working
directory, so Python imports from it first, and the Portal, the MCP server and
the CLI agents' slash commands are served or read from it too. A directory in
it that other local accounts can write lets them plant a module the service
imports (the dev box had the root, Orchestrator/, Portal/, Manifest/ and
.claude/commands/ at 0777), and a file in it they can write is the same hole
without the planting (the ledger, a module, .claude/settings.local.json, whose
hooks run as whoever starts Claude Code there). Manifest/ (MCP tokens, OAuth stores, update state)
and credentials/ hold 0600 files, but in a directory others can open or write
those can be listed, unlinked and replaced, so both must be 0700.

install.sh fixes all of it as the checkout's owner (harden_tree_permissions),
and moves the service off a sudo-capable account on request (--service-user;
service_account_warnings, X04-security-clients-infra-12, says when it has not).
This module only REPORTS what is still wrong, for the startup hook in
Orchestrator/startup.py to put in the journal: it never changes a mode and
never raises. Group write counts only when the group is one other accounts can
hold: on Ubuntu every user has a private group of their own, and a 0775
directory in that group is writable by its owner alone.
"""
from __future__ import annotations

import grp
import os
import pwd
import stat
from pathlib import Path

# Directories that are 0700 or a problem.
PRIVATE_DIRS = ("Manifest", "credentials")

# Not walked: installed packages and build output, not the project's own code
# (install.sh's harden_tree_permissions still covers them), and other
# checkouts nested in this one (they are checked by their own service, if any).
_PRUNE_NAMES = frozenset({"venv", "node_modules", "target"})
_PRUNE_PATHS = frozenset({".claude/worktrees"})

# How many directories one start may look at (the dev box's checkout, minus
# the pruned trees, is about 9000).
MAX_DIRS = 20000
# How many writable files a warning names (the rest are counted).
SHOWN = 8

_REMEDY = "sudo bash Scripts/install.sh fixes it (it removes those bits as the checkout's owner)"


def audit_checkout(root: Path, *, max_dirs: int = MAX_DIRS) -> list[str]:
    """Warnings (sentences) about `root`, empty when there is nothing wrong."""
    root = Path(root)
    warnings: list[str] = []
    writable: list[str] = []
    writable_files: list[str] = []
    seen = 0
    cut_short = False
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        rel = os.path.relpath(dirpath, root)
        dirnames[:] = [d for d in dirnames
                       if d not in _PRUNE_NAMES
                       and os.path.normpath(os.path.join(rel, d)) not in _PRUNE_PATHS]
        if seen >= max_dirs:
            cut_short = True
            break
        seen += 1
        try:
            st = os.lstat(dirpath)
        except OSError:
            continue
        if _writable_by_others(st):
            writable.append("(the checkout root)" if rel == "." else rel)
        for name in filenames:
            try:
                fst = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            # Others' write only, as install.sh removes (a file's group write
            # in the owner's private group is the owner's alone).
            if stat.S_ISREG(fst.st_mode) and fst.st_mode & stat.S_IWOTH:
                writable_files.append(os.path.normpath(os.path.join(rel, name)))
    note = f" (stopped after {max_dirs} directories)" if cut_short else ""
    if writable:
        noun = "directory" if len(writable) == 1 else "directories"
        warnings.append(
            f"{len(writable)} {noun} in {root} can be written by other local accounts"
            f"{note}: {_some(writable)}. Any of them can plant code this service imports or "
            f"serves; {_REMEDY}.")
    if writable_files:
        noun = "file" if len(writable_files) == 1 else "files"
        warnings.append(
            f"{len(writable_files)} {noun} in {root} can be written by every local account"
            f"{note}: {_some(writable_files)}. Any of them can be rewritten in place (code, "
            f"config, the ledger); {_REMEDY}.")
    # A parent anyone can write (without the sticky bit) lets them move the
    # whole checkout aside and put another in its place.
    for parent in root.resolve().parents:
        try:
            st = os.stat(parent)
        except OSError:
            continue
        if st.st_mode & stat.S_IWOTH and not st.st_mode & stat.S_ISVTX:
            warnings.append(
                f"{parent} (a parent of the checkout) can be written by every local "
                f"account, which can replace the checkout itself (fix: "
                f"chmod o-w {parent}).")
    for name in PRIVATE_DIRS:
        try:
            st = os.stat(root / name)
        except OSError:
            continue  # absent on a fresh box: created 0700 by install.sh
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077:
            warnings.append(
                f"{root / name}/ is mode {mode:o}, not 0700: it holds tokens and "
                f"credentials other local accounts must not list or replace; {_REMEDY} "
                f"(or: chmod 0700 {root / name}).")
    return warnings


def _some(paths: list[str]) -> str:
    return ", ".join(paths[:SHOWN]) + (f" and {len(paths) - SHOWN} more" if len(paths) > SHOWN else "")


# Groups whose members can become root with their own password.
ADMIN_GROUPS = frozenset({"sudo", "admin", "wheel"})


def service_account_warnings() -> list[str]:
    """One warning when this process runs as a sudo-capable account (audit
    X04-security-clients-infra-12): a compromised service can then rewrite that
    account's shell rc files and autostart entries and reach its next
    interactive sudo. `install.sh --service-user` moves the service to a
    dedicated non-login account; until then this is the documented boundary.
    Never raises."""
    try:
        name, groups = _account_groups()
    except (KeyError, OSError):
        return []
    admin = sorted(ADMIN_GROUPS.intersection(groups))
    if not admin:
        return []
    return [f"This service runs as {name}, a member of {', '.join(admin)}: anything that "
            f"compromises the service can write {name}'s home and wait for its next "
            f"sudo. Move it to a dedicated non-login account: sudo bash "
            f"Scripts/install.sh --service-user blackbox --dry-run, then without "
            f"--dry-run (docs/runbooks/2026-10-29-update-trust-and-service-user.md)."]


def _account_groups() -> tuple[str, list[str]]:
    """This process's user name and the names of all its groups."""
    name = pwd.getpwuid(os.getuid()).pw_name
    return name, [grp.getgrgid(g).gr_name for g in os.getgroups()]


def _writable_by_others(st: os.stat_result) -> bool:
    if not stat.S_ISDIR(st.st_mode):
        return False
    if st.st_mode & stat.S_IWOTH:
        return True
    return bool(st.st_mode & stat.S_IWGRP) and _group_is_shared(st.st_gid, st.st_uid)


def _real_group_is_shared(gid: int, owner_uid: int) -> bool:
    """True unless `gid` is the owner's own private group with no other
    members (an unknown group or owner counts as shared)."""
    try:
        group = grp.getgrgid(gid)
        owner = pwd.getpwuid(owner_uid)
    except KeyError:
        return True
    if group.gr_name != owner.pw_name or owner.pw_gid != gid:
        return True
    return any(member != owner.pw_name for member in group.gr_mem)


# Looked up at call time, so a test can stand in for the account database.
_group_is_shared = _real_group_is_shared
