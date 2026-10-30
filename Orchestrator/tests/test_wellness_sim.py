"""The end-to-end gate of the box core: scripts/wellness_sim_phone.py against
the real app in-process (a TestClient seen from the sim's loopback address,
the sim's listener on a thread), through register -> enroll -> push 120 days
-> status -> summary -> nudge -> erase. Plus the sim's outbox rules against a
scripted box, and simulator mode staying off unless config.ini says so."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from Orchestrator.tests.wellness_support import (OPERATOR, SIM_IP, ClientTransport, fixtures,
                                                 free_port, load_sim, roster, simulator_mode)
from Orchestrator.wellness import api, enroll, paths

REPO = Path(__file__).resolve().parents[2]
FX = fixtures()
SIM = load_sim()


@pytest.fixture
def sim_box(isolated_stores, toolvault_embed_sync, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from Orchestrator.app import app
    roster(monkeypatch, OPERATOR, "other-human", "Flight Recorder")
    port = free_port(SIM_IP)
    simulator_mode(port)
    with TestClient(app, client=(SIM_IP, 50000)) as client:
        state = SIM.State(tmp_path / "sim-state")
        yield {"t": ClientTransport(client), "state": state, "port": port, "client": client}


def test_end_to_end(sim_box):
    t, state, port = sim_box["t"], sim_box["state"], sim_box["port"]
    reg = SIM.register(t, state, operator=OPERATOR, ip=SIM_IP, port=port,
                       pairing_file=paths.sim_pairing_path())
    assert reg == {"device_id": "sim-phone", "ip": SIM_IP, "operator": OPERATOR}

    caps = FX.load("continuous_120")["capabilities"]
    out = SIM.enroll(t, state, operator=OPERATOR, capability_report=caps, ip=SIM_IP, port=port)
    assert out["status"] == 200 and out["state"] == "active" and out["epoch"] == 1, out
    assert state.load()["key"] and state.load()["enrollment_id"] == out["enrollment_id"]

    pushed = SIM.push(t, state, fixture="continuous_120", log=lambda m: None)
    n = len(FX.load("continuous_120")["batches"])
    assert pushed["pushed"] == n and pushed["quarantined"] == 0 and pushed["accepted"] > 30_000
    # Blood pressure is off by default: the fixture's readings were dropped
    # at the door, so none is stored.
    with enroll.store_for(enroll.get_enrollment(OPERATOR)).read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reading WHERE metric='blood_pressure'"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM reading").fetchone()[0] > 0

    code, status = SIM.status(t, state)
    assert code == 200 and status["state"] in ("active", "stale") and status["enrolled"]
    assert {"troubles", "categories", "wear", "sync"} <= set(status)
    assert status["sync"]["outbox_hint"]["batches_received"] == n
    assert status["source_label"] == "Fixture phone (SM-TEST1)"

    now = FX.now_ms()
    week = api.summary(OPERATOR, "last_7_days", now_ms=now)
    sleep = next(m for m in week["metrics"] if m["metric"] == "sleep_duration")
    assert sleep["coverage"]["label"] == "worn 4 of 7 nights"
    night = api.summary(OPERATOR, "custom", start=FX.day_of(-5), end=FX.day_of(-5), now_ms=now)
    assert next(m for m in night["metrics"] if m["metric"] == "sleeping_hr_median")["value"] \
        == 47.3
    hist = api.history(OPERATOR, FX.day_of(-3), FX.day_of(-3), now_ms=now)
    assert dict(zip(hist["columns"], hist["rows"][0]))["steps"] == 12345

    again = SIM.push(t, state, fixture="continuous_120", limit=2, log=lambda m: None)
    assert again["duplicates"] == 2 and again["accepted"] == 0

    server = SIM.start_listener(state, SIM_IP, port)
    try:
        r = sim_box["client"].post("/wellness/sync-now", json={"operator": OPERATOR})
        assert r.json() == {"nudged": True}
        assert len(state.load()["syncs"]) == 1
        signer = SIM._signer(state)
        code, _, body = SIM._call(t, "POST", "/wellness/erase", {"operator": OPERATOR},
                                  signed=signer)
        assert code == 200 and body == {"state": "not_set_up"}
        assert state.load()["key"] is None             # the box told the phone to unbind
    finally:
        server.shutdown()
        server.server_close()
    assert enroll.get_enrollment(OPERATOR) is None
    code, status = SIM.status(t, state)
    assert status == {"state": "not_set_up", "source_label": None, "last_sync": None,
                      "enrolled": False}


def test_simulator_mode_is_off_by_default(sim_box):
    t, state, port = sim_box["t"], sim_box["state"], sim_box["port"]
    SIM.register(t, state, operator=OPERATOR, ip=SIM_IP, port=port,
                 pairing_file=paths.sim_pairing_path())
    simulator_mode(port, on=False)
    out = SIM.enroll(t, state, operator=OPERATOR, ip=SIM_IP, port=port, wait_s=1)
    assert out["status"] == 404 and out["error"] == "unknown_device"
    assert enroll.pairing_headers(SIM_IP) is None


def test_the_sim_never_answers_a_bind_without_its_live_nonce(sim_box):
    t, state, port = sim_box["t"], sim_box["state"], sim_box["port"]
    SIM.register(t, state, operator=OPERATOR, ip=SIM_IP, port=port,
                 pairing_file=paths.sim_pairing_path())
    server = SIM.start_listener(state, SIM_IP, port)
    try:
        status = enroll.call_phone(SIM_IP, "POST", "/wellness/bind",
                                   {"operator": OPERATOR, "enrollment_id": "en_00000001",
                                    "epoch": 1, "nonce": "ab" * 16, "k": "x" * 43},
                                   timeout=5)
        assert status == 403
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------------ outbox rules

class ScriptedBox:
    """Answers each ingest with the next scripted (status, headers, body)."""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def request(self, method, path, body=b"", headers=None):
        import gzip
        batch = json.loads(gzip.decompress(body))
        self.seen.append(batch["batch_id"])
        status, h, b = self.script.pop(0) if self.script else (200, {}, {"accepted": 1})
        if callable(status):
            status, h, b = status(batch)
        return status, h, json.dumps(b).encode()


def _enrolled_state(tmp_path):
    state = SIM.State(tmp_path / "s")
    state.update(key="A" * 43, enrollment_id="en_0000abcd", epoch=1, operator=OPERATOR)
    return state


def test_outbox_split_on_413_and_quarantine_on_422(tmp_path):
    state = _enrolled_state(tmp_path)
    box = ScriptedBox([(413, {}, {"error": "too_large"}), (422, {}, {"error": "invalid",
                                                                      "detail": "x"})])
    out = SIM.push(box, state, fixture="days_10", limit=1, log=lambda m: None)
    assert out["split"] == 1 and out["quarantined"] == 1 and out["pushed"] == 1
    assert box.seen[1].endswith("-a") and box.seen[2].endswith("-b")


def test_outbox_backs_off_then_stops_and_clears_on_410(tmp_path):
    state = _enrolled_state(tmp_path)
    waits = []
    box = ScriptedBox([(429, {"retry-after": "7"}, {"error": "rate_limited"}),
                       (200, {}, {"accepted": 5}),
                       (410, {}, {"error": "source_moved"})])
    out = SIM.push(box, state, fixture="days_10", sleep=waits.append, log=lambda m: None)
    assert waits == [7.0] and out["pushed"] == 1 and out["stopped"] == 410
    assert out["error"] == "source_moved" and state.load()["key"] is None


def test_outbox_stops_on_401_and_gives_up_after_retries(tmp_path):
    state = _enrolled_state(tmp_path)
    out = SIM.push(ScriptedBox([(401, {}, {"error": "bad_signature"})]), state,
                   fixture="days_10", log=lambda m: None)
    assert out["stopped"] == 401 and state.load()["key"] == "A" * 43   # kept: reconnect
    busy = ScriptedBox([(503, {"retry-after": "1"}, {"error": "paused"})] * 10)
    out = SIM.push(busy, state, fixture="days_10", sleep=lambda s: None, log=lambda m: None)
    assert out["stopped"] == 503 and len(busy.seen) == SIM.MAX_RETRIES + 1


def test_the_sim_signer_matches_the_golden_vectors():
    vectors = json.loads((REPO / "Orchestrator" / "tests" / "fixtures" / "wellness" /
                          "origin_proof_vectors.json").read_text(encoding="utf-8"))
    import base64
    for v in vectors:
        key = base64.urlsafe_b64decode(v["k_b64url"] + "=")
        body = base64.b64decode(v["body_b64"])
        assert SIM.sign(key, v["enr"], v["ts"], v["nonce"], v["method"], v["path"], body) == \
            v["expected_sig"]


def test_cli_help_runs():
    with pytest.raises(SystemExit) as e:
        SIM.main(["--help"])
    assert e.value.code == 0


class ScriptedChatBox:
    """Answers /chat/stream with a scripted SSE body and /chat/save with a
    scripted JSON reply; records every request so the save can be checked."""

    def __init__(self, sse_events, save_reply=None):
        self.sse = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in sse_events)
        self.save_reply = save_reply if save_reply is not None else {"success": True, "minted": True}
        self.requests = []

    def request(self, method, path, body=b"", headers=None):
        self.requests.append((method, path, json.loads(body) if body else None, headers or {}))
        if path == "/chat/stream":
            return 200, {"content-type": "text/event-stream"}, self.sse.encode()
        if path == "/chat/save":
            return 200, {}, json.dumps(self.save_reply).encode()
        return 404, {}, b"{}"


_SSE = [("stream_start", {"provider": "custom", "model": "box::m", "provenance": {},
                          "wellness_turn": "wt_0123456789abcdef"}),
        ("tool_start", {"tool": "wellness_summary"}),
        ("wellness_used", {"ref": "w_1", "categories": ["sleep"], "provider": "custom:box"}),
        ("content", "You slept "), ("content", "well."),
        ("done", {})]


def test_chat_reports_the_turn_key_and_saves_it_the_way_a_client_does(tmp_path):
    state = _enrolled_state(tmp_path)
    box = ScriptedChatBox(_SSE)
    out = SIM.chat(box, state, prompt="How did I sleep?", provider="custom", model="box::m")
    assert out["status"] == 200 and out["reply"] == "You slept well."
    assert out["wellness_turn"] == "wt_0123456789abcdef"
    assert [t["tool"] for t in out["tools"]] == ["wellness_summary"]
    assert out["wellness_used"][0]["ref"] == "w_1" and out["error"] is None
    assert "saved" not in out and [p for _, p, _, _ in box.requests] == ["/chat/stream"]
    stream_headers = box.requests[0][3]
    assert stream_headers["X-BlackBox-Origin"].startswith("v1;enr=en_0000abcd;")

    box = ScriptedChatBox(_SSE, save_reply={"success": True, "minted": True, "snap_id": "SNAP-1"})
    out = SIM.chat(box, state, prompt="How did I sleep?", provider="custom", model="box::m",
                   save=True)
    assert out["saved"] == {"status": 200,
                            "body": {"success": True, "minted": True, "snap_id": "SNAP-1"}}
    method, path, saved, headers = box.requests[1]
    assert (method, path) == ("POST", "/chat/save")
    assert saved == {"operator": OPERATOR, "user_message": "How did I sleep?",
                     "assistant_response": "You slept well.", "carry_location": False,
                     "model": "box::m", "wellness_turn": "wt_0123456789abcdef"}
    assert "X-BlackBox-Origin" not in headers   # the save is unsigned, as the clients send it


def test_chat_without_a_turn_key_saves_without_one_and_a_failed_stream_saves_nothing(tmp_path):
    state = _enrolled_state(tmp_path)
    box = ScriptedChatBox([("stream_start", {"provider": "google", "model": "g"}),
                           ("content", "Hi."), ("done", {})])
    out = SIM.chat(box, state, prompt="hi", save=True)
    assert out["wellness_turn"] is None and out["wellness_used"] == [] and out["tools"] == []
    assert "wellness_turn" not in box.requests[1][2] and box.requests[1][2]["model"] == "g"

    box = ScriptedChatBox([("error", {"message": "provider down"})])
    out = SIM.chat(box, state, prompt="hi", save=True)
    assert out["reply"] == "" and out["error"] == {"message": "provider down"}
    assert "saved" not in out and len(box.requests) == 1

    assert SIM.chat(box, SIM.State(tmp_path / "empty"), prompt="hi") == {"status": 0, "error": "not_enrolled"}
