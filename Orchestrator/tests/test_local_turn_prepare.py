#!/usr/bin/env python3
"""Tests for POST /local/turn/prepare — per-turn context assembly.

This endpoint is the first leg of the server-bracketed on-device turn: the phone
POSTs the user prompt + operator; the BlackBox assembles a LEAN per-turn context
package (persona + fossils + injected tools) and returns it; the phone then runs
the on-device Gemma model locally on that package.

The three building blocks (build_fossil_context, build_injected_tools,
get_behavioral_core) are patched in the local_routes namespace so these tests
exercise ONLY the handler's assembly/validation logic.
"""

from unittest import mock

# Importing local_routes registers the route on the shared FastAPI `app`.
import Orchestrator.routes.local_routes  # noqa: F401
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
         mock.patch("Orchestrator.routes.local_routes.get_behavioral_core",
                    return_value="PERSONA"):
        resp = client.post("/local/turn/prepare",
                           json={"prompt": "roll dice", "operator": "Brandon"})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True
    assert "PERSONA" in data["system_prompt"]
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
         mock.patch("Orchestrator.routes.local_routes.get_behavioral_core",
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
         mock.patch("Orchestrator.routes.local_routes.get_behavioral_core",
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
         mock.patch("Orchestrator.routes.local_routes.get_behavioral_core",
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

    # Persona/system prompt is still present...
    assert "PERSONA" in data["system_prompt"]
    # ...but NO fossil/context block was pushed (system_prompt is persona-only,
    # no trailing fossil section) and provenance carries no snapshots.
    assert data["system_prompt"] == "PERSONA"
    assert data["provenance"]["semantic"] == []
    assert data["provenance"]["checkpoint"] == []

    # Tools are still injected - the model pulls memory via tools on demand.
    assert data["tools"][0]["name"] == "roll_dice"
