import asyncio
import datetime as _dt
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from Orchestrator.cli_agent import get_backend
from Orchestrator.cli_agent import zellij_client, zellij_state
from Orchestrator.cli_agent.operator_config import OperatorConfig
from Orchestrator.cli_agent.path_validator import PathValidator, WorkspaceViolation
from Orchestrator.cli_agent.session_manager import (
    TmuxSessionManager, session_name,
)
from Orchestrator.cli_agent.pty_bridge import PtyBridge


logger = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APPS_ROOT = Path(os.getenv("CLI_AGENT_APPS_ROOT",
                           str(PROJECT_ROOT / "Apps")))
CLAUDE_CFG_ROOT = Path(os.getenv("CLI_AGENT_CONFIG_ROOT",
                                  str(Path.home() / ".claude-bbox")))
_ALLOWED = os.getenv("CLI_AGENT_OPERATORS", "").strip()
ALLOWED_OPS: Optional[set] = set(filter(None, _ALLOWED.split(","))) if _ALLOWED else None


_PROVIDER_BINARY_NAMES = {
    # Most providers' CLI binary name matches the provider id, but
    # Antigravity ships as `agy` (not `antigravity`) per its install
    # script (Track 1 of 2026-05-22-antigravity-cli-integration plan).
    "antigravity": "agy",
    # grok (xAI CLI) installs as `grok` under ~/.local/bin (covered by
    # path_extension.extended_path_dirs()).
    "grok": "grok",
}


def _resolve_provider_bin(name: str) -> str:
    """Resolve a CLI agent binary to an absolute path.

    systemd's restricted PATH excludes user-local install dirs like
    ~/.local/bin/, and per-version nvm dirs like
    ~/.nvm/versions/node/<ver>/bin. We search an extended PATH that
    includes both. Falls back to the bare name (which will fail loudly
    via tmux if the binary truly cannot be found).

    For providers whose binary filename differs from the provider id
    (currently just antigravity → agy), the lookup uses the binary
    name; the dual fallback for Antigravity also probes
    ~/.local/bin/agy explicitly in case shutil.which misses it (e.g.,
    the install ran but the dir isn't on the systemd PATH and the
    extended-path lookup somehow falls short).

    See Orchestrator.cli_agent.path_extension for the shared dir list.
    """
    from Orchestrator.cli_agent.path_extension import extended_path_dirs
    bin_name = _PROVIDER_BINARY_NAMES.get(name, name)
    extended_path = os.pathsep.join([
        os.environ.get("PATH", ""),
        *extended_path_dirs(),
    ])
    found = shutil.which(bin_name, path=extended_path)
    if found:
        return found
    # Antigravity install.sh installs to ~/.local/bin/agy. Explicit
    # fallback (D5b) so the resolver works even if the extended path
    # somehow misses ~/.local/bin (e.g., user moved their install).
    if name == "antigravity":
        fallback = os.path.expanduser("~/.local/bin/agy")
        if os.path.exists(fallback) and os.access(fallback, os.X_OK):
            return fallback
    return bin_name


SUPPORTED_PROVIDERS: tuple[str, ...] = ("claude", "gemini", "codex", "antigravity", "grok")


def provider_bin(name: str) -> str | None:
    """Resolve a CLI agent binary path on each call (T0 / audit I5).

    Previously cached as a module-level PROVIDER_BIN dict populated at
    import. The update pipeline can change the active Node version mid-
    session (npm install of new CLI versions under nvm), which made
    cached paths go stale until the service restarted. shutil.which is
    sub-millisecond; safe to call on every WebSocket connect.

    Returns the absolute path, or None for unknown providers.
    """
    if name not in SUPPORTED_PROVIDERS:
        return None
    env_override = os.getenv(f"CLI_AGENT_{name.upper()}_BIN")
    return env_override or _resolve_provider_bin(name)

# Per-provider extra args appended to the spawn command.
#
# Codex's --no-alt-screen flag puts it in inline mode, which routes
# its output through the terminal's normal scrollback buffer instead
# of the alt-screen. Without this flag, PgUp/PgDn don't scroll
# Codex's conversation history (Codex doesn't bind those keys to
# scroll, AND Codex explicitly disables mouse mode so wheel events
# don't reach it either). With the flag, the Termux TerminalView's
# built-in scrollback handles scroll natively. Same trick that
# Claude's POST_ATTACH_HOOKS["claude"] -> /tui fullscreen achieves
# via slash command at runtime.
#
# Gemini deliberately absent — Gemini has no equivalent flag.
# Scroll for Gemini requires Path 3 (Compose-side scrollback proxy)
# or accept the limitation.
PROVIDER_ARGS: dict[str, list[str]] = {
    "codex": ["--no-alt-screen"],
}


