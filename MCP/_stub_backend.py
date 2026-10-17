#!/usr/bin/env python3
"""A stand-in for the Orchestrator API (:9091) for the MCP script harnesses.

The harnesses' proxied-tool checks (get_current_operator and the roster the
server reads) do one real GET /operators round-trip. MCP/run_tests.sh points
BLACKBOX_URL at this stub instead of the live service, so the harnesses never
reach this box's Orchestrator and run the same on a CI runner, which has none.
It serves the synthetic roster the harnesses bind their test tokens to.

Prints the port it bound (127.0.0.1, an ephemeral port) on its first stdout
line, then serves until killed. Stdlib only.

    python MCP/_stub_backend.py
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Same shape as GET /operators (Orchestrator/routes/admin_routes.py).
OPERATORS = {"operators": ["alice", "bob"], "default": "alice", "reserved": []}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        if self.path.split("?", 1)[0] == "/operators":
            self._send(200, OPERATORS)
        else:
            self._send(404, {"detail": f"{self.path} is not served by the MCP test stub"})

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    print(server.server_address[1], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
