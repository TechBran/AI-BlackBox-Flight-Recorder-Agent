"""Ask a CLI agent whether it is signed in, instead of guessing from files.

GET /onboarding/cli-agent/status used to report authenticated = "a
credential-looking file exists under ~". On the production box (2026-10-29)
~/.claude held its marker while `claude auth status` said loggedIn=false, so
the wizard said "Ready" for an agent that could not run. A provider whose CLI
has a cheap, non-interactive status command is now asked directly (verified on
the dev box 2026-10-29):

  * claude  `claude auth status` prints JSON {"loggedIn": bool, "authMethod":
            ...} (its default format); exit 1 when signed out, and the JSON
            still says so.
  * codex   `codex login status` prints "Logged in using ..." (exit 0) or
            "Not logged in" (exit 1), on stderr.
  * gemini has no status command (0.66: only mcp/extensions/skills/hooks/gemma
    subcommands) and antigravity keeps its login in the OS keyring: both stay
    on the marker heuristic in onboarding_routes.

The probe runs the binary in the env every CLI agent gets
(headless.build_child_env: the shared env_scrub table, the provider's billing
keys stripped, PATH extended so an nvm CLI's `#!/usr/bin/env node` resolves),
so the server's ANTHROPIC_API_KEY / OPENAI_API_KEY never reach the CLI and
cannot be reported back as a login. stdin is /dev/null, the CLI runs in its own
process group and the whole group is killed after PROBE_TIMEOUT_S, and nothing
it prints is logged or returned (the output names the account).

probe() returns True/False as the CLI reports it, or None when the CLI cannot
say (no status command, binary missing, timeout, output it does not recognise);
the caller then falls back to the marker. Answers, None included, are cached
per process for CACHE_TTL_S, keyed on the binary, HOME and the stat of the
provider's credential files: a sign-in or sign-out in a terminal rewrites those
files, so the wizard's Re-check sees it at once, and a hung CLI costs one
timeout per window instead of one per GET. The hub's rollup (GET
/onboarding/status, also built on the SSE stream's event loop) promises no
subprocess, so it reads the cache only (cached_only=True).
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 3.0
CACHE_TTL_S = 60.0

# provider -> argv after the binary. Only commands that never prompt.
STATUS_ARGS: dict[str, tuple[str, ...]] = {
    "claude": ("auth", "status"),
    "codex": ("login", "status"),
}

_CACHE: dict[tuple, tuple[float, Optional[bool]]] = {}
_LOCK = threading.Lock()
_now = time.monotonic  # the cache's clock (tests move it)


def reset_cache() -> None:
    """Forget every cached answer (tests; a restart does the same)."""
    with _LOCK:
        _CACHE.clear()


def has_status_command(provider: str) -> bool:
    return provider in STATUS_ARGS


def _parse_claude(rc: int, out: str, err: str) -> Optional[bool]:
    try:
        doc = json.loads(out)
    except ValueError:
        return None
    logged_in = doc.get("loggedIn") if isinstance(doc, dict) else None
    return logged_in if isinstance(logged_in, bool) else None


_CODEX_OUT = re.compile(r"^(Not logged in|Logged in)\b", re.MULTILINE)


def _parse_codex(rc: int, out: str, err: str) -> Optional[bool]:
    said = {m.group(1) for m in _CODEX_OUT.finditer(f"{out}\n{err}")}
    if said == {"Logged in"} and rc == 0:
        return True
    if said == {"Not logged in"} and rc != 0:
        return False
    return None


_PARSERS = {"claude": _parse_claude, "codex": _parse_codex}


def _run(provider: str, bin_path: str, home: Path) -> Optional[bool]:
    from Orchestrator.cli_agent.headless import build_child_env

    env = build_child_env(provider)
    env["HOME"] = str(home)
    try:
        proc = subprocess.Popen(
            [bin_path, *STATUS_ARGS[provider]],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=str(home) if home.is_dir() else None,
            start_new_session=True, text=True, errors="replace")
    except OSError as exc:
        logger.warning("[CLI-AUTH] %s status could not start (%s); using the credential marker",
                       provider, type(exc).__name__)
        return None
    with proc:
        try:
            out, err = proc.communicate(timeout=PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait()
            logger.warning("[CLI-AUTH] %s status did not answer within %.1fs; using the credential marker",
                           provider, PROBE_TIMEOUT_S)
            return None
    answer = _PARSERS[provider](proc.returncode, out or "", err or "")
    if answer is None:
        logger.warning("[CLI-AUTH] %s status output not recognised (exit %s); using the credential marker",
                       provider, proc.returncode)
    return answer


def _marker_signature(home: Path, markers: Sequence[str]) -> tuple:
    sig = []
    for rel in markers:
        try:
            st = (home / rel).stat()
            sig.append((rel, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((rel, None, None))
    return tuple(sig)


def probe(provider: str, bin_path: str, *, home: Path, markers: Sequence[str] = (),
          cached_only: bool = False) -> Optional[bool]:
    """Whether ``provider``'s CLI at ``bin_path`` says it is signed in for
    ``home``: True / False, or None when it cannot say. Cached (see module).
    ``cached_only``: never spawn; answer from a fresh cache entry or None (the
    onboarding hub's probe-free rollup)."""
    if provider not in STATUS_ARGS:
        return None
    key = (provider, bin_path, str(home), _marker_signature(home, markers))
    now = _now()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    if cached_only:
        return None
    answer = _run(provider, bin_path, home)
    with _LOCK:
        # Entries for an older credential-file state are dead; drop them.
        for stale in [k for k in _CACHE if k[:3] == key[:3]]:
            del _CACHE[stale]
        _CACHE[key] = (_now() + CACHE_TTL_S, answer)
    return answer