def _manager() -> TmuxSessionManager:
    pv = PathValidator(apps_root=APPS_ROOT)
    oc = OperatorConfig(root=CLAUDE_CFG_ROOT)
    return TmuxSessionManager(path_validator=pv, operator_config=oc)


def _clamp_dim(n: int, default: int) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return default
    return max(1, min(1000, v))


router = APIRouter(prefix="/cli-agent", tags=["cli-agent"])


@router.get("/sessions")
def list_sessions(op: str = Query(...)):
    if ALLOWED_OPS is not None and op not in ALLOWED_OPS:
        raise HTTPException(403, f"Operator {op} not allowed")
    sessions = _manager().list_for_operator(op)
    return {"sessions": [
        {"session_id": s.name, "cwd": str(s.cwd)} for s in sessions
    ]}


@router.delete("/sessions/{session_id}")
def kill_session(session_id: str):
    if not session_id.startswith("cli-agent-"):
        raise HTTPException(400, "Invalid session id")
    mgr = _manager()
    if not mgr.has_session(session_id):
        return {"killed": False, "reason": "not-found"}
    mgr.kill(session_id)
    return {"killed": True}


@router.websocket("/ws/{session_id}")
async def ws_cli_agent(
    websocket: WebSocket,
    session_id: str,
    op: str,
    provider: str,
    app: str,
    cols: int = 80,
    rows: int = 24,
):
    if ALLOWED_OPS is not None and op not in ALLOWED_OPS:
        await websocket.close(code=4003)
        return

    expected = session_name(op, provider, app)
    if session_id != expected:
        await websocket.close(code=4003)
        return

    if provider not in SUPPORTED_PROVIDERS:
        await websocket.close(code=4003)
        return

    cols = _clamp_dim(cols, 80)
    rows = _clamp_dim(rows, 24)

    mgr = _manager()
    try:
        # Wrap blocking tmux subprocess calls so they don't stall the
        # event loop during the WebSocket handshake.
        info = await asyncio.to_thread(
            mgr.attach_or_create,
            operator=op, provider=provider, app=app,
            command=[provider_bin(provider), *PROVIDER_ARGS.get(provider, [])],
        )
    except WorkspaceViolation:
        await websocket.close(code=4003)
        return

    await websocket.accept()
    await websocket.send_text(json.dumps({
        "type": "session_info",
        "state": "created" if info.created else "attaching",
    }))

    # TERM must be set on the *attaching* PTY too, otherwise tmux logs
    # "open terminal failed: terminal does not support clear" and exits.
    env = {
        **os.environ,
        **OperatorConfig(root=CLAUDE_CFG_ROOT).env_for(op),
        "TERM": os.environ.get("TERM") or "xterm-256color",
    }
    bridge = PtyBridge.spawn(
        ["tmux", "attach", "-t", session_id],
        env=env, cols=cols, rows=rows,
    )

    # OAuth URL scraper: extract auth URLs from PTY output BEFORE xterm.js
    # renders them with line wraps. Solves the user-visible bug where
    # Brandon's manual copy of Antigravity's printed auth URL returned 404
    # (long URL + 80-col terminal = wrap-induced copy corruption). When a
    # URL is detected, we push a sidechannel {type:"auth_url_detected"}
    # text message so the client can surface a clickable "Open OAuth"
    # banner instead of relying on copy-paste from the rendered terminal.
    # Per-session dedup so the same URL doesn't spam the banner if agy
    # reprints it.
    # OAuth URL extraction via tmux capture-pane:
    # agy prints long OAuth URLs that line-wrap at terminal width (80 cols),
    # so a 470-char Google OAuth URL ends up split across 7 visual lines.
    # Naive regex on the raw PTY byte stream captures only one line at a
    # time (the wrap inserts \r\n between URL chars). Brandon hit this:
    # captured URL was 91 chars (cut at `client_id=1071006060591-tmhss`),
    # missing `response_type` + the rest of the params, which Google rejected.
    #
    # The robust fix is `tmux capture-pane -J` which JOINS wrapped lines back
    # into logical (unwrapped) text. We trigger capture-pane only when a URL
    # prefix is detected in the recent PTY stream (cheap heuristic), so we
    # don't shell out on every read for sessions that never print URLs.
    _auth_url_pattern = re.compile(
        r"https?://[A-Za-z0-9.\-]+\.(?:google|googleusercontent|googleapis|antigravity\.google)[A-Za-z0-9.\-]*/[A-Za-z0-9._\-~:/?#\[\]@!$&()*+,;=%]+"
    )
    # Cheap prefix detector — runs on every read. Triggers capture-pane only
    # when this matches. Catches all relevant Google OAuth + Antigravity URLs.
    _auth_url_trigger = re.compile(
        rb"https?://(?:accounts\.google|oauth2\.googleapis|antigravity\.google)"
    )
    _announced_auth_urls: set[str] = set()
    _scan_buffer = bytearray()

    async def pty_to_ws():
        # Bail out promptly when the WebSocket disconnects, even if the
        # PTY is idle (no bytes flowing). Without this check, the loop
        # polls forever and the route handler hangs forever, exhausting
        # the asyncio thread pool after enough disconnects.
        from starlette.websockets import WebSocketState
        while bridge.isalive():
            if websocket.client_state != WebSocketState.CONNECTED:
                return
            data = await bridge.read(timeout=0.1)
            if data:
                try:
                    await websocket.send_bytes(data)
                except (WebSocketDisconnect, RuntimeError):
                    return
                # Cheap trigger: only run capture-pane when we see an OAuth URL
                # prefix in recent PTY output. Keeps overhead near-zero for
                # sessions that don't print URLs.
                _scan_buffer.extend(data)
                if len(_scan_buffer) > 4096:
                    del _scan_buffer[:-4096]
                if not _auth_url_trigger.search(_scan_buffer):
                    continue
                # Capture the rendered tmux pane with -J (join wrapped lines)
                # so URLs reconstitute from their visual line-wraps. Runs in
                # a thread so we don't block the PTY read loop.
                try:
                    cap = await asyncio.wait_for(
                        asyncio.to_thread(
                            subprocess.run,
                            ["tmux", "capture-pane", "-p", "-J", "-t", session_id],
                            capture_output=True, text=True, timeout=2,
                        ),
                        timeout=3,
                    )
                except (asyncio.TimeoutError, subprocess.TimeoutExpired):
                    continue
                if cap.returncode != 0:
                    continue
                for m in _auth_url_pattern.finditer(cap.stdout):
                    url = m.group(0).rstrip(".,;:!?)\"'")
                    if not url or url in _announced_auth_urls:
                        continue
                    _announced_auth_urls.add(url)
                    print(f"[CLI-AGENT] auth URL detected ({len(url)} chars): {url[:120]}...")
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "auth_url_detected",
                            "url": url,
                        }))
                    except (WebSocketDisconnect, RuntimeError):
                        return

    async def ws_to_pty():
        while True:
            try:
                msg = await websocket.receive()
            except (WebSocketDisconnect, RuntimeError):
                return
            if msg.get("type") == "websocket.disconnect":
                return
            try:
                if "bytes" in msg and msg["bytes"] is not None:
                    await bridge.write(msg["bytes"])
                elif "text" in msg and msg["text"]:
                    try:
                        ctrl = json.loads(msg["text"])
                    except Exception:
                        continue
                    t = ctrl.get("type")
                    if t == "resize":
                        bridge.resize(
                            cols=_clamp_dim(ctrl.get("cols"), 80),
                            rows=_clamp_dim(ctrl.get("rows"), 24),
                        )
                    elif t == "paste":
                        text_bytes = ctrl.get("text", "").encode("utf-8")
                        # Reject paste containing the bracketed-paste close marker —
                        # would break out of paste mode and inject keystrokes.
                        if b"\x1b[201~" in text_bytes:
                            await websocket.send_text(json.dumps({
                                "type": "error",
                                "code": "paste-rejected",
                                "message": "Paste contains a bracketed-paste close sequence",
                            }))
                            continue
                        framed = b"\x1b[200~" + text_bytes + b"\x1b[201~"
                        await bridge.write(framed)
                    elif t == "kill":
                        await asyncio.to_thread(mgr.kill, session_id)
                        return
            except (WebSocketDisconnect, RuntimeError):
                return

    try:
        await asyncio.gather(pty_to_ws(), ws_to_pty(), return_exceptions=True)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        # Schedule bridge close in a thread so it can't block the event loop
        # if the underlying ptyprocess teardown is slow.
        try:
            await asyncio.to_thread(bridge.close)
        except Exception:
            pass


