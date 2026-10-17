"""The ONE env table every CLI-agent / terminal spawn inherits through
(audit O14-cli-agents-devices-02 / -03).

Before this module the project had three divergent strip lists: zellij panes
dropped only ANTHROPIC_API_KEY, the tmux backend dropped nothing, and the
headless runner dropped 2-3 keys per provider. So `env` in any terminal pane
printed the server's Google/OpenAI/xAI/Twilio/AMI credentials, and any YOLO
agent launched there could read them. These tests pin the shared contract:
deny by secret-shaped NAME (so a future .env addition is covered without a code
change), keep the session plumbing a CLI needs, and prove it with a real child.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from Orchestrator.cli_agent import env_scrub


# Every secret-bearing variable the BlackBox's own .env / config can carry
# (names only; the values are obvious fakes).
_SERVER_SECRETS = {
    "ANTHROPIC_API_KEY": "fake-anthropic",
    "ANTHROPIC_AUTH_TOKEN": "fake-anthropic-token",
    "OPENAI_API_KEY": "fake-openai",
    "GOOGLE_API_KEY": "fake-google",
    "GEMINI_API_KEY": "fake-gemini",
    "XAI_API_KEY": "fake-xai",
    "PERPLEXITY_API_KEY": "fake-perplexity",
    "ELEVENLABS_API_KEY": "fake-elevenlabs",
    "VOYAGE_API_KEY": "fake-voyage",
    "COHERE_API_KEY": "fake-cohere",
    "GOOGLE_OAUTH_CLIENT_SECRET": "fake-oauth-secret",
    "GOOGLE_OAUTH_CLIENT_ID": "fake-oauth-client",
    "TWILIO_ACCOUNT_SID": "fake-twilio-sid",
    "TWILIO_AUTH_TOKEN": "fake-twilio-token",
    "ASTERISK_AMI_SECRET": "fake-ami-secret",
    "ASTERISK_ARI_PASSWORD": "fake-ari-password",
    "TELEPHONY_SECRET_KEY": "fake-telephony-key",
    "TG200_HTTP_PASSWORD": "fake-tg200",
    "FREESWITCH_ESL_PASSWORD": "fake-esl",
    "DRACHTIO_SECRET": "fake-drachtio",
    "PBX_3CX_PASSWORD": "fake-3cx",
    "HF_TOKEN": "fake-hf",
    "BLACKBOX_MCP_TOKENS": "fake-mcp-tokens",
    "PHONE_PIN_CODE": "fake-pin",
    # Provider redirect vars: a CLI must talk to its own vendor, never inherit
    # the orchestrator's proxy config (headless M5 defense-in-depth).
    "ANTHROPIC_BASE_URL": "http://proxy.invalid",
    "OPENAI_BASE_URL": "http://proxy.invalid",
}

# Session plumbing every CLI needs (PATH/HOME/locale/terminal/keyring/display).
_SESSION_VARS = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/someone",
    "USER": "someone",
    "LOGNAME": "someone",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "LC_CTYPE": "C.UTF-8",
    "TERM": "xterm-256color",
    "SHELL": "/bin/bash",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "XDG_CONFIG_HOME": "/home/someone/.config",
    # Matches the _TOKEN suffix but is session plumbing (Wayland focus token):
    # the XDG_* keep-list must win over the name rule.
    "XDG_ACTIVATION_TOKEN": "wayland-activation",
    "DISPLAY": ":0",
    "WAYLAND_DISPLAY": "wayland-0",
    "XAUTHORITY": "/run/user/1000/gdm/Xauthority",
    # Vertex/ADC service-account PATH (not a pay-per-call key) — deliberately
    # kept, same decision the headless runner and the zellij comment record.
    "GOOGLE_APPLICATION_CREDENTIALS": "/home/someone/vertex.json",
    "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent",
    "NVM_DIR": "/home/someone/.nvm",
}


def _base() -> dict:
    return {**_SESSION_VARS, **_SERVER_SECRETS}


def test_every_server_secret_is_dropped():
    env = env_scrub.scrubbed_child_env(_base())
    leaked = sorted(k for k in _SERVER_SECRETS if k in env)
    assert leaked == [], f"server secrets leaked into the child env: {leaked}"
    for value in _SERVER_SECRETS.values():
        assert value not in env.values()


def test_session_plumbing_survives():
    env = env_scrub.scrubbed_child_env(_base())
    for k, v in _SESSION_VARS.items():
        assert env.get(k) == v, f"{k} must survive the scrub"


@pytest.mark.parametrize("name", [
    "NEW_VENDOR_API_KEY", "SOME_SECRET", "GH_TOKEN", "GITHUB_TOKEN", "DB_PASSWORD",
    "ACME_SID", "ACME_AUTH", "AWS_SECRET_ACCESS_KEY", "MY_PRIVATE_KEY",
    "SMTP_PASSWD", "API_KEY", "TOKEN", "my_api_key", "Vendor_Secret",
    "CLAUDE_CODE_OAUTH_TOKEN",
])
def test_name_rule_covers_unlisted_secrets(name):
    """The rule is by NAME SHAPE, so a key added to .env tomorrow is covered
    without touching this module."""
    assert env_scrub.is_secret_name(name)
    env = env_scrub.scrubbed_child_env({"PATH": "/bin", name: "v"})
    assert name not in env


@pytest.mark.parametrize("name", [
    "PATH", "HOME", "TERM", "LANG", "SSH_AUTH_SOCK", "GPG_AGENT_INFO",
    "XDG_SESSION_ID", "PHONE_ENABLED", "TELEPHONY_PROVIDER", "NODE_OPTIONS",
    "COLORTERM", "GOOGLE_APPLICATION_CREDENTIALS",
])
def test_ordinary_names_are_not_secrets(name):
    assert not env_scrub.is_secret_name(name)


def test_zellij_vars_are_always_dropped():
    """The orchestrator can itself run inside a zellij pane; an inherited
    ZELLIJ_SESSION_NAME makes a child target (and nest in) the USER's session."""
    env = env_scrub.scrubbed_child_env(
        {"PATH": "/bin", "ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "live", "ZELLIJ_PANE_ID": "1"})
    assert not any(k.startswith("ZELLIJ") for k in env)


def test_keep_readmits_only_the_named_variable():
    base = _base()
    env = env_scrub.scrubbed_child_env(base, keep=("GEMINI_API_KEY",))
    assert env["GEMINI_API_KEY"] == "fake-gemini"
    assert "GOOGLE_API_KEY" not in env and "OPENAI_API_KEY" not in env


def test_base_is_not_mutated():
    base = _base()
    snapshot = dict(base)
    env_scrub.scrubbed_child_env(base)
    assert base == snapshot


def test_defaults_to_the_live_process_environment(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake-live-twilio")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-live-openai")
    env = env_scrub.scrubbed_child_env()
    assert "TWILIO_AUTH_TOKEN" not in env and "OPENAI_API_KEY" not in env
    assert env.get("PATH") == os.environ.get("PATH")


def test_real_child_cannot_see_any_secret_value():
    """Proven with a real process, not a dict check: `env` in the child (what an
    operator or a YOLO agent would run in a pane) prints no secret."""
    env = env_scrub.scrubbed_child_env(_base())
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    out = subprocess.run(["env"], env=env, capture_output=True, text=True, check=True).stdout
    for name, value in _SERVER_SECRETS.items():
        assert value not in out, f"{name}'s value reached the child"
    assert "C.UTF-8" in out  # control: the child really did receive an environment
