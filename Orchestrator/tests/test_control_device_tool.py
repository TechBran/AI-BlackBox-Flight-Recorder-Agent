"""Hermetic tests for the control_device ToolVault executor (M2).

The executor is loaded directly from its file (the same mechanism the registry uses). The
mesh device resolution + the frontier loop are monkeypatched, so no tailscale, no sockets, no
real phone, and no Gemini are touched. Proves the executor resolves a device, invokes the
loop, and returns a well-formed ToolResult (success + structured errors), plus that the tool
is discoverable + loadable through the ToolVault registry.
"""
import asyncio
import importlib.util
from pathlib import Path

from Orchestrator.toolvault.context import ToolContext
from Orchestrator.local_provider.mesh import Node
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


def test_requires_task():
    res = _run(cd.execute({}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "invalid_argument"


def test_no_reachable_device(monkeypatch):
    monkeypatch.setattr(cd.mesh, "resolve_origin", lambda *a, **k: None)
    res = _run(cd.execute({"task": "open maps"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "no_device"


def test_happy_path_returns_toolresult(monkeypatch):
    monkeypatch.setattr(cd.mesh, "resolve_origin", lambda *a, **k: NODE)
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
    monkeypatch.setattr(cd.mesh, "resolve_origin", lambda *a, **k: NODE)

    async def fake_loop(**kwargs):
        return FrontierResult(False, "Lost contact with the device.",
                              error_kind="lost_contact", steps=2, device="d")

    monkeypatch.setattr(cd.frontier_agent_loop, "run_frontier_loop", fake_loop)
    res = _run(cd.execute({"task": "x"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "lost_contact"
    assert res.data["device"] == NODE.dns_name


def test_explicit_device_matches_reachable(monkeypatch):
    rec = {"operator": "Brandon", "device_id": "d1", "model_slug": "m",
           "tailnet_name": "brandon-fold6", "node": {
               "hostname": "brandon-fold6", "dns_name": NODE.dns_name,
               "ip": NODE.ip, "online": True, "os": "android"}}
    monkeypatch.setattr(cd.mesh, "reachable_devices", lambda *a, **k: [rec])
    captured = {}

    async def fake_loop(**kwargs):
        captured["base"] = kwargs["device_base_url"]
        return FrontierResult(True, "done", steps=1)

    monkeypatch.setattr(cd.frontier_agent_loop, "run_frontier_loop", fake_loop)
    res = _run(cd.execute({"task": "x", "device": "brandon-fold6"}, CTX))
    assert res.success is True
    assert NODE.dns_name in captured["base"]


def test_explicit_device_not_found_is_invalid_target(monkeypatch):
    monkeypatch.setattr(cd.mesh, "reachable_devices", lambda *a, **k: [])
    res = _run(cd.execute({"task": "x", "device": "nonexistent-tablet"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "invalid_target"
    assert res.data["requested"] == "nonexistent-tablet"


def test_loop_exception_is_caught(monkeypatch):
    monkeypatch.setattr(cd.mesh, "resolve_origin", lambda *a, **k: NODE)

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
