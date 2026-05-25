"""
Python adapter for Zellij CLI invocations.

Wraps `zellij` and `zellij web` CLI commands so the orchestrator can mint
tokens, create sessions, list/kill sessions, and inject text. All commands
run as the orchestrator's own user (same as zellij-web.service per audit
C0) — no sudo needed; shared filesystem namespace makes tokens.db readable
directly.

Locked design decisions (from Phase 0 + production-install audit):

- Token model: short-lived mint-per-launch (audit I7 Option B). Caller must
  return the raw value in the launch-response payload then forget it.
- State: this module is stateless. (operator, provider, app) → token_name
  mapping lives in zellij_state.py.
- Auth: shared user namespace; no sudo.
- HTTPS: NOT enforced on localhost (per plan AC2 — TLS terminates at the
  orchestrator's Tailscale-funnel edge). web_server_healthy() uses http://.

All subprocess calls use the args-list form (never shell=True) and run
synchronously — Zellij CLI invocations are sub-100ms in practice, so the
caller (an async route handler) should wrap calls in
``asyncio.to_thread()`` if non-blocking behavior is required.
"""
from __future__ import annotations

import logging
import os
import pty
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Locked port for zellij-web on the BlackBox device (plan Track A T1,
# Phase 0 finding #10). install.sh seeds the same value into config.kdl;
# this constant is the fallback when config.kdl is unreadable.
_DEFAULT_ZELLIJ_WEB_PORT = 9097

_ZELLIJ_BIN = "/usr/local/bin/zellij"

# Path to the user-level config file we manage. install.sh seeds the
# initial file; ensure_config() refreshes it at orchestrator startup so
# new required fields land without re-running install.sh (audit M15).
_CONFIG_PATH = Path.home() / ".config" / "zellij" / "config.kdl"

# Required fields for zellij-web to listen + accept tokens on localhost.
# Keep this list in lock-step with installer/templates/zellij-config.kdl
# and plan AC2. If you change anything here, bump the regeneration in
# ensure_config() — the function rewrites the whole file, never patches.
_REQUIRED_CONFIG_LINES = (
    'web_server true',
    'web_server_ip "127.0.0.1"',
    f'web_server_port {_DEFAULT_ZELLIJ_WEB_PORT}',
    'web_sharing "on"',
    'enforce_https_for_localhost false',
)

# Stdout from `zellij web --create-token` looks like:
#   Created token successfully
#
#   token_3: 6dac3716-1a65-4ea6-95f8-c54af9bdebb0
#
# Match the `name: uuid` payload anywhere in stdout. UUID match is lax
# (any hex-with-dashes) because we only need to extract the value Zellij
# already minted — validation is Zellij's job, not ours.
_TOKEN_LINE_RE = re.compile(
    r'(?P<name>token_\d+)\s*:\s*'
    r'(?P<value>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12})'
)

# `zellij web --list-tokens` rows look like:
#   token_1: created at 2026-05-25 03:50:21
_TOKEN_LIST_LINE_RE = re.compile(
    r'^(?P<name>token_\d+)\s*:\s*created at\s+(?P<created_at>.+)$'
)

# `zellij list-sessions --no-formatting` rows look like:
#   bbx test [Created 2h 59m 20s ago]
_SESSION_LIST_LINE_RE = re.compile(
    r'^(?P<name>\S+)\s+\[Created\s+(?P<created_at>[^\]]+)\]\s*$'
)


# --- internal helpers ----------------------------------------------------


def _run(
    args: list[str],
    *,
    check: bool = True,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess:
    """Thin wrapper around subprocess.run with project conventions.

    - args-list form only (never shell=True)
    - text mode (str stdout/stderr)
    - bounded timeout (Zellij CLI calls are sub-100ms in practice; 10s
      is a generous ceiling for I/O hiccups)
    - logs full argv at DEBUG, failure detail at DEBUG (callers decide
      whether to upgrade to ERROR — some failures are idempotent paths)
    """
    logger.debug("zellij_client._run: %s", args)
    try:
        result = subprocess.run(
            args,
            check=check,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        logger.debug(
            "zellij command non-zero exit (rc=%s): %s -- stdout=%r stderr=%r",
            exc.returncode,
            args,
            (exc.stdout or "").strip(),
            (exc.stderr or "").strip(),
        )
        raise
    except subprocess.TimeoutExpired:
        logger.error("zellij command timed out after %ss: %s", timeout, args)
        raise
    return result


def _read_port_from_config() -> int:
    """Parse ~/.config/zellij/config.kdl for web_server_port. Falls back
    to the default port on any read/parse failure — the daemon may not
    have been configured yet at first-boot, and the caller's retry loop
    will still get the right answer if the default matches reality.
    """
    try:
        text = _CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return _DEFAULT_ZELLIJ_WEB_PORT
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("//") or not line:
            continue
        if line.startswith("web_server_port"):
            # Format: web_server_port 9097
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1].strip('"').strip())
                except ValueError:
                    continue
    return _DEFAULT_ZELLIJ_WEB_PORT


# --- token operations (Option B, mint-per-launch — audit I7) -------------


def mint_token() -> tuple[str, str]:
    """Mint a new Zellij web token. Returns ``(auto_name, uuid_value)``.

    Runs ``zellij web --create-token``, parses stdout for the auto-assigned
    name (``token_1``, ``token_2``, ...) and its UUID value. Zellij 0.44.3
    treats ``--create-token`` and ``--token-name`` as mutually exclusive,
    so the name is whatever Zellij assigns.

    SECURITY (audit I7): the caller MUST embed ``uuid_value`` in the
    launch-response payload and then forget it. NEVER persist
    ``uuid_value`` to disk. State stores ``auto_name`` only.
    """
    result = _run([_ZELLIJ_BIN, "web", "--create-token"])
    match = _TOKEN_LINE_RE.search(result.stdout)
    if not match:
        logger.error(
            "mint_token: could not parse `zellij web --create-token` "
            "output: %r",
            result.stdout,
        )
        raise RuntimeError(
            "Failed to parse `zellij web --create-token` output"
        )
    name = match.group("name")
    value = match.group("value")
    logger.info("zellij token minted: name=%s", name)
    return name, value


def revoke_token(name: str) -> None:
    """Revoke a previously-minted Zellij token. Idempotent.

    Runs ``zellij web --revoke-token <name>``. If the token does not
    exist (already revoked, or never existed) Zellij exits with rc=2 and
    the message "Token by that name does not exist." — we log and
    swallow that as the desired idempotent behavior.
    """
    try:
        _run([_ZELLIJ_BIN, "web", "--revoke-token", name])
        logger.info("zellij token revoked: name=%s", name)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        # Zellij prints the "does not exist" message to stdout in 0.44.3
        # (not stderr); check both to be safe across version drift.
        if "does not exist" in stderr.lower() or "does not exist" in stdout.lower():
            logger.info(
                "revoke_token: token %s already absent (idempotent no-op)",
                name,
            )
            return
        raise


def list_tokens() -> list[dict]:
    """Return all tokens known to Zellij as
    ``[{"name": str, "created_at": str}, ...]``.

    Runs ``zellij web --list-tokens``. Used by
    :func:`zellij_state.reconcile_or_wipe` for state ↔ tokens.db
    consistency checks. Note that Zellij does NOT expose raw token values
    via this command (hash-only storage — Phase 0 finding #6).
    """
    result = _run([_ZELLIJ_BIN, "web", "--list-tokens"])
    rows: list[dict] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _TOKEN_LIST_LINE_RE.match(line)
        if not match:
            logger.debug("list_tokens: skipping unparseable line: %r", line)
            continue
        rows.append({
            "name": match.group("name"),
            "created_at": match.group("created_at").strip(),
        })
    return rows


# --- session operations --------------------------------------------------


def launch_session(
    name: str,
    binary: Optional[str] = None,
    args: Optional[list[str]] = None,
) -> None:
    """Create a Zellij session named ``name``.

    If ``binary`` is provided, the session auto-runs that binary on
    creation (CLI-provider path). If ``binary`` is None, the session
    runs the default user shell (BlackBox Terminal mode — AC10).

    Mechanism:

    - With binary:  ``zellij --session <name> -- <binary> <args...>``
    - Without:      ``zellij --session <name>``

    Per Phase 0 + Phase 1, the session_name MUST follow the
    ``{operator}__{provider}__{app_or_root}`` convention. Callers (route
    endpoints) own the prefix discipline (audit I8). This adapter does
    not validate the name.

    Note: ``zellij --session`` attaches to (and creates if missing) the
    named session in the current terminal. When invoked from the
    orchestrator (which has no controlling TTY) the spawn returns
    immediately after the session is registered with the zellij-web
    daemon, which is what we want. The CLI keeps running under Zellij's
    per-session backend process — not under the orchestrator.

    Raises ``subprocess.CalledProcessError`` on failure (caller decides
    how to surface the error to the user).
    """
    argv = [_ZELLIJ_BIN, "--session", name]
    if binary is not None:
        argv.append("--")
        argv.append(binary)
        if args:
            argv.extend(args)

    logger.info(
        "launch_session: name=%s binary=%s args=%s",
        name,
        binary,
        args,
    )

    # The orchestrator runs headless under systemd — no controlling TTY.
    # `zellij --session` panics on "could not enable raw mode" without a
    # pty (the client tries to put stdin into raw mode for the user's
    # eventual interactive use). We allocate a throwaway pty just to
    # satisfy that check; the actual interactive client is the browser
    # connecting via zellij-web. The session backend process runs
    # independently of this pty's lifetime — closing the pty does NOT
    # kill the session.
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=subprocess.PIPE,
            start_new_session=True,
            text=True,
            close_fds=True,
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        raise

    # The Popen now owns the slave-end via dup; we can close our copy.
    os.close(slave_fd)

    # Brief wait so the session lands in `list_sessions()` before the
    # caller asks for it. Zellij registers the session with the daemon
    # well within 500ms in practice; if Popen has already exited by
    # then it usually means an error (bad binary, name collision).
    try:
        rc = proc.wait(timeout=2.0)
        # If we got here zellij exited quickly — either failure, or the
        # session is fully detached and that's normal. Check rc.
    except subprocess.TimeoutExpired:
        # Healthy path: the client is still running, session is registered,
        # backend is alive. Detach: kill the orchestrator-side client
        # process (the per-session backend is separate and survives).
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.0)
        os.close(master_fd)
        return

    os.close(master_fd)

    if rc != 0:
        stderr = (proc.stderr.read() if proc.stderr else "") or ""
        logger.error(
            "launch_session failed (rc=%s) for %s: %s",
            rc,
            name,
            stderr.strip(),
        )
        raise subprocess.CalledProcessError(rc, argv, output="", stderr=stderr)


