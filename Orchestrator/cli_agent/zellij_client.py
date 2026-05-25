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
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ZellijBinaryMissing(RuntimeError):
    """Raised when the zellij binary is not present at the expected path.

    Typically means install.sh has not been run (or Phase 1 dependencies
    were skipped). Surfaced as a typed error so route handlers can render
    a friendly "run install.sh" hint instead of a raw FileNotFoundError
    traceback.
    """

# Locked port for zellij-web on the BlackBox device (plan Track A T1,
# Phase 0 finding #10). install.sh seeds the same value into config.kdl;
# this constant is the fallback when config.kdl is unreadable.
_DEFAULT_ZELLIJ_WEB_PORT = 9097

_ZELLIJ_BIN = "/usr/local/bin/zellij"

# Env vars stripped from the child environment when spawning a Zellij
# session.
#
# ANTHROPIC_API_KEY (added 2026-05-25 — Brandon MSO2 report): the
# orchestrator's server-side key from .env propagates into the claude
# pane and triggers claude's "both a token and an API key are set"
# warning at the top of every session — the user's cached OAuth token
# (or Vertex auth via CLAUDE_CODE_USE_VERTEX=1) is ambiguous with the
# leaked env var. Strip it so each pane's claude falls back to its own
# user-side auth without warnings. Other CLI providers (gemini, codex,
# agy) don't read ANTHROPIC_API_KEY, so stripping is safe globally.
# GOOGLE_APPLICATION_CREDENTIALS is intentionally NOT stripped —
# claude needs it for Vertex AI auth and gemini may use it too.
_ENV_DENYLIST_FOR_PANES = frozenset({
    "ANTHROPIC_API_KEY",
})

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
    - logs full argv at DEBUG, failure detail at WARNING (visible in
      production journalctl which usually filters at INFO+). Callers can
      still catch and decide whether to upgrade to ERROR for fatal cases
      or downgrade to INFO for known-idempotent paths.
    - FileNotFoundError on the zellij binary is re-raised as
      ZellijBinaryMissing so route handlers can render an actionable
      "install.sh" hint rather than a cryptic Python traceback.
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
    except FileNotFoundError as exc:
        # Most likely: /usr/local/bin/zellij absent (install.sh not run).
        if args and args[0] == _ZELLIJ_BIN:
            logger.error(
                "zellij binary missing at %s — run install.sh to install "
                "Phase 1 dependencies",
                _ZELLIJ_BIN,
            )
            raise ZellijBinaryMissing(
                f"zellij binary missing at {_ZELLIJ_BIN} — "
                "run install.sh to install Phase 1 dependencies"
            ) from exc
        # Some other binary missing (unlikely but don't swallow).
        logger.error("subprocess binary missing: %s (%s)", args[0] if args else "?", exc)
        raise
    except subprocess.CalledProcessError as exc:
        logger.warning(
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

    ROBUSTNESS: takes the LAST ``token_N: <uuid>`` line in stdout (not
    the first) so any future Zellij version that prints the full token
    list with the new one appended still yields the just-minted token.
    Also requires the "Created token successfully" preamble — if that's
    missing, the output format has changed in a way we can't safely
    parse, and we raise rather than silently grab the wrong row.
    """
    # Zellij 0.44.3 auto-names tokens token_1, token_2, etc. — but doesn't
    # always reuse the lowest-free slot after revokes (empirically observed
    # T15: counter sometimes drifts and a mint trips "Token name 'token_N'
    # already exists"). Force-revoke any colliding name and retry. Up to 5
    # collisions per mint; beyond that, the token-store is genuinely wedged
    # and the caller should surface the original error.
    result = None
    last_err: Optional[Exception] = None
    for _ in range(6):
        try:
            result = _run([_ZELLIJ_BIN, "web", "--create-token"])
            break
        except subprocess.CalledProcessError as exc:
            combined = ((exc.stderr or "") + (exc.stdout or ""))
            m = re.search(r"Token name '([^']+)' already exists", combined)
            if not m:
                raise
            collide_name = m.group(1)
            logger.warning(
                "mint_token: collision on %s — force-revoking + retrying",
                collide_name,
            )
            try:
                _run([_ZELLIJ_BIN, "web", "--revoke-token", collide_name])
            except subprocess.CalledProcessError as revoke_err:
                logger.warning(
                    "mint_token: force-revoke of %s failed: %s",
                    collide_name,
                    revoke_err,
                )
            last_err = exc
    if result is None:
        # Exhausted retries — re-raise the most recent collision error.
        raise last_err or RuntimeError("mint_token retry budget exhausted")
    stdout = result.stdout or ""

    # Preamble sanity check — guard against silent format drift.
    if "Created token successfully" not in stdout:
        logger.error(
            "mint_token: 'Created token successfully' preamble missing "
            "in `zellij web --create-token` output: %r",
            stdout,
        )
        raise RuntimeError(
            "Unexpected `zellij web --create-token` output format "
            "(preamble missing) — refusing to parse"
        )

    matches = list(_TOKEN_LINE_RE.finditer(stdout))
    if not matches:
        logger.error(
            "mint_token: could not parse `zellij web --create-token` "
            "output: %r",
            stdout,
        )
        raise RuntimeError(
            "Failed to parse `zellij web --create-token` output"
        )
    # Take last match — the just-minted token will be the most recent
    # one printed, even if Zellij decides to also dump the full list.
    match = matches[-1]
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


def _kdl_escape(value: str) -> str:
    """Escape a string for safe inclusion in a KDL double-quoted literal.

    KDL string literals support C-style backslash escapes. We escape
    backslash + double-quote so a malicious binary/arg cannot break out
    of the quoted token, terminate the pane block, or smuggle KDL syntax
    into the layout file. Newlines/tabs are also escaped to keep the
    generated KDL on one line per token (cleaner for debug logs and
    avoids accidental block-comment / multi-line-string surprises).
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _build_layout_kdl(binary: str, args: Optional[list[str]]) -> str:
    """Render the minimal KDL layout that auto-runs ``binary`` (with
    optional ``args``) inside the session's first pane.

    The KDL syntax Zellij 0.44.3 expects is:

    .. code-block:: kdl

        layout {
            pane command="BINARY" {
                args "ARG1" "ARG2"
            }
        }

    The ``args`` block is omitted when no args are provided (otherwise
    ``args`` with zero values is a KDL parse error in some Zellij
    versions). All user-controlled strings are run through
    :func:`_kdl_escape` to neutralise quote/backslash injection.
    """
    binary_q = _kdl_escape(binary)
    if args:
        args_line = " ".join(f'"{_kdl_escape(a)}"' for a in args)
        return (
            "layout {\n"
            f'    pane command="{binary_q}" {{\n'
            f'        args {args_line}\n'
            "    }\n"
            "}\n"
        )
    return (
        "layout {\n"
        f'    pane command="{binary_q}"\n'
        "}\n"
    )


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

    - With binary:  ``zellij --session <name> -n <layout-file>`` where
      the layout file contains a KDL ``pane command="<binary>"`` spec
      that auto-runs the binary on session open. We write the KDL to a
      throwaway temp file (deleted in finally) because Zellij 0.44.3
      rejects ``--layout-string`` combined with ``--new-session-with-layout``
      AND rejects ``--layout`` / ``--layout-string`` combined with bare
      ``--session`` ("Session not found" — those flags only add tabs to
      EXISTING sessions). Only ``-n``/``--new-session-with-layout`` with
      a file path actually creates a new session with a pre-baked layout.
    - Without:      ``zellij --session <name>`` (opens default shell)

    The earlier ``zellij --session NAME -- <binary>`` shape from the T6
    spec was rejected by Zellij 0.44.3 ("Found argument 'claude' which
    wasn't expected") — the ``--`` separator doesn't carry binary args
    through to the spawned pane. T8 surfaced this; the layout-file
    approach is the documented Zellij path for "session that auto-runs
    X" (KDL ``pane command=`` block).

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
    # Build argv. When binary is supplied we hand Zellij a KDL layout
    # file via -n; otherwise the bare `--session NAME` form creates a
    # default-shell session (terminal mode, unchanged from T6).
    argv: list[str] = [_ZELLIJ_BIN, "--session", name]
    layout_path: Optional[str] = None
    if binary is not None:
        kdl = _build_layout_kdl(binary, args)
        # NamedTemporaryFile with delete=False so the child process can
        # open the path; we delete it ourselves in the outer finally
        # once the session has either failed fast or detached (the
        # session backend reads the layout into memory at startup, so
        # the file is no longer needed after that point).
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".kdl",
            prefix="blackbox-zellij-layout-",
            encoding="utf-8",
            delete=False,
        )
        try:
            tmp.write(kdl)
            tmp.flush()
        finally:
            tmp.close()
        layout_path = tmp.name
        argv.extend(["-n", layout_path])

    logger.info(
        "launch_session: name=%s binary=%s args=%s layout=%s",
        name,
        binary,
        args,
        layout_path,
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
    slave_closed = False
    rc: Optional[int] = None
    detached = False
    proc: Optional[subprocess.Popen] = None
    # Build the child env for the Zellij session backend (and thereby every
    # bash + CLI agent the user spawns in a pane):
    # 1. Strip orchestrator API/auth keys (denylist). They'd leak server-side
    #    credentials into user-interactive contexts AND, in claude's case,
    #    silently hang the auth handshake when the key isn't claude-code-
    #    scoped. Each CLI must fall back to its own user-side OAuth/cached
    #    session — the desired UX.
    # 2. Inject DBUS_SESSION_BUS_ADDRESS if absent. Claude reads the OS
    #    keychain (libsecret/GNOME-Keyring over DBus) during interactive
    #    startup; with no bus address set, the DBus connect hangs forever.
    #    The standard user-session bus lives at /run/user/<uid>/bus; pointing
    #    at it lets claude reach the keychain (other CLIs ignore this var).
    #    T15 surfaced empirically — symptom was "claude just sits there".
    child_env = {
        k: v for k, v in os.environ.items()
        if k not in _ENV_DENYLIST_FOR_PANES
    }
    if "DBUS_SESSION_BUS_ADDRESS" not in child_env:
        bus_path = f"/run/user/{os.getuid()}/bus"
        if os.path.exists(bus_path):
            child_env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"
    # Augment PATH with user-local + nvm node bin dirs so CLI agents whose
    # entry scripts use `#!/usr/bin/env node` (gemini, codex) can find
    # their interpreter. The orchestrator's systemd PATH excludes
    # ~/.nvm/versions/node/<ver>/bin, so the shebang fails with
    # "no such file or directory" when the script is exec'd directly by
    # zellij's pane layout. Mirrors the spawn-time augmentation the
    # tmux backend has in session_manager.
    from Orchestrator.cli_agent.path_extension import extended_path_dirs
    child_env["PATH"] = os.pathsep.join([
        *extended_path_dirs(),
        child_env.get("PATH", ""),
    ])
    try:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=subprocess.PIPE,
                start_new_session=True,
                text=True,
                close_fds=True,
                env=child_env,
            )
        except Exception:
            # Popen failed before the child got the slave fd dup'd —
            # close it explicitly. (master_fd is closed by the outer
            # finally.)
            try:
                os.close(slave_fd)
            except OSError:
                pass
            slave_closed = True
            raise

        # The Popen now owns the slave-end via dup; we can close our copy.
        try:
            os.close(slave_fd)
        except OSError:
            pass
        slave_closed = True

        # Brief wait so the session lands in `list_sessions()` before the
        # caller asks for it. Zellij registers the session with the daemon
        # well within 500ms in practice; if Popen has already exited by
        # then it usually means an error (bad binary, name collision).
        try:
            rc = proc.wait(timeout=2.0)
            # If we got here zellij exited quickly — either failure, or
            # the session is fully detached and that's normal. Check rc
            # below the outer try/finally.
        except subprocess.TimeoutExpired:
            # Healthy path: the client is still running, session is
            # registered, backend is alive. Detach: kill the
            # orchestrator-side client process (the per-session backend
            # is separate and survives).
            #
            # Wrap terminate/wait/kill so any exception (OSError from a
            # process already reaped, TimeoutExpired from a zombie) does
            # NOT leak master_fd — the outer finally guarantees close.
            detached = True
            try:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=1.0)
                        except subprocess.TimeoutExpired:
                            # Zombie / stuck — log and move on. The
                            # backend session is what matters; the
                            # client process will be reaped by init.
                            logger.warning(
                                "launch_session: client process for %s "
                                "did not exit after kill; abandoning",
                                name,
                            )
                except OSError as exc:
                    # Process already reaped between terminate and wait,
                    # or signal delivery failed — both safe to ignore.
                    logger.debug(
                        "launch_session: terminate/kill OSError "
                        "(process likely already reaped): %s",
                        exc,
                    )
            finally:
                pass  # master_fd close handled by outer finally
    finally:
        # Outer guarantee: master_fd ALWAYS closed, regardless of which
        # exception path we took (Popen raise, inner terminate failure,
        # zombie kill, normal exit).
        try:
            os.close(master_fd)
        except OSError:
            pass
        # Defense in depth: if Popen raised before we closed slave_fd
        # AND the inner handler missed it, close here too.
        if not slave_closed:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        # Always remove the throwaway layout file. The session backend
        # has already read it into memory by the time we get here
        # (either via clean detach or via failed Popen / quick exit);
        # leaving it would litter /tmp with one file per launch.
        if layout_path is not None:
            try:
                os.unlink(layout_path)
            except OSError as exc:
                logger.debug(
                    "launch_session: cleanup of layout file %s failed: %s",
                    layout_path,
                    exc,
                )

    if detached:
        return

    if rc != 0:
        stderr = (proc.stderr.read() if proc and proc.stderr else "") or ""
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
    """Kill a Zellij session by name AND free its name from the namespace.

    Uses ``delete-session --force`` rather than ``kill-session``: the latter
    terminates the process but leaves the name reserved (visible in
    list-sessions as EXITED), which blocks any re-launch under the same name.
    Fixed-name sessions (e.g. terminal provider's ``{op}__terminal``) hit
    this on every relaunch. Idempotent — deleting an already-absent session
    logs and returns cleanly.

    Note: Zellij 0.44.3 prints "No session named X found." to stdout
    (not stderr) and exits rc=1 in that case. We match either stream.
    """
    try:
        _run([_ZELLIJ_BIN, "delete-session", "--force", name])
        logger.info("zellij session deleted: name=%s", name)
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


