#!/usr/bin/env python3
"""
test_tool_injection.py — unit tests for the shared ToolVault discovery to specs
helper (Orchestrator.local_provider.tool_injection).

build_injected_tools performs the semantic search + per-hit read that both
POST /local/tools/search and the upcoming POST /local/turn/prepare use to
inject directly-callable ToolVault tools. These tests drive the helper in
isolation by patching its module-level meta_tool reference, exercising:
  - top-K spec shaping ({name, description, parameters})
  - blank-query short-circuit (no meta_tool call)
  - empty-matches yields an empty list
  - never-raises contract (any meta_tool error yields an empty list)
  - fault isolation (unreadable hit skipped, readable hit kept)
"""

import types
from unittest import mock

from Orchestrator.local_provider import tool_injection as ti


def _result(success, data):
    """Tiny stand-in for a meta_tool ToolResult (only .success/.data read)."""
    return types.SimpleNamespace(success=success, data=data)


def _desc(name):
    """Deterministic per-tool description used by the fakes."""
    return "desc-for-{}".format(name)


def test_returns_topk_specs():
    """search yields 2 matches, each read OK, gives 2 spec dicts."""

    def fake_execute(action, **params):
        if action == "search":
            return _result(True, {"matches": [
                {"name": "roll_dice"},
                {"name": "generate_image"},
            ]})
        if action == "read":
            name = params.get("tool_name")
            return _result(True, {"description": _desc(name), "schema": {"type": "object"}})
        return _result(False, None)

    with mock.patch.object(ti.meta_tool, "execute", side_effect=fake_execute):
        tools = ti.build_injected_tools("roll dice", k=5)

    assert len(tools) == 2
    by_name = {t["name"]: t for t in tools}
    assert set(by_name) == {"roll_dice", "generate_image"}
    for name, spec in by_name.items():
        assert spec["description"] == _desc(name)
        assert spec["parameters"] == {"type": "object"}


def test_empty_query_returns_empty():
    """Blank/whitespace query gives an empty list and meta_tool is NEVER called."""
    with mock.patch.object(ti.meta_tool, "execute") as exec_mock:
        assert ti.build_injected_tools("", k=5) == []
        assert ti.build_injected_tools("   ", k=5) == []
        exec_mock.assert_not_called()


def test_no_matches_returns_empty():
    """search succeeds but yields zero matches, returns an empty list."""

    def fake_execute(action, **params):
        if action == "search":
            return _result(True, {"matches": []})
        return _result(False, None)

    with mock.patch.object(ti.meta_tool, "execute", side_effect=fake_execute):
        assert ti.build_injected_tools("nothing here", k=5) == []


def test_never_raises_on_meta_tool_error():
    """Any exception from meta_tool is caught and an empty list is returned."""

    def boom(action, **params):
        raise RuntimeError("meta_tool exploded")

    with mock.patch.object(ti.meta_tool, "execute", side_effect=boom):
        assert ti.build_injected_tools("anything", k=5) == []


def test_skips_unreadable_tool():
    """A hit whose read fails (stale/renamed) is skipped; readable hit kept."""

    def fake_execute(action, **params):
        if action == "search":
            return _result(True, {"matches": [
                {"name": "good_tool"},
                {"name": "stale_tool"},
            ]})
        if action == "read":
            name = params.get("tool_name")
            if name == "good_tool":
                return _result(True, {"description": "works", "schema": {"type": "object"}})
            return _result(False, None)
        return _result(False, None)

    with mock.patch.object(ti.meta_tool, "execute", side_effect=fake_execute):
        tools = ti.build_injected_tools("do something", k=5)

    assert len(tools) == 1
    assert tools[0]["name"] == "good_tool"


def test_excludes_self_delegating_device_control_tools():
    """The on-device model must never be offered control_phone /
    control_android_device / use_computer (they delegate device control back to this
    phone -> recursion). They're dropped even if search ranks them first, WITHOUT
    consuming a top-k slot (filtered before the slice), so usable tools survive."""

    def fake_execute(action, **params):
        if action == "search":
            return _result(True, {"matches": [
                {"name": "control_phone"},
                {"name": "control_android_device"},
                {"name": "use_computer"},
                {"name": "open_app"},
            ]})
        if action == "read":
            name = params.get("tool_name")
            return _result(True, {"description": _desc(name), "schema": {"type": "object"}})
        return _result(False, None)

    with mock.patch.object(ti.meta_tool, "execute", side_effect=fake_execute):
        tools = ti.build_injected_tools("control the phone", k=2)

    names = {t["name"] for t in tools}
    assert names.isdisjoint({"control_phone", "control_android_device", "use_computer"})
    # open_app survived: the excluded hits did NOT eat the k=2 slots (filter-before-slice).
    assert "open_app" in names
