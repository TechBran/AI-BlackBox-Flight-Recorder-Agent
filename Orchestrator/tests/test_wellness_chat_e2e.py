"""End to end, wave 2: the phone simulator (scripts/wellness_sim_phone.py)
enrolls the fixture operator against the real app in-process and pushes the
120-day fixture; then a /chat/stream turn SIGNED with the device key runs on a
model server on this network (provider custom, allowed by default) whose fake
upstream calls wellness_summary. The tool ran, the Signal frame was emitted,
the delivered row exists with this turn's key, and the reply quotes the display
string. The same turn unsigned is offered nothing. Nothing logged carries a
value. And POST /chat hands a verified request to its worker as a one-use
grant, never a field a client can forge."""
from __future__ import annotations

import copy
import json
import logging
import re

import httpx
import pytest

from Orchestrator.tests.wellness_agent_support import (
    LAN_MODEL, custom_server, no_embeddings, sentinel_hits, stub_retrieval)
from Orchestrator.tests.wellness_support import (OPERATOR, SIM_IP, ClientTransport, fixtures,
                                                 free_port, load_sim, roster, simulator_mode)
from Orchestrator.wellness import access, catalog, enroll

FX = fixtures()
SIM = load_sim()
DAY = FX.day_of(FX.SENTINEL_DAYS["sleeping_hr"])
QUESTION = f"What was my sleeping heart rate on {DAY}?"
DISPLAY_RE = re.compile(r"sleeping_hr_median \(Sleeping heart rate\): (\d+ bpm)")


