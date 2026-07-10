"""G2-T10 (M2.2) — the three CLI-agent ToolVault tools.

`claude_code_task`, `gemini_cli_task`, `codex_cli_task` are thin ToolVault tools
that fail-fast when their CLI is NOT authenticated and otherwise create an
async `TaskType.CLI_AGENT` task (runner/worker/cancel/concurrency all shipped in
T8/T9). This file verifies BEHAVIOR, not mocks:

  * Schema: groups are EXACTLY the four conversational surfaces and NEVER `mcp`
    (Brandon's standing D1 invariant — `mcp` == public Funnel ingress == remote
    RCE under the operator's shell); flat plain-string params only (the voice
    serializers reject enum/nested); no version named anywhere.
  * Surfacing: each tool appears in the OpenAI-realtime, Grok-live, Gemini-live,
    and chat converters, serialized flat.
  * Auth fail-fast: with a fake HOME lacking creds each executor returns a
    structured, NON-retryable failure — in BOTH `.result` (JSON) and `.data` —
    naming the sign-in command, and creates NO task.
  * Model semantics: claude validates a model CLASS (structured retryable error
    on an unknown one; a valid class threaded into result_data); gemini/codex
    forward a concrete id verbatim and default to None.
  * The validate.py guard: an `x-availability.feature` that is not a known
    availability.FEATURES key is rejected.
"""
import asyncio
import json
import os
import re

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.tools import tool_registry


# tool name -> (provider, auth relpath under HOME, sign-in token expected in msg)
_TOOLS = {
    "claude_code_task": ("claude", ".claude/.credentials.json", "claude"),
    "gemini_cli_task": ("gemini", ".gemini/oauth_creds.json", "gemini"),
    "codex_cli_task": ("codex", ".codex/auth.json", "codex login"),
}
_ALL = sorted(_TOOLS)
_FOUR_GROUPS = {"chat", "realtime", "gemini_live", "grok_live"}
_ALLOWED_PARAM_KEYS = {"type", "description"}  # the flat shape voice surfaces accept


