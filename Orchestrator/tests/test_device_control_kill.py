"""(M8.2) Tests for the remote incident kill switch + the frontier loop's killed terminal.

Covers three surfaces, fully mocked (no tailscale, no sockets, no device, no Gemini):
  * frontier_agent_loop._terminal_error classifies `killed` and `intent_only` correctly, and the
    loop SHORT-CIRCUITS on a killed action_result with error_kind=killed;
  * the stop_device_control executor resolves the device (origin-aware) and POSTs /kill-all or
    /kill/{task_id}, surfacing killed_count + structured resolution errors;
  * control_device(action="stop") delegates to the same kill path.
"""
import asyncio
import importlib.util
from pathlib import Path

from Orchestrator.toolvault.context import ToolContext
from Orchestrator.local_provider.mesh import Node, DeviceResolutionError
from Orchestrator import frontier_agent_loop as fal
from Orchestrator.frontier_agent_loop import Decision


def _load(tool):
    path = Path(__file__).resolve().parents[2] / "ToolVault" / "tools" / tool / "executor.py"
    spec = importlib.util.spec_from_file_location(f"{tool}_executor", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


stop_mod = _load("stop_device_control")
cd = _load("control_device")

NODE = Node(hostname="brandon-fold6", dns_name="brandon-fold6.ts.net",
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


# ── _terminal_error: killed + intent_only (M8) ───────────────────────────────────────
def test_terminal_error_classifies_killed_and_intent_only():
    te = fal._terminal_error
    # operator incident-kill → killed (distinct from a user stop)
    assert te({"success": False, "error": "killed"})[0] == "killed"
    assert te({"success": False, "detail": "remote control killed by operator"})[0] == "killed"
    # a11y-revocation → intent_only (checked BEFORE accessibility_off though its detail names it)
    assert te({"success": False, "error": "intent_only_mode",
               "detail": "intent_only_mode: accessibility is off; intents work"})[0] == "intent_only"
    # back-compat: a user STOP is still `stopped`, and a plain a11y-off is still accessibility_off
    assert te({"success": False, "detail": "remote control stopped by user"})[0] == "stopped"
    assert te({"success": False, "error": "not_enabled"})[0] == "accessibility_off"
    # a recoverable failure is NOT terminal
    assert te({"success": False, "error": "node_not_found"}) is None


class _FakeDriver:
    def __init__(self, script):
        self._script = list(script)

    async def next_action(self, observation, last_result):
        if not self._script:
            return Decision(kind="done", text="done")
        return Decision(kind="action", model_action=self._script.pop(0))


def _fast(monkeypatch):
    for name, val in (("_retry_max", 0), ("_retry_backoff_secs", 0.0), ("_per_action_secs", 5.0),
                      ("_per_turn_secs", 5.0), ("_session_base_secs", 300.0),
                      ("_session_max_secs", 600.0), ("_max_steps", 40)):
        monkeypatch.setattr(fal, name, lambda v=val: v)


_OBS = {"msg": "observation",
        "ui_tree": [{"node_id": 0, "role": "View", "text": "", "resource_id": "root",
                     "bounds": "0,0,1080,2400", "clickable": True, "editable": False,
                     "is_password": False}],
        "device_capability": {"formFactor": "phone", "hasScreenshot": False,
                              "supportsCoordinateGesture": True, "displayId": 0},
        "timestamp": 1}


def test_loop_short_circuits_on_killed(monkeypatch):
    _fast(monkeypatch)

    async def fake_pull(base_url, task_id, operator, timeout):
        return _OBS

    async def fake_post(base_url, frame, timeout):
        # a concurrent operator incident-kill lands → the phone refuses with the killed detail.
        return {"msg": "action_result", "success": False,
                "detail": "remote control killed by operator"}

    monkeypatch.setattr(fal, "_pull_observation", fake_pull)
    monkeypatch.setattr(fal, "_post_action", fake_post)
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: _FakeDriver([{"op": "tap", "x": 5, "y": 5}]))

    res = _run(fal.run_frontier_loop("http://phone:8765", "t", "Brandon"))
    assert res.success is False
    assert res.error_kind == "killed"


def test_loop_short_circuits_on_intent_only(monkeypatch):
    _fast(monkeypatch)

    async def fake_pull(base_url, task_id, operator, timeout):
        return _OBS

    async def fake_post(base_url, frame, timeout):
        return {"msg": "action_result", "success": False, "error": "intent_only_mode",
                "detail": "intent_only_mode: accessibility is off; intents work: open_url, dial"}

    monkeypatch.setattr(fal, "_pull_observation", fake_pull)
    monkeypatch.setattr(fal, "_post_action", fake_post)
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: _FakeDriver([{"op": "tap", "x": 5, "y": 5}]))

    res = _run(fal.run_frontier_loop("http://phone:8765", "t", "Brandon"))
    assert res.success is False
    assert res.error_kind == "intent_only"


