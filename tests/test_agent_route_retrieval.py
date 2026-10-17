"""Integration tests for the agent routes' fossil retrieval injection.

Verifies that the three agent dispatch paths now:

* `/ws/agent/{session_id}` (Claude Code CLI agent)        — Plan Task 4
* `/ws/gemini-agent/{session_id}` (Gemini CLI agent)      — Plan Task 4
* `POST /gemini-cu/run` & `/stream` (Gemini Computer Use) — Plan Task 4

…all call `build_fossil_context(operator=..., user_text=...)` and emit a
`{"type":"provenance","data":{...}}` event back to the client at session start
(WS) or as the first SSE event (REST).

These tests probe the **live service** on port 9091. If the service is not
running they will skip with a clear message rather than hard-fail.
"""
from __future__ import annotations

import asyncio
import json
import socket

import pytest


def _service_up(host: str = "127.0.0.1", port: int = 9091, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.live_service


@pytest.fixture(autouse=True)
def _require_service():
    # Probed per test, not at import, so collecting this module opens no socket.
    if not _service_up():
        pytest.skip("Orchestrator service is not running on localhost:9091 — skip live WS probes")


def _import_websockets():
    try:
        import websockets
        return websockets
    except ImportError:
        pytest.skip("websockets package not installed in this venv")


async def _drain_for_provenance(ws, timeout_total: float = 25.0) -> dict | None:
    """Pull frames until a `{"type":"provenance"}` arrives or we time out."""
    deadline = asyncio.get_event_loop().time() + timeout_total
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(5.0, remaining))
        except asyncio.TimeoutError:
            return None
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if msg.get("type") == "provenance":
            return msg.get("data") or {}


@pytest.mark.asyncio
async def test_agent_ws_session_start_emits_provenance():
    """Plan Task 4 Step 1: agent WS must emit provenance on first prompt."""
    websockets = _import_websockets()
    uri = "ws://localhost:9091/ws/agent/test-session-plan-task4-claude"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({
            "type": "prompt",
            "operator": "Brandon",
            "text": "tool vault recent work",
            "model": "sonnet",
            "skip_permissions": True,
        }))
        prov = await _drain_for_provenance(ws)
    assert prov is not None, "agent WS never emitted provenance event"
    assert set(prov) == {"recent", "keyword", "semantic", "checkpoint"}, \
        f"provenance keys mismatch: {set(prov)}"
    assert len(prov["recent"]) >= 1, "Brandon should have recent snapshots"


@pytest.mark.asyncio
async def test_gemini_agent_ws_session_start_emits_provenance():
    """Plan Task 4 Step 1 (gemini-agent variant)."""
    websockets = _import_websockets()
    uri = "ws://localhost:9091/ws/gemini-agent/test-session-plan-task4-gemini"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({
            "type": "prompt",
            "operator": "Brandon",
            "text": "tool vault recent work",
            "yolo_mode": True,
        }))
        prov = await _drain_for_provenance(ws)
    assert prov is not None, "gemini-agent WS never emitted provenance event"
    assert set(prov) == {"recent", "keyword", "semantic", "checkpoint"}
    assert len(prov["recent"]) >= 1, "Brandon should have recent snapshots"


@pytest.mark.asyncio
async def test_agent_ws_unknown_operator_emits_empty_provenance():
    """Per-operator scoping: an unknown operator yields empty retrieval."""
    websockets = _import_websockets()
    uri = "ws://localhost:9091/ws/agent/test-session-plan-task4-empty"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({
            "type": "prompt",
            "operator": "nonexistent-test-user-xyz-9999",
            "text": "irrelevant query",
            "model": "sonnet",
            "skip_permissions": True,
        }))
        prov = await _drain_for_provenance(ws)
    assert prov is not None, "agent WS never emitted provenance even for empty operator"
    assert set(prov) == {"recent", "keyword", "semantic", "checkpoint"}
    # Unknown operators must not leak any other operator's snapshots
    assert prov["recent"] == [], f"unexpected recent for unknown operator: {prov['recent']}"
    assert prov["keyword"] == [], f"unexpected keyword for unknown operator: {prov['keyword']}"
    assert prov["semantic"] == [], f"unexpected semantic for unknown operator: {prov['semantic']}"
    assert prov["checkpoint"] == [], f"unexpected checkpoint for unknown operator: {prov['checkpoint']}"


# -----------------------------------------------------------------------------
# Gemini CU is REST/SSE, not WS — separate probe.
# -----------------------------------------------------------------------------

def test_gemini_cu_stream_emits_provenance():
    """Plan Task 4 Step 1 (gemini-cu variant): SSE stream must include provenance.

    `/gemini-cu/stream` is the SSE entry point. We do NOT need to actually
    drive the CU agent to completion — we only need to confirm a provenance
    event is the first (or among the first) SSE frames yielded.
    """
    httpx = pytest.importorskip("httpx")

    body = {
        "prompt": "tool vault recent work",
        "operator": "Brandon",
        "device_id": "blackbox",
    }
    saw_provenance = False
    try:
        with httpx.stream(
            "POST", "http://localhost:9091/gemini-cu/stream",
            json=body, timeout=10,
        ) as resp:
            if resp.status_code != 200:
                pytest.skip(
                    f"/gemini-cu/stream returned {resp.status_code} "
                    "— device may not be available; skipping live probe"
                )
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                if event.get("type") == "provenance":
                    saw_provenance = True
                    prov = event.get("data") or {}
                    assert set(prov) == {"recent", "keyword", "semantic", "checkpoint"}
                    assert len(prov["recent"]) >= 1
                    break
    except httpx.ConnectError:
        pytest.skip("Cannot connect to localhost:9091")

    # Clean up the session so the CU agent doesn't keep ticking after we
    # close the stream — otherwise it can starve subsequent WS tests.
    try:
        httpx.delete("http://localhost:9091/gemini-cu/session/Brandon", timeout=5)
    except Exception:
        pass

    assert saw_provenance, "/gemini-cu/stream never yielded a provenance event"