def spawn_in_place(session_name: str, binary: str) -> None:
    """Spawn ``binary`` IN PLACE of the current pane inside ``session_name``.

    Uses ``zellij action new-pane -i --close-on-exit -- BINARY`` which
    REPLACES the currently-focused pane with a new one running the binary.
    This is the path that empirically works for claude in an iframe-attached
    client:
      - `write-chars "claude\\n"` (the old /inject path) — bash receives but
        silently doesn't execute claude. Other binaries work; claude doesn't.
      - `new-tab -- claude` — claude spawns BUT only in the backend's client
        focus, not the iframe-attached client. Brandon sees a blank pane.
      - `new-pane -i -- claude` — replaces the focused pane in the SESSION
        state (not per-client), so every attached client sees the change.
        When claude exits (Ctrl+C → twice), the bash pane comes back.

    SECURITY: caller MUST validate binary is in an allowlist BEFORE
    invoking — the binary string is passed directly via `--`. The Zellij
    CLI tokenizes correctly but a malicious binary path would still pwn
    anything claude/agy/etc. can pwn. Caller is the perimeter.
    """
    argv = [
        _ZELLIJ_BIN,
        "--session",
        session_name,
        "action",
        "new-pane",
        "-i",
        "--close-on-exit",
        "--",
        binary,
    ]
    _run(argv)
    logger.info("zellij in-place pane spawned: session=%s binary=%s", session_name, binary)


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

    IDENTITY CHECK: an HTTP 200 alone is not sufficient — a different
    service (operator misconfig, port collision) could return 200 too.
    We read the first ~2KB of the response body and require it to
    contain Zellij's xterm.js signature ("Zellij Web Client" in the
    <title>, with "xterm" as a fallback signature). Returns False with
    a LOUD warning if 200 but body doesn't look like Zellij.
    """
    port = _read_port_from_config()
    url = f"http://127.0.0.1:{port}/"
    attempts = max(1, int(retries))

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if 200 <= resp.status < 300:
                    # Identity check: read first 2KB, look for Zellij
                    # signature in the served HTML.
                    try:
                        body_bytes = resp.read(2048)
                    except (OSError, TimeoutError) as exc:
                        logger.warning(
                            "web_server_healthy: %s returned 200 but body "
                            "read failed (%s); treating as unhealthy",
                            url,
                            exc,
                        )
                        if attempt < attempts:
                            time.sleep(backoff_seconds)
                        continue
                    body = body_bytes.decode("utf-8", errors="replace")
                    if (
                        "Zellij Web Client" in body
                        or "xterm" in body.lower()
                    ):
                        if attempt > 1:
                            logger.info(
                                "web_server_healthy: ok at %s on attempt %d",
                                url,
                                attempt,
                            )
                        return True
                    logger.error(
                        "web_server_healthy: port %d returned 200 but does "
                        "NOT look like Zellij — another service may have "
                        "claimed the port. body[:200]=%r",
                        port,
                        body[:200],
                    )
                    return False
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

    # Backup existing config (if any and content actually differs) so
    # operator hand-edits aren't silently lost. Use a unix-timestamp
    # suffix so multiple regenerations don't overwrite each other.
    if file_present:
        try:
            current_content = _CONFIG_PATH.read_text(encoding="utf-8")
        except OSError:
            current_content = None
        if current_content is not None and current_content != body:
            backup_path = _CONFIG_PATH.with_suffix(
                _CONFIG_PATH.suffix + f".bak.{int(time.time())}"
            )
            try:
                shutil.copy2(_CONFIG_PATH, backup_path)
                logger.info(
                    "ensure_config: backed up prior %s to %s "
                    "(operator hand-edits preserved here)",
                    _CONFIG_PATH,
                    backup_path,
                )
            except OSError as exc:
                logger.warning(
                    "ensure_config: backup of %s -> %s failed: %s "
                    "(continuing with regeneration)",
                    _CONFIG_PATH,
                    backup_path,
                    exc,
                )

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
