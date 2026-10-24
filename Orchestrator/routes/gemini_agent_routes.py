#!/usr/bin/env python3
from __future__ import annotations
from dataclasses import dataclass, field

"""
gemini_agent_routes.py - Gemini CLI agent, WebSocket, session management
Mirrors the Claude Code CLI integration for Gemini CLI, over the same
CliAgentTransport (Orchestrator/cli_agent/transport.py)
"""

# Standard library imports
import asyncio
import json
import os
import threading
from collections import OrderedDict
from typing import Dict, Optional, List, Tuple

# External library imports
from fastapi import WebSocket, WebSocketDisconnect

# Local imports
from Orchestrator.agent_context import (
    compose_with_fossils,
    resolve_operator,
    retrieve_for_agent,
)
from Orchestrator.checkpoint import app
from Orchestrator.cli_agent.env_scrub import scrubbed_child_env
from Orchestrator.cli_agent.transport import (
    CliAgentTransport,
    Cursor,
    FailureHook,
    SessionReaper,
    SpawnSpec,
    cancel_task,
    refuse_turn,
    relay,
    requested_seq,
    safe_ws_send,
    skipped_notice,
    spawn_error_text,
)
from Orchestrator.config import GEMINI_API_KEY, current_default
from Orchestrator.utils.paths import blackbox_root
from Orchestrator.utils.testing import boot_hook
from Orchestrator.volume import now_utc_iso


async def _inject_fossil_context_gemini(
    websocket: WebSocket,
    operator_raw: str,
    user_text: str,
    log_prefix: str = "[GEMINI-AGENT]",
) -> tuple[str, dict]:
    """Run shared retrieval, emit provenance to client. Mirrors agent_routes._inject_fossil_context."""
    operator = resolve_operator(operator_raw, log_prefix)
    fossil_text, provenance = retrieve_for_agent(
        user_text=user_text,
        operator=operator,
        log_prefix=log_prefix,
    )
    await safe_ws_send(websocket, {"type": "provenance", "data": provenance})
    return fossil_text, provenance

# =============================================================================
# Session Model
# =============================================================================

@dataclass
class GeminiAgentSession:
    """Represents a Gemini CLI agent session."""
    session_id: str
    operator: str = ""
    # Runs the session's CLI turns and holds its bounded output.
    transport: Optional[CliAgentTransport] = None
    # The Gemini CLI's own id for this conversation, from its "init" event.
    cli_session_id: str = ""
    mode: str = "streaming"  # streaming or interactive
    working_directory: str = ""
    created_at: str = ""
    last_activity: str = ""
    status: str = "idle"  # idle, running, completed, error
    message_count: int = 0
    yolo_mode: bool = True  # Auto-approve all tool calls
    websocket_clients: List[WebSocket] = field(default_factory=list)
    apps: List[Dict] = field(default_factory=list)
    # Snapshot retrieval provenance from build_fossil_context (Plan Task 4)
    provenance: Dict[str, List[str]] = field(default_factory=dict)


# Global storage for Gemini agent sessions
GEMINI_AGENT_SESSIONS: Dict[str, GeminiAgentSession] = {}
gemini_agent_lock = threading.Lock()

# The CLI session id (and working directory: the Gemini CLI keeps sessions per
# project directory) of each (operator, socket session id) seen since startup,
# kept when the reaper evicts the session, so a later prompt on that socket
# session resumes the right conversation. Bounded; lost on restart (then a
# resume starts fresh).
_GEMINI_RESUME_IDS: "OrderedDict[Tuple[str, str], Tuple[str, str]]" = OrderedDict()
_GEMINI_RESUME_IDS_MAX = 512


def _remember_resume_id(operator: str, session_id: str, cli_session_id: str,
                        working_directory: str = "") -> None:
    if not cli_session_id:
        return
    key = (operator, session_id)
    _GEMINI_RESUME_IDS[key] = (cli_session_id, working_directory)
    _GEMINI_RESUME_IDS.move_to_end(key)
    while len(_GEMINI_RESUME_IDS) > _GEMINI_RESUME_IDS_MAX:
        _GEMINI_RESUME_IDS.popitem(last=False)


