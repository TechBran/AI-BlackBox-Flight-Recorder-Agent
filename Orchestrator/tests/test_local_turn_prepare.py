#!/usr/bin/env python3
"""Tests for POST /local/turn/prepare — per-turn context assembly.

This endpoint is the first leg of the server-bracketed on-device turn: the phone
POSTs the user prompt + operator; the BlackBox assembles a LEAN per-turn context
package (persona + fossils + injected tools) and returns it; the phone then runs
the on-device Gemma model locally on that package.

The three building blocks (build_fossil_context, build_injected_tools,
get_persona) are patched in the local_routes namespace so these tests
exercise ONLY the handler's assembly/validation logic. (get_persona is patched
defensively even though the lean local prepare path no longer injects persona —
the patch keeps the namespace honest if that ever changes.)
"""

from unittest import mock

# Importing local_routes registers the route on the shared FastAPI `app`.
import Orchestrator.routes.local_routes  # noqa: F401
from Orchestrator.routes.local_routes import LOCAL_TOOL_CALLER_SYSTEM_PROMPT
from Orchestrator.checkpoint import app
from fastapi.testclient import TestClient

client = TestClient(app)

_FOSSIL_RV = ("FOSSIL", {"semantic": ["SNAP-1"], "checkpoint": ["SNAP-CP"],
                         "recent": [], "keyword": []})
_TOOLS_RV = [{"name": "roll_dice", "description": "d", "parameters": {"type": "object"}}]


def test_prepare_happy_path():
    """Test A: valid request assembles persona + fossil + tools + budget."""
    with mock.patch("Orchestrator.routes.local_routes.build_fossil_context",
                    return_value=_FOSSIL_RV), \
         mock.patch("Orchestrator.routes.local_routes.build_injected_tools",
                    return_value=_TOOLS_RV), \
         mock.patch("Orchestrator.routes.local_routes.get_persona",
                    return_value="PERSONA"):
        resp = client.post("/local/turn/prepare",
                           json={"prompt": "roll dice", "operator": "Brandon"})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True
    # The heavy behavioral_core persona is intentionally NOT injected for the
    # lean on-device tool-caller — only the minimal tool-caller instruction.
    assert "PERSONA" not in data["system_prompt"]
    assert data["system_prompt"].startswith(LOCAL_TOOL_CALLER_SYSTEM_PROMPT)
    assert "FOSSIL" in data["system_prompt"]
    assert data["tools"][0]["name"] == "roll_dice"
    assert data["provenance"]["checkpoint"] == ["SNAP-CP"]
    assert data["provenance"]["semantic"] == ["SNAP-1"]
    assert data["budget"]["cap_chars"] == 16000
    assert data["budget"]["package_chars"] == len(data["system_prompt"])
    assert data["turn_id"]  # non-empty


def test_prepare_blank_operator_400():
    """Test B: blank operator -> 400."""
    with mock.patch("Orchestrator.routes.local_routes.build_fossil_context",
                    return_value=_FOSSIL_RV), \
         mock.patch("Orchestrator.routes.local_routes.build_injected_tools",
                    return_value=_TOOLS_RV), \
         mock.patch("Orchestrator.routes.local_routes.get_persona",
                    return_value="PERSONA"):
        resp = client.post("/local/turn/prepare",
                           json={"prompt": "x", "operator": "  "})

    assert resp.status_code == 400
    assert resp.json()["success"] is False
    assert resp.json()["error"] == "operator required"


def test_prepare_operator_passthrough():
    """Test C: operator + lean-local kwargs are passed straight through."""
    with mock.patch("Orchestrator.routes.local_routes.build_fossil_context",
                    return_value=_FOSSIL_RV) as m_fossil, \
         mock.patch("Orchestrator.routes.local_routes.build_injected_tools",
                    return_value=_TOOLS_RV), \
         mock.patch("Orchestrator.routes.local_routes.get_persona",
                    return_value="PERSONA"):
        resp = client.post("/local/turn/prepare",
                           json={"prompt": "roll dice", "operator": "Brandon"})

    assert resp.status_code == 200, resp.text
    _, kwargs = m_fossil.call_args
    assert kwargs["operator"] == "Brandon"
    assert kwargs["provider"] == "local"
    assert kwargs["semantic_k"] == 0
    assert kwargs["checkpoint_count"] == 0
    assert kwargs["include_recent"] is False
    assert kwargs["include_keyword"] is False


