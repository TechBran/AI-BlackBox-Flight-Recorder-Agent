"""Pinned update origin + release-signature gate (2026-10 audit rank 79,
X04-security-clients-infra-11).

Both updaters apply the same two checks before they change anything on the
box: this module for the in-app runner (runner.py), and Scripts/update.sh for
the SSH path (its check_pinned_origin / verify_release_commit, which follow the
rules below line for line).

  1. ORIGIN. /etc/blackbox/update-origin lists the URLs updates may come from,
     one per line (# comments and blank lines ignored). The effective
     `git remote get-url origin` (url.*.insteadOf applied) must be one of them,
     compared after trimming one trailing "/" and one trailing ".git" and
     ignoring case (GitHub owner and repository names are case-insensitive).
     Checked BEFORE the fetch, so the box never fetches from anywhere else.
  2. SIGNATURE. /etc/blackbox/update-allowed-signers is an ssh-keygen ALLOWED
     SIGNERS file. When it holds at least one key, the commit about to be
     installed must carry an SSH signature (a `gpgsig -----BEGIN SSH
     SIGNATURE-----` header) that `git verify-commit` accepts against that
     file. The verification pins gpg.ssh.allowedSignersFile and gpg.ssh.program
     on the command line, which beats anything .git/config says, and points the
     PGP and X.509 programs at `false`, so neither a planted config nor a
     signature from a key the box was never given can pass.

Both files are written ONLY by `sudo bash install.sh` (publish_update_pins, from
the tracked installer/update-origin and installer/allowed_signers), root-owned
like the template store, and /etc/blackbox is never in the service's
ReadWritePaths. A file that is not root-owned, is a symlink, or is writable by
group or others is not a pin, and the update is refused. A MISSING file means
"not pinned yet" (a box that has not run that install.sh, or a repo that ships
no release key yet): the update goes ahead with a warning in its log, the way
the template store is adopted. See
docs/runbooks/2026-10-29-update-trust-and-service-user.md (how releases get
signed, and how a box adopts the pins).

What this does NOT cover: code already on disk. The runner is itself service
code, so a compromised service could skip these checks; the gate protects the
box from what arrives over the network (a hijacked or renamed repository, a
tampered origin URL, a man in the middle), which is where the commits it
installs come from.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Optional

from Orchestrator.update import git_ops

# Root-owned trust anchors. Module constants (never environment variables: the
# service's environment comes partly from the service-writable .env), so only a
# test can move them, with monkeypatch.
TRUST_DIR = "/etc/blackbox"
ORIGIN_PIN = "update-origin"
SIGNERS_PIN = "update-allowed-signers"
TRUSTED_UID = 0

# The tracked files install.sh publishes as those pins.
PIN_SOURCES = ("installer/update-origin", "installer/allowed_signers")

# The raw commit header line of an SSH signature (git writes the armour's first
# line into the gpgsig header).
SSH_SIGNATURE_HEADER = "gpgsig -----BEGIN SSH SIGNATURE-----"

_GIT_ERRORS = (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError)


class UpdateRefused(Exception):
    """The update may not be installed. The message is a sentence for the
    operator, naming what to fix."""


def check_origin(root: Path) -> list[str]:
    """Refuse (UpdateRefused) unless the checkout's origin is a pinned one.

    Returns the log notes for the update: one warning when no origin is
    pinned on this box yet, else nothing."""
    pins = _read_pin(ORIGIN_PIN)
    if pins is None:
        return [f"WARNING: no update origin is pinned on this box ({_pin_path(ORIGIN_PIN)} "
                f"is missing), so the origin was not checked. `sudo bash "
                f"{root}/Scripts/install.sh` pins it."]
    if not pins:
        raise UpdateRefused(f"{_pin_path(ORIGIN_PIN)} lists no update origin, so no "
                            f"origin is accepted. Re-run `sudo bash {root}/Scripts/install.sh` "
                            f"to restore it.")
    try:
        url = git_ops.origin_url(root)
    except _GIT_ERRORS as e:
        raise UpdateRefused(f"Could not read this checkout's origin remote: {_git_error(e)}") from e
    if normalize_origin(url) not in {normalize_origin(p) for p in pins}:
        raise UpdateRefused(
            f"This checkout fetches updates from {url}, which is not a pinned update "
            f"origin ({_pin_path(ORIGIN_PIN)} lists {', '.join(pins)}). Nothing was "
            f"fetched or changed. If the origin was changed by mistake, put it back with "
            f"`git -C {root} remote set-url origin {pins[0]}`.")
    return []


def check_commit(root: Path, sha: str) -> list[str]:
    """Refuse (UpdateRefused) unless commit `sha` is signed by a pinned release
    key, when this box pins any.

    Returns the log notes: one warning when no release key is pinned (the
    signature was not checked), else nothing."""
    keys = _read_pin(SIGNERS_PIN)
    if not keys:
        where = (f"{_pin_path(SIGNERS_PIN)} is missing" if keys is None
                 else f"{_pin_path(SIGNERS_PIN)} holds no key")
        return [f"WARNING: no release signing key is pinned on this box ({where}), so "
                f"the signature of {sha[:7]} was not checked."]
    try:
        raw = git_ops.raw_commit(root, sha)
    except _GIT_ERRORS as e:
        raise UpdateRefused(f"Could not read commit {sha[:7]}: {_git_error(e)}") from e
    header = raw.split("\n\n", 1)[0].splitlines()
    if SSH_SIGNATURE_HEADER not in header:
        raise UpdateRefused(
            f"Commit {sha[:7]} is not signed with a release key (no SSH signature), "
            f"and this box only installs signed releases ({_pin_path(SIGNERS_PIN)}). "
            f"Nothing was changed.")
    keygen = _program("ssh-keygen")
    false = _program("false")
    if not keygen or not false:
        raise UpdateRefused("ssh-keygen (openssh-client) is not installed, so the release "
                            "signature cannot be checked. Nothing was changed.")
    try:
        out = git_ops.verify_commit(root, sha, allowed_signers=_pin_path(SIGNERS_PIN),
                                    ssh_keygen=keygen, false=false)
    except _GIT_ERRORS as e:
        raise UpdateRefused(f"Could not check the signature of {sha[:7]}: {_git_error(e)}") from e
    if out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip().splitlines()
        raise UpdateRefused(
            f"The signature on commit {sha[:7]} is not from a pinned release key "
            f"({_pin_path(SIGNERS_PIN)}): {detail[-1] if detail else 'verification failed'}. "
            f"Nothing was changed.")
    return []


def normalize_origin(url: str) -> str:
    """The comparable form of an origin URL: trimmed, lower-cased, then one
    trailing "/" and one trailing ".git" removed (Scripts/update.sh's
    normalize_origin does the same, in the same order)."""
    url = url.strip().lower()
    if url.endswith("/"):
        url = url[:-1]
    if url.endswith(".git"):
        url = url[:-4]
    return url


def _pin_path(name: str) -> str:
    return os.path.join(TRUST_DIR, name)


def _read_pin(name: str) -> Optional[list[str]]:
    """The non-comment lines of trust file `name`: None when it does not exist.
    Raises UpdateRefused when it, or the directory holding it, is not
    root-owned or is writable by anyone else: that is not a pin."""
    path = _pin_path(name)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise UpdateRefused(f"Could not read {path}: {e}") from e
    _require_trusted(TRUST_DIR, os.lstat(TRUST_DIR), directory=True)
    _require_trusted(path, st, directory=False)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise UpdateRefused(f"Could not read {path}: {e}") from e
    return [line.strip() for line in lines
            if line.strip() and not line.strip().startswith("#")]


def _require_trusted(path: str, st: os.stat_result, *, directory: bool) -> None:
    kind_ok = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
    if not kind_ok or st.st_uid != TRUSTED_UID or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UpdateRefused(
            f"{path} is not a trusted pin (it must be a root-owned "
            f"{'directory' if directory else 'regular file'} nobody else can write), so "
            f"the update was refused. `sudo bash install.sh` writes it correctly.")


def _program(name: str) -> str:
    """Absolute path of a system program, preferring /usr/bin over PATH."""
    for candidate in (f"/usr/bin/{name}", f"/bin/{name}"):
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which(name) or ""


def _git_error(e: BaseException) -> str:
    if isinstance(e, subprocess.TimeoutExpired):
        return f"git timed out after {e.timeout:.0f}s"
    if isinstance(e, subprocess.CalledProcessError):
        lines = (e.stderr or e.stdout or "").strip().splitlines()
        return lines[-1] if lines else f"git exited {e.returncode}"
    return str(e)