# =============================================================================
# Process Creation
# =============================================================================

def get_gemini_path() -> str:
    """Path to the Gemini CLI: the same resolver as the CLI-agent terminals
    (provider_bin: CLI_AGENT_GEMINI_BIN, else PATH + ~/.local/bin + every nvm
    Node version, newest first). The bare name when it is not installed."""
    from Orchestrator.routes.cli_agent_routes import provider_bin
    return provider_bin("gemini") or "gemini"


def _gemini_spawn_path(gemini_path: str, base_path: str) -> str:
    """PATH for the CLI child: the CLI's own bin dir first (an nvm-installed
    CLI's `#!/usr/bin/env node` shebang needs the node beside it), then the
    inherited PATH and the standard extension dirs."""
    from Orchestrator.cli_agent.path_extension import extended_path_dirs
    dirs = []
    if os.path.isabs(gemini_path):
        dirs.append(os.path.dirname(gemini_path))
    dirs += [d for d in (base_path or "").split(os.pathsep) if d]
    dirs += extended_path_dirs()
    return os.pathsep.join(dict.fromkeys(dirs))


def gemini_spawn_spec(
    working_dir: str,
    prompt: str,
    yolo_mode: bool = True,
    resume_session: Optional[str] = None,
) -> SpawnSpec:
    """The Gemini CLI run (streaming JSON mode) for one agent turn.

    Args:
        working_dir: Working directory for Gemini CLI
        prompt: The prompt to send
        yolo_mode: If True, auto-approve all tool calls (-y flag)
        resume_session: The CLI session id to resume. Never "latest": that is
            the most recent session in the working directory, which every
            operator shares (audit O06-frontier-agent-13).
    """
    gemini_path = get_gemini_path()

    # Build command arguments
    cmd = [gemini_path]

    # Output format for streaming
    cmd.extend(["--output-format", "stream-json"])

    # YOLO mode - auto-approve all tool calls
    if yolo_mode:
        cmd.append("-y")
    else:
        cmd.extend(["--approval-mode", "default"])

    # Session continuity: this conversation, by id
    if resume_session:
        cmd.extend(["--resume", str(resume_session)])
        print(f"[GEMINI-AGENT] Resuming session: {resume_session}")

    # Add the prompt as positional argument
    cmd.append(prompt)

    print(f"[GEMINI-AGENT] Command: {' '.join(cmd[:6])}... (yolo={yolo_mode})")

    # Environment with nvm. Scrubbed (shared env_scrub table) so the YOLO
    # agent never inherits the server's other credentials (Anthropic/OpenAI/
    # xAI/Twilio/AMI — audit O14-cli-agents-devices-02/-03).
    env = scrubbed_child_env()
    env["PATH"] = _gemini_spawn_path(gemini_path, env.get("PATH", ""))
    nvm_dir = os.path.expanduser("~/.nvm")
    if os.path.exists(nvm_dir):
        env["NVM_DIR"] = nvm_dir

    # Add API key (central config falls back from GEMINI_API_KEY to GOOGLE_API_KEY).
    # The ONE deliberate exception to the scrub: this legacy agent-mode route has
    # always authenticated the gemini CLI with the server's configured key (it
    # does not rely on a gemini sign-in), so it re-adds exactly that variable.
    env["GEMINI_API_KEY"] = GEMINI_API_KEY

    return SpawnSpec(argv=cmd, cwd=working_dir, env=env)


# =============================================================================
# Session hooks and the event translator
# =============================================================================

