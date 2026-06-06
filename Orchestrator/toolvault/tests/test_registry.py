"""Tests for the ToolVault v2 module registry (Task 1.1).

The registry loads + validates every ``ToolVault/tools/<name>/schema.json`` into
an in-memory canonical list (the single source of truth), with a cheap
max-mtime cache keyed off ``TOOLS_DIR``.

Tests are hermetic: they point ``registry.TOOLS_DIR`` at a ``tmp_path`` and call
``invalidate_cache()`` — they never read the real ``ToolVault/tools``.
"""

import json
import os

import pytest

from Orchestrator.toolvault import registry


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _valid_schema(name, groups=("chat", "mcp"), tier=2, description="A tool."):
    return {
        "name": name,
        "description": description,
        "category": "communication",
        "groups": list(groups),
        "tier": tier,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A query"},
            },
            "required": ["query"],
        },
    }


def _write_module(tools_dir, name, schema=None, raw=None):
    """Write a module folder with a schema.json (dict -> JSON, or raw string)."""
    folder = tools_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "schema.json"
    if raw is not None:
        path.write_text(raw)
    else:
        path.write_text(json.dumps(schema if schema is not None else _valid_schema(name)))
    return path


@pytest.fixture
def tools_dir(tmp_path, monkeypatch):
    """Point the registry at an empty tmp tools dir and reset its cache."""
    d = tmp_path / "tools"
    d.mkdir()
    monkeypatch.setattr(registry, "TOOLS_DIR", d)
    registry.invalidate_cache()
    yield d
    registry.invalidate_cache()


# ---------------------------------------------------------------------------
# load_canonical / load_errors
# ---------------------------------------------------------------------------

def test_two_valid_one_invalid(tools_dir):
    _write_module(tools_dir, "send_sms", _valid_schema("send_sms"))
    _write_module(tools_dir, "web_search", _valid_schema("web_search"))
    # Invalid: tier out of range.
    _write_module(tools_dir, "bad_tier", _valid_schema("bad_tier", tier=9))

    canonical = registry.load_canonical()
    names = [t["name"] for t in canonical]
    assert names == ["send_sms", "web_search"]  # sorted, invalid excluded

    errors = registry.load_errors()
    assert "bad_tier" in errors
    assert errors["bad_tier"]  # non-empty list
    assert "send_sms" not in errors
    assert "web_search" not in errors


def test_malformed_json_recorded_as_error(tools_dir):
    _write_module(tools_dir, "good", _valid_schema("good"))
    _write_module(tools_dir, "broken", raw="{not valid json,,,")

    canonical = registry.load_canonical()
    assert [t["name"] for t in canonical] == ["good"]

    errors = registry.load_errors()
    assert "broken" in errors
    assert errors["broken"]


def test_load_canonical_group_filter(tools_dir):
    _write_module(tools_dir, "phone_tool", _valid_schema("phone_tool", groups=["chat", "phone"]))
    _write_module(tools_dir, "chat_only", _valid_schema("chat_only", groups=["chat"]))

    phone = registry.load_canonical(group="phone")
    assert [t["name"] for t in phone] == ["phone_tool"]

    chat = registry.load_canonical(group="chat")
    assert sorted(t["name"] for t in chat) == ["chat_only", "phone_tool"]


def test_get_tool(tools_dir):
    _write_module(tools_dir, "send_sms", _valid_schema("send_sms"))

    entry = registry.get_tool("send_sms")
    assert entry is not None
    assert entry["name"] == "send_sms"
    assert entry["category"] == "communication"

    assert registry.get_tool("nonexistent") is None


def test_invalid_module_not_returned_by_get_tool(tools_dir):
    _write_module(tools_dir, "bad_tier", _valid_schema("bad_tier", tier=9))
    assert registry.get_tool("bad_tier") is None


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def test_cache_auto_invalidates_on_mtime_change(tools_dir):
    path = _write_module(tools_dir, "send_sms", _valid_schema("send_sms", description="original"))

    first = registry.load_canonical()
    assert first[0]["description"] == "original"

    # Rewrite with a new description and bump mtime into the future so the
    # max-mtime sweep reliably sees the change in a fast test.
    path.write_text(json.dumps(_valid_schema("send_sms", description="updated")))
    future = path.stat().st_mtime + 1000
    os.utime(path, (future, future))

    second = registry.load_canonical()
    assert second[0]["description"] == "updated"


def test_manual_invalidate_cache(tools_dir):
    _write_module(tools_dir, "send_sms", _valid_schema("send_sms"))
    registry.load_canonical()
    registry.invalidate_cache()
    # Still works after manual bust.
    assert [t["name"] for t in registry.load_canonical()] == ["send_sms"]


# ---------------------------------------------------------------------------
# Empty / missing dir
# ---------------------------------------------------------------------------

def test_empty_dir(tools_dir):
    assert registry.load_canonical() == []
    assert registry.load_errors() == {}


def test_missing_dir(tmp_path, monkeypatch):
    missing = tmp_path / "does_not_exist"
    monkeypatch.setattr(registry, "TOOLS_DIR", missing)
    registry.invalidate_cache()
    assert registry.load_canonical() == []
    assert registry.load_errors() == {}


# ---------------------------------------------------------------------------
# Alias / executor-name resolution
# ---------------------------------------------------------------------------

def test_resolve_alias():
    assert registry.resolve_alias("search_memory") == "search_snapshots"
    assert registry.resolve_alias("get_recent_snapshots") == "list_recent_snapshots"
    # Non-alias passes through unchanged.
    assert registry.resolve_alias("web_search") == "web_search"


def test_resolve_executor_name():
    assert registry.resolve_executor_name("search_snapshots") == "search_memory"
    # Not in the map -> identity.
    assert registry.resolve_executor_name("web_search") == "web_search"
