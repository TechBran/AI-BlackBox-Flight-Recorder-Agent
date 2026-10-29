"""POST /chat/save and the CU / on-device equivalents: every finished turn
through the shared persist_turn_and_maybe_mint tail.

Part of the chat routes (audit rank 74): see Orchestrator/routes/chat_routes.py,
the namespace every importer and test uses.
"""
from __future__ import annotations

import json
import sys

from Orchestrator.utils.operators import InvalidOperatorName, validate_operator
from fastapi import Request
from fastapi.responses import JSONResponse
from Orchestrator.checkpoint import app
from Orchestrator.artifacts import parse_and_process_artifacts_with_meta
from Orchestrator import location as user_location
from Orchestrator.state import get_state
from Orchestrator.reply_envelope import unwrap_reply_envelope
from Orchestrator.turn_persistence import MintGate, persist_turn_and_maybe_mint
# The auto-mint knobs and hooks MintGate.of reads from this module on every
# turn (a stub set on chat_routes lands here).
from Orchestrator.checkpoint import perform_mint, should_create_checkpoint  # noqa: F401
from Orchestrator.config import (  # noqa: F401
    AUTO_ENABLE, DEBOUNCE_MS, ON_RED, ON_YELLOW, TOKENS_THRESHOLD, TURNS_THRESHOLD,
)
from Orchestrator.monitoring import drift_state_for  # noqa: F401
from Orchestrator.state import save_operator_state  # noqa: F401
from Orchestrator.tasks import create_task  # noqa: F401

from Orchestrator.routes.chat.context import _checked_operator, _take_turn_location


def _mint_gate() -> MintGate:
    """This module's auto-mint knobs and hooks, read per call: a stub on
    chat_routes.perform_mint / AUTO_ENABLE / ... lands here and reaches the
    shared tail."""
    return MintGate.of(sys.modules[__name__])


async def _cu_save_to_blackbox(operator: str, user_text: str, result_text: str, session):
    """Save a completed CU turn to BlackBox history and trigger auto-mint.
    Internal equivalent of the /chat/save endpoint — called by the background
    agent loop so results persist even if the frontend disconnected.
    """
    try:
        # The CU loops carry the operator of whatever route started them; refuse
        # an unsafe one here, before get_state() or the ledger ever sees it.
        validate_operator(operator)
    except InvalidOperatorName as e:
        print(f"[CU-SAVE] Not saving: {e}")
        return
    try:
        turn = await persist_turn_and_maybe_mint(
            get_state(operator), operator, user_text, result_text, gate=_mint_gate(),
            prompt_tokens=session.usage.get("prompt_tokens", 0),
            completion_tokens=session.usage.get("completion_tokens", 0),
            model="computer-use",
            # Provenance — trace which fossils informed this CU session
            provenance=getattr(session, "provenance", {}),
            tag="[CU-SAVE]",
        )
        print(f"[CU-SAVE] Saved conversation for {operator} (minted={turn.minted})")
    except Exception as e:
        import traceback
        print(f"[CU-SAVE] Error saving: {e}\n{traceback.format_exc()}")


async def persist_local_turn_and_mint(
    operator: str, prompt: str, final_response: str,
    tool_transcript: list | None = None, provenance: dict | None = None,
) -> dict:
    """Persist a completed on-device (local provider) turn and auto-mint it.

    Composes the snapshot body SERVER-SIDE from the phone-supplied turn (the
    on-device 4B never authors snapshot content), then records it through the
    persist + mint tail every chat path shares. Returns {"snap_id": str|None,
    "minted": bool, "checkpoint_triggered": bool}.
    """
    try:
        # --- Compose the assistant response SERVER-SIDE -------------------
        # Start with the model's final answer; if the phone reported tool use,
        # append a compact, bounded provenance block so the snapshot records
        # what the turn actually touched (the 4B never authors this).
        # Phase 0 (0b-ii): defensive unwrap in case the on-device model leaked a
        # reply envelope onto its final text; store the clean reply, not raw JSON.
        final_response, _ = unwrap_reply_envelope(final_response)
        assistant_response = final_response
        if tool_transcript:
            lines = ["", "[TOOLS USED]"]
            for call in tool_transcript:
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name", "tool"))
                args = call.get("args", {})
                args_str = json.dumps(args, default=str) if isinstance(args, (dict, list)) else str(args)
                if len(args_str) > 200:
                    args_str = args_str[:200] + "..."
                result_str = str(call.get("result", ""))
                if len(result_str) > 200:
                    result_str = result_str[:200] + "..."
                lines.append(f"- {name}({args_str}) -> {result_str}")
            assistant_response = final_response + "\n" + "\n".join(lines)

        # Token counts are 0 for an on-device turn; the model is the on-device provider.
        turn = await persist_turn_and_maybe_mint(
            get_state(operator), operator, prompt, assistant_response, gate=_mint_gate(),
            model="local", provenance=provenance, tag="[LOCAL-SAVE]",
        )
        print(f"[LOCAL-SAVE] Saved on-device turn for {operator} (minted={turn.minted})")
        return {"snap_id": turn.snap_id, "minted": turn.minted,
                "checkpoint_triggered": turn.checkpoint_triggered}
    except Exception as e:
        import traceback
        print(f"[LOCAL-SAVE] Error saving: {e}\n{traceback.format_exc()}")
        return {"snap_id": None, "minted": False, "checkpoint_triggered": False, "error": str(e)}