def _note_gemini_line(session: GeminiAgentSession, line: str) -> None:
    """Every stdout line, client attached or not: stamp activity and capture the
    CLI's session id from its "init" event (the id the next turn resumes)."""
    session.last_activity = now_utc_iso()
    if '"init"' not in line:
        return
    try:
        event = json.loads(line)
    except ValueError:
        return
    if not isinstance(event, dict) or event.get("type") != "init":
        return
    cli_session = event.get("session_id")
    if isinstance(cli_session, str) and cli_session:
        session.cli_session_id = cli_session
        _remember_resume_id(session.operator, session.session_id, cli_session,
                            session.working_directory)
        print(f"[GEMINI-AGENT] Init event - model: {event.get('model', 'unknown')}, session: {cli_session[:8]}...")


def _finish_gemini_turn(session: GeminiAgentSession, returncode: int) -> None:
    with gemini_agent_lock:
        session.status = "completed"
        session.last_activity = now_utc_iso()


def _new_gemini_transport(session: GeminiAgentSession) -> CliAgentTransport:
    return CliAgentTransport(
        f"gemini-{session.session_id[:8]}",
        on_line=lambda line: _note_gemini_line(session, line),
        on_exit=lambda returncode: _finish_gemini_turn(session, returncode),
    )


def gemini_event_frames(line: str) -> List[dict]:
    """Client frames for one Gemini CLI stream-json line (one parser: it used to
    be duplicated, slightly differently, for the live loop and the final drain)."""
    try:
        event = json.loads(line)
    except ValueError:
        # Not JSON, send as raw output
        return [{"type": "raw", "data": line}]
    if not isinstance(event, dict):
        return [{"type": "raw", "data": line}]
    event_type = event.get("type", "")

    # Skip user/prompt events - these echo the user's input
    if event_type in ("user", "prompt", "input", "user_message"):
        return []

    if event_type == "message":
        # Message event contains response text; skip the user's own messages
        if event.get("role", "assistant") == "user":
            return []
        content = event.get("content", "")
        return [{"type": "content", "data": content}] if content else []

    if event_type in ("tool_call", "tool_use"):
        # Gemini CLI uses: tool_name, parameters; Claude uses: name, input
        tool_name = event.get("tool_name") or event.get("name", "")
        tool_input = event.get("parameters") or event.get("input", {})
        return [{"type": "tool_use", "data": {"name": tool_name, "input": tool_input}}] if tool_name else []

    if event_type == "tool_result":
        # Tool result - check multiple possible fields
        result = event.get("result") or event.get("output") or event.get("content", "")
        return [{"type": "tool_result", "data": result}] if result else []

    if event_type == "thinking":
        thinking = event.get("content", "")
        return [{"type": "thinking", "data": thinking}] if thinking else []

    if event_type == "error":
        error = event.get("message", event.get("error", "Unknown error"))
        return [{"type": "error", "data": error}]

    if event_type == "init":
        # Session initialization event from Gemini CLI (its id is captured by _note_gemini_line)
        return [{"type": "info", "data": f"Gemini session started: {event.get('model', 'unknown')}"}]

    if event_type in ("done", "end", "system", "result", "progress", "status"):
        # Completion/status events: the turn's end comes from the process exit
        return []

    # Unknown event type - try to extract useful data
    if "text" in event:
        return [{"type": "content", "data": event["text"]}]
    if "content" in event and event.get("role") != "user":
        return [{"type": "content", "data": event["content"]}]
    if "message" in event:
        return [{"type": "content", "data": str(event["message"])}]
    print(f"[GEMINI-AGENT] Unknown event: {str(event)[:200]}")
    return []


# =============================================================================
# WebSocket Endpoint
# =============================================================================

PREVIOUS_TURN_WAIT_SECONDS = 5.0
OTHER_OPERATOR_MESSAGE = (
    "This agent session belongs to another operator. Start a new session to talk "
    "to the agent as yourself.")
UNKNOWN_RESUME_MESSAGE = (
    "The previous Gemini conversation for this session is not known to the server "
    "any more (it restarted), so this starts a new conversation with your memory context.")
