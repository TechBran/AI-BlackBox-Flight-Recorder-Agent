"""M3 routing tests for control_phone — proves it now resolves via mesh.resolve_device
(origin-aware) and maps DeviceResolutionError.kind → data["error_kind"], including the
never-silent-retarget origin_mismatch. Complements test_control_phone_tool.py (which still
exercises the legacy resolve_origin fallback path unchanged).
"""
import asyncio
import importlib.util
from pathlib import Path

from Orchestrator.toolvault.context import ToolContext
from Orchestrator.local_provider.mesh import Node, DeviceResolutionError

_EXEC_PATH = (Path(__file__).resolve().parents[2]
              / "ToolVault" / "tools" / "control_phone" / "executor.py")
_spec = importlib.util.spec_from_file_location("control_phone_executor_routing", _EXEC_PATH)
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

NODE = Node(hostname="brandon-fold6", dns_name="brandon-fold6.tailnet-abc.ts.net",
            ip="100.88.0.7", online=True, os="android")


def _run(coro):
    return asyncio.run(coro)


def test_routes_through_resolve_device_with_origin_and_target(monkeypatch):
    captured = {}

    def fake_resolve(*, operator, origin_device_id, target_device_id):
        captured.update(operator=operator, origin_device_id=origin_device_id,
                        target_device_id=target_device_id)
        return NODE

    monkeypatch.setattr(cp.mesh, "resolve_device", fake_resolve)
    monkeypatch.setattr(cp, "_poll_interval_secs", lambda: 0.0)

    async def fake_post(base_url, payload):
        assert "brandon-fold6" in base_url
        return {"task_id": "t1"}

    async def fake_status(base_url, task_id):
        return {"phase": "done", "result": "ok"}

    monkeypatch.setattr(cp, "_post_task", fake_post)
    monkeypatch.setattr(cp, "_get_status", fake_status)

    ctx = ToolContext(operator="Brandon", origin_device_id="brandon-fold6")
    res = _run(cp.execute({"task": "open maps", "device": "work-tablet"}, ctx))
    assert res.success is True
    assert captured == {"operator": "Brandon", "origin_device_id": "brandon-fold6",
                        "target_device_id": "work-tablet"}


def test_origin_mismatch_maps_to_error_kind(monkeypatch):
    def boom(**kwargs):
        raise DeviceResolutionError("origin_mismatch", "not your device",
                                    detail={"origin": "someone-else"})

    monkeypatch.setattr(cp.mesh, "resolve_device", boom)
    ctx = ToolContext(operator="Brandon", origin_device_id="someone-else")
    res = _run(cp.execute({"task": "x"}, ctx))
    assert res.success is False
    assert res.data["error_kind"] == "origin_mismatch"
    assert res.data["origin"] == "someone-else"


def test_no_primary_device_maps_to_error_kind(monkeypatch):
    def boom(**kwargs):
        raise DeviceResolutionError("no_primary_device", "primary offline")

    monkeypatch.setattr(cp.mesh, "resolve_device", boom)
    res = _run(cp.execute({"task": "x"}, ToolContext(operator="Brandon")))
    assert res.success is False
    assert res.data["error_kind"] == "no_primary_device"