# === Zellij backend (Phase 2 T8) =========================================
#
# New Zellij-backed endpoints, coexisting with the tmux-backed endpoints
# above. Selection is governed by ``CLI_AGENT_BACKEND`` via
# :func:`Orchestrator.cli_agent.get_backend`; each endpoint here returns
# HTTP 503 when the effective backend is not "zellij" so callers fail
# fast instead of silently invoking the wrong backend.
#
# Operator identity (audit I8): we reuse the existing route convention
# (``?op=<name>`` query param, gated by the ``CLI_AGENT_OPERATORS``
# allowlist via :data:`ALLOWED_OPS`). The plan's "informational only"
# note refers to the future cookie/session world; in the current
# orchestrator the env-allowlisted ``op`` IS the trusted identity.
# Session names embed ``op`` so the prefix gate on DELETE remains
# meaningful.
#
# Tokens (audit I7): :func:`zellij_client.mint_token` returns
# ``(name, value)``. Only ``name`` is persisted to state; ``value`` is
# returned once in the launch response and then forgotten by the
# orchestrator.

_ZELLIJ_WEB_PORT = 9097  # locked per zellij_client._DEFAULT_ZELLIJ_WEB_PORT

# Provider id (Portal-facing) -> binary name for `zellij --session NAME -- BINARY`.
# "terminal" is special: no binary, default shell.
_ZELLIJ_PROVIDER_BINARIES: dict[str, Optional[str]] = {
    "claude": "claude",
    "gemini": "gemini",
    "codex": "codex",
    "agy": "agy",
    "antigravity": "agy",
    "grok": "grok",
    "terminal": None,
}

