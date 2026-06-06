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


def test_cache_invalidates_on_deletion_of_non_max_mtime_module(tools_dir):
    """Deleting a module whose mtime is NOT the max must still cache-miss.

    A max-mtime-only key leaves max unchanged when the deleted module wasn't
    the newest, so the stale entry keeps being served. Keying on the full file
    set fixes this.
    """
    beta = _write_module(tools_dir, "beta", _valid_schema("beta"))
    alpha = _write_module(tools_dir, "alpha", _valid_schema("alpha"))
    # beta older than alpha; deleting beta leaves alpha (the max) untouched.
    os.utime(beta, (1000, 1000))
    os.utime(alpha, (5000, 5000))

    first = registry.load_canonical()
    assert [t["name"] for t in first] == ["alpha", "beta"]

    # Delete beta's folder entirely (max mtime, alpha=5000, is unchanged).
    import shutil
    shutil.rmtree(tools_dir / "beta")

    second = registry.load_canonical()
    assert [t["name"] for t in second] == ["alpha"]


def test_cache_invalidates_on_add_of_older_mtime_module(tools_dir):
    """Adding a module whose mtime is BELOW the current max must cache-miss.

    git checkout / cp -p / tar / rsync preserve source mtimes, so a freshly
    added module can have an mtime older than an existing one. A max-mtime key
    misses it entirely; keying on the full file set fixes this.
    """
    alpha = _write_module(tools_dir, "alpha", _valid_schema("alpha"))
    os.utime(alpha, (5000, 5000))

    first = registry.load_canonical()
    assert [t["name"] for t in first] == ["alpha"]

    # Add delta with an OLDER mtime than alpha (2000 < 5000).
    delta = _write_module(tools_dir, "delta", _valid_schema("delta"))
    os.utime(delta, (2000, 2000))

    second = registry.load_canonical()
    assert "delta" in [t["name"] for t in second]


# ---------------------------------------------------------------------------
# Cache isolation — returned dicts must not be live cache references
# ---------------------------------------------------------------------------

def test_returned_entries_are_isolated_from_cache(tools_dir):
    """Mutating a returned entry must not poison the cache for later callers."""
    _write_module(tools_dir, "send_sms", _valid_schema("send_sms", description="original"))

    first = registry.load_canonical()
    entry = first[0]
    entry["description"] = "POISONED"
    entry["groups"].append("phone")

    second = registry.load_canonical()
    assert second[0]["description"] == "original"
    assert "phone" not in second[0]["groups"]


def test_get_tool_returns_isolated_copy(tools_dir):
    """get_tool must also return a copy, not the live cached dict."""
    _write_module(tools_dir, "send_sms", _valid_schema("send_sms", description="original"))

    entry = registry.get_tool("send_sms")
    entry["description"] = "POISONED"
    entry["groups"].append("phone")

    again = registry.get_tool("send_sms")
    assert again["description"] == "original"
    assert "phone" not in again["groups"]


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
