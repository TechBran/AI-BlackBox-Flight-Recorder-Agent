"""
Tests for M4.1b — Auto (empty model) resolves to the provider's configured
default at fire time, and the provider sent to /chat stays correct.

A job now stores an explicit `provider` alongside its (possibly empty) model.
When the model is empty/whitespace ("Auto"), execute_cron_job must resolve it
to THAT provider's *_MODEL_DEFAULT — not guess from the empty model string —
while a specific id is sent verbatim.

We mock the actual HTTP POST to /chat (no live server) and assert the payload
the executor would send.
"""

import asyncio

import pytest

from Orchestrator.scheduler import executor as executor_mod
from Orchestrator.config import (
    ANTHROPIC_MODEL_DEFAULT,
    OPENAI_MODEL_DEFAULT,
    GEMINI_MODEL_DEFAULT,
    XAI_MODEL_DEFAULT,
)


# ---------------------------------------------------------------------------
# Capture the /chat payload without a live server.
# ---------------------------------------------------------------------------

class _FakeResp:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return {"task_id": "task_fake_123"}

    async def text(self):
        return ""


class _FakeSession:
    """Records the JSON body of the POST to /chat, then short-circuits the
    rest of execute_cron_job by handing back a fake task id."""

    captured = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, timeout=None):
        _FakeSession.captured["url"] = url
        _FakeSession.captured["json"] = json
        return _FakeResp()


@pytest.fixture()
def capture_chat(monkeypatch):
    _FakeSession.captured = {}
    monkeypatch.setattr(executor_mod.aiohttp, "ClientSession", _FakeSession)

    # Short-circuit the polling so the test ends right after the POST.
    async def fake_poll(task_id, job_name):
        return "ok"

    monkeypatch.setattr(executor_mod, "_poll_task_until_done", fake_poll)
    return _FakeSession


def _run(job):
    return asyncio.get_event_loop().run_until_complete(
        executor_mod.execute_cron_job(job)
    )


def _base_job(**over):
    job = {
        "id": "cron_x",
        "name": "t",
        "prompt": "hello",
        "operator": "system",
        "delivery": "snapshot",
        "delivery_target": "",
    }
    job.update(over)
    return job


# ---------------------------------------------------------------------------
# Auto (empty model) → provider default
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_anthropic_resolves_to_anthropic_default(capture_chat):
    job = _base_job(provider="anthropic", model="")
    await executor_mod.execute_cron_job(job)
    payload = capture_chat.captured["json"]
    assert payload["provider"] == "anthropic"
    assert payload["model"] == ANTHROPIC_MODEL_DEFAULT


@pytest.mark.asyncio
async def test_auto_openai_resolves_to_openai_default(capture_chat):
    job = _base_job(provider="openai", model="")
    await executor_mod.execute_cron_job(job)
    payload = capture_chat.captured["json"]
    assert payload["provider"] == "openai"
    assert payload["model"] == OPENAI_MODEL_DEFAULT


@pytest.mark.asyncio
async def test_auto_whitespace_model_resolves_to_default(capture_chat):
    """A whitespace-only model counts as Auto."""
    job = _base_job(provider="google", model="   ")
    await executor_mod.execute_cron_job(job)
    payload = capture_chat.captured["json"]
    assert payload["provider"] == "google"
    assert payload["model"] == GEMINI_MODEL_DEFAULT


@pytest.mark.asyncio
async def test_auto_xai_resolves_to_xai_default(capture_chat):
    job = _base_job(provider="xai", model="")
    await executor_mod.execute_cron_job(job)
    payload = capture_chat.captured["json"]
    assert payload["provider"] == "xai"
    assert payload["model"] == XAI_MODEL_DEFAULT


# ---------------------------------------------------------------------------
# Specific id → sent verbatim, provider preserved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_specific_id_sent_verbatim(capture_chat):
    job = _base_job(provider="openai", model="gpt-5.1")
    await executor_mod.execute_cron_job(job)
    payload = capture_chat.captured["json"]
    assert payload["provider"] == "openai"
    assert payload["model"] == "gpt-5.1"


@pytest.mark.asyncio
async def test_specific_anthropic_id_sent_verbatim(capture_chat):
    job = _base_job(provider="anthropic", model="claude-opus-4-8")
    await executor_mod.execute_cron_job(job)
    payload = capture_chat.captured["json"]
    assert payload["provider"] == "anthropic"
    assert payload["model"] == "claude-opus-4-8"
