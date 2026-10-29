"""The live registry enforces the same schema guards as the CI gate
(audit O17-toolvault-runtime-04, O20-mcp-server-05, O20-mcp-server-17).

The injector picks schema edits up through the registry's mtime cache, so a
schema saved on a running box is served at once. Three guards used to run only
in ``validate.py`` (the CI gate): an unknown ``x-availability.feature`` raised
KeyError inside ``filter_available`` and took down every injection and the MCP
``list_tools``; an array without ``items`` closed every Gemini Live session with
1007; and a ``cli_agent`` tool in the public ``mcp`` group would be served over
the Funnel. They now run in ``schema_spec.validate_module_dict``, so the
registry excludes such a module at load time, and ``enabled_providers`` is
fail-soft for a feature it does not know.

Also here: the registry's warnings go to stderr (stdout is the MCP stdio
JSON-RPC channel), and importing the registry stays light -- the toolvault and
tools packages resolve their re-exports lazily, so the lean MCP venv never
imports the injector, the embeddings layer or the tool executor.

Hermetic: tmp tool dirs, no network, subprocesses run with ``python -I``-style
isolation from the box (BLACKBOX_ROOT points at the checkout under test).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from Orchestrator.toolvault import availability, injector, registry, schema_spec
from Orchestrator.tools import tool_registry

_REPO = Path(__file__).resolve().parents[3]


def _schema(name, **extra):
    d = {
        "name": name,
        "description": f"The {name} tool.",
        "category": "utility",
        "groups": ["chat", "mcp", "gemini_live"],
        "tier": 1,
        "parameters": {"type": "object",
                       "properties": {"q": {"type": "string", "description": "q"}},
                       "required": []},
    }
    d.update(extra)
    return d


def _write(tools_dir, schema):
    folder = tools_dir / schema["name"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "schema.json").write_text(json.dumps(schema))


@pytest.fixture
def tools_dir(tmp_path, monkeypatch):
    d = tmp_path / "tools"
    d.mkdir()
    monkeypatch.setattr(registry, "TOOLS_DIR", d)
    registry.invalidate_cache()
    tool_registry.reset_cache()
    yield d
    registry.invalidate_cache()
    tool_registry.reset_cache()


# ---------------------------------------------------------------------------
# The three CI-only guards now run on the live load path
# ---------------------------------------------------------------------------

def test_unknown_availability_feature_is_excluded_at_load(tools_dir, monkeypatch):
    monkeypatch.setattr(availability, "_read_env", lambda: {})
    _write(tools_dir, _schema("good_tool"))
    _write(tools_dir, _schema("bogus_feature_tool",
                              **{"x-availability": {"feature": "video_typo",
                                                    "provider": "veo"}}))
    names = [t["name"] for t in registry.load_canonical()]
    assert names == ["good_tool"]
    errs = registry.load_errors()["bogus_feature_tool"]
    assert any("video_typo" in e for e in errs), errs
    # Every consumer of the registry keeps working: chat injection, the
    # group getters and the MCP catalog.
    assert [t["name"] for t in tool_registry.get_tools_by_group("chat")] == ["good_tool"]
    tools, _ = injector.inject_for_prompt("", "anthropic", group="chat")
    assert "good_tool" in {t.get("name") for t in tools}
    assert tool_registry.get_group_tool_names("mcp") == ["good_tool"]


def test_array_without_items_is_excluded_at_load(tools_dir):
    bad = _schema("flat_array_tool")
    bad["parameters"]["properties"]["values"] = {"type": "array",
                                                 "description": "rows"}
    _write(tools_dir, bad)
    _write(tools_dir, _schema("good_tool"))
    assert [t["name"] for t in registry.load_canonical("gemini_live")] == ["good_tool"]
    errs = registry.load_errors()["flat_array_tool"]
    assert any("items" in e for e in errs), errs


def test_nested_array_without_items_is_excluded_at_load(tools_dir):
    bad = _schema("grid_tool")
    bad["parameters"]["properties"]["grid"] = {
        "type": "array", "items": {"type": "array"}, "description": "2-D"}
    _write(tools_dir, bad)
    assert registry.load_canonical() == []
    assert "grid_tool" in registry.load_errors()


def test_cli_agent_tool_in_mcp_group_is_never_served(tools_dir):
    _write(tools_dir, _schema("rogue_cli_task", category="cli_agent",
                              groups=["chat", "mcp"]))
    _write(tools_dir, _schema("clean_cli_task", category="cli_agent",
                              groups=["chat"]))
    assert [t["name"] for t in registry.load_canonical("mcp")] == []
    assert [t["name"] for t in registry.load_canonical()] == ["clean_cli_task"]
    assert any("mcp" in e for e in registry.load_errors()["rogue_cli_task"])
    assert tool_registry.get_group_tool_names("mcp") == []


def test_a_schema_edit_on_a_running_box_cannot_break_injection(tools_dir, monkeypatch):
    """The hot-reload path itself: an edit picked up by the mtime cache."""
    monkeypatch.setattr(availability, "_read_env", lambda: {})
    _write(tools_dir, _schema("edited_tool"))
    assert [t["name"] for t in registry.load_canonical()] == ["edited_tool"]
    path = tools_dir / "edited_tool" / "schema.json"
    _write(tools_dir, _schema("edited_tool",
                              **{"x-availability": {"feature": "nonexistent",
                                                    "provider": "x"}}))
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))
    assert registry.load_canonical() == []
    tools, _ = injector.inject_for_prompt("", "anthropic", group="chat")
    assert "edited_tool" not in {t.get("name") for t in tools}


def test_validate_module_dict_runs_the_guards_for_every_caller():
    """The guards live in schema_spec, so any caller of validate_module_dict
    (registry, validate.py, the tests that vet one schema) gets them."""
    bad_feature = _schema("t", **{"x-availability": {"feature": "nope", "provider": "p"}})
    assert any("nope" in e for e in
               schema_spec.validate_module_dict(bad_feature, "t", known_sources=set()))
    rogue = _schema("t", category="cli_agent", groups=["mcp"])
    assert schema_spec.validate_module_dict(rogue, "t", known_sources=set())
    flat = _schema("t")
    flat["parameters"]["properties"]["a"] = {"type": "array"}
    assert schema_spec.validate_module_dict(flat, "t", known_sources=set())
    # An explicit feature set is honoured (the registry passes FEATURES' keys).
    known = _schema("t", **{"x-availability": {"feature": "custom", "provider": "p"}})
    assert schema_spec.validate_module_dict(
        known, "t", known_sources=set(), known_features={"custom"}) == []


# ---------------------------------------------------------------------------
# enabled_providers is fail-soft for an unknown feature
# ---------------------------------------------------------------------------

def test_enabled_providers_unknown_feature_is_empty_not_keyerror(monkeypatch, capsys):
    monkeypatch.setattr(availability, "_read_env", lambda: {})
    assert availability.enabled_providers("no_such_feature") == set()
    err = capsys.readouterr()
    assert "no_such_feature" in err.err
    assert err.out == ""


def test_filter_available_drops_an_unknown_feature_entry(monkeypatch):
    monkeypatch.setattr(availability, "_read_env", lambda: {})
    entries = [{"name": "plain"},
               {"name": "odd", "x-availability": {"feature": "bogus", "provider": "p"}}]
    assert [e["name"] for e in availability.filter_available(entries)] == ["plain"]
    assert availability.is_available(entries[1]) is False


# ---------------------------------------------------------------------------
# Registry warnings go to stderr, never stdout (the stdio JSON-RPC channel)
# ---------------------------------------------------------------------------

def test_registry_warnings_go_to_stderr(tools_dir, capsys):
    (tools_dir / "broken").mkdir()
    (tools_dir / "broken" / "schema.json").write_text("{not json")
    _write(tools_dir, _schema("invalid_tool", tier=9))
    registry.load_canonical()
    out = capsys.readouterr()
    assert out.out == ""
    assert "broken" in out.err and "invalid_tool" in out.err


def test_bad_executor_warning_goes_to_stderr(tools_dir, capsys):
    _write(tools_dir, _schema("noexec_tool"))
    (tools_dir / "noexec_tool" / "executor.py").write_text("x = 1\n")
    assert registry.get_executor("noexec_tool") is None
    out = capsys.readouterr()
    assert out.out == ""
    assert "noexec_tool" in out.err


def test_unknown_x_source_warning_goes_to_stderr(capsys):
    from Orchestrator.toolvault import resolvers
    resolvers.resolve_schema({"parameters": {"type": "object", "properties": {
        "p": {"type": "string", "x-source": "not_a_source"}}}})
    out = capsys.readouterr()
    assert out.out == ""
    assert "not_a_source" in out.err


# ---------------------------------------------------------------------------
# Import contract: the registry path is light (lean MCP venv)
# ---------------------------------------------------------------------------

# Orchestrator.config needs the whole backend stack. The resolvers try it
# first and fall back to config.ini, so only the bare registry import must
# keep it out (in the lean MCP venv the import fails and the fallback runs).
_HEAVY = {"Orchestrator.toolvault.injector", "Orchestrator.toolvault.embeddings",
          "Orchestrator.tools.blackbox_tools", "Orchestrator.contacts"}


def _loaded_after(code: str) -> set:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env["BLACKBOX_ROOT"] = str(_REPO)
    env["PYTHONPATH"] = str(_REPO)
    out = subprocess.run(
        [sys.executable, "-c", code + "\nimport json, sys\n"
         "print(json.dumps(sorted(m for m in sys.modules if m.startswith('Orchestrator'))))"],
        cwd=str(_REPO), env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return set(json.loads(out.stdout.strip().splitlines()[-1]))


def test_importing_the_registry_does_not_import_the_injector():
    loaded = _loaded_after("from Orchestrator.toolvault import registry, availability, resolvers")
    heavy = _HEAVY | {"Orchestrator.config"}
    assert not loaded & heavy, sorted(loaded & heavy)


def test_mcp_tool_catalog_builds_without_the_heavy_modules():
    # The resolved mcp group is what get_mcp_tools() converts (the conversion
    # itself needs the mcp SDK, which this venv lacks; MCP/test_mcp_hardening.py
    # covers it in the lean MCP venv).
    loaded = _loaded_after(
        "from Orchestrator.tools import tool_registry\n"
        "assert len(tool_registry._resolved_group('mcp')) > 0")
    assert not loaded & _HEAVY, sorted(loaded & _HEAVY)


def test_package_reexports_still_resolve():
    import Orchestrator.toolvault as tv
    import Orchestrator.tools as tools_pkg
    assert tv.inject_for_prompt is injector.inject_for_prompt
    assert tv.load_canonical is registry.load_canonical
    assert tv.ToolResult is tools_pkg.ToolResult
    from Orchestrator.tools import BlackBoxToolExecutor, blackbox_tools
    assert BlackBoxToolExecutor is blackbox_tools.BlackBoxToolExecutor
    assert sorted(tv.__all__) == sorted(set(tv.__all__))
    with pytest.raises(AttributeError):
        tv.not_a_name  # noqa: B018
    with pytest.raises(AttributeError):
        tools_pkg.not_a_name  # noqa: B018
