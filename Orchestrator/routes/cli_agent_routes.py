import asyncio
import json
import os
import shutil
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


def _resolve_provider_bin(name: str) -> str:
    """Resolve a CLI agent binary to an absolute path.

    systemd's restricted PATH excludes user-local install dirs like
    ~/.local/bin/, and per-version nvm dirs like
    ~/.nvm/versions/node/<ver>/bin. We search an extended PATH that
    includes both. Falls back to the bare name (which will fail loudly
    via tmux if the binary truly cannot be found).

    See Orchestrator.cli_agent.path_extension for the shared dir list.
    """
    from Orchestrator.cli_agent.path_extension import extended_path_dirs
    extended_path = os.pathsep.join([
        os.environ.get("PATH", ""),
        *extended_path_dirs(),
    ])
    found = shutil.which(name, path=extended_path)
    return found or name


SUPPORTED_PROVIDERS: tuple[str, ...] = ("claude", "gemini", "codex")


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
