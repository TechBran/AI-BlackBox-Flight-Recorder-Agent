"""In-place update pipeline (T6 / audit-revised plan).

The Portal's "Install update" button triggers a full-stack update of the
BlackBox install: git pull, pip install, MCP venv update, apt install (via
the bounded /usr/local/sbin/blackbox-apt-install helper), systemd unit
regeneration (via /usr/local/sbin/blackbox-write-systemd helper), and a
detached service restart. User data (Volumes/, Manifest/, Fossils/,
Portal/uploads/, .env, config.ini, devices.json) is preserved by the
combination of .gitignore (those dirs are untracked → git reset --hard
won't touch them) and pip-sync-style rollback for the venvs.

Module map:
  - changes.py        — categorize git-diff file lists into action buckets
  - git_ops.py        — thin subprocess wrappers around git commands
  - active_sessions.py — detect in-flight tmux CLI sessions before restart
  - manager.py        — flock-based mutex + persistent state machine
  - runner.py         — full update flow with SSE-friendly event emission
"""
