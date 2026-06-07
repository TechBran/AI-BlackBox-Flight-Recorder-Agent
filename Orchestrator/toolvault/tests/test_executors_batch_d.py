"""Tests for Batch D module executors (Task 6.2).

Batch D migrates 7 analysis + audio/voice executors OUT of the monolithic
``blackbox_tools._execute_<name>`` methods INTO per-tool
``ToolVault/tools/<name>/executor.py`` modules:

    analyze_image, analyze_audio, analyze_video, speech_to_text,
    text_to_speech, list_tts_voices, gemini_pro_tts

These tests run against the REAL on-disk modules (no tmp_path) — they assert the
7 executors load cleanly and a couple route correctly without touching the
network (short-circuit on missing params, or mocked HTTP for the param-free one).
"""

import asyncio
import inspect
from unittest.mock import patch

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor


BATCH_D = [
    "analyze_image",
    "analyze_audio",
    "analyze_video",
    "speech_to_text",
    "text_to_speech",
    "list_tts_voices",
    "gemini_pro_tts",
]


@pytest.fixture(autouse=True)
def fresh_registry():
    """Invalidate the executor cache around each test so on-disk edits register."""
    registry.invalidate_cache()
    yield
    registry.invalidate_cache()


# ---------------------------------------------------------------------------
# 1. Every Batch D executor loads: callable, no load_errors, valid signature.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", BATCH_D)
def test_executor_is_callable(name):
    ex = registry.get_executor(name)
    assert ex is not None, f"get_executor({name!r}) returned None"
    assert callable(ex)
    assert inspect.iscoroutinefunction(ex), f"{name} executor is not async"
    positional = [
        p
        for p in inspect.signature(ex).parameters.values()
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    assert len(positional) == 2, f"{name} executor must take (params, ctx)"


@pytest.mark.parametrize("name", BATCH_D)
def test_no_load_error_for_executor(name):
    registry.get_executor(name)
    errors = registry.load_errors()
    assert name not in errors, f"{name} has load errors: {errors.get(name)}"


def test_all_batch_d_loaded():
    """All 7 resolve to a callable."""
    assert all(registry.get_executor(n) is not None for n in BATCH_D)


# ---------------------------------------------------------------------------
# 2. Routing smokes (no network needed — short-circuit on missing params).
# ---------------------------------------------------------------------------

def test_analyze_image_requires_image_url():
    ex = registry.get_executor("analyze_image")
    result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "image_url is required" in result.result


def test_analyze_audio_requires_file_path_via_dispatch():
    """analyze_audio short-circuits on a missing file_path (no network)."""
    ex = BlackBoxToolExecutor(operator="Brandon")
    result = asyncio.run(ex.execute("analyze_audio", {}))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "file_path is required" in result.result


def test_analyze_video_requires_video_url():
    ex = registry.get_executor("analyze_video")
    result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "video_url is required" in result.result


def test_speech_to_text_requires_audio_path():
    ex = registry.get_executor("speech_to_text")
    result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "audio_path is required" in result.result


def test_text_to_speech_requires_text():
    ex = registry.get_executor("text_to_speech")
    result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "text is required" in result.result


def test_gemini_pro_tts_requires_text():
    ex = registry.get_executor("gemini_pro_tts")
    result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "text is required" in result.result


# ---------------------------------------------------------------------------
# 3. Network-heavy param-free one: mock the HTTP client and assert routing.
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, *a, **kw):
        return _FakeResp(self._payload)

    def post(self, *a, **kw):
        return _FakeResp(self._payload)


def test_list_tts_voices_summarizes_voices():
    """list_tts_voices takes no required params — mock the HTTP GET and verify
    it summarizes the voice list."""
    ex = registry.get_executor("list_tts_voices")
    payload = {
        "voices": [
            {"name": "en-US-Wavenet-A", "languageCodes": ["en-US"]},
            {"name": "en-GB-Wavenet-B", "languageCodes": ["en-GB"]},
            {"name": "fr-FR-Wavenet-C", "languageCodes": ["fr-FR"]},
        ]
    }
    with patch("aiohttp.ClientSession", return_value=_FakeSession(payload)):
        result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is True
    assert "3" in result.result
    assert result.data == {"voice_count": 3}


def test_gemini_pro_tts_routes_to_task(monkeypatch):
    """gemini_pro_tts with text posts and returns a task_id (mocked HTTP)."""
    ex = registry.get_executor("gemini_pro_tts")
    payload = {"task_id": "task-123"}
    with patch("aiohttp.ClientSession", return_value=_FakeSession(payload)):
        result = asyncio.run(
            ex({"text": "hello"}, ToolContext(operator="system"))
        )
    assert isinstance(result, ToolResult)
    assert result.success is True
    assert "task-123" in result.result
    assert result.data["task_id"] == "task-123"