# Phase 5 master-token model (2026-05-26) removed the per-session-token
# TTL machinery (_ZELLIJ_TOKEN_TTL_SECONDS, _schedule_token_revoke). The
# orchestrator now holds a single long-lived master token and injects it
# at the proxy boundary; per-session tokens no longer exist. See
# Orchestrator/cli_agent/zellij_client.py ensure_master_zellij_token().


def _require_zellij_backend() -> None:
    """Raise HTTP 503 if ``get_backend() != "zellij"``.

    Called at the top of every Zellij endpoint so misrouted requests
    fail loudly rather than fall through to undefined behavior.
    """
    backend = get_backend()
    if backend != "zellij":
        raise HTTPException(
            status_code=503,
            detail=f"Zellij backend not active (effective backend={backend!r})",
        )


def _check_operator_allowed(op: str) -> None:
    """Apply the existing CLI_AGENT_OPERATORS allowlist gate."""
    if ALLOWED_OPS is not None and op not in ALLOWED_OPS:
        raise HTTPException(403, f"Operator {op} not allowed")


def _zellij_resume_name(
    operator: str,
    provider: str,
    app: Optional[str],
) -> str:
    """DETERMINISTIC session name = the resume identity for an
    (operator, provider, app) triple. NO timestamp.

    Shape:
      - CLI providers: ``{operator}__{provider}__{app_or_root}``
      - Terminal mode: ``{operator}__terminal__{app_or_root}``
        (``app_or_root`` = "root" when no app context).

    "Open the terminal for app X" always maps to this same name, so a
    relaunch ATTACHES the existing session instead of minting a new one
    (Phase 2 resume — the deferred v1.1 piece). The "+ New" fork path
    uses :func:`_zellij_fork_name` to append a uniqueness suffix.

    Pure function so it can be unit-tested without FastAPI.
    """
    app_part = app if app else "root"
    return f"{operator}__{provider}__{app_part}"


def _zellij_fork_name(
    operator: str,
    provider: str,
    app: Optional[str],
) -> str:
    """UNIQUE session name for an explicit "+ New" fork.

    ``{operator}__{provider}__{app_or_root}__{unix_ts}`` — the timestamp
    suffix guarantees a distinct session even when the deterministic
    resume session for the same triple is already running. This is the
    "fork a second concurrent terminal for app X" path.

    Pure function so it can be unit-tested without FastAPI.
    """
    app_part = app if app else "root"
    return f"{operator}__{provider}__{app_part}__{int(time.time() * 1000)}"


def _generate_zellij_session_name(
    operator: str,
    provider: str,
    app: Optional[str],
    fork: bool = False,
) -> str:
    """Compute the Zellij session name for an (operator, provider, app).

    ``fork=False`` (default) -> deterministic resume name (no timestamp);
    ``fork=True`` -> unique forked name (timestamp suffix). See
    :func:`_zellij_resume_name` / :func:`_zellij_fork_name`.

    Kept for backward-compat with existing callers/tests; new code can
    call the two specific helpers directly.
    """
    if fork:
        return _zellij_fork_name(operator, provider, app)
    return _zellij_resume_name(operator, provider, app)


def _validate_operator_prefix(session_name_: str, operator: str) -> bool:
    """Return True iff ``session_name_`` starts with ``{operator}__``.

    Pure function so T9 can unit-test cross-operator delete attempts
    without HTTP plumbing. Audit I8 gate.
    """
    return session_name_.startswith(f"{operator}__")


def _zellij_session_url(session_name_: str, token_value: Optional[str] = None) -> str:
    """Compose the session URL the Portal iframe will load.

    Same-origin via existing ``/app-proxy/{port}/{path}`` route — keeps
    TLS termination, auth, and CORS at the orchestrator edge instead of
    leaking ``http://localhost:9097`` to the customer's browser.

    Session name goes in the URL PATH, not a query param: Zellij's
    web client reads it via ``location.pathname.split('/').pop()``
    (assets/index.js). When the last path segment is empty, zellij-web
    auto-creates a brand-new session and our pre-minted session is
    orphaned in /tmp. T11c surfaced this empirically.

    Phase 5 master-token model (2026-05-26): ``token_value`` is now
    always ``None`` (kept as a parameter for backward-compat with any
    older callers). The orchestrator's app-proxy injects the master
    token cookie on upstream forward, so the session URL has no
    ``?token=`` query param. If a non-None value is passed it's
    silently ignored — clients should NEVER hold session tokens.
    """
    return f"/app-proxy/{_ZELLIJ_WEB_PORT}/{session_name_}"


