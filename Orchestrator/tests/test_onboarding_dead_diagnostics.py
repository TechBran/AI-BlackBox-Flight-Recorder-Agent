"""U10 (audit O16-onboarding-update-10): the six tmux-era "blank Claude modal"
diagnostics are gone from the production onboarding router.

They had no Portal, Android, MCP or installer caller, and each was a liability:
spawn-claude-bare-pty leaked a zombie per call, claude-doctor ran two billable
`claude --print` calls and dumped ~/.claude/settings.json, pane-content returned
CLAUDE* entries of a pane's /proc environ, reset-tmux killed every operator's
tmux sessions, plus clear-claude-cloud-auth-cache and xdg-open-log. The live
CLI-agent surface (status, spawn-terminal, url-handlers) must stay.
"""
from Orchestrator.routes.onboarding_routes import router

DEAD = (
    "/onboarding/cli-agent/spawn-claude-bare-pty",
    "/onboarding/cli-agent/clear-claude-cloud-auth-cache",
    "/onboarding/cli-agent/reset-tmux",
    "/onboarding/cli-agent/claude-doctor",
    "/onboarding/cli-agent/pane-content",
    "/onboarding/cli-agent/xdg-open-log",
)
KEPT = (
    "/onboarding/cli-agent/status",
    "/onboarding/cli-agent/spawn-terminal",
    "/onboarding/cli-agent/url-handlers",
)


def test_tmux_era_diagnostics_are_not_routed():
    paths = {getattr(r, "path", None) for r in router.routes}
    assert not paths.intersection(DEAD), sorted(paths.intersection(DEAD))


def test_live_cli_agent_routes_remain():
    paths = {getattr(r, "path", None) for r in router.routes}
    for p in KEPT:
        assert p in paths, p
