"""Voice tool calls survive stringified arguments (audit O09-live-voice-providers-14).

Models stringify numbers, arrays and booleans in tool calls (a known quirk). The
voice tool ladders read raw arguments, so ``min(args.get("k", 3), 5)`` raised
TypeError on k="2" and the model heard that its memory search failed;
``sample_count > 1`` and ``img_count > 1`` did the same for music and images,
and a stringified reference_images list counted its characters as images. The
one shared ladder normalises the arguments against the tool's schema first,
for every provider.
"""
import asyncio
import json

import pytest

from Orchestrator import models
from Orchestrator.tests import voice_golden_harness as harness

PROVIDERS = ["gemini", "openai", "grok"]


def _call(provider: str, name: str, args: dict) -> str:
    """Run one tool call through the provider's handler; the answer text."""
    async def run():
        patches, cap = harness.Patches(), harness.Capture()
        try:
            harness.install(patches, cap)
            spec = harness._spec(provider)
            ws_attr = {"gemini": "gemini_ws", "grok": "grok_ws", "openai": "openai_ws"}[provider]
            cls = {"gemini": models.GeminiLiveSession, "grok": models.GrokLiveSession,
                   "openai": models.RealtimeSession}[provider]
            session = cls(session_id=f"coerce-{provider}", operator=harness.OPERATOR)
            session.portal_ws = harness.FakeClient()
            upstream = harness.ScriptedUpstream()
            setattr(session, ws_attr, upstream)
            if provider == "gemini":
                await spec.mod.handle_gemini_message(session, {"toolCall": {"functionCalls": [
                    {"id": "fc-1", "name": name, "args": args}]}})
            else:
                handle = spec.mod.handle_grok_message if provider == "grok" else spec.mod.handle_openai_message
                await handle(session, {"type": "response.function_call_arguments.done",
                                       "call_id": "call-1", "name": name,
                                       "arguments": json.dumps(args)})
            await harness.settle(session.portal_ws, cap)
            frame = upstream.sent[0]
            if provider == "gemini":
                return frame["toolResponse"]["functionResponses"][0]["response"]["result"], cap
            return frame["item"]["output"], cap
        finally:
            patches.undo()

    return asyncio.run(run())


@pytest.mark.parametrize("provider", PROVIDERS)
def test_stringified_k_still_searches(provider):
    out, _ = _call(provider, "search_snapshots", {"query": "ann", "k": "2"})
    assert "failed" not in out, out
    assert out.startswith("Found 2 relevant snapshot(s) for: ann")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_stringified_count_still_lists_recent(provider):
    out, _ = _call(provider, "list_recent_snapshots", {"count": "1"})
    assert "failed" not in out, out
    assert out.startswith("Found 1 recent snapshot(s)")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_stringified_sample_count_starts_the_music(provider):
    out, cap = _call(provider, "lyria_music", {"prompt": "calm piano", "sampleCount": "2"})
    assert "failed" not in out, out
    assert "(2 variations)" in out
    assert cap.tasks[0]["result_data"]["sample_count"] == 2


@pytest.mark.parametrize("provider", PROVIDERS)
def test_stringified_image_args_start_the_right_images(provider):
    out, cap = _call(provider, "gemini_image", {
        "prompt": "a red barn", "numberOfImages": "2",
        "reference_images": "[\"/ui/uploads/a.png\"]"})
    assert "failed" not in out, out
    assert "(using 1 reference image)" in out and "Creating 2 images" in out
    options = cap.tasks[0]["result_data"]["options"]
    assert options["numberOfImages"] == 2
    assert options["reference_images"] == ["/ui/uploads/a.png"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_garbage_k_falls_back_to_the_default(provider):
    out, _ = _call(provider, "search_snapshots", {"query": "ann", "k": "lots"})
    assert out.startswith("Found 3 relevant snapshot(s) for: ann"), out


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("value", [2, "2"])
def test_the_declared_limit_sets_the_search_size(provider, value):
    # ToolVault declares search_snapshots {query, operator, limit}; the voice
    # ladder read only `k`, so a model following the schema always got 3.
    out, _ = _call(provider, "search_snapshots", {"query": "ann", "limit": value})
    assert out.startswith("Found 2 relevant snapshot(s) for: ann"), out


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_search_size_stays_capped_at_five(provider):
    out, _ = _call(provider, "search_snapshots", {"query": "ann", "limit": "50"})
    assert out.startswith("Found 5 relevant snapshot(s) for: ann"), out


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("args", [{"k": "-1"}, {"limit": "0"}, {"limit": -3}])
def test_a_non_positive_search_size_still_searches(provider, args):
    out, _ = _call(provider, "search_snapshots", {"query": "ann", **args})
    assert out.startswith("Found 1 relevant snapshot(s) for: ann"), out


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("count", ["-2", "0", -1])
def test_a_non_positive_count_lists_one_not_all_but_some(provider, count):
    # matching[:-1] is every snapshot but one: for the system operator, the
    # whole index read off the volume into one tool result.
    out, _ = _call(provider, "list_recent_snapshots", {"count": count})
    assert out.startswith("Found 1 recent snapshot(s)"), out


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_garbage_sample_count_falls_back_to_one(provider):
    out, cap = _call(provider, "lyria_music", {"prompt": "calm piano", "sampleCount": "two"})
    assert out.startswith("Music generation started"), out
    assert "variations" not in out
    assert "sample_count" not in cap.tasks[0]["result_data"]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("value", ["2.5", "many", 0, "-4"])
def test_a_garbage_image_count_falls_back_to_one(provider, value):
    out, cap = _call(provider, "gemini_image", {"prompt": "a red barn", "numberOfImages": value})
    assert out.startswith("Image generation started"), out
    assert "Creating 1 image at" in out
    assert cap.tasks[0]["result_data"]["options"]["numberOfImages"] == 1
