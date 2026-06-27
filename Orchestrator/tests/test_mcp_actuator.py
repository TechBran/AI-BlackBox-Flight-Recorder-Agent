"""M7.4: the privileged MCP actuator -- argv must match the sudoers grants exactly."""
import asyncio

from Orchestrator.onboarding import mcp_actuator


def _capture(monkeypatch):
    calls = []

    async def fake_run(*argv, timeout=15):
        calls.append(list(argv))
        return 0, "", ""

    monkeypatch.setattr(mcp_actuator, "_run", fake_run)
    return calls


def test_funnel_up_argv_matches_grant(monkeypatch):
    calls = _capture(monkeypatch)
    res = asyncio.run(mcp_actuator.funnel_up())
    assert res["ok"] is True
    # MUST match sudoers: /usr/bin/tailscale funnel --bg --https=8443 9093
    assert calls[0] == ["sudo", "-n", "/usr/bin/tailscale", "funnel",
                        "--bg", "--https=8443", "9093"]


def test_funnel_reset_argv_matches_grant(monkeypatch):
    calls = _capture(monkeypatch)
    asyncio.run(mcp_actuator.funnel_reset())
    assert calls[0] == ["sudo", "-n", "/usr/bin/tailscale", "funnel", "reset"]


def test_service_action_argv_matches_grant(monkeypatch):
    calls = _capture(monkeypatch)
    asyncio.run(mcp_actuator.service_action("restart"))
    assert calls[0] == ["sudo", "-n", "/usr/bin/systemctl", "restart", "blackbox-mcp.service"]
    asyncio.run(mcp_actuator.service_action("start"))
    assert calls[1] == ["sudo", "-n", "/usr/bin/systemctl", "start", "blackbox-mcp.service"]


def test_service_action_rejects_unknown(monkeypatch):
    calls = _capture(monkeypatch)
    res = asyncio.run(mcp_actuator.service_action("rm -rf"))
    assert res["ok"] is False and not calls       # no sudo call for an unknown verb
