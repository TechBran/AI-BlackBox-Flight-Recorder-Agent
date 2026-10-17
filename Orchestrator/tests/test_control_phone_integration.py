"""Integration smoke for the control_phone executor (Task 9).

Unlike test_control_phone_tool.py (which monkeypatches the HTTP seams), this drives
the REAL aiohttp _post_task/_get_status against a REAL local stub HTTP server — the
end-to-end resolve -> POST /task -> poll /status -> result path over a socket — plus
the unreachable error path and ToolVault discoverability/executability.
"""
import asyncio
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from Orchestrator.toolvault.context import ToolContext
from Orchestrator.local_provider.mesh import Node

@pytest.fixture
def require_pairing(monkeypatch):
    """[devices] require_pairing = true: an unpaired device is refused, never called.
    (The default during the rollout calls it without a header; see pairing.py.)"""
    from Orchestrator import config
    monkeypatch.setattr(config, "DEVICES_REQUIRE_PAIRING", True)


_EXEC_PATH = (Path(__file__).resolve().parents[2]
              / "ToolVault" / "tools" / "control_phone" / "executor.py")
_spec = importlib.util.spec_from_file_location("control_phone_executor_integ", _EXEC_PATH)
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

CTX = ToolContext(operator="Brandon", base_url="http://localhost:9091")

# The stub phone listens on loopback, which is never a pairable (tailnet) address, so the
# pairing lookup is pinned to this header instead of going through the store.
TEST_SECRET = "c3" * 32
AUTH_HEADER = f"Bearer {TEST_SECRET}"


def _make_stub(status_sequence, seen_auth=None):
    """A localhost HTTP stub phone. POST /task -> {task_id}; GET /status/<id> walks
    `status_sequence`. Every request's (method, path, Authorization) is appended to
    `seen_auth` when given. Returns (server, port); call server.shutdown() to stop."""
    seq = iter(status_sequence)
    seen = seen_auth if seen_auth is not None else []

    class Handler(BaseHTTPRequestHandler):
        def _record(self):
            seen.append((self.command, self.path, self.headers.get("Authorization")))

        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self._record()
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self._send({"task_id": "t1"})

        def do_GET(self):
            self._record()
            if self.path.startswith("/status/"):
                self._send(next(seq))
            else:
                self._send({"ok": True})

        def log_message(self, *args):
            pass  # silence the test stub

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _paired(monkeypatch):
    monkeypatch.setattr(cp.pairing, "auth_headers_for_node",
                        lambda node: {"Authorization": AUTH_HEADER})


def _point_at(monkeypatch, port):
    monkeypatch.setattr(cp.mesh, "resolve_origin",
                        lambda *a, **k: Node(hostname="stub", dns_name="", ip="127.0.0.1", online=True))
    monkeypatch.setattr(cp, "_control_port", lambda: port)
    monkeypatch.setattr(cp, "_poll_interval_secs", lambda: 0.0)
    _paired(monkeypatch)


def _send_json(handler, code, body):
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def test_resolve_post_poll_returns_result(monkeypatch):
    seen = []
    server, port = _make_stub([
        {"phase": "waking"}, {"phase": "working"}, {"phase": "done", "result": "Opened Maps."},
    ], seen_auth=seen)
    try:
        _point_at(monkeypatch, port)
        res = asyncio.run(cp.execute({"task": "open maps"}, CTX))
        assert res.success is True
        assert res.result == "Opened Maps."
        assert res.data["phase"] == "done"
    finally:
        server.shutdown()
    # The pairing secret reached the phone on the POST and on every /status poll.
    assert [m for m, _, _ in seen] == ["POST", "GET", "GET", "GET"]
    assert seen[0][1] == "/task"
    assert all(path.startswith("/status/") for _, path, _ in seen[1:])
    assert all(auth == AUTH_HEADER for _, _, auth in seen), seen


def test_remote_error_phase_is_surfaced(monkeypatch):
    server, port = _make_stub([
        {"phase": "working"}, {"phase": "error", "error": "tool refused for remote control"},
    ])
    try:
        _point_at(monkeypatch, port)
        res = asyncio.run(cp.execute({"task": "send an sms"}, CTX))
        assert res.success is False
        assert res.data["error_kind"] == "remote_error"
        assert "refused" in res.result.lower()
    finally:
        server.shutdown()


def test_unreachable_device_is_wake_failed(monkeypatch):
    # resolve to a port nothing is listening on -> connection refused -> wake_failed.
    monkeypatch.setattr(cp.mesh, "resolve_origin",
                        lambda *a, **k: Node(hostname="dead", dns_name="", ip="127.0.0.1", online=True))
    monkeypatch.setattr(cp, "_control_port", lambda: 1)  # nothing listens on :1
    _paired(monkeypatch)
    res = asyncio.run(cp.execute({"task": "x"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "wake_failed"


def test_no_device_when_unresolved(monkeypatch):
    monkeypatch.setattr(cp.mesh, "resolve_origin", lambda *a, **k: None)
    res = asyncio.run(cp.execute({"task": "x"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "no_device"


@pytest.mark.usefixtures("require_pairing")
def test_unpaired_device_is_never_called(monkeypatch):
    """No secret on record -> not_paired with the re-pair hint, and NO request on the wire
    (never a silent unauthenticated call the phone would refuse anyway)."""
    seen = []
    server, port = _make_stub([{"phase": "done", "result": "x"}], seen_auth=seen)
    try:
        monkeypatch.setattr(cp.mesh, "resolve_origin",
                            lambda *a, **k: Node(hostname="stub", dns_name="", ip="127.0.0.1",
                                                 online=True))
        monkeypatch.setattr(cp, "_control_port", lambda: port)
        res = asyncio.run(cp.execute({"task": "x"}, CTX))
    finally:
        server.shutdown()
    assert res.success is False
    assert res.data["error_kind"] == "not_paired"
    assert "open the blackbox app" in res.result.lower()
    assert seen == []


def test_phone_401_is_not_paired(monkeypatch):
    """A real 401 from the phone (it holds a different secret) -> not_paired, not refused."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.headers.get("Authorization"))
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            _send_json(self, 401, b'{"error":"valid pairing secret required"}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _point_at(monkeypatch, server.server_address[1])
        res = asyncio.run(cp.execute({"task": "x"}, CTX))
    finally:
        server.shutdown()
    assert res.success is False
    assert res.data["error_kind"] == "not_paired"
    assert res.data["http_status"] == 401
    assert "re-pairs" in res.result
    assert seen == [AUTH_HEADER]


def test_phone_403_is_refused(monkeypatch):
    """A real 403 from the phone's auth (operator/source) -> refused, not wake_failed."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            _send_json(self, 403, b'{"error":"operator not authorized for this device"}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _point_at(monkeypatch, server.server_address[1])
        res = asyncio.run(cp.execute({"task": "x"}, CTX))
        assert res.success is False
        assert res.data["error_kind"] == "refused"
        assert res.data["http_status"] == 403
    finally:
        server.shutdown()


def test_tool_is_discoverable_and_executable():
    from Orchestrator.toolvault import registry
    names = {t.get("name") for t in registry.load_canonical()}
    assert "control_phone" in names, "control_phone must be registered in ToolVault"
    assert registry.get_executor("control_phone") is not None, "executor must load"
