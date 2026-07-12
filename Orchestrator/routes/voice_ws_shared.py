#!/usr/bin/env python3
"""
voice_ws_shared.py - Cross-route helpers shared by the three realtime voice
WebSocket bridges (OpenAI realtime_routes, xAI grok_live_routes, Google
gemini_live_routes).

P1b hardening (2026-07-11 voice-agent upgrade pass, workstream 6):
- Tool-dispatch exception -> error payload back to the model, so a raised
  executor NEVER dangles a function call id (previously: silent dead turn,
  the model waits forever on a tool result and the user hears dead air).
- save_voice_transcript(): transcript persistence via POST /chat/save
  (direct persistence + turns_threshold=1 auto-mint) instead of POST /chat
  (full LLM round-trip, ~400x more expensive — CLAUDE.md anti-pattern).

Keep this module LIGHT (json/aiohttp/starlette only): it is imported by all
three voice routes and, through them, the phone bridge chain.
"""

import json
from typing import Dict, Optional, Set

from starlette.websockets import WebSocketState


def tool_error_text(name: str, exc: BaseException) -> str:
    """Error payload returned to the model in place of a tool result."""
    return (
        f"Tool '{name}' failed: {type(exc).__name__}: {exc}. "
        "Briefly tell the user the action failed, then continue the conversation."
    )


async def _safe_portal_send(websocket, data: dict) -> bool:
    """Best-effort JSON send to the client WS; never raises.

    Local copy of the routes' _safe_ws_send — importing it from a route module
    here would create an import cycle (routes import this module).
    """
    try:
        if websocket and hasattr(websocket, "application_state") \
                and websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_json(data)
            return True
    except Exception:
        pass
    return False


async def send_openai_style_tool_error(upstream_ws, portal_ws,
                                       event: Dict, exc: BaseException) -> bool:
    """Answer a dangling OpenAI-schema function call with an error payload.

    Used by BOTH the OpenAI Realtime and xAI Grok routes (identical wire
    format): sends conversation.item.create/function_call_output for the
    event's call_id followed by response.create, and notifies the portal
    with an error-flagged tool_result. No-op (returns False) when the event
    is not a function-call event. Never raises.
    """
    if not event or event.get("type") != "response.function_call_arguments.done":
        return False

    call_id = event.get("call_id", "")
    name = event.get("name", "")
    result = tool_error_text(name, exc)

    sent = False
    if upstream_ws:
        try:
            await upstream_ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result,
                },
            }))
            await upstream_ws.send(json.dumps({"type": "response.create"}))
            sent = True
        except Exception as send_err:
            print(f"[VOICE-SHARED] Could not deliver tool error for '{name}' "
                  f"(call_id={call_id}): {send_err}")

    await _safe_portal_send(portal_ws, {
        "type": "tool_result",
        "data": {"name": name, "result_length": len(result), "error": True},
    })
    return sent


async def send_gemini_tool_error(gemini_ws, portal_ws, event: Dict,
                                 exc: BaseException,
                                 answered_ids: Optional[Set[str]] = None) -> bool:
    """Answer dangling Gemini functionCalls with error functionResponses.

    A Gemini toolCall event carries a LIST of functionCalls; a raise mid-loop
    dangles every not-yet-answered id. `answered_ids` (recorded by the dispatch
    loop) prevents double-answering ids that already got a real response.
    No-op (returns False) when the event has no toolCall or nothing is
    unanswered. Never raises.
    """
    tool_call = (event or {}).get("toolCall")
    if not tool_call:
        return False

    answered = answered_ids or set()
    pending = [fc for fc in tool_call.get("functionCalls", [])
               if fc.get("id", "") not in answered]
    if not pending:
        return False

    responses = [{
        "id": fc.get("id", ""),
        "name": fc.get("name", ""),
        "response": {"result": tool_error_text(fc.get("name", ""), exc)},
    } for fc in pending]

    sent = False
    if gemini_ws:
        try:
            await gemini_ws.send(json.dumps(
                {"toolResponse": {"functionResponses": responses}}))
            sent = True
        except Exception as send_err:
            print(f"[VOICE-SHARED] Could not deliver Gemini tool error "
                  f"({len(responses)} call(s)): {send_err}")

    for fc in pending:
        await _safe_portal_send(portal_ws, {
            "type": "tool_result",
            "data": {"name": fc.get("name", ""), "result_length": 0, "error": True},
        })
    return sent