# _schedule_token_revoke() removed in Phase 5 master-token model
# (2026-05-26). Per-session tokens no longer exist; nothing to revoke.


@router.post("/zellij/launch", status_code=201)
async def zellij_launch(
    body: dict = Body(...),
    op: str = Query(...),
):
    """Launch OR resume a Zellij session and return the iframe URL.

    Body::

        {"provider": "claude"|"gemini"|"codex"|"agy"|"grok"|"terminal",
         "app": "<app-name>" | null,
         "fork": false}   # optional; default false

    Resume model (Phase 2):

    - ``fork`` omitted/false -> ATTACH-IF-EXISTS on the DETERMINISTIC name
      ``{op}__{provider}__{app_or_root}`` (no timestamp). If a zellij
      session with that name already exists (running OR exited-and-
      resurrectable) we DO NOT relaunch (that would error rc=1); we
      upsert the state row and return the same connection info. The
      zellij-web client resurrects an exited session on attach.
    - ``fork`` true -> "+ New": mint a UNIQUE timestamped name so a second
      concurrent session for the same triple can coexist with the resume
      session. A fork always creates.

    Either way the response is identical in shape (the client reattaches
    by name via the app-proxy; the master-token cookie is injected
    upstream by the orchestrator — clients never hold tokens).
    """
    _check_operator_allowed(op)
    _require_zellij_backend()

    provider = body.get("provider")
    if provider not in _ZELLIJ_PROVIDER_BINARIES:
        raise HTTPException(
            400,
            f"Unknown provider {provider!r}; expected one of "
            f"{sorted(_ZELLIJ_PROVIDER_BINARIES)}",
        )
    app = body.get("app")
    if app is not None and not isinstance(app, str):
        raise HTTPException(400, "app must be a string or null")
    fork = bool(body.get("fork", False))

    bare_binary = _ZELLIJ_PROVIDER_BINARIES[provider]
    # Resolve to absolute path: orchestrator service's PATH doesn't include
    # user-local bin dirs (~/.local/bin, ~/.nvm/*/bin). Without absolute path,
    # the KDL layout's `pane command=BINARY` triggers Zellij's "Command not
    # found" error inside the pane (T15-final empirical finding). terminal
    # provider passes None → no KDL layout, no binary lookup needed.
    #
    # Pass `provider` (not `bare_binary`) to provider_bin: provider_bin's
    # SUPPORTED_PROVIDERS gate is keyed on the public provider id (e.g.,
    # "antigravity"), not on the binary name ("agy"), and its
    # _PROVIDER_BINARY_NAMES map performs the id→binary lookup internally.
    # Passing the binary name skipped that gate and returned None for
    # antigravity, breaking the agy shortcut.
    binary = provider_bin(provider) if bare_binary else None
    if bare_binary and not binary:
        raise HTTPException(
            500,
            f"Provider {provider!r} binary {bare_binary!r} not found in any known location",
        )
    if fork:
        session_name_ = _zellij_fork_name(op, provider, app)
    else:
        session_name_ = _zellij_resume_name(op, provider, app)

    # Phase 5 master-token model (2026-05-26): no per-session token mint.
    # The orchestrator's app-proxy (Orchestrator/routes/agent_routes.py)
    # injects the master token cookie on every upstream forward, so
    # clients never need to know about zellij tokens. We still record a
    # state row for operator-prefix gate enforcement (audit I8) and for
    # listZellijSessions consumers, but the token_name field becomes
    # a constant marker. See ensure_master_zellij_token() kdoc for the
    # architectural rationale + the audit-I7 deviation it represents.
    token_name = "master"
    token_value: Optional[str] = None  # never returned to client
    expires_at: Optional[str] = None  # master token doesn't expire

    # Attach-if-exists (Phase 2 resume). For a non-fork (deterministic)
    # launch, check whether the session already exists in zellij — running
    # OR exited-and-resurrectable. If so, SKIP the launch: re-running
    # `zellij --session NAME` on an existing name errors rc=1 ("already
    # exists"), and the client only needs the URL to reattach (the
    # zellij-web client resurrects an exited session on attach). A fork
    # always creates, so we never attach-if-exists for forks.
    #
    # `resumed` controls the state-write-failure cleanup below: we must
    # NOT kill a session we merely attached to (G3 — a name-collision
    # cleanup would destroy the user's live terminal). We only kill on a
    # cleanup path for a session WE just created.
    resumed = False
    if not fork:
        try:
            resumed = await asyncio.to_thread(
                zellij_client.session_exists, session_name_
            )
        except Exception as exc:  # noqa: BLE001 — treat "can't tell" as absent
            logger.warning(
                "zellij_launch: session_exists(%s) probe failed (%s) — "
                "proceeding as create",
                session_name_,
                exc,
            )
            resumed = False

    if resumed:
        logger.info(
            "zellij_launch: ATTACH-IF-EXISTS — session %s already present, "
            "resuming (no relaunch)",
            session_name_,
        )
    else:
        # Launch the session (blocking subprocess call).
        try:
            await asyncio.to_thread(
                zellij_client.launch_session,
                session_name_,
                binary,
            )
        except zellij_client.ZellijBinaryMissing as exc:
            logger.error("zellij_launch: %s", exc)
            raise HTTPException(503, str(exc))
        except Exception as exc:  # noqa: BLE001
            # Race / probe-miss: the name may have been created between our
            # existence probe and the launch (or the probe failed open).
            # zellij errors rc=1 on a name collision. Treat that single
            # case as a successful resume rather than a 500 — the session
            # exists and is reattachable, which is exactly what the caller
            # asked for. Re-check existence to confirm before swallowing.
            collided = False
            if not fork:
                try:
                    collided = await asyncio.to_thread(
                        zellij_client.session_exists, session_name_
                    )
                except Exception:  # noqa: BLE001
                    collided = False
            if collided:
                logger.info(
                    "zellij_launch: launch_session(%s) hit an existing name "
                    "(%s) — resuming the existing session instead",
                    session_name_,
                    exc,
                )
                resumed = True
            else:
                logger.error(
                    "zellij_launch: launch_session(%s) failed: %s",
                    session_name_,
                    exc,
                    exc_info=True,
                )
                raise HTTPException(500, f"Failed to launch Zellij session: {exc}")

    try:
        await asyncio.to_thread(
            zellij_state.add_session,
            op,
            provider,
            app,
            session_name_,
            token_name,
            expires_at,
        )
    except Exception as exc:  # noqa: BLE001
        # State write failed but the Zellij side is live — log loudly,
        # try to clean up, and surface 500 so the client doesn't think
        # the session is usable. No token-revoke cleanup needed (no
        # per-session token was minted).
        #
        # G3 GUARD: only kill a session WE created. If we RESUMED an
        # existing session, killing it on a state-write failure would
        # destroy the user's live terminal — exactly the regression this
        # whole feature is meant to prevent. Leave a resumed session
        # alone; the next launch/reconcile will re-establish its row.
        logger.error(
            "zellij_launch: state.add_session(%s) failed: %s",
            session_name_,
            exc,
            exc_info=True,
        )
        if not resumed:
            try:
                await asyncio.to_thread(zellij_client.kill_session, session_name_)
            except Exception as cleanup_exc:  # noqa: BLE001
                logger.warning(
                    "zellij_launch: cleanup kill_session(%s) failed: %s",
                    session_name_,
                    cleanup_exc,
                )
        else:
            logger.warning(
                "zellij_launch: NOT killing %s on state-write failure — "
                "it is a RESUMED (pre-existing) session, not one we created",
                session_name_,
            )
        raise HTTPException(500, f"Failed to record Zellij session state: {exc}")

    logger.info(
        "zellij_launch: operator=%s provider=%s app=%s session=%s "
        "token_name=%s expires_at=%s fork=%s resumed=%s",
        op,
        provider,
        app,
        session_name_,
        token_name,
        expires_at,
        fork,
        resumed,
    )

    return {
        "session_name": session_name_,
        "session_url": _zellij_session_url(session_name_, token_value),
        "token": token_value,
        "expires_at": expires_at,
        "resumed": resumed,
    }


