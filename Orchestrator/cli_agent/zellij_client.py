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
# OR (for sessions whose backend has exited but is resurrectable):
#   Brandon__terminal__1780682262 [Created 17days 6h ago] (EXITED - attach to resurrect)
#
# The trailing ``(EXITED - attach to resurrect)`` suffix MUST be tolerated:
# the original anchored-at-`]` regex matched ZERO rows on any box that had
# accumulated exited sessions (verified live 2026-06-22 — `list_sessions()`
# returned [] despite 18 sessions being present). We capture the optional
# state suffix so resume / reconcile / reaper can distinguish live-vs-exited.
_SESSION_LIST_LINE_RE = re.compile(
    r'^(?P<name>\S+)\s+\[Created\s+(?P<created_at>[^\]]+)\]'
    r'(?:\s+\((?P<state>[^)]*)\))?\s*$'
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
    # Scrub ZELLIJ* vars unconditionally: the orchestrator can itself run
    # inside a zellij pane, and an inherited ZELLIJ_SESSION_NAME makes a
    # bare action target the USER'S live session (probe 2026-07-20).
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZELLIJ")}
    try:
        result = subprocess.run(
            args,
            check=check,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
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


_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9._\- ]+$")


def send_key(session_name: str, byte_codes: list[int]) -> None:
    """Inject raw key bytes into a session's focused pane via
    ``zellij action write`` — the canonical programmatic key injection.

    WHY THIS EXISTS (Esc-key fix, 2026-07-11): zellij-web's terminal-WS input
    parser holds a BARE trailing ESC waiting for a possible escape-sequence
    continuation (there is no ESC timeout on the web path), so a client that
    sends Esc as its own 1-byte frame — the Android terminal's Esc button —
    never resolves to the Esc key. Complete sequences (arrows) and printable
    chars are unaffected, which is exactly the observed symptom. ``action
    write`` bypasses that parser and delivers the byte(s) to the focused pane
    directly; live-validated by injecting 27 into a real stuck claude session.

    NOTE: requires at least one attached client on the session (a detached
    session silently drops actions) — always true for the app's use (the
    button is pressed FROM an attached terminal).
    """
    if not session_name or not _SESSION_NAME_RE.match(session_name):
        raise ValueError(f"invalid zellij session name: {session_name!r}")
    if not byte_codes or len(byte_codes) > 32:
        raise ValueError("byte_codes must contain 1-32 bytes")
    codes = []
    for c in byte_codes:
        if not isinstance(c, int) or not (0 <= c <= 255):
            raise ValueError(f"byte out of range: {c!r}")
        codes.append(str(c))
    _run([_ZELLIJ_BIN, "--session", session_name, "action", "write", *codes])


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


# --- master token (Phase 5, master-token auth model) ---------------------
#
# Architectural override of the audit-I7 "mint-per-launch, revoke-on-load"
# policy for the orchestrator↔zellij-web boundary. Customer-facing auth
# still flows through Tailscale + the orchestrator's operator identity
# system; the audit-I8 operator-prefix gate still runs at the route layer
# (so multi-operator isolation is preserved). What changes: instead of
# each session getting its own short-lived token that clients must hold,
# the orchestrator mints ONE long-lived master token at startup, persists
# its value to a root-only file on disk, and injects it as the cookie on
# every upstream forward by the app-proxy (see Orchestrator/routes/
# agent_routes.py). Clients (browser, Android) never see tokens — they
# just open WSes; the orchestrator authenticates upstream on their behalf.
#
# Why the audit-I7 deviation: T23 device QA surfaced that the per-session
# model breaks session reattach across app restarts, and our deployment
# model (Tailscale-fronted, single-tenant box) doesn't need defense-in-
# depth at this boundary. The master token is intentionally persistent
# (the audit-I7 ban on persisting raw tokens was specifically about
# per-session ephemeral tokens, not a system-wide auth handle). Brandon's
# decision documented in plan AC2 + Phase 4 RESULTS section.
#
# Token VALUE storage: ~/.local/share/blackbox/zellij-master.token mode 0600.
# Token NAME ("master-blackbox") is also stored in zellij's tokens.db (sqlite)
# as part of `zellij web --create-token`'s normal flow. If either drops
# out, ensure_master_zellij_token() re-mints on next startup.

_MASTER_TOKEN_NAME = "master-blackbox"
_MASTER_TOKEN_FILE = Path.home() / ".local" / "share" / "blackbox" / "zellij-master.token"
_master_token: Optional[str] = None  # the AUTH token (input to /command/login)
_master_session_cookie: Optional[str] = None  # the session_token cookie (what app-proxy injects)


def ensure_master_zellij_token() -> str:
    """Mint or load the master zellij token. Idempotent.

    Called at orchestrator startup (see Orchestrator.cli_agent.__init__
    startup_initialize). Loads the persisted value from disk if present;
    if absent, mints a fresh one via the existing :func:`mint_token`
    flow and persists it. Sets the module-level :data:`_master_token`
    cache and returns the value.

    If anything goes wrong (file write fails, mint fails, etc.) we
    re-raise — the caller (startup hook) should catch and log, since
    the orchestrator can degrade to "Zellij auth unavailable" without
    crashing.
    """
    global _master_token

    # Fast path: already loaded in this process.
    if _master_token is not None:
        return _master_token

    # Try to load from disk first — survives orchestrator restarts.
    if _MASTER_TOKEN_FILE.exists():
        try:
            value = _MASTER_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if value:
                _master_token = value
                logger.info(
                    "ensure_master_zellij_token: loaded existing master token from %s",
                    _MASTER_TOKEN_FILE,
                )
                return value
            logger.warning(
                "ensure_master_zellij_token: %s exists but is empty — re-minting",
                _MASTER_TOKEN_FILE,
            )
        except OSError as exc:
            logger.warning(
                "ensure_master_zellij_token: failed to read %s (%s) — re-minting",
                _MASTER_TOKEN_FILE,
                exc,
            )

    # No persisted value (or unreadable): mint fresh + persist.
    # We use the auto-assigned token_N name and STORE the value to disk
    # (intentional deviation from audit-I7 — see module-header kdoc).
    _name, value = mint_token()

    _MASTER_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file then atomic-rename so a crash mid-write
    # doesn't leave a half-written token file.
    tmp_path = _MASTER_TOKEN_FILE.with_suffix(".token.tmp")
    tmp_path.write_text(value, encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(_MASTER_TOKEN_FILE)

    _master_token = value
    logger.info(
        "ensure_master_zellij_token: minted + persisted new master token to %s",
        _MASTER_TOKEN_FILE,
    )
    return value


def get_master_token() -> str:
    """Return the cached master AUTH token (input to /command/login).

    NOT the value the app-proxy should inject as a cookie — that's
    [get_master_session_cookie]. This function exists for callers
    that need to authenticate AS the master account, like the
    startup hook that exchanges the auth token for a session cookie.
    """
    if _master_token is None:
        raise RuntimeError(
            "Master zellij token not initialized — "
            "ensure_master_zellij_token() must be called at startup"
        )
    return _master_token


def _exchange_master_token_for_session_cookie() -> str:
    """POST /command/login with the master auth token; return the
    `session_token` cookie value.

    This is the second step zellij-web's browser JS does in
    `auth.js:initAuthentication()` — the auth_token alone doesn't
    grant session access; the SERVER mints a session_token cookie
    when /command/login succeeds, and that cookie is what
    subsequent requests (including WS upgrades) must carry.

    The session_token cookie value is what
    [get_master_session_cookie] returns and what the app-proxy
    injects on every upstream forward.

    Raises on failure (caller — the startup hook — should catch
    + log + continue in degraded mode).
    """
    if _master_token is None:
        raise RuntimeError(
            "Cannot exchange — master token not minted yet"
        )
    web_port = _read_port_from_config()
    url = f"http://127.0.0.1:{web_port}/command/login"
    body = (
        '{"auth_token": "' + _master_token + '", "remember_me": false}'
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        if resp.status != 200:
            raise RuntimeError(
                f"/command/login returned HTTP {resp.status}"
            )
        # Parse Set-Cookie. zellij sends:
        #   Set-Cookie: session_token=<uuid>; HttpOnly; SameSite=Strict; Path=/
        # We want just the value.
        set_cookie = resp.headers.get("Set-Cookie", "")
        match = re.search(r"session_token=([^;]+)", set_cookie)
        if not match:
            raise RuntimeError(
                f"/command/login response missing session_token cookie: {set_cookie!r}"
            )
        return match.group(1)


def ensure_master_session_cookie() -> str:
    """Mint (if needed) the session_token cookie value that the
    app-proxy injects on every upstream forward.

    Cached in module-level state. Refreshes on call only if the
    cache is empty (first call) — callers that hit 401 on upstream
    should call :func:`refresh_master_session_cookie` to force a
    fresh exchange.

    Self-healing: if the exchange returns HTTP 401 the master auth
    token on disk is stale (the token was revoked, e.g. manual
    revocation or a zellij reinstall). NOTE: reconcile_or_wipe no
    longer wipes tokens.db, so a restart alone won't trigger this.
    We re-mint a fresh master token + re-exchange once.
    """
    global _master_session_cookie, _master_token
    if _master_session_cookie is not None:
        return _master_session_cookie
    try:
        _master_session_cookie = _exchange_master_token_for_session_cookie()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            logger.warning(
                "ensure_master_session_cookie: /command/login returned 401 — "
                "master auth token is stale; re-minting fresh and retrying"
            )
            # Force the stored token to be discarded + re-mint.
            try:
                _MASTER_TOKEN_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            _master_token = None
            ensure_master_zellij_token()  # mints + persists fresh
            _master_session_cookie = _exchange_master_token_for_session_cookie()
        else:
            raise
    logger.info(
        "ensure_master_session_cookie: minted fresh session_token cookie via /command/login"
    )
    return _master_session_cookie


def refresh_master_session_cookie() -> str:
    """Force-refresh the session_token cookie (e.g. after upstream 401).

    Re-exchanges the master auth token for a new session cookie.
    Self-heals: if the master auth_token itself is stale (the token was
    revoked or zellij was reinstalled; reconcile_or_wipe no longer
    wipes tokens.db), the /command/login
    will return 401; we delete the on-disk auth_token file, re-mint
    fresh, and try the exchange again. Same pattern as
    [ensure_master_session_cookie]; copied here so the proxy's
    retry path is fully self-healing without needing two exception
    handlers at the call site.
    """
    global _master_session_cookie, _master_token
    try:
        _master_session_cookie = _exchange_master_token_for_session_cookie()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            logger.warning(
                "refresh_master_session_cookie: /command/login returned 401 — "
                "master auth token is stale; re-minting fresh and retrying"
            )
            try:
                _MASTER_TOKEN_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            _master_token = None
            ensure_master_zellij_token()
            _master_session_cookie = _exchange_master_token_for_session_cookie()
        else:
            raise
    logger.info("refresh_master_session_cookie: re-exchanged session cookie")
    return _master_session_cookie


def get_master_session_cookie() -> str:
    """Return the cached session_token cookie value the app-proxy
    injects on every upstream forward.

    Raises if not initialized — callers (proxy handlers) should
    let the exception propagate so the failure is visible.
    """
    if _master_session_cookie is None:
        raise RuntimeError(
            "Master session cookie not initialized — "
            "ensure_master_session_cookie() must be called at startup"
        )
    return _master_session_cookie


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
    ``[{"name": str, "created_at": str, "exited": bool}, ...]``.

    Runs ``zellij list-sessions --no-formatting``. This is a GLOBAL list
    — the orchestrator filters by operator-name-prefix in the route
    layer (audit I8); the adapter does not.

    ``exited`` is True when zellij reports the session backend has exited
    but is resurrectable (``(EXITED - attach to resurrect)`` suffix). Such
    a session is STILL a valid resume target: attaching (which the
    zellij-web client does on connect) resurrects it. Resume / reconcile /
    reaper all treat exited-but-present rows as "the session exists."
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
        state = (match.group("state") or "")
        rows.append({
            "name": match.group("name"),
            "created_at": match.group("created_at").strip(),
            "exited": "exited" in state.lower(),
        })
    return rows


def session_exists(name: str) -> bool:
    """Return True iff a zellij session named ``name`` currently exists —
    whether RUNNING or EXITED-but-resurrectable.

    The orchestrator's resume path uses this to decide attach-if-exists vs
    create: a name present in ``list-sessions`` (even in the EXITED state)
    must NOT be re-launched (``zellij --session NAME`` errors rc=1 on a
    name collision — verified live 2026-06-22). Attaching resurrects an
    exited session, so an exited row is a legitimate resume target.

    Returns False on any zellij CLI failure (caller treats "can't tell" as
    "doesn't exist" -> falls through to a normal launch, which is the safe
    default).
    """
    try:
        return any(s.get("name") == name for s in list_sessions())
    except Exception as exc:  # noqa: BLE001 -- defensive; daemon may be down
        logger.warning(
            "session_exists(%s): list_sessions failed (%s) -- treating as absent",
            name,
            exc,
        )
        return False


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