@pytest.fixture(autouse=True)
def _fresh():
    """Pick up on-disk schema/executor edits and force the tool_registry snapshot
    (get_*_tools reads it) to reload from disk around every test."""
    registry.invalidate_cache()
    tool_registry.reset_cache()
    yield
    registry.invalidate_cache()
    tool_registry.reset_cache()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake, empty HOME. Returns a helper that plants a provider's real
    credential file so the honest auth check reports 'authenticated'."""
    monkeypatch.setenv("HOME", str(tmp_path))

    def authenticate(provider: str):
        if provider == "claude":
            p = tmp_path / ".claude" / ".credentials.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}")
        elif provider == "gemini":
            p = tmp_path / ".gemini" / "oauth_creds.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}")
        elif provider == "codex":
            p = tmp_path / ".codex" / "auth.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            # auth_mode present (real key + tokens would also be here — the check
            # must read ONLY auth_mode and never surface a secret).
            p.write_text(json.dumps({"auth_mode": "chatgpt",
                                     "OPENAI_API_KEY": "sk-SECRET-do-not-leak",
                                     "tokens": {"access": "SECRET"}}))
        else:
            raise ValueError(provider)

    return authenticate


@pytest.fixture
def captured(monkeypatch):
    """Capture whatever create_task() would build; create no real task."""
    calls = []

    class _FakeTask:
        task_id = "task-cli-fake-001"

    def fake_create_task(task_type, operator=None, prompt=None, result_data=None, **kw):
        calls.append({"task_type": task_type, "operator": operator,
                      "prompt": prompt, "result_data": result_data})
        return _FakeTask()

    from Orchestrator import tasks as tasks_mod
    monkeypatch.setattr(tasks_mod, "create_task", fake_create_task)
    return calls


def _run(tool, params, ctx=None):
    ex = registry.get_executor(tool)
    assert ex is not None, f"{tool} executor failed to load: {registry.load_errors().get(tool)}"
    return asyncio.run(ex(params, ctx or ToolContext(operator="system")))


# ---------------------------------------------------------------------------
# Schema — groups, the D1 mcp guard, flatness, no version
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", _ALL)
def test_groups_are_exactly_the_four_conversational_surfaces(tool):
    t = registry.get_tool(tool)
    assert t is not None, f"{tool} schema missing"
    assert set(t["groups"]) == _FOUR_GROUPS, (
        f"{tool} groups must be exactly {_FOUR_GROUPS}, got {t['groups']}")


@pytest.mark.parametrize("tool", _ALL)
def test_mcp_group_is_never_present(tool):
    """THE D1 invariant guard (mutation target). `mcp` feeds the Tailscale-Funnel
    public MCP server; a CLI-agent tool there is remote arbitrary command exec."""
    t = registry.get_tool(tool)
    assert "mcp" not in t["groups"], (
        f"{tool} must NEVER be in the `mcp` group (public Funnel = RCE)")


@pytest.mark.parametrize("tool", _ALL)
def test_required_is_prompt_only(tool):
    t = registry.get_tool(tool)
    assert t["parameters"].get("required") == ["prompt"]


@pytest.mark.parametrize("tool", _ALL)
def test_params_are_flat_plain_strings(tool):
    t = registry.get_tool(tool)
    props = t["parameters"]["properties"]
    # Exactly the three we justified — prompt, model, cwd. Nothing else.
    assert set(props) == {"prompt", "model", "cwd"}, f"{tool} has unexpected params: {set(props)}"
    for name, p in props.items():
        assert p.get("type") == "string", f"{tool}.{name} must be a plain string"
        assert set(p).issubset(_ALLOWED_PARAM_KEYS), (
            f"{tool}.{name} has non-flat keys {set(p)} (voice surfaces reject enum/nested/x-source)")


@pytest.mark.parametrize("tool", _ALL)
def test_model_and_cwd_are_optional(tool):
    t = registry.get_tool(tool)
    req = t["parameters"].get("required", [])
    assert "model" not in req and "cwd" not in req


@pytest.mark.parametrize("tool", _ALL)
def test_no_version_named_anywhere_in_schema(tool):
    """We never pin a version in a schema (Brandon's GA/version rule + the model
    semantics of these CLIs)."""
    raw = (registry.TOOLS_DIR / tool / "schema.json").read_text()
    assert re.search(r"\d+\.\d+", raw) is None, f"{tool} schema names a version number"


@pytest.mark.parametrize("tool", _ALL)
def test_description_states_permissions_async_poll_cancel(tool):
    d = registry.get_tool(tool)["description"].lower()
    for token in ("permission", "asynchron", "task id", "get_task_status", "cancel"):
        assert token in d, f"{tool} description must mention {token!r}"


def test_claude_model_desc_names_the_class_set():
    m = registry.get_tool("claude_code_task")["parameters"]["properties"]["model"]
    d = m["description"].lower()
    for cls in ("fable", "opus", "sonnet", "haiku"):
        assert cls in d, f"claude model desc must name class {cls!r}"
    assert "class" in d


@pytest.mark.parametrize("tool", ["gemini_cli_task", "codex_cli_task"])
def test_gemini_codex_model_desc_says_concrete_id_not_class(tool):
    d = registry.get_tool(tool)["parameters"]["properties"]["model"]["description"].lower()
    assert "concrete" in d, f"{tool} model desc must say it takes a concrete id"
    # Must NOT advertise class aliases like claude does.
    for cls in ("opus", "sonnet", "haiku", "fable"):
        assert cls not in d, f"{tool} model desc must not claim class resolution ({cls})"


# ---------------------------------------------------------------------------
# Surfacing — each tool in every conversational converter, serialized flat
# ---------------------------------------------------------------------------

def _realtime_params(group, name):
    for t in tool_registry.get_openai_realtime_tools(group):
        if t.get("name") == name:
            assert t["type"] == "function"
            return t["parameters"]
    return None


def _gemini_live_params(name):
    decls = tool_registry.get_gemini_live_tools("gemini_live")
    assert isinstance(decls, list) and decls and "functionDeclarations" in decls[0]
    for d in decls[0]["functionDeclarations"]:
        if d.get("name") == name:
            return d["parameters"]
    return None


def _assert_flat(params):
    assert params is not None
    for name, p in params["properties"].items():
        assert "enum" not in p and "properties" not in p, f"{name} not flat: {p}"


@pytest.mark.parametrize("tool", _ALL)
def test_appears_flat_in_openai_realtime(tool):
    _assert_flat(_realtime_params("realtime", tool))


@pytest.mark.parametrize("tool", _ALL)
def test_appears_flat_in_grok_live(tool):
    _assert_flat(_realtime_params("grok_live", tool))


@pytest.mark.parametrize("tool", _ALL)
def test_appears_flat_in_gemini_live(tool):
    _assert_flat(_gemini_live_params(tool))


@pytest.mark.parametrize("tool", _ALL)
def test_appears_in_chat_group(tool):
    """Chat exposure = membership in the `chat` group (all tiers), parallel to
    the voice-serializer checks. (The live tier-2 semantic path needs a network
    embedding + reload and is not hermetic.)"""
    assert tool in tool_registry.get_group_tool_names("chat")
    names = [t["function"]["name"] for t in tool_registry.get_openai_rest_tools("chat")]
    assert tool in names


# ---------------------------------------------------------------------------
# Auth fail-fast — structured, non-retryable, no task, payload in .result + .data
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", _ALL)
def test_unauthenticated_fails_fast_and_creates_no_task(tool, home, captured):
    provider, _relpath, signin = _TOOLS[tool]
    # HOME is fresh/empty -> NO cred file planted -> not authenticated.
    res = _run(tool, {"prompt": "do work"})
    assert isinstance(res, ToolResult)
    assert res.success is False
    payload = json.loads(res.result)          # chat path forwards .result (a string)
    assert payload["success"] is False
    assert payload["retryable"] is False       # signing in is required; retry won't help
    assert signin in payload["reason"], f"reason must name the sign-in command {signin!r}"
    assert res.data == payload                 # voice surfaces read rich_result()/.data
    assert captured == [], "no task may be created for an unauthenticated CLI"


def test_claude_authenticated_creates_yolo_task(home, captured):
    home("claude")
    res = _run("claude_code_task", {"prompt": "refactor x"})
    assert res.success is True
    assert len(captured) == 1
    rd = captured[0]["result_data"]
    assert rd["provider"] == "claude"
    assert rd["permission_mode"] == "yolo"
    from Orchestrator.models import TaskType
    assert captured[0]["task_type"] == TaskType.CLI_AGENT
    assert captured[0]["operator"] == "system"


@pytest.mark.parametrize("tool,provider", [("gemini_cli_task", "gemini"),
                                           ("codex_cli_task", "codex")])
def test_gemini_codex_authenticated_creates_task(tool, provider, home, captured):
    home(provider)
    res = _run(tool, {"prompt": "do work"})
    assert res.success is True
    assert captured[0]["result_data"]["provider"] == provider


def test_codex_auth_reads_only_auth_mode_never_leaks_secret(home, captured):
    """The codex auth file carries a token + a stored key; the check reads ONLY
    auth_mode, and nothing secret may appear in the returned ToolResult."""
    home("codex")
    res = _run("codex_cli_task", {"prompt": "x"})
    blob = json.dumps({"result": res.result, "data": res.data})
    # The fixture seeds the key value + token with a distinctive sentinel; neither
    # may appear in anything the tool returns. (Don't test for "sk-" — it collides
    # with the "task-..." id substring.)
    assert "SECRET" not in blob and "sk-SECRET" not in blob


def test_codex_empty_auth_mode_is_unauthenticated(tmp_path, monkeypatch, captured):
    """Presence != mode: an auth.json with a blank auth_mode is NOT signed in."""
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / ".codex" / "auth.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"auth_mode": "", "OPENAI_API_KEY": "sk-x"}))
    res = _run("codex_cli_task", {"prompt": "x"})
    assert res.success is False
    assert captured == []


def test_wizard_false_positive_markers_do_not_authenticate(tmp_path, monkeypatch, captured):
    """.gemini/settings.json and .codex/config.toml exist after one run, signed
    in or not — they must NOT count as authenticated (the headless.py WARNING)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "settings.json").write_text("{}")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("")
    assert _run("gemini_cli_task", {"prompt": "x"}).success is False
    assert _run("codex_cli_task", {"prompt": "x"}).success is False
    assert captured == []


