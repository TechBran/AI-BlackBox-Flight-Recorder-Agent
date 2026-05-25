"""Unit tests for Orchestrator.cli_agent.get_backend.

Covers:
- C4 health-fallback: zellij requested + unhealthy → returns "tmux"
- Default backend (env unset)
- Invalid env value → WARNING + fallback
- TTL cache: web_server_healthy called only once across rapid calls
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

import Orchestrator.cli_agent as cli_agent


@pytest.fixture(autouse=True)
def _reset_cache_each_test():
    """Clear the TTL health cache before each test so probes aren't
    polluted by prior calls."""
    cli_agent._reset_health_cache_for_tests()
    yield
    cli_agent._reset_health_cache_for_tests()


def test_default_returns_tmux_when_env_unset(monkeypatch):
    monkeypatch.delenv("CLI_AGENT_BACKEND", raising=False)
    assert cli_agent.get_backend() == "tmux"


def test_default_returns_tmux_when_env_empty(monkeypatch):
    monkeypatch.setenv("CLI_AGENT_BACKEND", "")
    assert cli_agent.get_backend() == "tmux"


def test_zellij_env_returns_zellij_when_healthy(monkeypatch):
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")
    with patch.object(cli_agent.zellij_client, "web_server_healthy", return_value=True) as m:
        assert cli_agent.get_backend() == "zellij"
        assert m.call_count == 1


def test_zellij_env_falls_back_to_tmux_when_unhealthy(monkeypatch):
    """Audit C4 acceptance: zellij configured but daemon unhealthy →
    return tmux (with WARNING log, not silent)."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")
    with patch.object(cli_agent.zellij_client, "web_server_healthy", return_value=False):
        assert cli_agent.get_backend() == "tmux"


def test_invalid_env_logs_warning_and_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv("CLI_AGENT_BACKEND", "screen")
    with caplog.at_level("WARNING", logger="Orchestrator.cli_agent"):
        assert cli_agent.get_backend() == "tmux"
    msgs = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("CLI_AGENT_BACKEND" in m and "screen" in m for m in msgs), (
        f"expected WARNING about invalid backend value; got {msgs}"
    )


def test_invalid_env_case_insensitive_tmux(monkeypatch):
    """Uppercase 'TMUX' should resolve via the .lower() normalization."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "TMUX")
    assert cli_agent.get_backend() == "tmux"


def test_ttl_cache_avoids_repeated_health_probes(monkeypatch):
    """Two get_backend() calls within the TTL window should issue only ONE
    web_server_healthy probe. Audit-acceptance for the 30s TTL design."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")
    probe = MagicMock(return_value=True)
    with patch.object(cli_agent.zellij_client, "web_server_healthy", probe):
        # First call: cold cache → probe.
        assert cli_agent.get_backend() == "zellij"
        # Second call within TTL: cache hit → no second probe.
        assert cli_agent.get_backend() == "zellij"
        # Third — same.
        assert cli_agent.get_backend() == "zellij"
    assert probe.call_count == 1, (
        f"expected exactly 1 web_server_healthy call across 3 get_backend() "
        f"calls within TTL; got {probe.call_count}"
    )


def test_health_probe_exception_treated_as_unhealthy(monkeypatch):
    """Defensive: a raising web_server_healthy must not crash get_backend()
    — it should be treated as unhealthy (return tmux)."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")
    with patch.object(
        cli_agent.zellij_client,
        "web_server_healthy",
        side_effect=RuntimeError("daemon ate itself"),
    ):
        assert cli_agent.get_backend() == "tmux"