LOST_CONVERSATION_MESSAGE = (
    "The earlier Gemini conversation for this session is no longer on this box, so "
    "this starts a new conversation with your memory context.")
# What the Gemini CLI says (stderr) when --resume <id> names a session it does
# not have in this project directory; it then exits non-zero.
_GEMINI_MISSING_CONVERSATION = ("Error resuming session", "Invalid session identifier",
                                "No previous sessions found")


def _frame_operator(data: dict) -> str:
    raw = data.get("operator")
    return raw.strip() if isinstance(raw, str) else ""


def gemini_conversation_missing(diagnostics: str) -> bool:
    """Whether a failed `gemini --resume <id>` run failed because the CLI has no
    such session (``diagnostics``: its stderr tail and last stdout lines)."""
    return any(marker in (diagnostics or "") for marker in _GEMINI_MISSING_CONVERSATION)


def _existing_dir(path: Optional[str], fallback: str) -> str:
    """``path`` when it is still a directory, else ``fallback``."""
    if path and os.path.isdir(path):
        return path
    if path:
        print(f"[GEMINI-AGENT] Working directory {path} is gone; using {fallback}")
    return fallback


def _new_conversation_if_missing(session: GeminiAgentSession, prompt_text: str, *,
                                 cwd: str, yolo_mode: bool,
                                 fossil_text: Optional[str] = None) -> FailureHook:
    """on_failure for a resumed turn: when the CLI has no such session, the turn
    starts a new conversation (memory context, no --resume) instead of failing,
    and later turns resume the new one (its "init" event reports the id)."""
    async def start_new(returncode: int, diagnostics: str):
        if not gemini_conversation_missing(diagnostics):
            return None
        lost = session.cli_session_id or ""
        print(f"[GEMINI-AGENT] CLI session {lost[:8]}... is gone (rc={returncode}); starting a new "
              f"conversation for {session.session_id[:8]}")
        session.cli_session_id = ""
        _GEMINI_RESUME_IDS.pop((session.operator, session.session_id), None)
        context = fossil_text
        if context is None:
            context, _ = retrieve_for_agent(
                user_text=prompt_text,
                operator=resolve_operator(session.operator, "[GEMINI-AGENT]"),
                log_prefix="[GEMINI-AGENT]",
            )
        spec = gemini_spawn_spec(cwd, compose_with_fossils(context, prompt_text),
                                 yolo_mode=yolo_mode, resume_session=None)
        return spec, LOST_CONVERSATION_MESSAGE
    return start_new