# ---------------------------------------------------------------------------
# Model semantics — claude class validation vs gemini/codex verbatim id
# ---------------------------------------------------------------------------

def test_claude_unknown_class_is_structured_retryable_no_task(home, captured):
    home("claude")
    res = _run("claude_code_task", {"prompt": "x", "model": "banana"})
    assert res.success is False
    payload = json.loads(res.result)
    assert payload["retryable"] is True          # retry WITH a valid class helps
    for cls in ("fable", "opus", "sonnet", "haiku"):
        assert cls in payload["reason"], f"reason must name valid class {cls!r}"
    assert res.data == payload
    assert captured == [], "an unresolvable class must create no task"


def test_claude_valid_class_threaded_into_result_data(home, captured):
    home("claude")
    res = _run("claude_code_task", {"prompt": "x", "model": "opus"})
    assert res.success is True
    assert captured[0]["result_data"]["model"] == "opus"


@pytest.mark.parametrize("tool,provider", [("gemini_cli_task", "gemini"),
                                           ("codex_cli_task", "codex")])
def test_gemini_codex_omitted_model_is_none(tool, provider, home, captured):
    home(provider)
    res = _run(tool, {"prompt": "x"})
    assert res.success is True
    assert captured[0]["result_data"]["model"] is None


@pytest.mark.parametrize("tool,provider", [("gemini_cli_task", "gemini"),
                                           ("codex_cli_task", "codex")])
