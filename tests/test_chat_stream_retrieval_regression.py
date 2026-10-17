"""Live-service regression test for /chat/stream retrieval.

This test hits the running orchestrator on port 9091 and asserts that the
stream_start SSE event carries a fully-populated provenance object with all
four source buckets. If this test fails, `build_streaming_context` (or the
shared `build_fossil_context` it now delegates to) has regressed.
"""
import httpx
import json

import pytest

pytestmark = pytest.mark.live_service


def test_chat_stream_emits_populated_provenance():
    body = {
        "messages": [{"role": "user", "content": "regression test query tool vault"}],
        "operator": "Brandon",
        "provider": "gemini",
    }
    with httpx.stream(
        "POST", "http://localhost:9091/chat/stream",
        json=body, timeout=10,
    ) as resp:
        for line in resp.iter_lines():
            if line.startswith("data:") and '"provenance"' in line:
                payload = json.loads(line[5:].strip())
                prov = payload["provenance"]
                assert len(prov["recent"]) >= 1
                assert set(prov) == {"recent", "keyword", "semantic", "checkpoint"}
                return
        raise AssertionError("no stream_start event with provenance received")