@app.websocket("/ws/gemini-agent/{session_id}")
async def gemini_agent_websocket(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for Gemini CLI streaming.

    Message types:
    - prompt: Start new prompt
    - input: Send follow-up input
    - end_session: End current session
    - reconnect: Reconnect to existing session
    - ping: Keep-alive

    A follow-up resumes the conversation by the id the CLI reported in its
    "init" event (--resume <id>), never --resume latest.
    """
    print(f"[GEMINI-AGENT] WebSocket connection for session: {session_id}")
    await websocket.accept()

    relay_task: Optional[asyncio.Task] = None
    # The transport this socket's relay reads and how far it got (kept on the
    # transport when the socket closes, for this client's reconnect).
    relay_cursor: Optional[Tuple[CliAgentTransport, Cursor]] = None
    bound_operator = ""

    async def send(frame: dict) -> bool:
        return await safe_ws_send(websocket, frame)

    def follow(session: GeminiAgentSession, since_seq: int) -> None:
        """This socket's own reader of the session (replaces any earlier one)."""
        nonlocal relay_task, relay_cursor
        cancel_task(relay_task)
        with gemini_agent_lock:
            if websocket not in session.websocket_clients:
                session.websocket_clients.append(websocket)
        cursor = Cursor(since_seq)
        relay_cursor = (session.transport, cursor)
        relay_task = asyncio.create_task(
            relay(session.transport, send, gemini_event_frames, since_seq, cursor),
            name=f"gemini-relay-{session_id[:8]}")

    async def start_turn(session: GeminiAgentSession, spec: SpawnSpec,
                         on_failure: Optional[FailureHook] = None) -> Optional[int]:
        """Start one turn; when it cannot start, tell the client (refuse_turn)
        and return None."""
        try:
            return await session.transport.start(spec, on_failure=on_failure)
        except RuntimeError:
            # Another client's prompt started a turn while this one waited.
            message = "The agent is still working on another message in this session. Try again when it finishes."
        except OSError as e:
            print(f"[GEMINI-AGENT] Could not start {spec.argv[0]} in {spec.cwd}: {e}")
            message = spawn_error_text(e, spec, "Gemini", "Install it on this box, then try again.")
        await refuse_turn(send, message)
        return None

    try:
        while True:
            data = await websocket.receive_json()
            if not isinstance(data, dict):
                continue
            msg_type = data.get("type", "")
            print(f"[GEMINI-AGENT] Received: {msg_type}")

            if msg_type == "prompt":
                with gemini_agent_lock:
                    session = GEMINI_AGENT_SESSIONS.get(session_id)
                operator = (_frame_operator(data) or (session.operator if session else "")
                            or bound_operator or current_default())
                if session is not None and session.operator and operator != session.operator:
                    await refuse_turn(send, OTHER_OPERATOR_MESSAGE)
                    continue
                bound_operator = operator
                default_dir = str(blackbox_root())
                working_dir = data.get("working_dir") or default_dir
                prompt_text = data.get("text", "")
                yolo_mode = data.get("yolo_mode", True)
                # Frontend tells us if user has a persisted session to resume
                frontend_wants_resume = bool(data.get("resume_session", False))

                if not prompt_text:
                    await send({"type": "error", "data": "No prompt provided"})
                    continue

                # Plan Task 4: inject fossil retrieval at session start.
                # Always emit a provenance event so the Android UI has a
                # deterministic signal — even for unknown operators.
                fossil_text, provenance = await _inject_fossil_context_gemini(
                    websocket,
                    operator_raw=operator,
                    user_text=prompt_text,
                    log_prefix="[GEMINI-AGENT]",
                )

                if session is not None and session.transport is not None:
                    await session.transport.finish_or_stop(PREVIOUS_TURN_WAIT_SECONDS)

                # Which conversation to resume, by id: this session's own, else the
                # one this operator last had on this socket session (kept across
                # the reaper's eviction: reusing a socket session id continues
                # its conversation, resume_session or not), else none - a new one.
                resume_id = (session.cli_session_id if session else "") or ""
                remembered_dir = ""
                if not resume_id:
                    resume_id, remembered_dir = _GEMINI_RESUME_IDS.get((operator, session_id), ("", ""))
                if not resume_id and (frontend_wants_resume or (session and session.message_count > 0)):
                    print(f"[GEMINI-AGENT] No known CLI session to resume for {session_id[:8]}; starting fresh")
                    await send({"type": "info", "data": UNKNOWN_RESUME_MESSAGE})

                print(f"[GEMINI-AGENT] Resume decision: backend_session={session is not None}, frontend_wants={frontend_wants_resume}, resume_id={resume_id[:8] if resume_id else 'None'}")

                # Plan Task 4: only prepend fossil context on brand-new conversations.
                # A resumed conversation already carries its prior turns.
                effective_prompt = prompt_text
                if not resume_id:
                    effective_prompt = compose_with_fossils(fossil_text, prompt_text)

                if session is None:
                    session = GeminiAgentSession(
                        session_id=session_id,
                        operator=operator,
                        working_directory=(remembered_dir if resume_id and remembered_dir
                                           else working_dir),
                        created_at=now_utc_iso(),
                        last_activity=now_utc_iso(),
                        yolo_mode=yolo_mode,
                        provenance=provenance,
                    )
                if session.transport is None:
                    session.transport = _new_gemini_transport(session)
                cwd = session.working_directory = _existing_dir(session.working_directory, working_dir)

                spec = gemini_spawn_spec(
                    cwd,
                    effective_prompt,
                    yolo_mode=yolo_mode,
                    resume_session=resume_id or None,
                )
                # Set before the turn starts: its "init" event then reports the id
                # the CLI actually uses (a new conversation's, or the resumed one).
                session.cli_session_id = resume_id
                on_failure = _new_conversation_if_missing(
                    session, prompt_text, cwd=cwd, yolo_mode=yolo_mode,
                    fossil_text=fossil_text) if resume_id else None
                start_seq = await start_turn(session, spec, on_failure)
                if start_seq is None:
                    continue

                session.last_activity = now_utc_iso()
                session.status = "running"
                session.message_count += 1
                session.yolo_mode = yolo_mode
                session.provenance = provenance

                with gemini_agent_lock:
                    GEMINI_AGENT_SESSIONS[session_id] = session

                print(f"[GEMINI-AGENT] {'Resuming' if resume_id else 'Created new'} session (msg #{session.message_count})")
                follow(session, start_seq)

            elif msg_type == "input":
                # Handle follow-up input
                input_text = data.get("text", "")
                input_text = input_text.strip() if isinstance(input_text, str) else ""
                if not input_text:
                    continue

                with gemini_agent_lock:
                    session = GEMINI_AGENT_SESSIONS.get(session_id)

                if session is None or session.transport is None:
                    await send({"type": "error", "data": "No active session"})
                    continue
                # The same operator check as prompt and reconnect: a socket refused
                # as another operator must not drive the session with input frames.
                operator = _frame_operator(data) or bound_operator or session.operator
                if session.operator and operator != session.operator:
                    await refuse_turn(send, OTHER_OPERATOR_MESSAGE)
                    continue
                bound_operator = operator

                await session.transport.finish_or_stop(PREVIOUS_TURN_WAIT_SECONDS)

                if not session.cli_session_id:
                    await send({"type": "info", "data": UNKNOWN_RESUME_MESSAGE})
                cwd = session.working_directory = _existing_dir(
                    session.working_directory, str(blackbox_root()))
                spec = gemini_spawn_spec(
                    cwd,
                    input_text,
                    yolo_mode=session.yolo_mode,
                    resume_session=session.cli_session_id or None,
                )
                on_failure = _new_conversation_if_missing(
                    session, input_text, cwd=cwd,
                    yolo_mode=session.yolo_mode) if session.cli_session_id else None
                start_seq = await start_turn(session, spec, on_failure)
                if start_seq is None:
                    continue

                session.last_activity = now_utc_iso()
                session.status = "running"
                session.message_count += 1
                follow(session, start_seq)

            elif msg_type == "end_session":
                with gemini_agent_lock:
                    session = GEMINI_AGENT_SESSIONS.pop(session_id, None)
                owner = (session.operator if session else "") or _frame_operator(data) or bound_operator
                if session is not None:
                    if session.transport is not None:
                        # A running turn dies with the tools it started.
                        await session.transport.stop()
                    session.status = "completed"
                    session.message_count = 0
                # An ended conversation is not resumed, even after the reaper
                # evicted its session.
                _GEMINI_RESUME_IDS.pop((owner, session_id), None)
                cancel_task(relay_task)
                relay_task = None
                relay_cursor = None

                await send({"type": "session_ended", "data": "Session ended"})

            elif msg_type == "reconnect":
                with gemini_agent_lock:
                    session = GEMINI_AGENT_SESSIONS.get(session_id)

                if session is None:
                    await send({"type": "error", "data": "No session found"})
                    continue
                operator = _frame_operator(data) or bound_operator or session.operator
                if session.operator and operator != session.operator:
                    await send({"type": "error", "data": OTHER_OPERATOR_MESSAGE})
                    continue
                bound_operator = operator

                # Its own position, else where its reader stopped when its socket
                # closed, else the live tail (resume_position).
                since, skipped = (session.transport.resume_position(requested_seq(data))
                                  if session.transport is not None else (None, 0))
                if since is not None:
                    await send({
                        "type": "info",
                        "data": f"Reconnected to session (msg #{session.message_count})"
                    })
                    if skipped:
                        await send(skipped_notice(skipped))
                    follow(session, since)
                else:
                    with gemini_agent_lock:
                        if websocket not in session.websocket_clients:
                            session.websocket_clients.append(websocket)
                    await send({"type": "completed", "data": None})

            elif msg_type == "ping":
                await send({"type": "pong"})

    except WebSocketDisconnect:
        print(f"[GEMINI-AGENT] WebSocket disconnected: {session_id}")
    except Exception as e:
        print(f"[GEMINI-AGENT] WebSocket error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # This socket's reader goes with it; the turn and other clients carry on.
        # Where it stopped is kept for this client's reconnect.
        cancel_task(relay_task)
        if relay_cursor is not None:
            relay_cursor[0].detach(relay_cursor[1].seq)
        with gemini_agent_lock:
            session = GEMINI_AGENT_SESSIONS.get(session_id)
            if session and websocket in session.websocket_clients:
                session.websocket_clients.remove(websocket)


# Finished sessions nobody is attached to are evicted after a grace period
# (audit O06-frontier-agent-15); _GEMINI_RESUME_IDS keeps what a resume needs.
GEMINI_SESSION_REAPER = SessionReaper(GEMINI_AGENT_SESSIONS, gemini_agent_lock, label="gemini-agent")


@boot_hook(app, "startup")
async def start_gemini_session_reaper():
    GEMINI_SESSION_REAPER.start()


# =============================================================================
# HTTP Endpoints
# =============================================================================

@app.get("/gemini-agent/status")
async def gemini_agent_status():
    """Get Gemini CLI agent status."""
    gemini_path = get_gemini_path()
    # provider_bin already searched PATH, ~/.local/bin and every nvm version;
    # the bare name back means the CLI is not installed.
    gemini_exists = os.path.isabs(gemini_path) and os.access(gemini_path, os.X_OK)

    return {
        "available": gemini_exists,
        "gemini_path": gemini_path,
        "active_sessions": len(GEMINI_AGENT_SESSIONS),
        "api_key_configured": bool(GEMINI_API_KEY)
    }


@app.get("/gemini-agent/session/{operator}")
async def get_gemini_session_for_operator(operator: str):
    """Get existing Gemini agent session for an operator (``output_buffer`` is
    its bounded stdout tail, ``next_seq`` the reconnect position after it)."""
    with gemini_agent_lock:
        for session_id, session in GEMINI_AGENT_SESSIONS.items():
            if session.operator == operator and session.status != "completed":
                transport = session.transport
                return {
                    "session": {
                        "session_id": session.session_id,
                        "status": session.status,
                        "working_directory": session.working_directory,
                        "created_at": session.created_at,
                        "last_activity": session.last_activity,
                        "message_count": session.message_count,
                        "yolo_mode": session.yolo_mode
                    },
                    "output_buffer": transport.stdout_lines() if transport else [],
                    "next_seq": transport.buffer.next_seq if transport else 0,
                }
    return {"session": None}


@app.delete("/gemini-agent/session/{session_id}")
async def terminate_gemini_session(session_id: str):
    """Terminate a Gemini agent session: a running turn is killed with its whole
    process tree (off the lock, without blocking the event loop)."""
    with gemini_agent_lock:
        session = GEMINI_AGENT_SESSIONS.pop(session_id, None)
    if session is None:
        return {"status": "not_found"}
    if session.transport is not None:
        await session.transport.stop()
    session.status = "completed"
    return {"status": "terminated"}
