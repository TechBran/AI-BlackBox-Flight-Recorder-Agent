"""(M8.3) Tests for the frontier loop's per-step telemetry (Orchestrator/frontier_agent_loop.py).

Fully mocked (no device, no Gemini). Proves: run_frontier_loop records ONE non-sensitive
per-step record ({step, op, success, latency_ms, capture}) per dispatched action; to_data()
surfaces it bounded; the records carry NO screen text / typed text / coordinates (only the op
NAME, a bool, an int, and the capture kind); and the capture kind reflects the observation.
"""
import asyncio

from Orchestrator import frontier_agent_loop as fal
from Orchestrator.frontier_agent_loop import Decision, FrontierResult


def _node(node_id, bounds, *, clickable=False, editable=False, resource_id=""):
    return {"node_id": node_id, "role": "View", "text": "", "resource_id": resource_id,
            "bounds": bounds, "clickable": clickable, "editable": editable, "is_password": False}


OBS_TREE_ONLY = {
    "msg": "observation",
    "ui_tree": [
        _node(0, "0,0,1080,2400", clickable=True, resource_id="root"),
        _node(1, "400,600,700,850", editable=True, resource_id="app:id/field"),
        _node(2, "400,1600,700,1750", clickable=True, resource_id="app:id/submit"),
    ],
    "device_capability": {"formFactor": "phone", "hasScreenshot": False,
                          "supportsCoordinateGesture": True, "displayId": 0},
    "timestamp": 1,
}
OBS_WITH_SHOT = {**OBS_TREE_ONLY, "screenshot": "QUJD"}


class FakeDriver:
    def __init__(self, script):
        self._script = list(script)

    async def next_action(self, observation, last_result):
        if not self._script:
            return Decision(kind="done", text="done")
        item = self._script.pop(0)
        if item == "done":
            return Decision(kind="done", text="All set.")
        return Decision(kind="action", model_action=item)


def _fast(monkeypatch):
    monkeypatch.setattr(fal, "_retry_max", lambda: 0)
    monkeypatch.setattr(fal, "_retry_backoff_secs", lambda: 0.0)
    monkeypatch.setattr(fal, "_per_action_secs", lambda: 5.0)
    monkeypatch.setattr(fal, "_per_turn_secs", lambda: 5.0)
    monkeypatch.setattr(fal, "_session_base_secs", lambda: 300.0)
    monkeypatch.setattr(fal, "_session_max_secs", lambda: 600.0)
    monkeypatch.setattr(fal, "_max_steps", lambda: 40)


def _run(coro):
    return asyncio.run(coro)


def test_telemetry_records_one_step_per_action(monkeypatch):
    _fast(monkeypatch)

    async def fake_pull(base_url, task_id, operator, timeout):
        return OBS_TREE_ONLY

    async def fake_post(base_url, frame, timeout):
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_pull_observation", fake_pull)
    monkeypatch.setattr(fal, "_post_action", fake_post)
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: FakeDriver([
        {"op": "open_app", "app": "com.foo"},
        {"op": "type", "x": 500, "y": 300, "text": "hello secret"},
        {"op": "tap", "x": 500, "y": 700},
        "done",
    ]))

    res = _run(fal.run_frontier_loop("http://phone:8765", "do it", "Brandon"))
    assert res.success is True
    assert res.telemetry is not None
    assert len(res.telemetry) == 3
    ops = [r["op"] for r in res.telemetry]
    assert ops == ["open_app", "type", "tap"]
    for i, rec in enumerate(res.telemetry, start=1):
        assert rec["step"] == i
        assert rec["success"] is True
        assert isinstance(rec["latency_ms"], int) and rec["latency_ms"] >= 0
        assert rec["capture"] == "tree_only"           # OBS had no screenshot
        # exactly the non-sensitive keys — nothing else can carry content.
        assert set(rec.keys()) == {"step", "op", "success", "latency_ms", "capture"}


def test_telemetry_has_no_secrets(monkeypatch):
    _fast(monkeypatch)
    monkeypatch.setattr(fal, "_pull_observation",
                        lambda *a, **k: _acoro(OBS_TREE_ONLY))
    monkeypatch.setattr(fal, "_post_action", lambda *a, **k: _acoro({"success": True}))
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: FakeDriver([
        {"op": "type", "x": 500, "y": 300, "text": "hunter2-PASSWORD"},
        "done",
    ]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "t", "Brandon"))
    blob = str(res.telemetry)
    # the typed text, coordinates, and resource ids never enter telemetry.
    assert "hunter2" not in blob and "PASSWORD" not in blob
    assert "app:id" not in blob and "resource_id" not in blob
    assert "text" not in blob


def test_telemetry_capture_reflects_screenshot(monkeypatch):
    _fast(monkeypatch)
    monkeypatch.setattr(fal, "_pull_observation", lambda *a, **k: _acoro(OBS_WITH_SHOT))
    monkeypatch.setattr(fal, "_post_action", lambda *a, **k: _acoro({"success": True}))
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: FakeDriver([
        {"op": "tap", "x": 500, "y": 700}, "done"]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "t", "Brandon"))
    assert res.telemetry[0]["capture"] == "screenshot"


def test_to_data_surfaces_bounded_telemetry():
    fr = FrontierResult(True, "ok", steps=3,
                        telemetry=[{"step": i, "op": "tap", "success": True,
                                    "latency_ms": 1, "capture": "tree_only"} for i in range(200)])
    d = fr.to_data()
    assert "telemetry" in d
    # bounded to the last TELEMETRY_MAX_STEPS records.
    assert len(d["telemetry"]) == fal.TELEMETRY_MAX_STEPS


def test_to_data_omits_telemetry_when_absent():
    assert "telemetry" not in FrontierResult(True, "ok", steps=0).to_data()


def _acoro(value):
    async def _c(*a, **k):
        return value
    return _c()