def list_sessions() -> list[dict]:
    """Return all Zellij sessions as
    ``[{"name": str, "created_at": str}, ...]``.

    Runs ``zellij list-sessions --no-formatting``. This is a GLOBAL list
    — the orchestrator filters by operator-name-prefix in the route
    layer (audit I8); the adapter does not.
    """
    try:
        result = _run([_ZELLIJ_BIN, "list-sessions", "--no-formatting"])
    except subprocess.CalledProcessError as exc:
        # Zellij returns non-zero when there are no sessions on some
        # versions; treat that as an empty list rather than blowing up.
        stderr = (exc.stderr or "").lower()
        if "no active sessions" in stderr or "no sessions" in stderr:
            return []
        raise

    rows: list[dict] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _SESSION_LIST_LINE_RE.match(line)
        if not match:
            logger.debug("list_sessions: skipping unparseable line: %r", line)
            continue
        rows.append({
            "name": match.group("name"),
            "created_at": match.group("created_at").strip(),
        })
    return rows


def kill_session(name: str) -> None:
    """Kill a Zellij session by name. Idempotent — killing a session
    that does not exist logs and returns cleanly.

    Note: Zellij 0.44.3 prints "No session named X found." to stdout
    (not stderr) and exits rc=1 in that case. We match either stream.
    """
    try:
        _run([_ZELLIJ_BIN, "kill-session", name])
        logger.info("zellij session killed: name=%s", name)
    except subprocess.CalledProcessError as exc:
        combined = ((exc.stderr or "") + (exc.stdout or "")).lower()
        if (
            "no session" in combined
            or "does not exist" in combined
            or "not found" in combined
        ):
            logger.info(
                "kill_session: session %s already absent (idempotent)",
                name,
            )
            return
        raise


