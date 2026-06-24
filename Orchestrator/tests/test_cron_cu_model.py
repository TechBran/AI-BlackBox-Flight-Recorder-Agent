"""
Tests for M4.1c — honor a chosen computer-use model in _execute_cu_job
(it was hard-pinned to CU_MODEL_DEFAULT and never received the job's model).

Rules:
  * a valid CU model id (passes CU_MODEL_FILTERS) is used in the /chat/stream
    payload verbatim;
  * empty/Auto uses CU_MODEL_DEFAULT;
  * an id that FAILS the capability filters falls back to CU_MODEL_DEFAULT —
    the CU streaming path is never handed an unfilterable id.

Also: _model_to_provider must map CU sub-model ids (a gemini "*computer-use"
id, the openai computer-use ids) to "computer-use" so provider derivation of
a CU job stays correct.

The actual CU SSE stream POST is mocked (no live server); we assert the model
field of the captured payload.
"""

import asyncio

import pytest

from Orchestrator.scheduler import executor as executor_mod
from Orchestrator.config import CU_MODEL_DEFAULT, CU_GEMINI_MODEL_DEFAULT


# ---------------------------------------------------------------------------
# Mock the CU SSE POST and capture its payload.
# ---------------------------------------------------------------------------

class _FakeStreamContent:
    """Yields a single 'done' SSE event then stops."""

    def __aiter__(self):
        async def _gen():
            yield b"event: done\n"
            yield b'data: {"content": "cu ok"}\n'
        return _gen()


class _FakeResp:
    status = 200
    content = _FakeStreamContent()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return ""


class _FakeSession:
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
def capture_cu(monkeypatch):
    _FakeSession.captured = {}
    monkeypatch.setattr(executor_mod.aiohttp, "ClientSession", _FakeSession)
    # Reset the module-level CU lock so each test starts clean.
    monkeypatch.setattr(executor_mod, "_CU_LOCK", None)
    return _FakeSession


# ---------------------------------------------------------------------------
# _execute_cu_job honors / falls back on the chosen model
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_cu_model_used_verbatim(capture_cu):
    """A valid CU id (passes CU_MODEL_FILTERS) is used in the payload."""
    valid = CU_GEMINI_MODEL_DEFAULT  # a real "gemini-...computer-use" id
    await executor_mod._execute_cu_job("job", "do a thing", "system", model=valid)
    assert capture_cu.captured["json"]["model"] == valid
    assert capture_cu.captured["json"]["provider"] == "computer-use"


@pytest.mark.asyncio
async def test_empty_cu_model_falls_back_to_default(capture_cu):
    await executor_mod._execute_cu_job("job", "do a thing", "system", model="")
    assert capture_cu.captured["json"]["model"] == CU_MODEL_DEFAULT


@pytest.mark.asyncio
async def test_unfilterable_cu_model_falls_back_to_default(capture_cu):
    """An id that FAILS the capability filters must NOT be sent — fall back."""
    await executor_mod._execute_cu_job(
        "job", "do a thing", "system", model="totally-not-a-cu-model"
    )
    assert capture_cu.captured["json"]["model"] == CU_MODEL_DEFAULT


@pytest.mark.asyncio
async def test_cu_model_defaults_when_arg_omitted(capture_cu):
    """Back-compat: omitting model keeps the old default behaviour."""
    await executor_mod._execute_cu_job("job", "do a thing", "system")
    assert capture_cu.captured["json"]["model"] == CU_MODEL_DEFAULT


# ---------------------------------------------------------------------------
# execute_cron_job threads the resolved CU model through
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_cron_job_threads_cu_model(capture_cu, monkeypatch):
    """A CU job with a specific CU model id reaches the stream payload."""
    valid = CU_GEMINI_MODEL_DEFAULT
    job = {
        "id": "cron_cu",
        "name": "cu-job",
        "prompt": "navigate",
        "operator": "system",
        "provider": "computer-use",
        "model": valid,
        "delivery": "snapshot",
        "delivery_target": "",
    }
    result = await executor_mod.execute_cron_job(job)
    assert result == "cu ok"
    assert capture_cu.captured["json"]["model"] == valid
    assert capture_cu.captured["json"]["provider"] == "computer-use"


@pytest.mark.asyncio
async def test_execute_cron_job_cu_auto_uses_default(capture_cu):
    """A CU job with empty model (Auto) uses CU_MODEL_DEFAULT in the stream."""
    job = {
        "id": "cron_cu2",
        "name": "cu-auto",
        "prompt": "navigate",
        "operator": "system",
        "provider": "computer-use",
        "model": "",
        "delivery": "snapshot",
        "delivery_target": "",
    }
    await executor_mod.execute_cron_job(job)
    assert capture_cu.captured["json"]["model"] == CU_MODEL_DEFAULT


# ---------------------------------------------------------------------------
# _model_to_provider maps CU sub-model ids to "computer-use"
# ---------------------------------------------------------------------------

def test_model_to_provider_maps_gemini_cu_id():
    assert (
        executor_mod._model_to_provider("gemini-2.5-computer-use-preview-10-2025")
        == "computer-use"
    )


def test_model_to_provider_maps_openai_cu_id():
    assert executor_mod._model_to_provider("computer-use-preview") == "computer-use"


def test_model_to_provider_plain_aliases_still_work():
    assert executor_mod._model_to_provider("computer-use") == "computer-use"
    assert executor_mod._model_to_provider("cu") == "computer-use"
    # A non-CU gemini id is still plain google.
    assert executor_mod._model_to_provider("gemini-3.1-pro-preview") == "google"
    # A non-CU claude id is still anthropic.
    assert executor_mod._model_to_provider("claude-opus-4-8") == "anthropic"


# ---------------------------------------------------------------------------
# The sync-wrapper CU timeout keys off the provider (not just the model string)
# ---------------------------------------------------------------------------

def test_cu_timeout_keys_off_provider(tmp_path, monkeypatch):
    """A CU job carrying a SPECIFIC CU model id (so the old model-string check
    would miss it) still gets the long 660s timeout because the wrapper keys off
    the authoritative provider."""
    from Orchestrator.scheduler import manager as manager_mod
    from Orchestrator.scheduler.manager import CronJobManager

    db = tmp_path / "cron_cu_timeout.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    mgr = CronJobManager()
    job = mgr.create_job(
        name="cu",
        prompt="navigate",
        schedule="0 15 * * *",
        operator="system",
        provider="computer-use",
        model=CU_GEMINI_MODEL_DEFAULT,  # a specific CU id, NOT "computer-use"
    )

    # Capture the timeout the wrapper would request without running anything.
    captured = {}

    class _FakeFuture:
        def result(self, timeout=None):
            captured["timeout"] = timeout

    monkeypatch.setattr(
        manager_mod.asyncio,
        "run_coroutine_threadsafe",
        lambda coro, loop: (coro.close(), _FakeFuture())[1],
    )

    import asyncio as _aio
    mgr._loop = _aio.new_event_loop()
    try:
        mgr._execute_job_sync_wrapper(job["id"])
    finally:
        mgr._loop.close()

    assert captured["timeout"] == 660
