"""Every box-side caller of the phone's control listener sends its pairing secret
(audit X02/X04, rank 29).

The phone's RemoteControlServer now answers 401 to any request without
``Authorization: Bearer <secret>``. Each caller must therefore (a) look the secret up by
the resolved node's tailnet IP and send it, (b) refuse to call a device with no recorded
secret, returning the actionable ``not_paired`` error instead of a doomed unauthenticated
request, and (c) map a 401 from the device to the same actionable error rather than
"unreachable" / "refused" / a pointless retry.

Callers covered: control_phone (/task, /status), stop_device_control (/kill*),
navigate_device (/action), control_device -> the frontier loop (/stream, /action), and the
notification bus (/notify). Hermetic: the pairing store is a tmp file, the HTTP clients are
fakes, mesh resolution is monkeypatched.
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest

@pytest.fixture
def require_pairing(monkeypatch):
    """[devices] require_pairing = true: an unpaired device is refused, never called.
    (The default during the rollout calls it without a header; see pairing.py.)"""
    from Orchestrator import config
    monkeypatch.setattr(config, "DEVICES_REQUIRE_PAIRING", True)


from Orchestrator import frontier_agent_loop as fal
from Orchestrator.device_registry import pairing
from Orchestrator.frontier_agent_loop import Decision, FrontierResult
from Orchestrator.local_provider.mesh import Node
from Orchestrator.toolvault.context import ToolContext
import Orchestrator.notifications.bus as bus_mod

SECRET = "c3" * 32
BEARER = {"Authorization": f"Bearer {SECRET}"}
NODE = Node(hostname="test-phone", dns_name="test-phone.tailnet-abc.ts.net",
            ip="100.88.0.7", online=True, os="android")
CTX = ToolContext(operator="Alice", base_url="http://localhost:9091")


def _load(tool):
    path = Path(__file__).resolve().parents[2] / "ToolVault" / "tools" / tool / "executor.py"
    spec = importlib.util.spec_from_file_location(f"{tool}_auth_executor", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cp = _load("control_phone")
stop_mod = _load("stop_device_control")
nd = _load("navigate_device")
cd = _load("control_device")


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(pairing, "PAIRING_PATH", tmp_path / "device_pairing.json")


@pytest.fixture
def paired():
    pairing.register(device_id="test-phone", tailnet_ip=NODE.ip, secret=SECRET)


def _resolve_to_node(monkeypatch, mod):
    monkeypatch.setattr(mod.mesh, "resolve_device", lambda **kw: NODE)


def _Http401(status=401):
    """The status error aiohttp raises from ``raise_for_status()``."""
    import aiohttp
    return aiohttp.ClientResponseError(request_info=None, history=(), status=status,
                                       message="unauthorized")


class _Httpx401(Exception):
    """An httpx-style status error (``.response.status_code``)."""

    def __init__(self, status=401):
        super().__init__(f"HTTP {status}")
        self.response = type("R", (), {"status_code": status})()


def _boom(*a, **k):
    raise AssertionError("must not call the device without a pairing secret")


# ── control_phone ─────────────────────────────────────────────────────────────────

def test_control_phone_sends_the_secret_on_task_and_status(monkeypatch, paired):
    _resolve_to_node(monkeypatch, cp)
    monkeypatch.setattr(cp, "_poll_interval_secs", lambda: 0.0)
    seen = []

    async def post(base_url, payload, headers=None):
        seen.append(("task", headers))
        return {"task_id": "t1"}

    async def status(base_url, task_id, headers=None):
        seen.append(("status", headers))
        return {"phase": "done", "result": "ok"}

    monkeypatch.setattr(cp, "_post_task", post)
    monkeypatch.setattr(cp, "_get_status", status)
    res = _run(cp.execute({"task": "open maps"}, CTX))
    assert res.success is True
    assert seen == [("task", BEARER), ("status", BEARER)]


@pytest.mark.usefixtures("require_pairing")
def test_control_phone_refuses_an_unpaired_device(monkeypatch):
    _resolve_to_node(monkeypatch, cp)
    monkeypatch.setattr(cp, "_post_task", _boom)
    res = _run(cp.execute({"task": "open maps"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "not_paired"
    assert "Open the BlackBox app" in res.result


def test_control_phone_maps_a_401_on_task_to_not_paired(monkeypatch, paired):
    _resolve_to_node(monkeypatch, cp)

    async def post(base_url, payload, headers=None):
        raise _Http401()

    monkeypatch.setattr(cp, "_post_task", post)
    res = _run(cp.execute({"task": "open maps"}, CTX))
    assert res.data["error_kind"] == "not_paired"
    assert "rejected" in res.result


def test_control_phone_stops_polling_on_a_401(monkeypatch, paired):
    _resolve_to_node(monkeypatch, cp)
    monkeypatch.setattr(cp, "_poll_interval_secs", lambda: 0.0)
    calls = []

    async def post(base_url, payload, headers=None):
        return {"task_id": "t1"}

    async def status(base_url, task_id, headers=None):
        calls.append(1)
        raise _Http401()

    monkeypatch.setattr(cp, "_post_task", post)
    monkeypatch.setattr(cp, "_get_status", status)
    res = _run(cp.execute({"task": "open maps"}, CTX))
    assert res.data["error_kind"] == "not_paired"
    assert len(calls) == 1          # no 60 s "lost contact" grace for a refused secret


class _FakeAioResp:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeAioSession:
    calls = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, timeout=None, headers=None):
        _FakeAioSession.calls.append(("POST", url, headers))
        return _FakeAioResp({"task_id": "t1", "ok": True, "killed_count": 0})

    def get(self, url, timeout=None, headers=None):
        _FakeAioSession.calls.append(("GET", url, headers))
        return _FakeAioResp({"phase": "done"})


def test_control_phone_real_seams_put_the_header_on_the_wire(monkeypatch):
    _FakeAioSession.calls = []
    monkeypatch.setattr(cp.aiohttp, "ClientSession", _FakeAioSession)
    _run(cp._post_task("http://phone:8765", {"task": "x"}, BEARER))
    _run(cp._get_status("http://phone:8765", "t1", BEARER))
    assert _FakeAioSession.calls == [
        ("POST", "http://phone:8765/task", BEARER),
        ("GET", "http://phone:8765/status/t1", BEARER),
    ]


# ── stop_device_control ───────────────────────────────────────────────────────────

def test_stop_sends_the_secret(monkeypatch, paired):
    _resolve_to_node(monkeypatch, stop_mod)
    seen = {}

    async def post_kill(base_url, path, operator, headers=None):
        seen.update(path=path, headers=headers)
        return {"ok": True, "killed_count": 1}

    monkeypatch.setattr(stop_mod, "_post_kill", post_kill)
    res = _run(stop_mod.execute({}, CTX))
    assert res.success is True
    assert seen == {"path": "/kill-all", "headers": BEARER}


@pytest.mark.usefixtures("require_pairing")
def test_stop_refuses_an_unpaired_device(monkeypatch):
    _resolve_to_node(monkeypatch, stop_mod)
    monkeypatch.setattr(stop_mod, "_post_kill", _boom)
    res = _run(stop_mod.execute({}, CTX))
    assert res.data["error_kind"] == "not_paired"


def test_stop_maps_a_401_to_not_paired(monkeypatch, paired):
    _resolve_to_node(monkeypatch, stop_mod)

    async def post_kill(base_url, path, operator, headers=None):
        raise _Http401()

    monkeypatch.setattr(stop_mod, "_post_kill", post_kill)
    res = _run(stop_mod.execute({}, CTX))
    assert res.data["error_kind"] == "not_paired"


def test_stop_real_seam_puts_the_header_on_the_wire(monkeypatch):
    _FakeAioSession.calls = []
    monkeypatch.setattr(stop_mod.aiohttp, "ClientSession", _FakeAioSession)
    _run(stop_mod._post_kill("http://phone:8765", "/kill-all", "Alice", BEARER))
    assert _FakeAioSession.calls == [("POST", "http://phone:8765/kill-all", BEARER)]


# ── navigate_device ───────────────────────────────────────────────────────────────

NAV_OK = {"msg": "action_result", "success": True,
          "detail": "posted a navigation notification; awaiting user tap"}


def test_navigate_sends_the_secret(monkeypatch, paired):
    _resolve_to_node(monkeypatch, nd)
    seen = {}

    async def post_intent(base_url, frame, headers=None):
        seen["headers"] = headers
        return NAV_OK

    monkeypatch.setattr(nd, "_post_intent", post_intent)
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert res.success is True
    assert seen["headers"] == BEARER


@pytest.mark.usefixtures("require_pairing")
def test_navigate_refuses_an_unpaired_device(monkeypatch):
    _resolve_to_node(monkeypatch, nd)
    monkeypatch.setattr(nd, "_post_intent", _boom)
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert res.data["error_kind"] == "not_paired"
    assert res.data["destination"] == "CN Tower"


def test_navigate_maps_a_401_to_not_paired(monkeypatch, paired):
    _resolve_to_node(monkeypatch, nd)

    async def post_intent(base_url, frame, headers=None):
        raise _Httpx401()

    monkeypatch.setattr(nd, "_post_intent", post_intent)
    res = _run(nd.execute({"destination": "CN Tower"}, CTX))
    assert res.data["error_kind"] == "not_paired"
    assert res.data["http_status"] == 401


# ── control_device → frontier loop ────────────────────────────────────────────────

def test_control_device_hands_the_secret_to_the_loop(monkeypatch, paired):
    _resolve_to_node(monkeypatch, cd)
    monkeypatch.setattr(cd.mesh, "default_provider_for_node", lambda *a, **k: None)
    seen = {}

    async def fake_loop(**kwargs):
        seen.update(kwargs)
        return FrontierResult(True, "done", steps=1)

    monkeypatch.setattr(cd.frontier_agent_loop, "run_frontier_loop", fake_loop)
    res = _run(cd.execute({"task": "open maps", "provider": "gemini"}, CTX))
    assert res.success is True
    assert seen["device_headers"] == BEARER


@pytest.mark.usefixtures("require_pairing")
def test_control_device_refuses_an_unpaired_device(monkeypatch):
    _resolve_to_node(monkeypatch, cd)
    monkeypatch.setattr(cd.mesh, "default_provider_for_node", lambda *a, **k: None)
    monkeypatch.setattr(cd.frontier_agent_loop, "run_frontier_loop", _boom)
    res = _run(cd.execute({"task": "open maps", "provider": "gemini"}, CTX))
    assert res.data["error_kind"] == "not_paired"


_OBS = {"msg": "observation",
        "ui_tree": [{"node_id": 0, "role": "View", "text": "", "resource_id": "root",
                     "bounds": "0,0,1080,2400", "clickable": True, "editable": False,
                     "is_password": False}],
        "device_capability": {"formFactor": "phone", "hasScreenshot": False,
                              "supportsCoordinateGesture": True, "displayId": 0},
        "timestamp": 1}


class _Driver:
    def __init__(self, script):
        self._script = list(script)

    async def next_action(self, observation, last_result):
        if not self._script:
            return Decision(kind="done", text="done")
        return Decision(kind="action", model_action=self._script.pop(0))


def _fast(monkeypatch):
    for name, val in (("_retry_max", 2), ("_retry_backoff_secs", 0.0), ("_per_action_secs", 5.0),
                      ("_per_turn_secs", 5.0), ("_session_base_secs", 300.0),
                      ("_session_max_secs", 600.0), ("_max_steps", 40)):
        monkeypatch.setattr(fal, name, lambda v=val: v)


def test_frontier_loop_sends_the_secret_on_every_hop(monkeypatch):
    _fast(monkeypatch)
    seen = []

    async def pull(base_url, task_id, operator, timeout, headers=None):
        seen.append(("stream", headers))
        return _OBS

    async def post(base_url, frame, timeout, headers=None):
        seen.append(("action", headers))
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_pull_observation", pull)
    monkeypatch.setattr(fal, "_post_action", post)
    monkeypatch.setattr(fal, "_make_driver",
                        lambda *a, **k: _Driver([{"op": "open_app", "app": "com.foo"}]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Alice",
                                     device_headers=BEARER))
    assert res.success is True
    assert seen and all(h == BEARER for _, h in seen)
    assert {kind for kind, _ in seen} == {"stream", "action"}


def test_frontier_loop_reports_not_paired_on_a_401_observation(monkeypatch):
    _fast(monkeypatch)
    calls = []

    async def pull(base_url, task_id, operator, timeout, headers=None):
        calls.append(1)
        raise _Httpx401()

    monkeypatch.setattr(fal, "_pull_observation", pull)
    monkeypatch.setattr(fal, "_make_driver", _boom)
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Alice",
                                     device_headers=BEARER))
    assert res.success is False
    assert res.error_kind == "not_paired"
    assert len(calls) == 1          # a refused secret is not retried


def test_frontier_loop_reports_not_paired_on_a_401_action(monkeypatch):
    _fast(monkeypatch)

    async def pull(base_url, task_id, operator, timeout, headers=None):
        return _OBS

    async def post(base_url, frame, timeout, headers=None):
        raise _Httpx401()

    monkeypatch.setattr(fal, "_pull_observation", pull)
    monkeypatch.setattr(fal, "_post_action", post)
    monkeypatch.setattr(fal, "_make_driver",
                        lambda *a, **k: _Driver([{"op": "open_app", "app": "com.foo"}]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Alice",
                                     device_headers=BEARER))
    assert res.success is False
    assert res.error_kind == "not_paired"


class _FakeHttpx:
    inits = []

    def __init__(self, *a, **k):
        _FakeHttpx.inits.append(k)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, params=None):
        return _FakeStream()

    async def post(self, url, json=None):
        return _FakePost()


class _FakeStream:
    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        yield "data: {\"msg\": \"observation\"}"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakePost:
    def raise_for_status(self):
        pass

    def json(self):
        return {"success": True}


def test_frontier_real_seams_put_the_header_on_the_client(monkeypatch):
    _FakeHttpx.inits = []
    monkeypatch.setattr("httpx.AsyncClient", _FakeHttpx)
    _run(fal._pull_observation("http://phone:8765", "t1", "Alice", 5.0, headers=BEARER))
    _run(fal._post_action("http://phone:8765", {"msg": "action"}, 5.0, headers=BEARER))
    assert [k.get("headers") for k in _FakeHttpx.inits] == [BEARER, BEARER]


# ── notification bus ──────────────────────────────────────────────────────────────

def _target(ip=NODE.ip):
    return {"device_id": "test-phone", "tailnet_name": ip,
            "node": {"hostname": "test-phone", "dns_name": NODE.dns_name, "ip": ip,
                     "online": True, "os": "android"}}


def test_bus_sends_the_secret_to_a_paired_device(monkeypatch, paired):
    _FakeAioSession.calls = []
    monkeypatch.setattr(bus_mod.aiohttp, "ClientSession", _FakeAioSession)
    _run(bus_mod._post_to_device(_target(), {"title": "t"}))
    assert _FakeAioSession.calls == [("POST", f"http://{NODE.dns_name}:8765/notify", BEARER)]


@pytest.mark.usefixtures("require_pairing")
def test_bus_never_posts_to_an_unpaired_device(monkeypatch):
    _FakeAioSession.calls = []
    monkeypatch.setattr(bus_mod.aiohttp, "ClientSession", _FakeAioSession)
    monkeypatch.setattr(bus_mod, "reachable_subscribers", lambda op: [_target()])
    result = _run(bus_mod.notify("Alice", "Build done", "green"))
    assert result.delivered == []
    assert result.unreachable == ["test-phone"]
    assert _FakeAioSession.calls == []
