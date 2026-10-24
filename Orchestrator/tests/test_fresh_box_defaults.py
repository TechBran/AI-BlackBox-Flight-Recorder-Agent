"""Fresh-box defaults: no dev-box operator, port, Node version or off-switch baked in.

Audit rank 36 (O12-07, X05-architecture-17, O01-core-runtime-09/-13,
O06-frontier-agent-18): a customer box must behave like this one without the
dev box's .env. Covered here:
  * the pairing operator comes from the wizard / the live roster, never a
    literal (and /health reports the same one);
  * /agent/dev-snapshot names its operator; /debug/context and the IVRs fall
    back to the roster default;
  * ToolVault injection is on unless explicitly switched off;
  * loopback URLs come from ORCHESTRATOR_PORT through config.orchestrator_url;
  * the Gemini agent finds its CLI with provider_bin(), not one nvm version.
"""
import asyncio
import json
import os

import pytest

from Orchestrator import config


def _client():
    import Orchestrator.app  # noqa: F401 — registers routes on the shared app
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    return TestClient(app)


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """An empty .env standing in for the wizard's (never this box's own)."""
    from Orchestrator.onboarding import secrets_writer
    path = tmp_path / ".env"
    path.write_text("")
    monkeypatch.setattr(secrets_writer, "ENV_FILE", path)
    return path


@pytest.fixture
def roster(monkeypatch, env_file):
    """A box whose roster is Default + Alice + Bob, default Default, nothing in
    .env and no [pairing] default_operator."""
    monkeypatch.setattr(config, "USERS_LIST", ["Default", "Alice", "Bob"])
    monkeypatch.setattr(config, "USERS_DEFAULT", "Default")
    monkeypatch.delenv("DEFAULT_OPERATOR", raising=False)
    monkeypatch.setattr(config, "_pairing_config_operator", lambda: "")
    return config.USERS_LIST


# ── pairing ──────────────────────────────────────────────────────────────────

def test_pairing_operator_resolution(roster, monkeypatch):
    assert config.pairing_operator() == "Default"            # live roster default
    monkeypatch.setattr(config, "_pairing_config_operator", lambda: "Bob")
    assert config.pairing_operator() == "Bob"                # config.ini [pairing]
    monkeypatch.setenv("DEFAULT_OPERATOR", "Alice")
    assert config.pairing_operator() == "Alice"              # the wizard's choice
    assert config.pairing_operator("Bob") == "Bob"           # chosen for this token
    # Names that are not (or no longer) on the roster are skipped.
    monkeypatch.setenv("DEFAULT_OPERATOR", "Gone")
    monkeypatch.setattr(config, "_pairing_config_operator", lambda: "AlsoGone")
    assert config.pairing_operator() == "Default"
    assert config.pairing_operator("Mallory") == "Default"


def test_pairing_operator_reads_the_wizards_env_file_fresh(roster, env_file, monkeypatch):
    # The operator step writes DEFAULT_OPERATOR to .env; no restart needed.
    assert config.pairing_operator() == "Default"
    env_file.write_text("DEFAULT_OPERATOR=Bob\n")
    assert config.pairing_operator() == "Bob"
    monkeypatch.setenv("DEFAULT_OPERATOR", "Alice")     # the file wins over the process env
    assert config.pairing_operator() == "Bob"
    env_file.write_text("DEFAULT_OPERATOR=Gone\n")      # off the roster: skipped
    assert config.pairing_operator() == "Default"


@pytest.fixture
def pair_routes(monkeypatch, roster):
    import qrcode
    from Orchestrator.routes import pairing_routes
    monkeypatch.setattr(pairing_routes, "record_pairing", lambda **k: None)
    payloads = []

    class _Img:
        def save(self, buf, format=None):
            buf.write(b"png")

    monkeypatch.setattr(qrcode, "make", lambda data: payloads.append(json.loads(data)) or _Img())
    return payloads


