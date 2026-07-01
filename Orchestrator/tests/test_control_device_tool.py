"""Hermetic tests for the control_device ToolVault executor (M2 loop + M3 routing).

The executor is loaded directly from its file (the same mechanism the registry uses).
mesh.resolve_device (M3 origin-aware routing) + the frontier loop are monkeypatched, so no
tailscale, no sockets, no real phone, and no Gemini are touched. Proves the executor resolves a
device via resolve_device (passing ctx.origin_device_id + the explicit `device` param through),
invokes the loop, maps DeviceResolutionError.kind → data["error_kind"] (incl. the
never-silent-retarget origin_mismatch), and returns a well-formed ToolResult.
"""
import asyncio
import importlib.util
from pathlib import Path

from Orchestrator.toolvault.context import ToolContext
from Orchestrator.local_provider.mesh import Node, DeviceResolutionError
from Orchestrator.frontier_agent_loop import FrontierResult

_EXEC_PATH = (Path(__file__).resolve().parents[2]
              / "ToolVault" / "tools" / "control_device" / "executor.py")
_spec = importlib.util.spec_from_file_location("control_device_executor", _EXEC_PATH)
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)

NODE = Node(hostname="brandon-fold6", dns_name="brandon-fold6.tailnet-abc.ts.net",
            ip="100.88.0.7", online=True, os="android")
CTX = ToolContext(operator="Brandon", base_url="http://localhost:9091")


def _run(coro):
    return asyncio.run(coro)


def _resolves_to(node):
    def _f(**kwargs):
        return node
    return _f


def _raises(kind, message="nope", detail=None):
    def _f(**kwargs):
        raise DeviceResolutionError(kind, message, detail=detail)
    return _f


def test_requires_task():
    res = _run(cd.execute({}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "invalid_argument"


def test_no_reachable_device(monkeypatch):
    monkeypatch.setattr(cd.mesh, "resolve_device", _raises("no_device"))
    res = _run(cd.execute({"task": "open maps"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "no_device"


def test_happy_path_returns_toolresult(monkeypatch):
    monkeypatch.setattr(cd.mesh, "resolve_device", _resolves_to(NODE))
    seen = {}

    async def fake_loop(*, device_base_url, task, operator, model, capability):
        seen.update(device_base_url=device_base_url, task=task, operator=operator)
        return FrontierResult(True, "Opened Maps and searched for coffee.",
                              steps=4, device=device_base_url)

    monkeypatch.setattr(cd.frontier_agent_loop, "run_frontier_loop", fake_loop)
    res = _run(cd.execute({"task": "open maps and search coffee"}, CTX))
    assert res.success is True
    assert res.result == "Opened Maps and searched for coffee."
    assert res.data["steps"] == 4
    assert res.data["device"] == NODE.dns_name           # dns_name preferred
    # addressed the resolved node on the control port
    assert seen["device_base_url"] == f"http://{NODE.dns_name}:{cd.REMOTE_CONTROL_PORT}"
    assert seen["operator"] == "Brandon"


def test_structured_error_surfaces(monkeypatch):
    monkeypatch.setattr(cd.mesh, "resolve_device", _resolves_to(NODE))

    async def fake_loop(**kwargs):
        return FrontierResult(False, "Lost contact with the device.",
                              error_kind="lost_contact", steps=2, device="d")

    monkeypatch.setattr(cd.frontier_agent_loop, "run_frontier_loop", fake_loop)
    res = _run(cd.execute({"task": "x"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "lost_contact"
    assert res.data["device"] == NODE.dns_name


def test_explicit_device_and_origin_passed_to_resolver(monkeypatch):
    # The executor threads BOTH the explicit `device` param and ctx.origin_device_id
    # into resolve_device — the whole point of M3 origin-aware routing.
    captured = {}

    def fake_resolve(*, operator, origin_device_id, target_device_id):
        captured.update(operator=operator, origin_device_id=origin_device_id,
                        target_device_id=target_device_id)
        return NODE

    monkeypatch.setattr(cd.mesh, "resolve_device", fake_resolve)

    async def fake_loop(**kwargs):
        return FrontierResult(True, "done", steps=1)

    monkeypatch.setattr(cd.frontier_agent_loop, "run_frontier_loop", fake_loop)
    ctx = ToolContext(operator="Brandon", origin_device_id="brandon-fold6")
    res = _run(cd.execute({"task": "x", "device": "work-tablet"}, ctx))
    assert res.success is True
    assert captured == {"operator": "Brandon", "origin_device_id": "brandon-fold6",
                        "target_device_id": "work-tablet"}


def test_explicit_device_not_found_is_invalid_target(monkeypatch):
    monkeypatch.setattr(cd.mesh, "resolve_device",
                        _raises("invalid_target", "no such device",
                                detail={"requested": "nonexistent-tablet"}))
    res = _run(cd.execute({"task": "x", "device": "nonexistent-tablet"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "invalid_target"
    assert res.data["requested"] == "nonexistent-tablet"


def test_origin_mismatch_is_never_silent_retarget(monkeypatch):
    # The firm invariant: an origin naming a device that is NOT this operator's is an
    # ERROR (origin_mismatch), never a silent fallback to some other device.
    monkeypatch.setattr(cd.mesh, "resolve_device",
                        _raises("origin_mismatch", "not your device",
                                detail={"origin": "someone-else", "operator": "Brandon"}))
    ctx = ToolContext(operator="Brandon", origin_device_id="someone-else")
    res = _run(cd.execute({"task": "x"}, ctx))
    assert res.success is False
    assert res.data["error_kind"] == "origin_mismatch"
    assert res.data["origin"] == "someone-else"


def test_no_primary_device_surfaces(monkeypatch):
    monkeypatch.setattr(cd.mesh, "resolve_device",
                        _raises("no_primary_device", "primary offline"))
    res = _run(cd.execute({"task": "x"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "no_primary_device"


def test_loop_exception_is_caught(monkeypatch):
    monkeypatch.setattr(cd.mesh, "resolve_device", _resolves_to(NODE))

    async def boom(**kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cd.frontier_agent_loop, "run_frontier_loop", boom)
    res = _run(cd.execute({"task": "x"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "loop_error"


def test_tool_is_discoverable_and_loadable():
    # The tool is in the canonical catalog and its executor loads (ADDING_A_TOOL verify).
    from Orchestrator.toolvault import registry
    assert any(t["name"] == "control_device" for t in registry.load_canonical())
    assert registry.get_executor("control_device") is not None
