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

from Orchestrator.toolvault.context import ToolContext
from Orchestrator.local_provider.mesh import Node

_EXEC_PATH = (Path(__file__).resolve().parents[2]
              / "ToolVault" / "tools" / "control_phone" / "executor.py")
_spec = importlib.util.spec_from_file_location("control_phone_executor_integ", _EXEC_PATH)
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

CTX = ToolContext(operator="Brandon", base_url="http://localhost:9091")


def _make_stub(status_sequence):
    """A localhost HTTP stub phone. POST /task -> {task_id}; GET /status/<id> walks
    `status_sequence`. Returns (server, port); call server.shutdown() to stop."""
    seq = iter(status_sequence)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self._send({"task_id": "t1"})

        def do_GET(self):
            if self.path.startswith("/status/"):
                self._send(next(seq))
            else:
                self._send({"ok": True})

        def log_message(self, *args):
            pass  # silence the test stub

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _point_at(monkeypatch, port):
    monkeypatch.setattr(cp.mesh, "resolve_origin",
                        lambda *a, **k: Node(hostname="stub", dns_name="", ip="127.0.0.1", online=True))
    monkeypatch.setattr(cp, "_control_port", lambda: port)
    monkeypatch.setattr(cp, "_poll_interval_secs", lambda: 0.0)


def test_resolve_post_poll_returns_result(monkeypatch):
    server, port = _make_stub([
        {"phase": "waking"}, {"phase": "working"}, {"phase": "done", "result": "Opened Maps."},
    ])
    try:
        _point_at(monkeypatch, port)
        res = asyncio.run(cp.execute({"task": "open maps"}, CTX))
        assert res.success is True
        assert res.result == "Opened Maps."
        assert res.data["phase"] == "done"
    finally:
        server.shutdown()


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
    res = asyncio.run(cp.execute({"task": "x"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "wake_failed"


def test_no_device_when_unresolved(monkeypatch):
    monkeypatch.setattr(cp.mesh, "resolve_origin", lambda *a, **k: None)
    res = asyncio.run(cp.execute({"task": "x"}, CTX))
    assert res.success is False
    assert res.data["error_kind"] == "no_device"


def test_phone_403_is_refused(monkeypatch):
    """A real 403 from the phone's auth (operator/source) -> refused, not wake_failed."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = b'{"error":"operator not authorized for this device"}'
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
