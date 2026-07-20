import logging
import os
import re
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Default idle threshold: 7 days. Keep as the safety net for abandoned
# sessions (the user accepts it). Configurable via CLI_AGENT_IDLE_DAYS.
_DEFAULT_IDLE_SECONDS = 7 * 86400

# How often the in-process zellij reaper sweeps. Hourly is plenty for a
# 7-day threshold and keeps the accumulated EXITED zombies from lingering
# more than ~1h past the cutoff.
ZELLIJ_REAP_INTERVAL_SEC = 3600.0

# Only ever touch sessions whose name matches the BlackBox convention
# ``{operator}__{provider}[__...]`` — never a hand-created zellij session
# (e.g. "bbx test") an operator made outside the orchestrator.
_BBX_SESSION_RE = re.compile(r"^[^\s]+__[^\s]+(?:__|$)")

# Parse zellij's "Created X ago" age string, e.g.
#   "26days 15h 17m 30s ago" / "3h 59m 23s ago" / "4m 56s ago"
_AGE_RE = re.compile(
    r"(?:(?P<days>\d+)\s*days?)?\s*"
    r"(?:(?P<hours>\d+)\s*h)?\s*"
    r"(?:(?P<mins>\d+)\s*m)?\s*"
    r"(?:(?P<secs>\d+)\s*s)?"
)


def _parse_age_seconds(age: str) -> Optional[int]:
    """Convert a zellij "Created" age string to seconds, or None if it
    can't be parsed (caller then leaves the session alone — never reap on
    an unparseable age)."""
    if not age:
        return None
    m = _AGE_RE.search(age.replace(" ago", "").strip())
    if not m or not any(m.groupdict().values()):
        return None
    d = int(m.group("days") or 0)
    h = int(m.group("hours") or 0)
    mi = int(m.group("mins") or 0)
    se = int(m.group("secs") or 0)
    return d * 86400 + h * 3600 + mi * 60 + se


def reap_idle_zellij_sessions(idle_seconds: int = _DEFAULT_IDLE_SECONDS) -> List[str]:
    """Delete zellij sessions (BlackBox-named) older than ``idle_seconds``.

    This is the ZELLIJ analogue of :func:`reap_idle_sessions` (which is
    tmux-only). The live CLI-agent backend is zellij, so the tmux reaper
    reaps nothing — this is what actually clears the accumulated
    ``(EXITED - attach to resurrect)`` zombies AND abandoned-but-running
    sessions on a schedule.

    Idle proxy: zellij's CLI exposes only a "Created X ago" age, not a
    per-session activity timestamp. We use age-since-creation as the idle
    signal. This is sound because ATTACHING to a session (which the
    zellij-web client does whenever the user opens/resumes a terminal)
    RESETS the created clock — so an actively-used or recently-resumed
    session keeps a young age and is NEVER reaped, while a genuinely
    abandoned session ages past the threshold. Both EXITED and running
    sessions are subject to the same age cutoff (an EXITED session IS a
    resumable terminal until reaped; a young EXITED session is preserved
    so the user can still resurrect it).

    Safety:
      - only names matching the BlackBox convention (``a__b...``) are
        touched — never a hand-created zellij session;
      - a session with an unparseable age is left alone (fail-safe);
      - deletes via the idempotent :func:`zellij_client.kill_session`
        (``delete-session --force``), which also frees the name.

    Returns the list of deleted session names. Bails out cleanly (empty
    list) on any zellij CLI failure.
    """
    from Orchestrator.cli_agent import terminal_uploads, zellij_client

    try:
        sessions = zellij_client.list_sessions()
    except Exception as exc:  # noqa: BLE001 — daemon may be down
        logger.warning("reap_idle_zellij_sessions: list_sessions failed (%s)", exc)
        # No session list -> no reap AND no orphan sweep (we can't know
        # which upload folders are live; never sweep blind).
        return []

    killed: List[str] = []
    for sess in sessions:
        name = sess.get("name")
        if not name or not _BBX_SESSION_RE.match(name):
            continue
        age = _parse_age_seconds(sess.get("created_at", ""))
        if age is None:
            logger.debug(
                "reap_idle_zellij_sessions: unparseable age for %s (%r) — skipping",
                name, sess.get("created_at"),
            )
            continue
        if age < idle_seconds:
            continue
        try:
            zellij_client.kill_session(name)
            # State row (if any) is dropped on the next reconcile; remove it
            # now too so /zellij/sessions stops showing the reaped session.
            from Orchestrator.cli_agent import zellij_state
            zellij_state.remove_session(name)
            killed.append(name)
            logger.info(
                "reap_idle_zellij_sessions: reaped %s (age=%ds >= %ds, exited=%s)",
                name, age, idle_seconds, sess.get("exited"),
            )
        except Exception as exc:  # noqa: BLE001 — one bad session shouldn't stop the sweep
            logger.warning(
                "reap_idle_zellij_sessions: kill %s failed (%s)", name, exc
            )
            continue
        # Task 5: a reaped session's upload folder dies with it. Removed
        # DIRECTLY (not left to the orphan sweep below) because a
        # just-reaped folder's mtime may be younger than the sweep's age
        # grace. Best-effort — a folder failure must not stop the loop.
        try:
            terminal_uploads.remove_for_session(name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reap_idle_zellij_sessions: upload-folder removal for %s "
                "failed (non-fatal): %s", name, exc,
            )

    # Task 5: sweep upload folders orphaned by out-of-band deaths
    # (sessions killed from zellij's own session-manager never hit
    # DELETE /zellij/sessions). Live set = sessions still alive AFTER
    # this reap pass; the idle window doubles as the age grace so an
    # EXITED-but-resurrectable session (or a mid-upload race) keeps its
    # folder exactly as long as the session itself would be kept.
    try:
        live = {
            s.get("name") for s in sessions
            if s.get("name") and s.get("name") not in killed
        }
        swept = terminal_uploads.sweep_orphans(live, float(idle_seconds))
        if swept:
            logger.info(
                "reap_idle_zellij_sessions: swept %d orphan upload "
                "folder(s): %s", len(swept), swept,
            )
    except Exception as exc:  # noqa: BLE001 — cleanup must never fail the reap
        logger.warning(
            "reap_idle_zellij_sessions: orphan upload sweep failed "
            "(non-fatal): %s", exc,
        )
    return killed


