"""Tests for the send_sms ToolVault executor — TG200-only SMS path.

M0 of the MCP-server plan removed the Twilio SMS fallback. These tests pin
that behavior: the executor source has no Twilio/aiohttp remnant, and every
code path returns a ToolResult (including when Asterisk is the wrong/disabled
provider — i.e. no silent fall-through).
"""
import asyncio
import importlib.util
import inspect
from pathlib import Path
from unittest import mock

from Orchestrator.toolvault.context import ToolContext, ToolResult


def _load_executor():
    """Load the executor module from its ToolVault module folder."""
    repo_root = Path(__file__).resolve().parents[3]
    path = repo_root / "ToolVault" / "tools" / "send_sms" / "executor.py"
    spec = importlib.util.spec_from_file_location("send_sms_executor_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_executor_source_has_no_twilio_or_aiohttp():
    """The Twilio fallback (and its only consumer, aiohttp) must be gone."""
    module = _load_executor()
    src = inspect.getsource(module).lower()
    assert "twilio" not in src, "Twilio remnant found in send_sms executor"
    assert "aiohttp" not in src, "aiohttp remnant found in send_sms executor"


def test_executor_keeps_from_number_param():
    """from_number drives SIM/gateway selection in send_manual — keep it."""
    module = _load_executor()
    src = inspect.getsource(module)
    assert 'params.get("from_number")' in src
    assert "from_number=from_number" in src


def test_asterisk_disabled_returns_clean_error_not_none():
    """When Asterisk is not the provider, return a ToolResult, never None."""
    module = _load_executor()
    with mock.patch("Orchestrator.config.TELEPHONY_PROVIDER", "none"), \
         mock.patch("Orchestrator.config.ASTERISK_ENABLED", False):
        result = asyncio.run(
            module.execute(
                {"phone_number": "+15551234567", "message": "hi"},
                ToolContext(operator="system"),
            )
        )
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "tg200" in result.result.lower() or "unavailable" in result.result.lower()


def test_no_gateway_yields_tg200_failed_no_silent_fallback():
    """Asterisk enabled but no gateway -> explicit TG200 failure, not silent."""
    module = _load_executor()

    class _FakeRouter:
        async def send_manual(self, **kwargs):
            return {"success": False, "error": "No gateway available"}

    with mock.patch("Orchestrator.config.TELEPHONY_PROVIDER", "asterisk"), \
         mock.patch("Orchestrator.config.ASTERISK_ENABLED", True), \
         mock.patch("Orchestrator.sms.get_router", return_value=_FakeRouter()):
        result = asyncio.run(
            module.execute(
                {"phone_number": "+15551234567", "message": "hi"},
                ToolContext(operator="system"),
            )
        )
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "No gateway available" in result.result
    assert "TG200 SMS failed" in result.result