# ── stop_device_control executor ─────────────────────────────────────────────────────
def test_stop_device_control_kill_all(monkeypatch):
    monkeypatch.setattr(stop_mod.mesh, "resolve_device", _resolves_to(NODE))
    seen = {}

    async def fake_post_kill(base_url, path, operator):
        seen.update(base_url=base_url, path=path, operator=operator)
        return {"ok": True, "killed_count": 1}

    monkeypatch.setattr(stop_mod, "_post_kill", fake_post_kill)
    res = _run(stop_mod.execute({}, CTX))
    assert res.success is True
    assert res.data["killed_count"] == 1
    assert res.data["device"] == NODE.dns_name
    assert seen["path"] == "/kill-all"                 # no task_id → operator-wide kill
    assert seen["operator"] == "Brandon"
    assert seen["base_url"] == f"http://{NODE.dns_name}:{stop_mod.REMOTE_CONTROL_PORT}"


def test_stop_device_control_specific_task(monkeypatch):
    monkeypatch.setattr(stop_mod.mesh, "resolve_device", _resolves_to(NODE))
    seen = {}

    async def fake_post_kill(base_url, path, operator):
        seen["path"] = path
        return {"ok": True, "killed_count": 1}

    monkeypatch.setattr(stop_mod, "_post_kill", fake_post_kill)
    res = _run(stop_mod.execute({"task_id": "abc123"}, CTX))
    assert res.success is True
    assert seen["path"] == "/kill/abc123"


def test_stop_device_control_resolution_error(monkeypatch):
    monkeypatch.setattr(stop_mod.mesh, "resolve_device", _raises("no_device"))
    res = _run(stop_mod.execute({}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "no_device"


def test_stop_device_control_nothing_in_flight(monkeypatch):
    monkeypatch.setattr(stop_mod.mesh, "resolve_device", _resolves_to(NODE))

    async def fake_post_kill(base_url, path, operator):
        return {"ok": True, "killed_count": 0}

    monkeypatch.setattr(stop_mod, "_post_kill", fake_post_kill)
    res = _run(stop_mod.execute({}, CTX))
    assert res.success is True            # nothing to stop is still a clean result
    assert res.data["killed_count"] == 0


# ── control_device(action="stop") delegates to the kill path ─────────────────────────
def test_control_device_action_stop_delegates(monkeypatch):
    from Orchestrator.toolvault import registry
    calls = {}

    async def fake_stopper(params, ctx):
        calls["params"] = params
        from Orchestrator.toolvault.context import ToolResult
        return ToolResult(True, "stopped", data={"killed_count": 2})

    monkeypatch.setattr(registry, "get_executor",
                        lambda name: fake_stopper if name == "stop_device_control" else None)
    res = _run(cd.execute({"action": "stop", "task_id": "xyz", "task": "ignored"}, CTX))
    assert res.success is True
    assert res.data["killed_count"] == 2
    # it passed device/task_id through to the stopper (task itself is ignored for a stop).
    assert calls["params"]["task_id"] == "xyz"


def test_control_device_unknown_action_is_invalid_argument():
    res = _run(cd.execute({"action": "explode", "task": "x"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "invalid_argument"