def inject(session_name: str, text: str) -> None:
    """Inject ``text`` into the given Zellij session as if the user typed it.

    Runs ``zellij --session <session_name> action write-chars <text>``.
    Used by the AC10 shortcut palette.

    SECURITY (audit I10): the caller MUST enforce CSRF + operator-prefix
    gate + ``provider="terminal"`` restriction + audit log BEFORE
    calling this — the adapter only does the mechanical inject. No
    sanitization happens here; the caller is the security boundary.
    """
    argv = [
        _ZELLIJ_BIN,
        "--session",
        session_name,
        "action",
        "write-chars",
        text,
    ]
    logger.info(
        "inject: session=%s text_len=%d",
        session_name,
        len(text),
    )
    _run(argv)


# --- health + config (audit C2 + M15) ------------------------------------


def web_server_healthy(
    retries: int = 5,
    backoff_seconds: float = 2.0,
) -> bool:
    """Return True if the zellij-web daemon answers HTTP on its port.

    Curls ``http://127.0.0.1:<port>/`` with linear backoff between
    attempts. On first orchestrator boot the daemon may still be coming
    up; the default retries+backoff buy ~10 seconds total.

    Lighter than ``zellij web --status`` which has a ``--port``-flag
    respect bug in 0.44.3 (Phase 0 finding).

    Port is read from ``~/.config/zellij/config.kdl`` when available,
    falling back to the locked default 9097.
    """
    port = _read_port_from_config()
    url = f"http://127.0.0.1:{port}/"
    attempts = max(1, int(retries))

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if 200 <= resp.status < 300:
                    if attempt > 1:
                        logger.info(
                            "web_server_healthy: ok at %s on attempt %d",
                            url,
                            attempt,
                        )
                    return True
                logger.warning(
                    "web_server_healthy: %s returned HTTP %s on attempt %d",
                    url,
                    resp.status,
                    attempt,
                )
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            logger.debug(
                "web_server_healthy: attempt %d/%d failed: %s",
                attempt,
                attempts,
                exc,
            )

        if attempt < attempts:
            time.sleep(backoff_seconds)

    logger.error(
        "web_server_healthy: zellij-web not reachable at %s after %d attempts",
        url,
        attempts,
    )
    return False


