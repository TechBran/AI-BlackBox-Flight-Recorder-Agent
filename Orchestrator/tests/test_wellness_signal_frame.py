"""The UI-only Signal frame (spec §4.3): when a wellness tool answers on a
stream, the client gets {"type": "wellness_used", "ref", "categories", "as_of",
"provider"}: "Health data used · sleep, heart · as of 07:10 · shared with
Anthropic". It carries no value and never reaches a prompt or the ledger (the
system_activity precedent): it is an SSE event, not part of the tool result."""
from __future__ import annotations

import asyncio
import json

import pytest

from Orchestrator.tests.wellness_agent_support import (  # noqa: F401 - agent_box is a fixture
    FX, LAN_ALIAS, LAN_MODEL, agent_box, custom_server, edit_record, sentinel_hits, stamped)
from Orchestrator.tests.wellness_support import OPERATOR

# Each stream loop with a turn stamped for its own provider (a health tool
# runs only in the loop the turn was stamped for): (provider, model, the
# frame's provider id, its label).
STREAMS = {"stream_anthropic": ("anthropic", None, "anthropic", "Anthropic"),
           "stream_gemini": ("google", None, "google", "Google"),
           "stream_openai": ("openai", None, "openai", "OpenAI"),
           "stream_xai": ("xai", None, "xai", "xAI"),
           "stream_custom": ("custom", LAN_MODEL, f"custom:{LAN_ALIAS}", LAN_ALIAS)}


def _allow_every_provider():
    custom_server()
    edit_record(providers_allowed=["anthropic", "google", "openai", "xai"],
                google_paid_key_confirmed=True)


def _dispatch(loop: str, name="wellness_summary", args=None):
    from Orchestrator.providers import tool_dispatch as td
    from Orchestrator.routes.chat.providers import _tool_context
    return asyncio.run(td.dispatch_tool(name, dict(args or {"period": "last_7_days"}),
                                        _tool_context(OPERATOR, loop)))


def _frames(outcome):
    return [e for e in outcome.events if e.get("type") == "wellness_used"]


@pytest.mark.parametrize("loop", sorted(STREAMS))
def test_a_stream_gets_the_frame(agent_box, loop):
    provider, model, dest_id, dest_label = STREAMS[loop]
    _allow_every_provider()
    with stamped(provider=provider, model=model or "test-model") as t:
        outcome = _dispatch(loop)
    rows = agent_box["store"].deliveries_for_turn(t.turn_key)
    [frame] = _frames(outcome)
    data = frame["data"]
    assert set(data) == {"ref", "categories", "as_of", "provider", "provider_label"}
    assert data["ref"] == rows[0]["ref"] and data["categories"] == ["sleep", "heart", "activity"]
    assert data["as_of"].startswith(FX.day_of(0))
    assert (data["provider"], data["provider_label"]) == (dest_id, dest_label)
    assert not sentinel_hits(json.dumps(frame))
    # The frame is not what the model reads back.
    assert "wellness_used" not in outcome.result
    # The client's tool label carries no value either.
    [label] = [e for e in outcome.events if e.get("type") == "tool_result"]
    assert label["data"] == "wellness_summary: health data read"


def test_a_local_server_is_named_in_the_frame(agent_box):
    custom_server()
    with stamped(provider="custom", model=LAN_MODEL):
        [frame] = _frames(_dispatch("stream_custom"))
    assert frame["data"]["provider"] == f"custom:{LAN_ALIAS}"
    assert frame["data"]["provider_label"] == LAN_ALIAS


def test_label_order_follows_the_loop_profile(agent_box):
    _allow_every_provider()
    with stamped(provider="openai"):
        first = _dispatch("stream_openai").events       # label_first
    with stamped():
        last = _dispatch("stream_anthropic").events
    assert [e["type"] for e in first] == ["tool_result", "wellness_used"]
    assert [e["type"] for e in last] == ["wellness_used", "tool_result"]


def test_no_frame_off_a_stream_or_for_a_refusal(agent_box):
    with stamped():
        assert _dispatch("call_anthropic").events == []
        refused = _dispatch("stream_anthropic", args={"period": "today",
                                                      "categories": ["blood_pressure"]})
    assert not _frames(refused)
    with stamped(provider="openai"):
        assert not _frames(_dispatch("stream_openai"))


def test_the_frame_serializes_as_its_own_sse_event(agent_box):
    from Orchestrator.routes.chat.stream import _sse_frames
    with stamped():
        [frame] = _frames(_dispatch("stream_anthropic"))
    [sse] = _sse_frames(frame)
    assert sse.startswith("event: wellness_used\ndata: ")
    assert json.loads(sse.split("data: ", 1)[1]) == frame["data"]
