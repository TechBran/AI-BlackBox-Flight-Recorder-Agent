"""GET /onboarding/cli-agent/status asks the CLI whether it is signed in.

It used to report authenticated = "a credential-looking file exists under ~".
On the production box (2026-10-29) ~/.claude held its marker while
`claude auth status` said loggedIn=false, so the wizard said "Ready" for an
agent that could not run. Now claude and codex are asked directly (their
status commands are cheap and non-interactive), through the same scrubbed env
the CLI agents get, with a short timeout and a per-process cache; when the CLI
cannot answer the marker is used and the response says so (auth_source,
auth_verified).

Hermetic: HOME is a temp dir, the CLIs are fake shell scripts on a temp PATH,
and the binary search never leaves that temp tree.
"""
from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from Orchestrator.cli_agent import auth_probe
from Orchestrator.routes import onboarding_routes

_PLANTED = {"ANTHROPIC_API_KEY": "fake-anthropic-planted", "OPENAI_API_KEY": "fake-openai-planted"}

CLAUDE_OUT = '{\n  "loggedIn": %s,\n  "authMethod": "%s",\n  "apiProvider": "firstParty"\n}'


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A temp HOME and an empty temp bin dir that is the whole binary search."""
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    for k, v in _PLANTED.items():
        monkeypatch.setenv(k, v)
    import Orchestrator.cli_agent.path_extension as pe
    monkeypatch.setattr(pe, "extended_path_dirs", lambda: [str(bindir)])
    monkeypatch.delenv("CLI_AGENT_CLAUDE_BIN", raising=False)
    auth_probe.reset_cache()
    yield type("Box", (), {"home": home, "bin": bindir, "log": tmp_path / "calls.log"})
    auth_probe.reset_cache()


def fake_cli(box, name: str, body: str) -> Path:
    """A fake CLI that logs each call (argv + whether a planted key reached it)."""
    p = box.bin / name
    p.write_text(
        "#!/bin/sh\n"
        f'echo "{name} $* key=${{ANTHROPIC_API_KEY:-}}${{OPENAI_API_KEY:-}} home=$HOME" >> "{box.log}"\n'
        + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def marker(box, rel: str) -> Path:
    p = box.home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    return p


def calls(box) -> list[str]:
    return box.log.read_text().splitlines() if box.log.exists() else []


def status() -> dict:
    return onboarding_routes.cli_agent_status()


# ---------------------------------------------------------------- claude


def test_claude_logged_out_with_marker_is_not_authenticated(box):
    marker(box, ".claude/.credentials.json")
    fake_cli(box, "claude", f"printf '%s\\n' '{CLAUDE_OUT % ('false', 'none')}'\nexit 1\n")
    claude = status()["providers"]["claude"]
    assert claude["installed"] is True
    assert claude["authenticated"] is False, "the marker alone is not a login"
    assert claude["auth_source"] == "cli"
    assert claude["auth_verified"] is True
    assert calls(box)[0].startswith("claude auth status ")


def test_claude_logged_in_is_authenticated(box):
    fake_cli(box, "claude", f"printf '%s\\n' '{CLAUDE_OUT % ('true', 'claude.ai')}'\n")
    claude = status()["providers"]["claude"]
    assert (claude["authenticated"], claude["auth_source"], claude["auth_verified"]) == (True, "cli", True)


def test_the_probe_gets_the_scrubbed_env_and_this_home(box):
    fake_cli(box, "claude", f"printf '%s\\n' '{CLAUDE_OUT % ('true', 'claude.ai')}'\n")
    status()
    line = calls(box)[0]
    assert "key= " in line, f"a server API key reached the CLI: {line}"
    assert f"home={box.home}" in line
    for v in _PLANTED.values():
        assert v not in line


def test_a_hanging_claude_falls_back_to_the_marker_within_the_timeout(box, monkeypatch):
    monkeypatch.setattr(auth_probe, "PROBE_TIMEOUT_S", 0.5)
    marker(box, ".claude/.credentials.json")
    fake_cli(box, "claude", "sleep 30\n")
    t0 = time.monotonic()
    claude = status()["providers"]["claude"]
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, f"status took {elapsed:.1f}s; the probe must be bounded"
    assert claude["authenticated"] is True, "the marker answers when the CLI cannot"
    assert claude["auth_source"] == "marker"
    assert claude["auth_verified"] is False


def test_a_missing_claude_is_not_installed_and_uses_the_marker(box):
    claude = status()["providers"]["claude"]
    assert claude["installed"] is False
    assert claude["authenticated"] is False
    assert claude["auth_source"] == "marker"
    assert claude["auth_verified"] is False
    assert calls(box) == []


@pytest.mark.parametrize("out", ["Welcome to Claude Code", "{not json", '{"loggedIn": "yes"}', ""])
def test_unparsable_claude_output_falls_back_to_the_marker(box, out):
    marker(box, ".claude/.credentials.json")
    fake_cli(box, "claude", f"printf '%s\\n' '{out}'\n")
    claude = status()["providers"]["claude"]
    assert (claude["authenticated"], claude["auth_source"], claude["auth_verified"]) == (True, "marker", False)


# ---------------------------------------------------------------- codex


def test_codex_not_logged_in_with_marker_is_not_authenticated(box):
    marker(box, ".codex/auth.json")
    fake_cli(box, "codex", "echo 'Not logged in' >&2\nexit 1\n")
    codex = status()["providers"]["codex"]
    assert (codex["authenticated"], codex["auth_source"], codex["auth_verified"]) == (False, "cli", True)
    assert any(c.startswith("codex login status") for c in calls(box))


def test_codex_logged_in_is_authenticated(box):
    fake_cli(box, "codex", "echo 'Logged in using ChatGPT' >&2\n")
    codex = status()["providers"]["codex"]
    assert (codex["authenticated"], codex["auth_source"], codex["auth_verified"]) == (True, "cli", True)


# ------------------------------------------------- gemini / antigravity


def test_gemini_has_no_status_command_and_stays_on_the_marker(box):
    marker(box, ".gemini/oauth_creds.json")
    fake_cli(box, "gemini", "echo 'must not be run' >&2\nexit 7\n")
    gemini = status()["providers"]["gemini"]
    assert (gemini["authenticated"], gemini["auth_source"], gemini["auth_verified"]) == (True, "marker", False)
    assert not any(c.startswith("gemini") for c in calls(box)), "gemini has no non-interactive status command"


def test_antigravity_auth_is_unobservable(box):
    agy = status()["providers"]["antigravity"]
    assert agy["authenticated"] is None
    assert agy["auth_method"] == "implicit_on_launch"
    assert (agy["auth_source"], agy["auth_verified"]) == ("none", False)


# ---------------------------------------------------------------- shape


def test_every_existing_field_keeps_its_type(box):
    fake_cli(box, "claude", f"printf '%s\\n' '{CLAUDE_OUT % ('true', 'claude.ai')}'\n")
    out = status()
    assert set(out) == {"providers", "ready"}
    assert isinstance(out["ready"], bool)
    assert set(out["providers"]) == {"claude", "gemini", "codex", "antigravity"}
    for prov, entry in out["providers"].items():
        assert isinstance(entry["installed"], bool), prov
        assert entry["authenticated"] is None or isinstance(entry["authenticated"], bool), prov
        assert entry["bin_path"] is None or isinstance(entry["bin_path"], str), prov
        assert isinstance(entry["install_command"], list), prov
        assert entry["auth_source"] in ("cli", "marker", "none"), prov
        assert isinstance(entry["auth_verified"], bool), prov
    assert out["providers"]["claude"]["package"] == "@anthropic-ai/claude-code"


def test_ready_follows_the_cli_not_the_marker(box):
    for rel in (".claude/.credentials.json", ".codex/auth.json", ".gemini/oauth_creds.json"):
        marker(box, rel)
    fake_cli(box, "claude", f"printf '%s\\n' '{CLAUDE_OUT % ('false', 'none')}'\nexit 1\n")
    fake_cli(box, "codex", "echo 'Logged in using ChatGPT' >&2\n")
    fake_cli(box, "gemini", "exit 0\n")
    fake_cli(box, "agy", "exit 0\n")
    assert status()["ready"] is False, "claude is signed out, whatever its marker says"


# ---------------------------------------------------------------- cache


def test_the_answer_is_cached_and_a_sign_in_is_seen_at_once(box, monkeypatch):
    creds = marker(box, ".claude/.credentials.json")
    flag = box.home / "logged-in"
    fake_cli(box, "claude",
             f'if [ -f "{flag}" ]; then printf \'%s\\n\' \'{CLAUDE_OUT % ("true", "claude.ai")}\'; '
             f'else printf \'%s\\n\' \'{CLAUDE_OUT % ("false", "none")}\'; exit 1; fi\n')
    assert status()["providers"]["claude"]["authenticated"] is False
    assert status()["providers"]["claude"]["authenticated"] is False
    assert len([c for c in calls(box) if c.startswith("claude")]) == 1, "the second GET reuses the answer"

    # A sign-in in a terminal rewrites the credential file: the next GET asks again.
    flag.write_text("")
    creds.write_text('{"fresh": true}')
    os.utime(creds, ns=(time.time_ns() + 10_000_000, time.time_ns() + 10_000_000))
    assert status()["providers"]["claude"]["authenticated"] is True
    assert len([c for c in calls(box) if c.startswith("claude")]) == 2

    # And the cache expires on its own.
    now = time.monotonic()
    monkeypatch.setattr(auth_probe, "_now", lambda: now + auth_probe.CACHE_TTL_S + 1)
    status()
    assert len([c for c in calls(box) if c.startswith("claude")]) == 3


def test_a_timeout_is_cached_too(box, monkeypatch):
    monkeypatch.setattr(auth_probe, "PROBE_TIMEOUT_S", 0.3)
    fake_cli(box, "claude", "sleep 30\n")
    status()
    t0 = time.monotonic()
    status()
    assert time.monotonic() - t0 < 0.3, "a hung CLI costs one timeout per cache window, not one per GET"
    assert len([c for c in calls(box) if c.startswith("claude")]) == 1


# ------------------------------------------------- the hub's rollup


def test_the_hub_rollup_never_spawns_and_reuses_a_cached_answer(box):
    marker(box, ".claude/.credentials.json")
    fake_cli(box, "claude", f"printf '%s\\n' '{CLAUDE_OUT % ('false', 'none')}'\nexit 1\n")
    rollup = onboarding_routes._cli_agent_status(probe_cli=False)["providers"]["claude"]
    assert (rollup["authenticated"], rollup["auth_source"]) == (True, "marker"), "nothing cached yet"
    assert calls(box) == [], "GET /onboarding/status promises no subprocess"

    assert status()["providers"]["claude"]["authenticated"] is False   # the wizard step asks
    rollup = onboarding_routes._cli_agent_status(probe_cli=False)["providers"]["claude"]
    assert (rollup["authenticated"], rollup["auth_source"], rollup["auth_verified"]) == (False, "cli", True)
    assert len(calls(box)) == 1


def test_the_status_rollup_reads_the_cli_status_without_probing(box, monkeypatch):
    seen = []

    def fake(*, probe_cli):
        seen.append(probe_cli)
        return {"providers": {}, "ready": False}

    monkeypatch.setattr(onboarding_routes, "_cli_agent_status", fake)
    onboarding_routes._collect_status_inputs()
    assert seen == [False]