def ensure_config() -> None:
    """Idempotently assert ``~/.config/zellij/config.kdl`` has all required
    fields for the current Zellij version.

    Called at orchestrator startup so version bumps with new required
    fields get applied without re-running install.sh (audit M15).

    install.sh seeds the initial file at install time; this function
    refreshes it on every orchestrator boot. The required content is:

    .. code-block:: kdl

        web_server true
        web_server_ip "127.0.0.1"
        web_server_port 9097
        web_sharing "on"
        enforce_https_for_localhost false

    If the file is missing OR missing any required line, REGENERATE the
    entire file (no patching — keep it simple). Logs the regeneration
    loudly so anyone reading journalctl knows their hand-edits got
    overwritten.
    """
    existing_lines: set[str] = set()
    file_present = _CONFIG_PATH.exists()
    if file_present:
        try:
            existing_lines = {
                line.strip()
                for line in _CONFIG_PATH.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("//")
            }
        except OSError as exc:
            logger.warning("ensure_config: cannot read %s: %s", _CONFIG_PATH, exc)
            existing_lines = set()

    missing = [
        line for line in _REQUIRED_CONFIG_LINES if line not in existing_lines
    ]
    if file_present and not missing:
        logger.debug("ensure_config: %s already has all required fields", _CONFIG_PATH)
        return

    if not file_present:
        logger.warning(
            "ensure_config: %s missing — regenerating from template",
            _CONFIG_PATH,
        )
    else:
        logger.warning(
            "ensure_config: %s missing required fields %s — regenerating",
            _CONFIG_PATH,
            missing,
        )

    body = (
        "// " + str(_CONFIG_PATH) + "\n"
        "// Generated by BlackBox orchestrator (zellij_client.ensure_config) — "
        "DO NOT EDIT BY HAND.\n"
        "// Zellij web server config for AI BlackBox CLI Agent.\n"
        "// HTTP on 127.0.0.1 only — TLS terminated at orchestrator edge "
        "(plan AC2).\n"
        + "\n".join(_REQUIRED_CONFIG_LINES)
        + "\n"
    )

    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write so we never leave a half-written config behind for
    # the daemon to choke on. Same .tmp + os.replace dance used
    # throughout the orchestrator for state files.
    tmp_path = _CONFIG_PATH.with_suffix(_CONFIG_PATH.suffix + ".tmp")
    tmp_path.write_text(body, encoding="utf-8")
    try:
        os.chmod(tmp_path, 0o644)
    except OSError as exc:
        logger.debug("ensure_config: chmod on %s failed (continuing): %s", tmp_path, exc)
    os.replace(tmp_path, _CONFIG_PATH)
    logger.info("ensure_config: wrote %s", _CONFIG_PATH)
