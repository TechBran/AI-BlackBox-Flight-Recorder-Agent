"""Shared helpers for the wellness tests (not a test module).

    fx = fixtures()                      # Orchestrator/tests/fixtures/wellness/generate_fixtures.py
    st = fresh_store(tmp_path, monkeypatch)
    ingest_fixture(st, "continuous_120")
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from Orchestrator.wellness import ingest, paths, schema, store

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "wellness"
ENR = "en_0f1c0f1c"
OP_KEY = "f1c0f1c0f1c0f1c0"

_FX = None


def fixtures():
    """The fixture generator module (stdlib only, loaded once)."""
    global _FX
    if _FX is None:
        spec = importlib.util.spec_from_file_location(
            "wellness_generate_fixtures", FIXTURE_DIR / "generate_fixtures.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _FX = mod
    return _FX


def fresh_store(tmp_path, monkeypatch, op_key: str = OP_KEY) -> store.Store:
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data" / "wellness")
    return store.open_store(op_key)


def ingest_batches(st: store.Store, batches, *, now_ms: int, enr: str = ENR, epoch: int = 1,
                   earliest_available=None):
    results = []
    for b in batches:
        b = dict(b, enrollment_id=enr, epoch=epoch)
        results.append(ingest.process_batch(st, schema.IngestBatch.model_validate(b),
                                            enrollment_id=enr, epoch=epoch, now_ms=now_ms,
                                            earliest_available=earliest_available))
    return results


def ingest_fixture(st: store.Store, name: str, *, reverse: bool = False, epoch: int = 1,
                   earliest_available=None):
    fx = fixtures()
    data = fx.load(name)
    batches = list(reversed(data["batches"])) if reverse else data["batches"]
    return ingest_batches(st, batches, now_ms=data["now_ms"], epoch=epoch,
                          earliest_available=earliest_available)


def rollup(st: store.Store, metric: str, grain: str, day: str):
    rows = st.rollups(grain, day, day, metric=metric)
    return rows[0] if rows else None


OPERATOR = "wellness-fixture"


def sandbox(tmp_path, monkeypatch):
    """Point every wellness store at tmp_path (the tests that do not use
    isolated_stores)."""
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data" / "wellness")
    monkeypatch.setattr(paths, "KEYS_PATH", tmp_path / "credentials" / "wellness_device_keys.json")
    monkeypatch.setattr(paths, "SIM_PAIRING_PATH",
                        tmp_path / "credentials" / "wellness_sim_pairing.json")


def roster(monkeypatch, *names):
    """The live operator roster the gate and enrollment read."""
    from Orchestrator import config
    monkeypatch.setattr(config, "USERS_LIST", list(names))


def write_record(operator: str = OPERATOR, **fields) -> dict:
    """Put an enrollment record straight into enrollments.json."""
    from Orchestrator.wellness import enroll
    base = {"operator": operator, "op_key": OP_KEY, "enrollment_id": ENR, "epoch": 1,
            "state": "active", "device_id": "sim-phone", "tailnet_ip": "100.100.7.7",
            "created_at": "2026-06-01T00:00:00Z"}
    base.update(fields)
    with enroll.editing() as data:
        data[operator] = enroll._normalize(base)
        return dict(data[operator])


# ------------------------------------------------------------------ a fake phone

class FakePhone:
    """The app's :8765 listener, as far as the box can tell: /wellness/bind
    (accepts only its own live intent nonce and its chosen operator, with the
    pairing bearer), /wellness/unbind, /wellness/sync, /notify and GET
    /healthz. Runs on 127.0.0.1 on an ephemeral port; `calls` records every
    request (method, path, body)."""

    def __init__(self, operator: str = OPERATOR, secret: str = None):
        import http.server
        import secrets as _secrets
        import threading
        self.operator = operator
        self.secret = secret or _secrets.token_hex(32)
        self.nonce = None
        self.key = None
        self.enrollment_id = None
        self.epoch = None
        self.calls = []
        self.bind_status = None          # force an answer to /wellness/bind
        self.down = False                # answer nothing (closes the socket)
        phone = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, body=None):
                data = json.dumps(body or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw or b"{}")
                except ValueError:
                    return {}

            def do_GET(self):
                phone.calls.append(("GET", self.path, None))
                if phone.down:
                    self.close_connection = True
                    return
                self._reply(200 if self.path == "/healthz" else 404, {"ok": True})

            def do_POST(self):
                body = self._body()
                phone.calls.append(("POST", self.path, body))
                if phone.down:
                    self.close_connection = True
                    return
                if self.headers.get("Authorization") != f"Bearer {phone.secret}":
                    return self._reply(401, {"error": "unauthorized"})
                if self.path == "/wellness/bind":
                    if phone.bind_status is not None:
                        return self._reply(phone.bind_status, {})
                    if body.get("nonce") != phone.nonce or body.get("operator") != phone.operator:
                        return self._reply(403, {"error": "nonce"})
                    phone.key = body.get("k")
                    phone.enrollment_id = body.get("enrollment_id")
                    phone.epoch = body.get("epoch")
                    phone.nonce = None
                    return self._reply(200, {"ok": True})
                if self.path == "/wellness/unbind":
                    phone.key = phone.enrollment_id = None
                    return self._reply(200, {"ok": True})
                if self.path == "/wellness/sync":
                    return self._reply(202, {"ok": True})
                if self.path == "/notify":
                    return self._reply(200, {"ok": True})
                return self._reply(404, {})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def mint_nonce(self) -> str:
        import secrets as _secrets
        self.nonce = _secrets.token_hex(16)
        return self.nonce

    def headers(self, method: str, path: str, body: bytes = b"", now=None) -> dict:
        from Orchestrator.wellness import origin_proof
        return origin_proof.signed_headers(origin_proof.decode_key(self.key), self.enrollment_id,
                                           method, path, body, now=now)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Box:
    """The real app in-process, seen from one phone at a tailnet address
    (requests arrive through the tailscale-serve loopback with X-Forwarded-For)."""

    def __init__(self, client, phone: FakePhone, ip: str, dialed: list):
        self.client, self.phone, self.ip, self.dialed = client, phone, ip, dialed

    def post(self, path, body=None, *, signed=False, raw: bytes = None, headers=None,
             method="POST", ip=None):
        data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
        h = {"X-Forwarded-For": ip or self.ip, "Content-Type": "application/json"}
        if signed:
            h.update(self.phone.headers(method, path.split("?")[0], data))
        h.update(headers or {})
        return self.client.request(method, path, content=data, headers=h)

    def get(self, path, *, signed=False, headers=None):
        return self.post(path, signed=signed, headers=headers, method="GET")

    def enroll(self, ip=None, **over):
        body = {"operator": OPERATOR, "install_id": "install-fixture-1",
                "tailnet_ips": [ip or self.ip], "nonce": self.phone.mint_nonce(),
                "capability_report": fixtures().load("continuous_120")["capabilities"],
                "categories": {"sleep": "ask", "heart": "ask", "activity": "ask",
                               "blood_pressure": "off"},
                "replace": False, "assign_to_me": False}
        body.update(over)
        return self.post("/wellness/enroll", body, ip=ip)

    def push(self, batch: dict, *, gzip_it: bool = True, **kw):
        import gzip as _gzip
        data = json.dumps(batch).encode()
        headers = {}
        if gzip_it:
            data = _gzip.compress(data)
            headers["Content-Encoding"] = "gzip"
        headers.update(kw.pop("headers", {}))
        return self.post("/wellness/ingest", raw=data, signed=True, headers=headers, **kw)


PHONE_IP = "100.100.7.7"


def add_device(device_id: str, ip: str, owner: str = "", kind: str = "android"):
    from Orchestrator.device_registry import get_registry
    from Orchestrator.device_registry.models import Device, DeviceProtocol, DeviceType
    get_registry().add_device(Device(id=device_id, name=device_id, tailscale_ip=ip,
                                     device_type=DeviceType(kind), protocol=DeviceProtocol.ADB,
                                     owner=owner))


def make_box(isolated_stores, monkeypatch, *, owner=OPERATOR, paired=True):
    """A TestClient over the real app plus a FakePhone enrolled-to-be at
    PHONE_IP (a registry device owned by `owner`, paired unless told not)."""
    from fastapi.testclient import TestClient

    from Orchestrator.device_registry import pairing
    from Orchestrator.wellness import enroll
    roster(monkeypatch, OPERATOR, "other-human", "Flight Recorder")
    phone = FakePhone()
    add_device("fixture-phone", PHONE_IP, owner)
    if paired:
        pairing.register("fixture-phone", PHONE_IP, phone.secret, "Fixture phone", "phone")
    dialed = []
    ports = {PHONE_IP: phone.port}

    def phone_address(ip):
        dialed.append(ip)
        return "127.0.0.1", ports.get(ip, 1)

    monkeypatch.setattr(enroll, "phone_address", phone_address)
    notices = []
    monkeypatch.setattr(enroll, "send_device_notice",
                        lambda op, did, ip, title, body: notices.append((op, did, title)))
    from Orchestrator.app import app
    client = TestClient(app, client=("127.0.0.1", 50000))
    client.__enter__()
    box = Box(client, phone, PHONE_IP, dialed)
    box.ports = ports
    box.notices = notices
    return box


def close_box(box: Box) -> None:
    box.client.__exit__(None, None, None)
    box.phone.close()


_DB_CACHE: dict = {}


def enrolled_fixture(tmp_path, monkeypatch, name: str = "continuous_120", **fields):
    """A sandboxed box where OPERATOR is enrolled and `name` was pushed. The
    ingested DB is built once per process and copied in after that."""
    import os
    sandbox(tmp_path, monkeypatch)
    fx = fixtures()
    data = fx.load(name)
    from Orchestrator.wellness import enroll
    last = enroll.iso(data["now_ms"] - 5 * 60_000)
    rec = write_record(last_push_at=last, **fields)
    target = paths.db_path(rec["op_key"])
    if name in _DB_CACHE:
        paths.ensure_private_dir()
        target.write_bytes(_DB_CACHE[name])
        os.chmod(target, 0o600)
        st = store.open_store(rec["op_key"])
    else:
        st = store.open_store(rec["op_key"])
        ingest_batches(st, data["batches"], now_ms=data["now_ms"])
        conn = st._connect()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        _DB_CACHE[name] = target.read_bytes()
    return rec, st, data


# ------------------------------------------------------------------ the phone simulator

SIM_IP = "127.0.0.2"
REPO = Path(__file__).resolve().parents[2]
_SIM = None


def load_sim():
    """scripts/wellness_sim_phone.py as a module (loaded once)."""
    global _SIM
    if _SIM is None:
        spec = importlib.util.spec_from_file_location(
            "wellness_sim_phone", REPO / "scripts" / "wellness_sim_phone.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SIM = mod
    return _SIM


class ClientTransport:
    """The sim's transport over a TestClient (the box sees the sim address)."""

    def __init__(self, client):
        self.client = client

    def request(self, method, path, body=b"", headers=None):
        r = self.client.request(method, path, content=body or b"", headers=headers or {})
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content


def free_port(ip: str) -> int:
    import socket
    with socket.socket() as s:
        s.bind((ip, 0))
        return s.getsockname()[1]


def simulator_mode(port: int, on: bool = True) -> None:
    """[wellness] simulator / simulator_port in the test box's config.ini."""
    from Orchestrator.utils.config_ini import update_config_ini

    def edit(p):
        if not p.has_section("wellness"):
            p.add_section("wellness")
        p.set("wellness", "simulator", "true" if on else "false")
        p.set("wellness", "simulator_port", str(port))
    update_config_ini(edit)