@app.post("/chat/save")
async def chat_save(request: Request):
    """Save a streaming conversation turn and trigger auto-mint.
    Called by frontend after /chat/stream completes to persist the conversation
    and create snapshots. This endpoint does NOT call any AI - it just saves.
    Body: {
        operator: string,
        user_message: string,
        assistant_response: string,
        reasoning: string (optional) - AI thinking/reasoning process,
        model: string (optional),
        tokens: { prompt: int, completion: int } (optional),
        carry_location: bool (optional, default true) - false for a background
            snapshot (the worker's CU / CLI results), which must not take the
            location a phone's /chat/stream carried for its own save
    }
    """
    try:
        body = await request.json()
        # Checked before anything is written: the operator names the artifact
        # files and the ledger's OPERATOR: line.
        try:
            operator = _checked_operator(body.get("operator"))
        except InvalidOperatorName as e:
            return JSONResponse(status_code=400, content={"error": f"Invalid operator: {e}"})
        user_message = body.get("user_message", "")
        assistant_response = body.get("assistant_response", "")
        reasoning = body.get("reasoning", "")
        provenance = body.get("provenance", {})  # Fossil retrieval provenance
        model = body.get("model", "")
        # `or {}` (not just a default): a tool-call turn can POST "tokens": null
        # explicitly, and body.get("tokens", {}) returns None for a present-but-null
        # key -> later tokens.get(...) crashed the save (AttributeError on NoneType).
        tokens = body.get("tokens") or {}

        if not assistant_response:
            return JSONResponse(status_code=400, content={"error": "assistant_response required"})

        # Debug: log incoming response
        print(f"[CHAT/SAVE] Received response ({len(assistant_response)} chars)")
        print(f"[CHAT/SAVE] Response preview: {assistant_response[:500]}...")

        # Get operator state
        s = get_state(operator)

        # Track queued media tasks to return to frontend
        media_tasks = []

        # OLD TEXT-PARSING METHOD REMOVED - Now using tool calling (gemini_image/openai_image/grok_image, generate_video, lyria_music tools)
        # Images, videos, and music are now generated via proper tool calls handled in streaming endpoints
        # Tool definitions generated from Orchestrator/tools/tool_registry.py

        # Process artifact generation commands
        has_artifacts = False
        artifacts_meta = []  # structured list for native clients (Android Phase 6b)
        if '[ARTIFACT:' in assistant_response:
            print(f"[CHAT/SAVE] Detected artifact generation command")
            assistant_response, artifacts_meta = parse_and_process_artifacts_with_meta(assistant_response, operator)
            has_artifacts = True

        # Phase 0 (0a): defensive unwrap of a leaked reply envelope. If a model
        # leaked {"ui_reply": ..., "snapshot_perspective": ...} onto the streamed
        # text channel, store the clean reply in BOTH the snapshot and the turn,
        # never raw JSON. Total function; never a parse-error sentinel.
        assistant_response_unwrapped, _persp = unwrap_reply_envelope(assistant_response)

        # Build snap_text with reasoning (if provided) + response
        # This goes into the snapshot's Raw Session Log
        if reasoning:
            snap_text = f"[REASONING]\n{reasoning}\n\n[RESPONSE]\n{assistant_response_unwrapped}"
        else:
            snap_text = assistant_response_unwrapped

        # M1 location ride-along. The turn's location is whatever the client
        # sent here, else the one carried from this turn's /chat/stream request
        # (popped once). Appending it to `user_message` is what puts the line in
        # the ledger: perform_mint renders conversation_log verbatim. Fully
        # guarded — a location can never fail a save/mint. Absent -> unchanged
        # text and an empty gauge, byte-identical to today.
        turn_location = None
        try:
            # A background snapshot (carry_location false: a Computer Use or
            # CLI-agent result the worker records) is not the turn the slot was
            # carried for; popping it there took the phone's fix from the
            # phone's own save, landing a moment later.
            background = body.get("carry_location", True) is False
            carried = None if background else _take_turn_location(operator)  # always pop, never let one linger
            turn_location = user_location.normalize(body.get("location")) or carried
            if turn_location and user_message:
                user_message = user_location.append_location_to_text(user_message, turn_location)
        except Exception:
            turn_location = None

        # The persist + auto-mint tail every chat path shares (audit rank 74):
        # the turn into the conversation log perform_mint reads, the counters,
        # the model / usage / provenance metadata, the mint gate, the checkpoint
        # cadence. The LOCATION gauge (perform_mint reads it into `gauges`) is set
        # UNCONDITIONALLY — "" for a locationless turn, so a phone turn's fix can
        # never bleed into the next Portal turn's snapshot.
        turn = await persist_turn_and_maybe_mint(
            s, operator, user_message, assistant_response_unwrapped, gate=_mint_gate(),
            snap_text=snap_text, prompt_tokens=tokens.get("prompt", 0),
            completion_tokens=tokens.get("completion", 0), model=model,
            location_gauge=user_location.format_location_gauge(turn_location),
            provenance=provenance, tag="[CHAT/SAVE]",
        )

        response = {
            "success": True,
            "operator": operator,
            "total_turns": s.total_turns,
            "conv_turns_since": s.conv_turns_since,
            "minted": turn.minted,
            "snap_id": turn.snap_id,
            "media_tasks": media_tasks,  # List of queued image/video/music generation tasks
            "modified_response": assistant_response if (media_tasks or has_artifacts) else None,  # Response with loading placeholders or artifact download buttons
            "artifacts": artifacts_meta,  # Structured artifacts[] for native clients (Phase 6a); [] when none
            "checkpoint_triggered": turn.checkpoint_triggered
        }
        # Task 1.4: the embedding dimension of the just-minted snapshot, so the
        # frontend's mint telemetry line can show "Nd". Present only when a
        # vector was actually generated (omitted on a vector-less / no-mint turn).
        if turn.dims is not None:
            response["dims"] = turn.dims
        return response

    except Exception as e:
        import traceback
        print(f"[CHAT/SAVE] Error: {e}\n{traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": str(e)})