def _claim(client, token):
    r = client.post("/pair/claim", json={"token": token, "device_name": "pixel",
                                          "device_kind": "android"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("body", [None, {}])
def test_fresh_box_pairing_hands_out_the_roster_default(pair_routes, body):
    client = _client()
    # The Portal's System Controls posts no body; the wizard and Android post {}.
    r = client.post("/pair/start") if body is None else client.post("/pair/start", json=body)
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert client.get(f"/pair/qr/{token}").status_code == 200
    assert pair_routes[-1]["operator"] == "Default"
    assert _claim(client, token)["operator"] == "Default"


def test_pairing_uses_the_operator_chosen_for_the_token(pair_routes):
    client = _client()
    token = client.post("/pair/start", json={"operator": "Bob"}).json()["token"]
    client.get(f"/pair/qr/{token}")
    assert pair_routes[-1]["operator"] == "Bob"
    assert _claim(client, token)["operator"] == "Bob"


def test_pairing_refuses_an_operator_that_is_not_on_the_roster(pair_routes):
    r = _client().post("/pair/start", json={"operator": "Mallory"})
    assert r.status_code == 400


def test_health_reports_the_pairing_operator(monkeypatch):
    monkeypatch.setattr(config, "pairing_operator", lambda requested=None: "Alice")
    import Orchestrator.routes.admin_routes as admin
    monkeypatch.setattr(admin, "_resolve_tailnet_origin", lambda: "")
    r = _client().get("/health")
    assert r.status_code == 200
    assert r.json()["pairing"]["default_operator"] == "Alice"


# ── operator fallbacks ───────────────────────────────────────────────────────

def test_dev_snapshot_requires_an_operator(monkeypatch):
    import Orchestrator.checkpoint as checkpoint
    minted = []
    monkeypatch.setattr(checkpoint, "mint_with_content",
                        lambda op, content, **k: minted.append(op) or {"snap_id": "SNAP-T"})
    client = _client()
    r = client.post("/agent/dev-snapshot", json={"content": "x" * 80})
    assert r.status_code == 400
    assert minted == []
    r = client.post("/agent/dev-snapshot", json={"content": "x" * 80, "operator": "Alice-DEV"})
    assert r.status_code == 200 and r.json()["success"] is True
    assert minted == ["Alice-DEV"]


def test_debug_context_defaults_to_the_roster_default(monkeypatch, roster):
    import Orchestrator.routes.admin_routes as admin
    seen = []
    monkeypatch.setattr(admin, "read_text_safe", lambda *a, **k: "")
    monkeypatch.setattr(admin, "get_recent_fossils_for_operator",
                        lambda vol, op, n, cap: seen.append(op) or [])
    monkeypatch.setattr(admin, "hybrid_retrieve", lambda *a, **k: [])
    r = _client().get("/debug/context")
    assert r.status_code == 200, r.text
    assert r.json()["operator"] == "Default" and seen == ["Default"]


def test_twilio_cli_call_operator_is_optional():
    from Orchestrator.routes.twilio_routes import CLICallRequest
    assert CLICallRequest(to="+15555550123").operator is None


def test_cellular_ivr_single_operator_falls_back_to_the_roster_default(monkeypatch):
    from Orchestrator.cellular import ivr as cell_ivr
    monkeypatch.setattr(config, "USERS_LIST", [])
    monkeypatch.setattr(config, "USERS_DEFAULT", "Default")
    session = cell_ivr.CellularIVR.__new__(cell_ivr.CellularIVR)
    assert asyncio.run(session.run_operator_selection()) == "Default"


def test_asterisk_ivr_single_operator_falls_back_to_the_roster_default(monkeypatch):
    from Orchestrator.asterisk import ivr as ast_ivr
    monkeypatch.setattr(config, "USERS_LIST", [])
    monkeypatch.setattr(config, "USERS_DEFAULT", "Default")
    cls = next(c for c in vars(ast_ivr).values()
               if isinstance(c, type) and hasattr(c, "_run_operator_select"))
    session = cls.__new__(cls)
    assert asyncio.run(session._run_operator_select()) == "Default"


@pytest.mark.parametrize("tool", ["gmail_send", "gmail_read", "gmail_reply",
                                  "gmail_search", "gmail_labels"])
def test_gmail_executors_fall_back_to_the_session_then_the_roster(monkeypatch, tool):
    import importlib
    import Orchestrator.gmail.service as gmail
    from Orchestrator.toolvault.context import ToolContext

    seen = []

    def _record(operator, *a, **k):
        seen.append(operator)
        return {"error": "stub"}

    for name in ("send_email", "get_message", "list_messages", "get_labels", "modify_message"):
        monkeypatch.setattr(gmail, name, _record)
    monkeypatch.setattr(config, "USERS_DEFAULT", "Default")
    mod = importlib.import_module(f"ToolVault.tools.{tool}.executor")
    params = {"to": "a@example.invalid", "subject": "s", "body": "b",
              "message_id": "m1", "thread_id": "t1", "query": ""}
    # The Gmail executors are async (their Google calls run in to_thread, M4 U30).
    import inspect
    for call in (mod.execute(dict(params), ToolContext(operator="Alice")),
                 mod.execute(dict(params, operator=""), ToolContext(operator=""))):
        if inspect.iscoroutine(call):
            asyncio.run(call)
    assert seen == ["Alice", "Default"]


def test_toolvault_roster_fallback_is_the_template_default(monkeypatch, tmp_path):
    import builtins
    from Orchestrator.toolvault import resolvers

    real_import = builtins.__import__

    def lean_import(name, *a, **k):  # what the lean MCP venv sees
        if name == "Orchestrator.config":
            raise ModuleNotFoundError(name)
        return real_import(name, *a, **k)

    monkeypatch.setenv("BLACKBOX_ROOT", str(tmp_path))   # no config.ini there
    monkeypatch.setattr(builtins, "__import__", lean_import)
    assert resolvers._list_operators(None) == ["Default"]


# ── ToolVault on by default ──────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (None, True), ("", True), ("true", True), ("1", True), ("yes", True),
    ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
])
def test_toolvault_enabled_unless_switched_off(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("TOOLVAULT_ENABLED", raising=False)
    else:
        monkeypatch.setenv("TOOLVAULT_ENABLED", value)
    assert config._env_flag("TOOLVAULT_ENABLED", True) is expected


def test_fresh_box_chat_tools_come_from_the_toolvault_injector(monkeypatch):
    """A box whose .env never mentions TOOLVAULT_ENABLED (install.sh copies
    .env.template) boots with ToolVault on, so chat tools come from
    inject_for_prompt, not the frozen static arrays."""
    import subprocess
    import sys
    from Orchestrator.routes import chat_routes
    from Orchestrator.toolvault import injector

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = dict(os.environ, PYTHONPATH=root)
    # Empty = unset for _env_flag, and load_dotenv never overrides a set
    # variable, so this box's own .env cannot leak into the fresh-box boot.
    env["TOOLVAULT_ENABLED"] = ""
    out = subprocess.run(
        [sys.executable, "-c",
         "from Orchestrator import config; print('TOOLVAULT=%s' % config.TOOLVAULT_ENABLED)"],
        cwd=root, env=env, capture_output=True, text=True, timeout=120,
    )
    assert "TOOLVAULT=True" in out.stdout, out.stdout[-800:] + out.stderr[-800:]

    calls = []
    def fake_inject(prompt, provider, group=None, **kw):
        calls.append((prompt, provider, group))
        return [{"name": "send_sms"}], ""
    monkeypatch.setattr(chat_routes, "TOOLVAULT_ENABLED", True)
    monkeypatch.setattr(injector, "inject_for_prompt", fake_inject)
    assert chat_routes._get_tools("anthropic", "text Alice I'm late") == [{"name": "send_sms"}]
    assert calls == [("text Alice I'm late", "anthropic", "chat")]


def test_env_template_does_not_switch_toolvault_off():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    text = open(os.path.join(root, ".env.template"), encoding="utf-8").read()
    assert "TOOLVAULT_ENABLED=false" not in text


# ── loopback URLs ────────────────────────────────────────────────────────────

def test_orchestrator_url_follows_the_configured_port(monkeypatch):
    monkeypatch.setattr(config, "ORCHESTRATOR_PORT", 9555)
    assert config.orchestrator_url() == "http://localhost:9555"
    assert config.orchestrator_url("/chat/save") == "http://localhost:9555/chat/save"
    assert config.orchestrator_url("/ws/x", scheme="ws", host="127.0.0.1") == "ws://127.0.0.1:9555/ws/x"


def test_loopback_callers_use_the_helper(monkeypatch):
    from Orchestrator.scheduler import executor
    from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor
    from Orchestrator.toolvault.context import ToolContext

    monkeypatch.setattr(config, "ORCHESTRATOR_PORT", 9555)
    assert executor._base_url() == "http://localhost:9555"
    assert BlackBoxToolExecutor(operator="Alice").base_url == "http://localhost:9555"
    assert ToolContext().base_url == "http://localhost:9555"
    assert BlackBoxToolExecutor(operator="Alice", base_url="http://x:1").base_url == "http://x:1"


def test_tool_context_default_without_config(monkeypatch):
    # The lean MCP venv never imports Orchestrator.config.
    import sys
    from Orchestrator.toolvault import context
    monkeypatch.delitem(sys.modules, "Orchestrator.config")
    monkeypatch.setenv("ORCHESTRATOR_PORT", "9556")
    assert context._local_base_url() == "http://localhost:9556"
    monkeypatch.delenv("ORCHESTRATOR_PORT")
    assert context._local_base_url().startswith("http://localhost:")


# ── Gemini CLI resolution ────────────────────────────────────────────────────

def test_gemini_agent_resolves_its_cli_with_provider_bin(monkeypatch, tmp_path):
    from Orchestrator.routes import cli_agent_routes, gemini_agent_routes

    gemini = tmp_path / "node-v99" / "bin" / "gemini"
    gemini.parent.mkdir(parents=True)
    gemini.write_text("#!/bin/sh\n")
    gemini.chmod(0o755)
    monkeypatch.setattr(cli_agent_routes, "provider_bin",
                        lambda name: str(gemini) if name == "gemini" else None)
    assert gemini_agent_routes.get_gemini_path() == str(gemini)
    # The spawn PATH leads with the CLI's own bin dir, so its node shebang resolves.
    path = gemini_agent_routes._gemini_spawn_path(str(gemini), "/usr/bin")
    assert path.split(os.pathsep)[0] == str(gemini.parent)
    assert "/usr/bin" in path.split(os.pathsep)

    status = asyncio.run(gemini_agent_routes.gemini_agent_status())
    assert status["available"] is True and status["gemini_path"] == str(gemini)

    monkeypatch.setattr(cli_agent_routes, "provider_bin", lambda name: "gemini")
    status = asyncio.run(gemini_agent_routes.gemini_agent_status())
    assert status["available"] is False


def test_gemini_agent_has_no_pinned_node_version():
    import inspect
    from Orchestrator.routes import gemini_agent_routes
    assert ".nvm/versions/node/v" not in inspect.getsource(gemini_agent_routes)


def test_claude_agent_resolves_its_cli_with_provider_bin(monkeypatch, tmp_path):
    from Orchestrator.routes import agent_routes, cli_agent_routes

    claude = tmp_path / "node-v99" / "bin" / "claude"
    monkeypatch.setattr(cli_agent_routes, "provider_bin",
                        lambda name: str(claude) if name == "claude" else None)
    assert agent_routes._claude_path() == str(claude)
    path = agent_routes._claude_child_path(str(claude)).split(":")
    assert str(claude.parent) in path
    assert os.path.expanduser("~/.local/bin") in path
    monkeypatch.setattr(cli_agent_routes, "provider_bin", lambda name: None)
    assert agent_routes._claude_path() == "claude"
