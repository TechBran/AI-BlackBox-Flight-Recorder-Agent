"""Categorize changed files into action buckets for the update runner.

Pure functions, no I/O. Given a list of git-relative paths (output of
`git diff --name-only HEAD..origin/main`), return which subsystems of
the install need re-running. The runner uses this to skip expensive
steps (apt install, pip install, daemon-reload) when nothing in that
bucket changed — code-only updates land in seconds.

Bucket semantics:
  - apt         : Scripts/onboarding/system-packages.txt changed → re-run
                  blackbox-apt-install for each new MUST_HAVE/SHOULD_HAVE
  - pip         : requirements.txt changed → re-pip-install in Orchestrator venv
  - mcp_pip     : MCP/requirements.txt changed → re-pip-install in MCP venv
  - systemd     : Scripts/install.sh changed (heuristic — any install.sh edit
                  may have touched a heredoc that regenerates a systemd unit
                  or drop-in; safer to re-run install.sh's idempotent blocks)
  - sudoers     : installer/templates/sudoers-blackbox-system changed → rewrite
                  the sudoers file via blackbox-write-systemd sudoers-system
  - helpers     : installer/templates/blackbox-{apt-install,write-systemd}.sh
                  changed → reinstall to /usr/local/sbin/ (mode 0755 root:root)
  - npm_globals : Scripts/install.sh's Step 1c npm install line changed →
                  re-run npm install -g (heuristic via install.sh hash too)
  - code_only   : nothing in the above → just a Python/JS/HTML/CSS file swap.
                  No system-level work needed, just service restart.

The runner ALWAYS runs `code_only` (because git reset --hard already
happened) + restart. The other buckets are conditional on category=True.
"""
from __future__ import annotations

from typing import Iterable


# File-path patterns that map to each bucket. Order matters only for
# documentation — the categorize() function checks all patterns.
_PATTERNS = {
    "apt":         ("Scripts/onboarding/system-packages.txt",),
    "pip":         ("requirements.txt",),
    "mcp_pip":     ("MCP/requirements.txt",),
    "sudoers":     ("installer/templates/sudoers-blackbox-system",),
    "helpers":     ("installer/templates/blackbox-apt-install.sh",
                    "installer/templates/blackbox-write-systemd.sh"),
    # install.sh changes are a HEURISTIC for systemd unit/drop-in regen.
    # The unit + override.conf + cli-agent-overrides.conf are all generated
    # inline via heredocs in install.sh's Steps 4 / 4b / 4b1. Any edit to
    # install.sh might have touched a heredoc — safer to re-run install.sh's
    # idempotent blocks (which is what the runner does for this bucket).
    "systemd":     ("Scripts/install.sh",),
    # npm_globals: install.sh Step 1c hard-codes the npm package list. Same
    # heuristic — any install.sh edit might have changed it. Bundled into
    # the "systemd" bucket since both are install.sh-re-run triggers.
}


def categorize(changed_files: Iterable[str]) -> dict[str, bool]:
    """Return a dict of bucket → bool indicating which action categories
    are triggered by this set of changed files.

    Always includes `code_only=True` as a sentinel — the caller can use it
    to detect "no files changed at all" by checking if all OTHER buckets
    are False.

    Args:
        changed_files: iterable of git-relative paths (forward slashes).

    Returns:
        Dict with keys apt, pip, mcp_pip, sudoers, helpers, systemd, code_only.
        Each is True if at least one matching file is in changed_files.
    """
    changed = set(changed_files)
    result: dict[str, bool] = {bucket: False for bucket in _PATTERNS}
    for bucket, patterns in _PATTERNS.items():
        for pattern in patterns:
            if pattern in changed:
                result[bucket] = True
                break
    # code_only is always True — every update results in some file swap.
    # Caller checks "is this a real update at all?" via len(changed) > 0.
    result["code_only"] = True
    return result


def all_buckets() -> tuple[str, ...]:
    """Return the canonical bucket name list (for test parametrization +
    UI category-badge rendering)."""
    return ("apt", "pip", "mcp_pip", "sudoers", "helpers", "systemd", "code_only")
