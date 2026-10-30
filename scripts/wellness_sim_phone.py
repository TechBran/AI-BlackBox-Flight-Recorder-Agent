#!/usr/bin/env python3
"""A fake phone for BlackBox Health: stands in for the Android app's :8765
listener and health collector so enrollment, signed ingest, rollups, status
and erase can all be exercised on a box with no phone. Stdlib only.

It runs on a loopback address (127.0.0.2 by default), which the box treats as
a phone ONLY while config.ini has

    [wellness]
    simulator = true
    simulator_port = 8765        ; where the box calls the fake phone back

(read fresh; no restart). Never set that on a box people use.

    python3 scripts/wellness_sim_phone.py register --operator wellness-fixture
    python3 scripts/wellness_sim_phone.py enroll   --operator wellness-fixture
    python3 scripts/wellness_sim_phone.py push --fixture continuous_120
    python3 scripts/wellness_sim_phone.py status
    python3 scripts/wellness_sim_phone.py capabilities
    python3 scripts/wellness_sim_phone.py serve          # the listener alone, foreground
    python3 scripts/wellness_sim_phone.py nudge-wait     # wait for the box's /wellness/sync
    python3 scripts/wellness_sim_phone.py chat --prompt "How did I sleep last night?"
    python3 scripts/wellness_sim_phone.py settings --providers anthropic
    python3 scripts/wellness_sim_phone.py unregister

Subcommands:

  register    adds a registry device "sim-phone" at the sim address (POST
              /devices/), assigns it to --operator (POST /devices/sim-phone/
              operator; the operator must be on the roster) and records a
              fresh pairing secret in <box-root>/credentials/
              wellness_sim_pairing.json (0600). The box's own pairing route
              accepts only tailnet addresses, so the simulator's secret lives
              in that separate file, which the box reads only in simulator
              mode.
  enroll      mints the 2-minute intent nonce, POSTs /wellness/enroll FROM the
              sim address (the box resolves the device by the caller's own
              address), answers the box's /wellness/bind with a listener of
              its own unless `serve` is already running, and keeps the key.
  push        signs and pushes every batch of a fixture (Orchestrator/tests/
              fixtures/wellness/) gzip'd, with the app's outbox rules: 200
              next; 401 stop ("Reconnect Health"); 409/410 stop and forget the
              key; 413 split the batch; 422 quarantine it; 429/503 wait the
              Retry-After (capped by --max-wait) and retry.
  capabilities, status    the signed capability report / the signed status.
  serve, nudge-wait       the listener: /wellness/bind, /wellness/sync,
              /wellness/unbind, /notify (pairing bearer), GET /healthz.
  chat        one POST /chat/stream turn SIGNED with the device key (the way
              the app's chat will sign it), so the box offers the wellness_*
              tools as it would on the enrolled phone. Prints the reply, the
              tools that ran and the UI-only wellness_used frames. --provider
              and --model as the chat clients send them (default: the box's).
              Also prints wellness_turn, the turn key stream_start carries.
              --save then records the turn through /chat/save echoing that
              key, the way the wave-3 clients do, so the ledger copy is
              masked and tagged (the box mints it per its auto-mint rule).
              The reply is the operator's own health data: it goes to this
              terminal only.
  settings    one SIGNED PUT /wellness/settings, as the phone's Health screen
              sends it: --providers <ids...> (the whole allowed list; no ids or
              --local-only: only AI on my network), --category NAME=on|off,
              --category-local-only NAME=on|off, --google-paid-key yes|no,
              --instinct-prompt TEXT ("" = the default). Widening needs this
              signature (rule W21). Prints the signed status's providers_allowed,
              categories and local_only before and after, so a cloud tick for
              the evals is scriptable and undoable.
  unregister  removes the registry device, the sim pairing row and the state.

State (the key, the enrollment, the intent nonce) lives in --state-dir,
default ~/.cache/bbx-wellness-sim/, 0700/0600. The signer here is written
independently of Orchestrator/wellness/origin_proof.py on purpose, the way the
Android one is: the box's golden vectors check both.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import hmac
import http.client
import http.server
import importlib.util
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "Orchestrator" / "tests" / "fixtures" / "wellness"
DEFAULT_IP = "127.0.0.2"
DEFAULT_PORT = 8765
DEFAULT_BOX = "http://127.0.0.1:9091"
DEVICE_ID = "sim-phone"
INTENT_LIFE_S = 120
STATE_FILE = "sim.json"
MAX_RETRIES = 5            # back-offs per batch before push gives up


# ------------------------------------------------------------------ state

def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class State:
    """sim.json in the state dir. Read fresh, written atomically, shared by
    the listener and the commands (even across processes)."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / STATE_FILE
        self.lock = threading.Lock()

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def update(self, **fields) -> dict:
        with self.lock:
            data = self.load()
            data.update(fields)
            _write_private(self.path, data)
            return data

    def append(self, key: str, item) -> None:
        with self.lock:
            data = self.load()
            data.setdefault(key, []).append(item)
            _write_private(self.path, data)


