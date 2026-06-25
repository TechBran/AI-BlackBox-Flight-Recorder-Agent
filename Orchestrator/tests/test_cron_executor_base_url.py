"""
Tests for M5.3 — the cron executor's BASE_URL is DERIVED from config, not a
hardcoded literal.

The executor talks to the LOCAL app (loopback), so BASE_URL must be
http://localhost:<port> where <port> comes from the same port config the rest
of the app uses (ORCHESTRATOR_PORT). On the default box that is still
http://localhost:9091 — but on a fresh box that configures a different port the
executor must follow it rather than pointing at a dead 9091.
"""

import importlib

from Orchestrator import config as config_mod
from Orchestrator.scheduler import executor as executor_mod


def test_base_url_is_loopback_and_config_derived():
    """BASE_URL is loopback and reflects the configured ORCHESTRATOR_PORT — on
    the default box that is http://localhost:9091."""
    assert executor_mod.BASE_URL == f"http://localhost:{config_mod.ORCHESTRATOR_PORT}"
    # Still loopback (the executor only ever talks to the local app).
    assert executor_mod.BASE_URL.startswith("http://localhost:")


def test_base_url_not_a_bare_hardcoded_literal():
    """The port in BASE_URL is the config value, not an independently hardcoded
    integer — proven by building the expected string from config and matching."""
    expected = f"http://localhost:{config_mod.ORCHESTRATOR_PORT}"
    assert executor_mod.BASE_URL == expected


def test_base_url_follows_overridden_port(monkeypatch):
    """When the configured port differs from the default, a freshly-imported
    executor's BASE_URL follows it (fresh-box portability) — proving the value
    is computed from config, not frozen at 9091."""
    monkeypatch.setenv("ORCHESTRATOR_PORT", "8123")
    # Re-import config + executor so the env override is picked up.
    importlib.reload(config_mod)
    try:
        reloaded_executor = importlib.reload(executor_mod)
        assert config_mod.ORCHESTRATOR_PORT == 8123
        assert reloaded_executor.BASE_URL == "http://localhost:8123"
    finally:
        # Restore the default-config modules for the rest of the suite.
        monkeypatch.delenv("ORCHESTRATOR_PORT", raising=False)
        importlib.reload(config_mod)
        importlib.reload(executor_mod)