def _idle_seconds_from_env() -> int:
    try:
        return int(os.getenv("CLI_AGENT_IDLE_DAYS", "7")) * 86400
    except (TypeError, ValueError):
        return _DEFAULT_IDLE_SECONDS


def start_zellij_reaper(interval: float = ZELLIJ_REAP_INTERVAL_SEC):
    """Start the periodic in-process zellij reaper on the running event
    loop (call from FastAPI startup). Mirrors live_session_reaper's
    pattern: a never-dying asyncio loop that swallows per-sweep errors.

    Returns the created asyncio.Task. The first sweep runs immediately
    (not after one interval) so accumulated zombies get cleared promptly
    on boot; subsequent sweeps run every ``interval`` seconds.
    """
    import asyncio

    idle_seconds = _idle_seconds_from_env()

    async def _loop():
        first = True
        while True:
            try:
                if not first:
                    await asyncio.sleep(interval)
                first = False
                # zellij_client.* are blocking subprocess calls — run off
                # the event loop.
                killed = await asyncio.to_thread(
                    reap_idle_zellij_sessions, idle_seconds
                )
                if killed:
                    logger.info(
                        "[ZELLIJ-REAPER] reaped %d idle session(s): %s",
                        len(killed), killed,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a maintenance loop must never die
                logger.error("[ZELLIJ-REAPER] sweep failed (non-fatal): %s", exc)

    return asyncio.create_task(_loop())



def reap_idle_sessions(manager, idle_seconds: int = 7 * 86400) -> List[str]:
    """Kill CLI Agent tmux sessions whose last activity is older than
    `idle_seconds`. Returns the list of killed session names.

    Talks to tmux via `manager._tmux(...)` so the same socket-aware
    invocation pattern used by the manager is reused. Bail out cleanly
    on any tmux failure (e.g., server not running) by returning [].
    """
    res = manager._tmux("list-sessions",
                         "-F", "#{session_name} #{session_activity}")
    if res.returncode != 0:
        return []
    cutoff = int(time.time()) - idle_seconds
    killed: List[str] = []
    for line in res.stdout.splitlines():
        try:
            name, activity = line.rsplit(" ", 1)
            activity_ts = int(activity)
        except (ValueError, IndexError):
            continue
        if not name.startswith("cli-agent-"):
            continue
        if activity_ts < cutoff:
            manager.kill(name)
            killed.append(name)
    return killed


def _main() -> int:
    """CLI entry point for scheduling.

    Schedule via crontab (simplest):
        0 4 * * * /home/.../Orchestrator/venv/bin/python \\
                  -m Orchestrator.cli_agent.reaper

    Or via systemd user timer (cleaner; survives blackbox.service restarts).
    Threshold is configurable via CLI_AGENT_IDLE_DAYS env var (default 7).

    Note: the cron system at /api/cron/jobs is LLM-prompt-driven and
    overkill for this deterministic system task — schedule directly via
    crontab/systemd timer instead.
    """
    from Orchestrator.cli_agent.path_validator import PathValidator
    from Orchestrator.cli_agent.operator_config import OperatorConfig
    from Orchestrator.cli_agent.session_manager import TmuxSessionManager

    apps_root = Path(os.getenv("CLI_AGENT_APPS_ROOT")
                      or Path(__file__).resolve().parents[2] / "Apps")
    cfg_root = Path(os.getenv("CLI_AGENT_CONFIG_ROOT")
                     or Path.home() / ".claude-bbox")
    idle_days = int(os.getenv("CLI_AGENT_IDLE_DAYS", "7"))

    pv = PathValidator(apps_root=apps_root)
    oc = OperatorConfig(root=cfg_root)
    mgr = TmuxSessionManager(path_validator=pv, operator_config=oc)
    killed = reap_idle_sessions(mgr, idle_seconds=idle_days * 86400)
    print(f"[cli-agent-reaper] killed {len(killed)} session(s) "
          f"(threshold: {idle_days}d)")
    for name in killed:
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