@router.get("/zellij/sessions")
async def zellij_list_sessions(op: str = Query(...)):
    """List this operator's active Zellij sessions.

    Intersection of (a) state rows for this operator and (b) sessions
    actually present in Zellij. A row that exists in state but not in
    Zellij is treated as stale and omitted (operator may have killed it
    out-of-band via ``zellij kill-session``); the next launch+reconcile
    cycle will clean it up.
    """
    _check_operator_allowed(op)
    _require_zellij_backend()

    try:
        zellij_sessions = await asyncio.to_thread(zellij_client.list_sessions)
    except zellij_client.ZellijBinaryMissing as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("zellij_list_sessions: list_sessions failed: %s", exc, exc_info=True)
        raise HTTPException(500, f"Failed to list Zellij sessions: {exc}")

    try:
        state_rows = await asyncio.to_thread(zellij_state.list_for_operator, op)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "zellij_list_sessions: state.list_for_operator(%s) failed: %s",
            op,
            exc,
            exc_info=True,
        )
        raise HTTPException(500, f"Failed to read Zellij state: {exc}")

    live_names = {s["name"] for s in zellij_sessions if "name" in s}
    out: list[dict] = []
    op_prefix = f"{op}__"
    for row in state_rows:
        name = row.get("session_name")
        if not name or not name.startswith(op_prefix):
            continue
        if name not in live_names:
            continue
        out.append({
            "name": name,
            "provider": row.get("provider"),
            "app": row.get("app"),
            "created_at": row.get("created_at"),
            "expires_at": row.get("expires_at"),
        })
    return {"sessions": out}