class FakeLanServer:
    """An OpenAI-compatible model server, in-process (httpx.AsyncClient): it
    calls wellness_summary when it is offered, then answers from the result."""

    def __init__(self):
        self.requests = []

    def _chunks(self, payload):
        tool_msgs = [m for m in payload["messages"] if m.get("role") == "tool"]
        offered = {t["function"]["name"] for t in payload.get("tools") or []}
        if not tool_msgs and "wellness_summary" in offered:
            args = json.dumps({"period": "custom", "start": DAY, "end": DAY})
            delta = {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                     "function": {"name": "wellness_summary",
                                                  "arguments": args}}]}
            return [{"choices": [{"delta": delta, "finish_reason": "tool_calls"}]}]
        if tool_msgs:
            m = DISPLAY_RE.search(tool_msgs[-1]["content"])
            text = f"Your sleeping heart rate was {m.group(1)}." if m else "No answer."
        else:
            text = "I can't see any health data here."
        return [{"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}]

    def client_class(self):
        server = self

        class _Resp:
            status_code = 200

            def __init__(self, chunks):
                self._chunks = chunks

            async def aiter_lines(self):
                for c in self._chunks:
                    yield "data: " + json.dumps(c)
                yield "data: [DONE]"

            async def aread(self):
                return b""

        class _Stream:
            def __init__(self, resp):
                self.resp = resp

            async def __aenter__(self):
                return self.resp

            async def __aexit__(self, *exc):
                return False

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def stream(self, method, url, headers=None, json=None, **kw):
                server.requests.append(copy.deepcopy(json))
                return _Stream(_Resp(server._chunks(json)))

        return _Client


def _events(sse: str):
    out = []
    for block in sse.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines()
                     if line.startswith(("event: ", "data: ")))
        if "event" in lines:
            out.append((lines["event"], json.loads(lines.get("data", "null"))))
    return out


@pytest.fixture
def enrolled_phone(isolated_stores, toolvault_embed_sync, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import Orchestrator.routes.chat_routes as cr
    from Orchestrator.app import app
    roster(monkeypatch, OPERATOR, "other-human", "Flight Recorder")
    port = free_port(SIM_IP)
    simulator_mode(port)
    with TestClient(app, client=(SIM_IP, 50000)) as client:
        t = ClientTransport(client)
        state = SIM.State(tmp_path / "sim-state")
        from Orchestrator.wellness import paths
        SIM.register(t, state, operator=OPERATOR, ip=SIM_IP, port=port,
                     pairing_file=paths.sim_pairing_path())
        out = SIM.enroll(t, state, operator=OPERATOR, ip=SIM_IP, port=port,
                         capability_report=FX.load("continuous_120")["capabilities"])
        assert out["status"] == 200 and out["state"] == "active", out
        pushed = SIM.push(t, state, fixture="continuous_120", log=lambda m: None)
        assert pushed["quarantined"] == 0
        # The chat turn: a model server on this network, ToolVault on, no
        # embedding provider, no ledger reads, the fake upstream.
        custom_server()
        monkeypatch.setattr(cr, "TOOLVAULT_ENABLED", True)
        no_embeddings(monkeypatch)
        stub_retrieval(monkeypatch)
        lan = FakeLanServer()
        monkeypatch.setattr(httpx, "AsyncClient", lan.client_class())
        yield {"client": client, "state": state, "lan": lan}


def _chat(box, *, signed: bool, extra=None):
    body = json.dumps({"messages": [{"role": "user", "content": QUESTION}], "provider": "custom",
                       "model": LAN_MODEL, "operator": OPERATOR, "origin_device_id": SIM_IP,
                       **(extra or {})}).encode()
    headers = {"Content-Type": "application/json"}
    if signed:
        st = box["state"].load()
        headers["X-BlackBox-Origin"] = SIM.origin_header(st["key"], st["enrollment_id"], "POST",
                                                         "/chat/stream", body)
    r = box["client"].post("/chat/stream", content=body, headers=headers)
    assert r.status_code == 200
    return _events(r.text)


def _store():
    return enroll.store_for(enroll.get_enrollment(OPERATOR), create=False)


def test_a_signed_turn_reads_health_data_and_an_unsigned_one_does_not(enrolled_phone, capsys,
                                                                      caplog):
    caplog.set_level(logging.DEBUG)
    lan = enrolled_phone["lan"]
    out = SIM.chat(ClientTransport(enrolled_phone["client"]), enrolled_phone["state"],
                   prompt=QUESTION, provider="custom", model=LAN_MODEL)
    assert out["status"] == 200 and out["error"] is None, out
    assert out["tools"] == ["wellness_summary"]
    first, second = lan.requests
    offered = {t["function"]["name"] for t in first["tools"]}
    assert "wellness_summary" in offered
    system = first["messages"][0]["content"]
    assert catalog.WELLNESS_RULES in system and catalog.DEFAULT_INSTINCT_PROMPT in system

    # The tool ran, inside this turn: one delivered row, this surface/route.
    [row] = _store().disclosures()
    assert row["tool"] == "wellness_summary" and row["turn_key"].startswith("wt_")
    assert (row["surface"], row["provider"], row["model"]) == ("android", "custom", LAN_MODEL)
    assert row["categories"] == ["activity", "heart", "sleep"]

    # The Signal frame, UI-only, names that row.
    frames = out["wellness_used"]
    assert len(frames) == 1 and frames[0]["ref"] == row["ref"]
    assert frames[0]["provider_label"] == "lan-box"
    tool_reply = [m for m in second["messages"] if m.get("role") == "tool"][0]["content"]
    assert "wellness_used" not in json.dumps(second)
    assert "47 bpm" in tool_reply and str(FX.SENTINEL_SLEEPING_HR) not in tool_reply

    # The reply quotes the display string.
    reply = out["reply"]
    assert reply == "Your sleeping heart rate was 47 bpm."

    # The same turn unsigned (two humans on the box: credentialed) gets nothing.
    lan.requests.clear()
    events = _chat(enrolled_phone, signed=False)
    [only] = lan.requests
    assert only["tools"]                                    # other tools are still there
    assert not [t for t in only["tools"] if t["function"]["name"].startswith("wellness_")]
    assert catalog.WELLNESS_RULES not in only["messages"][0]["content"]
    assert not [d for e, d in events if e == "wellness_used"]
    assert len(_store().disclosures()) == 1

    # A replayed signature is no credential either.
    lan.requests.clear()
    body = json.dumps({"messages": [{"role": "user", "content": QUESTION}], "provider": "custom",
                       "model": LAN_MODEL, "operator": OPERATOR}).encode()
    st = enrolled_phone["state"].load()
    header = SIM.origin_header(st["key"], st["enrollment_id"], "POST", "/chat/stream", body)
    for _ in range(2):
        enrolled_phone["client"].post("/chat/stream", content=body,
                                      headers={"Content-Type": "application/json",
                                               "X-BlackBox-Origin": header})
    offered = [{t["function"]["name"] for t in req["tools"]} for req in lan.requests
               if not any(m.get("role") == "tool" for m in req["messages"])]
    assert "wellness_summary" in offered[0] and "wellness_summary" not in offered[-1]

    out = capsys.readouterr()
    printed = out.out + out.err + "\n".join(r.getMessage() for r in caplog.records)
    assert not sentinel_hits(printed), sentinel_hits(printed)


def test_post_chat_hands_a_verified_request_to_its_worker_as_a_grant(enrolled_phone,
                                                                     monkeypatch):
    import Orchestrator.routes.chat_routes as cr
    created = []

    class _Task:
        task_id, status = "task-wgrant", "pending"

    def fake_create_task(task_type, operator=None, result_data=None, **kw):
        created.append(copy.deepcopy(result_data))
        return _Task()

    monkeypatch.setattr(cr, "create_task", fake_create_task)
    client, state = enrolled_phone["client"], enrolled_phone["state"].load()
    body = json.dumps({"messages": [{"role": "user", "content": QUESTION}], "provider": "custom",
                       "model": LAN_MODEL, "operator": OPERATOR,
                       access.GRANT_KEY: "wg_forged"}).encode()
    signed = SIM.origin_header(state["key"], state["enrollment_id"], "POST", "/chat", body)
    assert client.post("/chat", content=body, headers={"Content-Type": "application/json",
                                                       "X-BlackBox-Origin": signed}).status_code \
        == 200
    assert client.post("/chat", content=body,
                       headers={"Content-Type": "application/json"}).status_code == 200
    granted, unsigned = created
    assert granted[access.GRANT_KEY] != "wg_forged"
    assert access.GRANT_KEY not in unsigned                    # a client's own field is dropped
    assert access.redeem_grant(granted[access.GRANT_KEY], OPERATOR) == state["enrollment_id"]
    turn = access.task_turn(unsigned, OPERATOR, "custom", LAN_MODEL)
    assert turn.surface == "portal" and not turn.credential_ok
