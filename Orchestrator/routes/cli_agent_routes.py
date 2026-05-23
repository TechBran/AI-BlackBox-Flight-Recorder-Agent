import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from Orchestrator.cli_agent.operator_config import OperatorConfig
from Orchestrator.cli_agent.path_validator import PathValidator, WorkspaceViolation
from Orchestrator.cli_agent.session_manager import (
    TmuxSessionManager, session_name,
)
from Orchestrator.cli_agent.pty_bridge import PtyBridge


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


SUPPORTED_PROVIDERS: tuple[str, ...] = ("claude", "gemini", "codex", "antigravity")


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