def test_prepare_local_is_lean_no_fossil():
    """Lean LOCAL profile: with local_semantic_k=0 AND local_checkpoint_count=0
    configured (see [context] in config.ini), the on-device prepare pushes NO
    fossil context. build_fossil_context is driven with semantic_k=0 /
    checkpoint_count=0 (which yields an empty fossil block); the response carries
    an EMPTY fossil/context block but STILL includes the persona/system prompt and
    the injected tools - the model now builds context conversationally + pulls
    memory via tools on demand instead of receiving a pushed package.
    """
    # build_fossil_context returns ("", {empty provenance}) when the lean (0/0)
    # kwargs are in effect - mirror that here so the assembly logic is exercised.
    empty_fossil = ("", {"semantic": [], "checkpoint": [], "recent": [], "keyword": []})
    with mock.patch("Orchestrator.routes.local_routes.build_fossil_context",
                    return_value=empty_fossil) as m_fossil, \
         mock.patch("Orchestrator.routes.local_routes.build_injected_tools",
                    return_value=_TOOLS_RV), \
         mock.patch("Orchestrator.routes.local_routes.get_persona",
                    return_value="PERSONA"):
        resp = client.post("/local/turn/prepare",
                           json={"prompt": "roll dice", "operator": "Brandon"})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True

    # Lean config drove build_fossil_context with NO semantic/checkpoint fossils.
    _, kwargs = m_fossil.call_args
    assert kwargs["semantic_k"] == 0, "config [context].local_semantic_k must be 0"
    assert kwargs["checkpoint_count"] == 0, "config [context].local_checkpoint_count must be 0"

    # The minimal tool-caller system prompt is present (the heavy persona is NOT)...
    assert "PERSONA" not in data["system_prompt"]
    # ...and NO fossil/context block was pushed (system_prompt is the minimal
    # tool-caller instruction only) and provenance carries no snapshots.
    assert data["system_prompt"] == LOCAL_TOOL_CALLER_SYSTEM_PROMPT
    assert data["provenance"]["semantic"] == []
    assert data["provenance"]["checkpoint"] == []

    # Tools are still injected - the model pulls memory via tools on demand.
    assert data["tools"][0]["name"] == "roll_dice"

def test_prepare_local_drops_persona():
    """The on-device tool-caller does NOT receive the heavy behavioral_core
    persona. Even when get_persona would return a long persona sentinel,
    the prepare response's system_prompt must NOT contain it (persona is no
    longer injected for the lean local path) and must instead be the short
    tool-caller instruction. This frees ~1000 tokens in the phone's window.
    """
    PERSONA_SENTINEL = (
        "BEHAVIORAL_CORE_PERSONA_SENTINEL " + ("blah " * 400)
    )  # ~2000 chars — stands in for the real ~4244-char persona.
    empty_fossil = ("", {"semantic": [], "checkpoint": [], "recent": [], "keyword": []})
    with mock.patch("Orchestrator.routes.local_routes.build_fossil_context",
                    return_value=empty_fossil), \
         mock.patch("Orchestrator.routes.local_routes.build_injected_tools",
                    return_value=_TOOLS_RV), \
         mock.patch("Orchestrator.routes.local_routes.get_persona",
                    return_value=PERSONA_SENTINEL):
        resp = client.post("/local/turn/prepare",
                           json={"prompt": "roll dice", "operator": "Brandon"})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True

    # The persona sentinel must NOT appear — persona is no longer injected.
    assert "BEHAVIORAL_CORE_PERSONA_SENTINEL" not in data["system_prompt"]
    # The system prompt IS the minimal tool-caller instruction (short).
    assert data["system_prompt"] == LOCAL_TOOL_CALLER_SYSTEM_PROMPT
    # "Short" = an order of magnitude under the ~4,244-char behavioral_core
    # persona this prompt replaced (the prompt is currently exactly 400 chars;
    # the old `< 400` bound was an off-by-one against its own fixture).
    assert len(data["system_prompt"]) < 600, "tool-caller prompt should be short"
    # Tools are still injected — the model carries out the request via tools.
    assert data["tools"][0]["name"] == "roll_dice"