def test_gemini_codex_concrete_id_forwarded_verbatim(tool, provider, home, captured):
    home(provider)
    res = _run(tool, {"prompt": "x", "model": "some-exact-model-id"})
    assert res.success is True
    assert captured[0]["result_data"]["model"] == "some-exact-model-id"


# ---------------------------------------------------------------------------
# result_data threading — cwd + permission_mode
# ---------------------------------------------------------------------------

def test_cwd_threaded_and_permission_mode_yolo(home, captured):
    home("claude")
    res = _run("claude_code_task", {"prompt": "x", "cwd": "/srv/project"})
    assert res.success is True
    rd = captured[0]["result_data"]
    assert rd["cwd"] == "/srv/project"
    assert rd["permission_mode"] == "yolo"
    assert set(rd) == {"provider", "model", "cwd", "permission_mode"}


def test_cwd_omitted_is_none(home, captured):
    home("claude")
    res = _run("claude_code_task", {"prompt": "x"})
    assert captured[0]["result_data"]["cwd"] is None


def test_missing_prompt_short_circuits_before_auth(tmp_path, monkeypatch, captured):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = _run("claude_code_task", {})
    assert res.success is False
    assert "prompt" in res.result.lower()
    assert captured == []


# ---------------------------------------------------------------------------
# validate.py guard — x-availability.feature must be a known FEATURES key
# ---------------------------------------------------------------------------

def test_validate_flags_unknown_feature_key():
    from Orchestrator.toolvault import validate as v
    errs = v._x_availability_feature_errors(
        {"x-availability": {"provider": "cli_agent", "feature": "cli_agent"}})
    assert errs and any("feature" in e for e in errs)


def test_validate_accepts_known_feature_and_absent_feature():
    from Orchestrator.toolvault import validate as v
    assert v._x_availability_feature_errors(
        {"x-availability": {"provider": "openai", "feature": "image"}}) == []
    assert v._x_availability_feature_errors(
        {"x-availability": {"provider": "grok_x", "requires_env": ["XAI_API_KEY"]}}) == []
    assert v._x_availability_feature_errors({}) == []


def test_validate_all_fails_on_unknown_feature(tmp_path, monkeypatch):
    """The guard is wired into validate_all(), not just a helper."""
    from Orchestrator.toolvault import validate as v
    tools = tmp_path / "tools"
    bad = tools / "bogus_gated_tool"
    bad.mkdir(parents=True)
    (bad / "schema.json").write_text(json.dumps({
        "name": "bogus_gated_tool",
        "description": "A tool with a bogus availability feature.",
        "category": "utility",
        "groups": ["chat"],
        "tier": 2,
        "parameters": {"type": "object", "properties": {}, "required": []},
        "x-availability": {"provider": "cli_agent", "feature": "cli_agent"},
    }))
    monkeypatch.setattr(registry, "TOOLS_DIR", tools)
    report = v.validate_all()
    assert report["ok"] is False
    assert "bogus_gated_tool" in report["errors"]
    assert any("feature" in m for m in report["errors"]["bogus_gated_tool"])