@router.delete("/zellij/sessions/{name}", status_code=204)
async def zellij_delete_session(name: str, op: str = Query(...)):
    """Kill a Zellij session + revoke its token + remove the state row.

    Idempotent: returns 204 even if the session/token were already gone
    by the time the request arrived.

    Audit I8: rejects with 403 if ``name`` doesn't start with the
    requesting operator's prefix.
    """
    _check_operator_allowed(op)

    if not _validate_operator_prefix(name, op):
        logger.warning(
            "zellij_delete_session: operator-prefix gate VIOLATION — "
            "operator=%s attempted to delete session=%s",
            op,
            name,
        )
        raise HTTPException(
            403,
            "Cannot delete session belonging to another operator",
        )

    _require_zellij_backend()

    # Look up token_name from state so we can revoke it. If the row is
    # already gone (e.g. previous delete attempt half-succeeded), skip
    # the revoke — kill_session + remove_session are both idempotent.
    token_name: Optional[str] = None
    try:
        rows = await asyncio.to_thread(zellij_state.list_for_operator, op)
        for row in rows:
            if row.get("session_name") == name:
                token_name = row.get("token_name")
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "zellij_delete_session: state lookup failed for %s (%s) "
            "— proceeding with kill+remove anyway",
            name,
            exc,
        )

    if token_name:
        try:
            await asyncio.to_thread(zellij_client.revoke_token, token_name)
        except Exception as exc:  # noqa: BLE001
            # Token revoke failure shouldn't block the kill — log and
            # continue. Next boot's reconcile_or_wipe will catch
            # orphaned tokens.
            logger.warning(
                "zellij_delete_session: revoke_token(%s) failed: %s",
                token_name,
                exc,
            )

    try:
        await asyncio.to_thread(zellij_client.kill_session, name)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "zellij_delete_session: kill_session(%s) failed: %s",
            name,
            exc,
            exc_info=True,
        )
        raise HTTPException(500, f"Failed to kill Zellij session: {exc}")

    try:
        await asyncio.to_thread(zellij_state.remove_session, name)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "zellij_delete_session: state.remove_session(%s) failed: %s",
            name,
            exc,
            exc_info=True,
        )
        raise HTTPException(500, f"Failed to remove Zellij state row: {exc}")

    logger.info("zellij_delete_session: operator=%s session=%s deleted", op, name)
    # 204 No Content
    return Response(status_code=204)


# ── T14.8: POST /zellij/inject — type text into a live session ──────────
# Consumed by the launcher's shortcut dropdown to send "claude\n" etc into
# the active terminal pane. Wraps zellij_client.inject (which runs
# `zellij --session NAME action write-chars TEXT` under the hood).
#
# Security boundary (audit I10):
#   1. Operator must be in ALLOWED_OPS (_check_operator_allowed)
#   2. Session name must start with {operator}__ (operator-prefix gate)
#   3. Text length capped at 4096 chars — prevents pathologically large
#      injects (the launcher only ever sends short binary names like
#      "claude\n", so this is comfortably under the cap)
#
# Note: this endpoint deliberately does NOT enforce provider="terminal"
# yet (plan T14.8 mentioned it as a future hardening). The operator-
# prefix gate already prevents cross-operator injection; whether a given
# operator can inject into their OWN claude session vs only their
# terminal session is a UX-policy decision deferred to T14.9.
class ZellijInjectRequest(BaseModel):
    session_name: str
    text: str


class ZellijSpawnRequest(BaseModel):
    session_name: str
    binary: str  # must be in _SPAWN_BINARY_ALLOWLIST


# Allowlist for /spawn — keep tight because the binary name is exec'd
# directly via Zellij's `action new-tab -- BINARY`. Resolution happens at
# request time via shutil.which (or the existing CLI agent provider_bin
# lookup) so we don't ship hardcoded user-specific paths.
_SPAWN_ALLOWED_BINARIES = frozenset({"claude", "gemini", "codex", "agy", "antigravity", "grok"})


