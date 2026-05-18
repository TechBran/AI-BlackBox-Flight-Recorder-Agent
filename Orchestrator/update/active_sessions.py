"""Detect in-flight CLI agent tmux sessions before service restart (audit M8).

The update flow restarts blackbox.service, which kills all open WebSocket
PTY bridges. The tmux SERVERS + child claude/gemini/codex processes
SURVIVE thanks to KillMode=process (E22 drop-in), but the user's browser
loses its terminal stream and has to re-attach manually after restart.

For multi-operator deployments and for active in-progress conversations,
the wizard should warn before triggering an update. This module provides
the enumeration helper.
"""
from __future__ import annotations

import subprocess


_PREFIX = "cli-agent-"


def list_active_cli_sessions() -> list[str]:
    """Return tmux session names that look like CLI agent sessions.

    Uses `tmux list-sessions -F "#{session_name}"` (the same call
    session_manager.py uses internally). If no tmux server is running,
    tmux exits non-zero and we return an empty list — which is correct:
    no sessions means no in-flight CLI work to warn about.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # tmux not installed (unlikely — it's MUST_HAVE) OR hung. Treat as
        # "can't detect" → return empty. Better to false-negative the warn
        # banner than false-positive and refuse all updates.
        return []
    if result.returncode != 0:
        # Typical: "no server running on /tmp/tmux-1000/default" → exit 1
        return []
    sessions = []
    for line in result.stdout.splitlines():
        name = line.strip()
        if name.startswith(_PREFIX):
            sessions.append(name)
    return sessions


def count_active_cli_sessions() -> int:
    """Convenience: just the count. Used by the wizard's preflight banner
    ("N active CLI sessions will be disconnected on update")."""
    return len(list_active_cli_sessions())
