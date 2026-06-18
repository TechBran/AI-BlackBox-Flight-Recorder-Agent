#!/usr/bin/env python3
"""Tests for POST /local/turn/complete - server-composed mint + provenance.

Second leg of the server-bracketed on-device turn: after the phone runs the
on-device Gemma model locally (on the package from /local/turn/prepare), it POSTs
the completed turn back; the BlackBox composes the snapshot body SERVER-SIDE (the
4B never authors it), persists it, and AUTO-MINTS it (inline embedding) so it is
instantly recallable.

perform_mint is patched in the chat_routes namespace so no real embedding runs;
the conversation-log append lands in in-memory operator state. We patch
chat_routes.TURNS_THRESHOLD = 1 + AUTO_ENABLE = True so a single on-device turn
mints deterministically (mirroring how a real single /chat/save mints under the
prod turns_threshold=1 config).
"""

from unittest import mock

# Importing local_routes registers the route on the shared FastAPI `app`.
import Orchestrator.routes.local_routes  # noqa: F401
import Orchestrator.routes.chat_routes as chat_routes
from Orchestrator.checkpoint import app
from Orchestrator.state import get_state
from fastapi.testclient import TestClient

client = TestClient(app)

_OPERATOR = "Brandon-LOCAL-TEST"


def _conv_log_text(operator):
    """Concatenate all persisted snap_text/text from the operator's conv log."""
    s = get_state(operator)
    log = getattr(s, "conversation_log", []) or []
    return "\n".join((t.get("snap_text") or t.get("text") or "") for t in log)


def test_complete_composes_and_mints():
    """Test A: a complete payload persists a server-composed turn + auto-mints."""
    with mock.patch.object(chat_routes, "perform_mint",
                           return_value={"snap_id": "SNAP-LOCAL-1"}), \
         mock.patch.object(chat_routes, "AUTO_ENABLE", True), \
         mock.patch.object(chat_routes, "TURNS_THRESHOLD", 1), \
         mock.patch.object(chat_routes, "DEBOUNCE_MS", 0), \
         mock.patch.object(chat_routes, "should_create_checkpoint",
                           return_value=False):
        resp = client.post("/local/turn/complete", json={
            "turn_id": "t-1",
            "operator": _OPERATOR,
            "prompt": "roll a die",
            "final_response": "I rolled a 3 for you.",
            "tool_transcript": [
                {"name": "roll_dice", "args": {"sides": 6},
                 "result": "Rolled 1d6: [3]"}
            ],
        })

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True
    assert data["snap_id"] == "SNAP-LOCAL-1"
    assert data["checkpoint_triggered"] is False

    # The persisted assistant turn must contain BOTH the final_response and the
    # server-composed provenance block (the tool that was used).
    persisted = _conv_log_text(_OPERATOR)
    assert "I rolled a 3 for you." in persisted
    assert "roll_dice" in persisted


def test_complete_blank_operator_400():
    """Test B (part 1): blank operator -> 400."""
    resp = client.post("/local/turn/complete", json={
        "turn_id": "t-2",
        "operator": "  ",
        "final_response": "hi",
    })
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"] == "operator required"


def test_complete_missing_final_response_400():
    """Test B (part 2): missing/blank final_response -> 400."""
    resp = client.post("/local/turn/complete", json={
        "turn_id": "t-3",
        "operator": _OPERATOR,
        "prompt": "hello",
    })
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"] == "final_response required"