# ------------------------------------------------------------------ signing

def sign(key: bytes, enr: str, ts: int, nonce: str, method: str, path: str, body: bytes) -> str:
    msg = "v1|%s|%d|%s|%s|%s|%s" % (enr, ts, nonce, method.upper(), path.split("?", 1)[0],
                                     hashlib.sha256(body).hexdigest())
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def origin_header(key_b64: str, enr: str, method: str, path: str, body: bytes) -> str:
    key = base64.urlsafe_b64decode(key_b64 + "=" * (-len(key_b64) % 4))
    ts, nonce = int(time.time()), secrets.token_hex(16)
    return f"v1;enr={enr};ts={ts};nonce={nonce};sig={sign(key, enr, ts, nonce, method, path, body)}"


# ------------------------------------------------------------------ transport

Response = Tuple[int, Dict[str, str], bytes]


class HttpTransport:
    """Requests to the box FROM the sim address (the box resolves the phone
    by the caller's own address)."""

    def __init__(self, box_url: str, source_ip: str, timeout: float = 60.0):
        u = urllib.parse.urlsplit(box_url)
        self.https = u.scheme == "https"
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or (443 if self.https else 80)
        self.source_ip = source_ip
        self.timeout = timeout

    def request(self, method: str, path: str, body: bytes = b"",
                headers: Optional[dict] = None) -> Response:
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        conn = cls(self.host, self.port, timeout=self.timeout, source_address=(self.source_ip, 0))
        try:
            conn.request(method, path, body=body or None, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()


def _call(transport, method: str, path: str, body=None, *, signed: Optional[dict] = None,
          raw: Optional[bytes] = None, extra: Optional[dict] = None) -> Tuple[int, dict, dict]:
    data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
    headers = {"Content-Type": "application/json"}
    if signed:
        headers["X-BlackBox-Origin"] = origin_header(signed["key"], signed["enrollment_id"],
                                                     method, path, data)
    headers.update(extra or {})
    status, resp_headers, payload = transport.request(method, path, data, headers)
    try:
        parsed = json.loads(payload or b"{}")
    except ValueError:
        parsed = {"raw": payload.decode("utf-8", "replace")[:200]}
    return status, resp_headers, parsed


# ------------------------------------------------------------------ the listener

class _Handler(http.server.BaseHTTPRequestHandler):
    state: State = None

    def log_message(self, *args):  # the listener never logs bodies
        pass

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        secret = self.state.load().get("pairing_secret")
        got = self.headers.get("Authorization", "")
        return bool(secret) and hmac.compare_digest(got.encode(), f"Bearer {secret}".encode())

    def do_GET(self):
        if self.path == "/healthz":
            return self._reply(200, {"ok": True})
        return self._reply(404, {"error": "not_found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            body = {}
        if not self._authorized():
            return self._reply(401, {"error": "unauthorized"})
        st = self.state.load()
        if self.path == "/wellness/bind":
            intent = st.get("intent") or {}
            fresh = intent.get("expires_at", 0) >= time.time()
            if not fresh or body.get("nonce") != intent.get("nonce") or \
                    body.get("operator") != intent.get("operator"):
                return self._reply(403, {"error": "no_live_intent"})
            self.state.update(key=body.get("k"), enrollment_id=body.get("enrollment_id"),
                              epoch=body.get("epoch"), operator=body.get("operator"),
                              intent=None)
            return self._reply(200, {"ok": True})
        if body.get("operator") != st.get("operator"):
            return self._reply(403, {"error": "operator"})
        if self.path == "/wellness/sync":
            self.state.append("syncs", time.time())
            return self._reply(202, {"ok": True})
        if self.path == "/wellness/unbind":
            self.state.update(key=None, enrollment_id=None, epoch=None)
            return self._reply(200, {"ok": True})
        if self.path == "/notify":
            self.state.append("notices", {"title": body.get("title"), "at": time.time()})
            return self._reply(200, {"ok": True})
        return self._reply(404, {"error": "not_found"})


def start_listener(state: State, ip: str, port: int) -> http.server.ThreadingHTTPServer:
    handler = type("SimHandler", (_Handler,), {"state": state})
    server = http.server.ThreadingHTTPServer((ip, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _listening(ip: str, port: int) -> bool:
    try:
        conn = http.client.HTTPConnection(ip, port, timeout=1)
        conn.request("GET", "/healthz")
        ok = conn.getresponse().status == 200
        conn.close()
        return ok
    except OSError:
        return False


# ------------------------------------------------------------------ commands

def register(transport, state: State, *, operator: str, ip: str, port: int,
             pairing_file: Path, device_id: str = DEVICE_ID) -> dict:
    status, _, body = _call(transport, "POST", "/devices/", {
        "id": device_id, "name": device_id, "tailscale_ip": ip, "device_type": "android",
        "protocol": "adb", "description": "BlackBox Health phone simulator"})
    if status not in (200, 409):
        raise SystemExit(f"register: POST /devices/ -> {status} {body}")
    status, _, body = _call(transport, "POST", f"/devices/{device_id}/operator",
                            {"operator": operator})
    if status != 200:
        raise SystemExit(f"register: assigning {device_id} -> {status} {body}")
    secret = secrets.token_hex(32)
    try:
        rows = json.loads(pairing_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rows = {}
    rows[ip] = {"device_id": device_id, "secret": secret,
                "paired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _write_private(pairing_file, rows)
    state.update(operator=operator, ip=ip, port=port, device_id=device_id, pairing_secret=secret)
    return {"device_id": device_id, "ip": ip, "operator": operator}


def enroll(transport, state: State, *, operator: str, capability_report: Optional[dict] = None,
           categories: Optional[dict] = None, replace: bool = False,
           assign_to_me: bool = False, ip: Optional[str] = None, port: Optional[int] = None,
           wait_s: float = 15.0) -> dict:
    st = state.load()
    ip = ip or st.get("ip") or DEFAULT_IP
    port = port or st.get("port") or DEFAULT_PORT
    nonce = secrets.token_hex(16)
    state.update(operator=operator, intent={"nonce": nonce, "operator": operator,
                                            "expires_at": time.time() + INTENT_LIFE_S})
    server = None if _listening(ip, port) else start_listener(state, ip, port)
    try:
        status, _, body = _call(transport, "POST", "/wellness/enroll", {
            "operator": operator, "install_id": st.get("install_id") or "sim-" + secrets.token_hex(4),
            "tailnet_ips": [ip], "nonce": nonce, "capability_report": capability_report,
            "categories": categories, "replace": replace, "assign_to_me": assign_to_me})
        if status != 200:
            return {"status": status, **body}
        deadline = time.time() + wait_s
        while state.load().get("enrollment_id") != body.get("enrollment_id"):
            if time.time() > deadline:
                return {"status": 504, "error": "bind_not_seen"}
            time.sleep(0.05)
        return {"status": 200, **body}
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


def _signer(state: State) -> Optional[dict]:
    st = state.load()
    if not st.get("key") or not st.get("enrollment_id"):
        return None
    return {"key": st["key"], "enrollment_id": st["enrollment_id"], "epoch": st.get("epoch")}


def _fixture_module():
    spec = importlib.util.spec_from_file_location("bbx_wellness_fixtures",
                                                  FIXTURES / "generate_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_fixture(name: str) -> dict:
    return _fixture_module().load(name)


def _split(batch: dict) -> Optional[Tuple[dict, dict]]:
    parts = []
    for key in ("buckets", "sessions", "readings"):
        items = batch.get(key) or []
        parts += [(key, x) for x in items]
    if len(parts) < 2:
        return None
    half = len(parts) // 2
    out = []
    for suffix, chunk in (("a", parts[:half]), ("b", parts[half:])):
        b = dict(batch, batch_id=f"{batch['batch_id']}-{suffix}", buckets=[], sessions=[],
                 readings=[], deleted_ids=batch.get("deleted_ids") if suffix == "a" else [])
        for key, x in chunk:
            b[key].append(x)
        out.append(b)
    return out[0], out[1]


def push(transport, state: State, *, fixture: str, limit: Optional[int] = None,
         max_wait_s: float = 60.0, sleep: Callable[[float], None] = time.sleep,
         log: Callable[[str], None] = print) -> dict:
    signer = _signer(state)
    if signer is None:
        return {"error": "not_enrolled", "pushed": 0}
    data = load_fixture(fixture)
    queue = []
    for b in data["batches"][:limit] if limit else data["batches"]:
        queue.append(dict(b, enrollment_id=signer["enrollment_id"], epoch=signer["epoch"]))
    result = {"pushed": 0, "accepted": 0, "duplicates": 0, "quarantined": 0, "split": 0,
              "notices": 0}
    tries: Dict[str, int] = {}
    while queue:
        batch = queue.pop(0)
        raw = gzip.compress(json.dumps(batch, separators=(",", ":")).encode())
        status, headers, body = _call(transport, "POST", "/wellness/ingest", raw=raw,
                                      signed=signer, extra={"Content-Encoding": "gzip"})
        if status == 200:
            result["pushed"] += 1
            result["accepted"] += body.get("accepted", 0)
            result["duplicates"] += 1 if body.get("duplicate") else 0
            result["notices"] += len(body.get("pending_notices") or [])
        elif status == 413:
            halves = _split(batch)
            if halves is None:
                result["quarantined"] += 1
            else:
                result["split"] += 1
                queue[:0] = list(halves)
        elif status == 422:
            result["quarantined"] += 1
            log(f"push: quarantined {batch['batch_id']}: {body.get('detail')}")
        elif status in (429, 503):
            wait = min(max_wait_s, float(headers.get("retry-after") or 5))
            tries[batch["batch_id"]] = tries.get(batch["batch_id"], 0) + 1
            log(f"push: {status} {body.get('error')}, waiting {wait:.0f} s")
            if wait <= 0 or tries[batch["batch_id"]] > MAX_RETRIES:
                return dict(result, stopped=status, error=body.get("error"))
            sleep(wait)
            queue.insert(0, batch)
        elif status in (409, 410):
            state.update(key=None, enrollment_id=None, epoch=None)
            return dict(result, stopped=status, error=body.get("error"))
        else:   # 401 and anything unexpected: stop, "Reconnect Health"
            return dict(result, stopped=status, error=body.get("error"))
    return result


def capabilities(transport, state: State, report: dict) -> Tuple[int, dict]:
    signer = _signer(state)
    if signer is None:
        return 0, {"error": "not_enrolled"}
    status, _, body = _call(transport, "POST", "/wellness/capabilities", report, signed=signer)
    return status, body


def status(transport, state: State, operator: Optional[str] = None) -> Tuple[int, dict]:
    st = state.load()
    op = operator or st.get("operator") or ""
    path = "/wellness/status?" + urllib.parse.urlencode({"operator": op})
    status_code, _, body = _call(transport, "GET", path, signed=_signer(state))
    return status_code, body


def nudge_wait(state: State, *, timeout_s: float, ip: str, port: int) -> Optional[float]:
    before = len(state.load().get("syncs") or [])
    server = None if _listening(ip, port) else start_listener(state, ip, port)
    try:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            syncs = state.load().get("syncs") or []
            if len(syncs) > before:
                return syncs[-1]
            time.sleep(0.1)
        return None
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


def _sse_events(text: str) -> list:
    """[(event, data)] of an SSE body (comment frames dropped)."""
    out = []
    for block in text.split("\n\n"):
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if event:
            try:
                out.append((event, json.loads(data) if data is not None else None))
            except ValueError:
                out.append((event, data))
    return out


def chat(transport, state: State, *, prompt: str, provider: Optional[str] = None,
         model: Optional[str] = None, operator: Optional[str] = None,
         save: bool = False) -> dict:
    """One signed /chat/stream turn: {status, reply, tools, wellness_used,
    wellness_turn, error}; with save, also {saved: {status, body}} from
    /chat/save echoing the turn key (unsigned, as the clients send it)."""
    signer = _signer(state)
    if signer is None:
        return {"status": 0, "error": "not_enrolled"}
    st = state.load()
    body = {"messages": [{"role": "user", "content": prompt}],
            "operator": operator or st.get("operator") or "", "origin_device_id": st.get("ip")}
    if provider:
        body["provider"] = provider
    if model:
        body["model"] = model
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json",
               "X-BlackBox-Origin": origin_header(signer["key"], signer["enrollment_id"],
                                                  "POST", "/chat/stream", data)}
    code, _, payload = transport.request("POST", "/chat/stream", data, headers)
    events = _sse_events(payload.decode("utf-8", "replace"))
    start = next((d for e, d in events if e == "stream_start" and isinstance(d, dict)), {})
    out = {"status": code,
           "reply": "".join(d for e, d in events if e == "content" and isinstance(d, str)),
           "tools": [d for e, d in events if e == "tool_start"],
           "wellness_used": [d for e, d in events if e == "wellness_used"],
           "wellness_turn": start.get("wellness_turn"),
           "error": next((d for e, d in events if e == "error"), None)}
    if save and code == 200 and out["reply"]:
        saved = {"operator": body["operator"], "user_message": prompt,
                 "assistant_response": out["reply"], "carry_location": False}
        if start.get("model"):
            saved["model"] = start["model"]
        if out["wellness_turn"]:
            saved["wellness_turn"] = out["wellness_turn"]
        s_code, _, s_payload = transport.request(
            "POST", "/chat/save", json.dumps(saved).encode(),
            {"Content-Type": "application/json"})
        try:
            s_body = json.loads(s_payload.decode("utf-8", "replace"))
        except ValueError:
            s_body = {"raw_len": len(s_payload)}
        out["saved"] = {"status": s_code, "body": s_body}
    return out


_KEEP = object()
CONSENT_WORDS = {"on": "ask", "ask": "ask", "proactive": "proactive", "off": "off"}
SWITCH_WORDS = {"on": True, "yes": True, "true": True, "off": False, "no": False, "false": False}


def _settings_view(body: dict) -> dict:
    """What a settings change moves (the instinct prompt as custom/default)."""
    body = body if isinstance(body, dict) else {}
    out = {k: body.get(k) for k in ("providers_allowed", "categories", "local_only",
                                    "google_paid_key_confirmed")}
    out["instinct_prompt"] = "custom" if body.get("instinct_prompt") else "default"
    return out


def settings(transport, state: State, *, operator: Optional[str] = None,
             providers: Optional[list] = None, categories: Optional[dict] = None,
             local_only: Optional[dict] = None, google_paid_key_confirmed: Optional[bool] = None,
             instinct_prompt=_KEEP) -> dict:
    """One SIGNED PUT /wellness/settings, the phone's own Health settings
    screen: `providers` is the whole allowed list ([] = only AI on my
    network); `categories` {category: ask|proactive|off}; `local_only`
    {category: bool}; `instinct_prompt` ("" or None = the default). Widening
    needs this signature (W21). Returns {status, before, after} (the signed
    status's providers_allowed, categories, local_only, ...) or the refusal."""
    signer = _signer(state)
    if signer is None:
        return {"status": 0, "error": "not_enrolled"}
    op = operator or state.load().get("operator") or ""
    _code, before = status(transport, state, op)
    body: dict = {"operator": op}
    if providers is not None:
        body["providers_allowed"] = list(providers)
    if categories:
        body["categories"] = dict(categories)
    if local_only:
        body["local_only"] = dict(local_only)
    if google_paid_key_confirmed is not None:
        body["google_paid_key_confirmed"] = bool(google_paid_key_confirmed)
    if instinct_prompt is not _KEEP:
        body["instinct_prompt"] = instinct_prompt or None
    code, _, after = _call(transport, "PUT", "/wellness/settings", body, signed=signer)
    out = {"status": code, "before": _settings_view(before)}
    if code == 200:
        out["after"] = _settings_view(after)
    else:
        out.update(error=after.get("error"), detail=after.get("detail"), fix=after.get("fix"))
    return out


def _pairs(items, words: dict, what: str) -> dict:
    out = {}
    for item in items or ():
        name, sep, value = str(item).partition("=")
        if not sep or value.strip().lower() not in words:
            raise SystemExit(f"settings: {what} takes NAME={'|'.join(sorted(words))}, "
                             f"got {item!r}")
        out[name.strip().lower()] = words[value.strip().lower()]
    return out


def settings_kwargs(args) -> dict:
    """The settings() keywords for the `settings` subcommand's flags."""
    if args.local_only and args.providers:
        raise SystemExit("settings: --local-only allows no cloud provider; drop --providers")
    kw: dict = {}
    if args.local_only:
        kw["providers"] = []
    elif args.providers is not None:
        kw["providers"] = list(args.providers)
    cats = _pairs(args.category, CONSENT_WORDS, "--category")
    if cats:
        kw["categories"] = cats
    local = _pairs(args.category_local_only, SWITCH_WORDS, "--category-local-only")
    if local:
        kw["local_only"] = local
    if args.google_paid_key is not None:
        kw["google_paid_key_confirmed"] = args.google_paid_key == "yes"
    if args.instinct_prompt is not None:
        kw["instinct_prompt"] = args.instinct_prompt
    if not kw:
        raise SystemExit("settings: nothing to change (see settings --help)")
    return kw


def unregister(transport, state: State, pairing_file: Path) -> None:
    st = state.load()
    _call(transport, "DELETE", f"/devices/{st.get('device_id') or DEVICE_ID}")
    try:
        rows = json.loads(pairing_file.read_text(encoding="utf-8"))
        rows.pop(st.get("ip") or DEFAULT_IP, None)
        _write_private(pairing_file, rows)
    except (OSError, ValueError):
        pass
    try:
        state.path.unlink()
    except OSError:
        pass


# ------------------------------------------------------------------ CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--box", default=DEFAULT_BOX, help="the BlackBox URL (default %(default)s)")
    p.add_argument("--state-dir", default=str(Path.home() / ".cache" / "bbx-wellness-sim"))
    p.add_argument("--ip", default=None, help=f"the sim's loopback address (default {DEFAULT_IP})")
    p.add_argument("--port", type=int, default=None,
                   help=f"the sim listener's port = the box's simulator_port (default {DEFAULT_PORT})")
    p.add_argument("--timeout", type=float, default=300.0,
                   help="seconds to wait for the box per request (a cold custom-server load "
                        "and a tool turn can take minutes; default 300)")
    p.add_argument("--box-root", default=str(REPO),
                   help="the box's install root, for credentials/wellness_sim_pairing.json")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register")
    r.add_argument("--operator", required=True)
    e = sub.add_parser("enroll")
    e.add_argument("--operator", required=True)
    e.add_argument("--replace", action="store_true")
    e.add_argument("--assign-to-me", action="store_true")
    e.add_argument("--fixture", default="continuous_120", help="whose capability report to send")
    pu = sub.add_parser("push")
    pu.add_argument("--fixture", default="continuous_120")
    pu.add_argument("--limit", type=int, default=None)
    pu.add_argument("--max-wait", type=float, default=60.0)
    c = sub.add_parser("capabilities")
    c.add_argument("--fixture", default="continuous_120")
    s = sub.add_parser("status")
    s.add_argument("--operator", default=None)
    sub.add_parser("serve")
    n = sub.add_parser("nudge-wait")
    n.add_argument("--timeout", type=float, default=120.0)
    ch = sub.add_parser("chat")
    ch.add_argument("--prompt", required=True)
    ch.add_argument("--provider", default=None)
    ch.add_argument("--model", default=None)
    ch.add_argument("--save", action="store_true",
                    help="record the turn through /chat/save, echoing wellness_turn")
    st = sub.add_parser("settings", help="the phone's Health settings (a signed PUT "
                                         "/wellness/settings)")
    st.add_argument("--operator", default=None)
    st.add_argument("--providers", nargs="*", default=None, metavar="ID",
                    help="the whole allowed list (anthropic, openai, xai, google, "
                         "custom:<alias>); with no ids: only AI on my network")
    st.add_argument("--local-only", action="store_true",
                    help="only AI on my network (no cloud provider), the screen's top switch")
    st.add_argument("--category", "--categories", dest="category", action="append", default=[],
                    metavar="NAME=on|off|ask|proactive",
                    help="a category's consent (sleep, heart, activity, blood_pressure)")
    st.add_argument("--category-local-only", action="append", default=[], metavar="NAME=on|off",
                    help="keep a category on local models only")
    st.add_argument("--google-paid-key", choices=("yes", "no"), default=None,
                    help="'My Google key is on a paid plan'")
    st.add_argument("--instinct-prompt", default=None,
                    help='the health instinct prompt ("" = back to the default)')
    sub.add_parser("unregister")
    return p


def main(argv=None) -> int:
    p = build_parser()
    args = p.parse_args(argv)

    state = State(Path(args.state_dir).expanduser())
    saved = state.load()
    ip = args.ip or saved.get("ip") or DEFAULT_IP
    port = args.port or saved.get("port") or DEFAULT_PORT
    transport = HttpTransport(args.box, ip, timeout=args.timeout)
    pairing_file = Path(args.box_root) / "credentials" / "wellness_sim_pairing.json"
    out: object
    if args.cmd == "register":
        out = register(transport, state, operator=args.operator, ip=ip, port=port,
                       pairing_file=pairing_file)
    elif args.cmd == "enroll":
        out = enroll(transport, state, operator=args.operator,
                     capability_report=load_fixture(args.fixture)["capabilities"],
                     replace=args.replace, assign_to_me=args.assign_to_me, ip=ip, port=port)
    elif args.cmd == "push":
        out = push(transport, state, fixture=args.fixture, limit=args.limit,
                   max_wait_s=args.max_wait)
    elif args.cmd == "capabilities":
        out = dict(zip(("status", "body"), capabilities(
            transport, state, load_fixture(args.fixture)["capabilities"])))
    elif args.cmd == "status":
        out = dict(zip(("status", "body"), status(transport, state, args.operator)))
    elif args.cmd == "serve":
        server = start_listener(state, ip, port)
        print(f"sim phone listening on http://{ip}:{port} (Ctrl-C to stop)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            server.shutdown()
        return 0
    elif args.cmd == "nudge-wait":
        at = nudge_wait(state, timeout_s=args.timeout, ip=ip, port=port)
        out = {"nudged": at is not None}
    elif args.cmd == "chat":
        out = chat(transport, state, prompt=args.prompt, provider=args.provider,
                   model=args.model, save=args.save)
    elif args.cmd == "settings":
        out = settings(transport, state, operator=args.operator, **settings_kwargs(args))
    else:
        unregister(transport, state, pairing_file)
        out = {"unregistered": True}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