@router.post("/zellij/inject", status_code=204)
async def zellij_inject(req: ZellijInjectRequest, op: str = Query(...)):
    """Inject text into a Zellij session as if the user typed it."""
    _check_operator_allowed(op)

    if not _validate_operator_prefix(req.session_name, op):
        logger.warning(
            "zellij_inject: operator-prefix gate VIOLATION — "
            "operator=%s attempted to inject into session=%s",
            op,
            req.session_name,
        )
        raise HTTPException(
            403,
            "Cannot inject into a session belonging to another operator",
        )

    if not req.text or len(req.text) > 4096:
        raise HTTPException(
            400,
            "Inject text must be 1-4096 chars",
        )

    _require_zellij_backend()

    try:
        await asyncio.to_thread(zellij_client.inject, req.session_name, req.text)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "zellij_inject: inject(%s, %d chars) failed: %s",
            req.session_name,
            len(req.text),
            exc,
            exc_info=True,
        )
        raise HTTPException(500, f"Failed to inject into Zellij session: {exc}")

    # Log without the text content (could leak whatever the launcher
    # decides to send later — names, tokens-in-shortcuts, etc.).
    logger.info(
        "zellij_inject: operator=%s session=%s text_len=%d injected",
        op,
        req.session_name,
        len(req.text),
    )
    return Response(status_code=204)


@router.post("/zellij/spawn", status_code=204)
async def zellij_spawn(req: ZellijSpawnRequest, op: str = Query(...)):
    """Spawn a binary in a NEW Zellij tab inside the operator's session.

    Used by the launcher's Shortcuts dropdown — clicking "Claude" / "Gemini"
    /etc. opens a fresh tab with that binary running. Bypasses the
    write-chars-into-bash path because claude empirically doesn't render
    when launched via bash inside Zellij (T15 diagnostic).
    """
    _check_operator_allowed(op)
    if not _validate_operator_prefix(req.session_name, op):
        logger.warning(
            "zellij_spawn: operator-prefix gate VIOLATION — operator=%s session=%s",
            op, req.session_name,
        )
        raise HTTPException(403, "Cannot spawn into a session belonging to another operator")
    if req.binary not in _SPAWN_ALLOWED_BINARIES:
        raise HTTPException(400, f"Binary '{req.binary}' not in allowlist: {sorted(_SPAWN_ALLOWED_BINARIES)}")

    _require_zellij_backend()

    # Resolve binary path via the existing provider_bin lookup. Falls back to
    # bare name if not found (Zellij will then try PATH inside its env).
    resolved = provider_bin(req.binary) or req.binary

    try:
        await asyncio.to_thread(zellij_client.spawn_in_place, req.session_name, resolved)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "zellij_spawn: spawn_in_new_tab(%s, %s) failed: %s",
            req.session_name, resolved, exc, exc_info=True,
        )
        raise HTTPException(500, f"Failed to spawn binary in Zellij session: {exc}")

    logger.info(
        "zellij_spawn: operator=%s session=%s binary=%s resolved=%s spawned",
        op, req.session_name, req.binary, resolved,
    )
    return Response(status_code=204)


@router.get("/zellij/backend-status")
async def zellij_backend_status(op: str = Query(...)):
    """Lightweight status for the Portal status-bar indicator (audit I9).

    Designed to respond fast even when Zellij is sick: ``get_backend()``
    is TTL-cached so it doesn't curl Zellij on the hot path, and the
    session-count probes only run when we know the daemon is up.
    """
    _check_operator_allowed(op)

    # Raw env (NOT the post-fallback effective value). Mirrors the
    # logic in cli_agent.__init__.get_backend so we can show
    # "configured zellij but falling back to tmux" in the UI.
    raw_env = os.environ.get("CLI_AGENT_BACKEND", "").strip().lower()
    if raw_env in {"tmux", "zellij"}:
        configured_backend = raw_env
    else:
        # Match the locked code default in cli_agent.__init__.
        configured_backend = "tmux"

    effective_backend = get_backend()

    web_daemon_running = False
    session_count_total = 0
    my_session_count = 0

    if effective_backend == "zellij":
        # Daemon-aware probes only when we believe Zellij is the active
        # backend; get_backend() already did the health check.
        web_daemon_running = True
        try:
            all_sessions = await asyncio.to_thread(zellij_client.list_sessions)
            session_count_total = len(all_sessions)
            op_prefix = f"{op}__"
            my_session_count = sum(
                1 for s in all_sessions if s.get("name", "").startswith(op_prefix)
            )
        except Exception as exc:  # noqa: BLE001 — never make status fail
            logger.warning(
                "zellij_backend_status: list_sessions failed (%s) — "
                "reporting zero counts",
                exc,
            )
            session_count_total = 0
            my_session_count = 0

    return {
        "web_daemon_running": web_daemon_running,
        "session_count_total": session_count_total,
        "my_session_count": my_session_count,
        "configured_backend": configured_backend,
        "effective_backend": effective_backend,
    }
