"""chat_routes is one namespace over the chat-route parts (audit rank 74).

The chat routes were split into Orchestrator/routes/chat/ (context, media,
providers, save, cu, stream, nonstream). Every importer and test still uses
Orchestrator.routes.chat_routes, so these pin what that relies on: every
name of every part is on chat_routes, no two parts bind one name to different
objects, and a stub set on chat_routes (monkeypatch, mock.patch.object)
reaches every part that uses the name and is undone there too.
"""
from unittest import mock

import pytest

import Orchestrator.routes.chat_routes as cr


def _part_names():
    for part in cr._PARTS:
        for name, value in vars(part).items():
            if not (name.startswith("__") and name.endswith("__")):
                yield part, name, value


def test_every_part_name_is_on_the_namespace():
    missing = [(p.__name__, n) for p, n, v in _part_names() if getattr(cr, n, object()) is not v]
    assert missing == []


def test_no_two_parts_bind_one_name_to_different_objects():
    seen, clashes = {}, []
    for part, name, value in _part_names():
        if name in seen and seen[name][1] is not value:
            clashes.append((name, seen[name][0], part.__name__))
        seen.setdefault(name, (part.__name__, value))
    assert clashes == []


def test_the_names_the_single_module_exported_are_still_there():
    # A sample of what importers and tests read: header imports (some unused by
    # the parts), part-defined helpers, and the re-exported adapter names.
    for name in ("read_text_safe", "httpx", "requests", "perform_mint", "create_task",
                 "TOOLVAULT_ENABLED", "VOL_PATH", "app", "_ORIGIN_DEVICE_ID", "_get_tools",
                 "call_gemini", "chat_stream_post", "chat_save", "stream_computer_use",
                 "execute_bash_command", "_cu_save_to_blackbox", "DeclinedReply",
                 "_guard_anthropic_history", "_browser_task_event", "media_auto_route"):
        assert hasattr(cr, name), name


def test_a_stub_set_on_the_namespace_reaches_every_part_and_is_undone(monkeypatch):
    def fake(*a, **k):
        return "stub"

    original = cr._cu_save_to_blackbox
    users = [p for p in cr._PARTS if "_cu_save_to_blackbox" in vars(p)]
    assert len(users) >= 2  # defined in save, used by cu
    monkeypatch.setattr(cr, "_cu_save_to_blackbox", fake)
    assert all(vars(p)["_cu_save_to_blackbox"] is fake for p in users)
    monkeypatch.undo()
    assert cr._cu_save_to_blackbox is original
    assert all(vars(p)["_cu_save_to_blackbox"] is original for p in users)


def test_mock_patch_object_reaches_the_part_that_reads_it():
    with mock.patch.object(cr, "AUTO_ENABLE", "patched"):
        assert cr._save.AUTO_ENABLE == "patched"
        assert cr._mint_gate().auto_enable == "patched"
    assert cr._save.AUTO_ENABLE is cr.AUTO_ENABLE


def test_a_new_attribute_stays_on_the_namespace(monkeypatch):
    monkeypatch.setattr(cr, "_test_only_marker", 1, raising=False)
    assert all("_test_only_marker" not in vars(p) for p in cr._PARTS)
    monkeypatch.undo()
    assert not hasattr(cr, "_test_only_marker")


@pytest.mark.parametrize("route", ["/chat/stream", "/chat/save", "/chat", "/chat/cu-status",
                                   "/chat/cu-stop", "/chat/cu-new-session", "/api/media/list"])
def test_the_routes_are_still_registered(route):
    paths = {getattr(r, "path", None) for r in cr.app.routes}
    assert route in paths
