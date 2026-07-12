"""transfer_call / hangup_call — scoped to an active xAI call session.

REST endpoints (recon xaiResearch.json api_details):
    POST /v1/realtime/calls/{call_id}/refer  {"target_uri": ...}
    POST /v1/realtime/calls/{call_id}/hangup
"""
import pytest

from Orchestrator.models import GROK_LIVE_SESSIONS, GrokLiveSession
from Orchestrator.xai_phone import call_control as cc


@pytest.fixture(autouse=True)
def clean_registry():
    GROK_LIVE_SESSIONS.clear()
    yield
    GROK_LIVE_SESSIONS.clear()


def _add_call(call_id: str, status: str = "connected"):
    sid = f"phone-xai-{call_id}"
    GROK_LIVE_SESSIONS[sid] = GrokLiveSession(session_id=sid, call_id=call_id, status=status)


@pytest.fixture
def posted(monkeypatch):
    calls = []

    async def fake_post(call_id, action, payload=None):
        calls.append((call_id, action, payload))
        return True, f"{action} accepted for call {call_id}"

    monkeypatch.setattr(cc, "_call_post", fake_post)
    return calls


# --------------------------------------------------------------- scope guard

@pytest.mark.asyncio
async def test_no_active_call_fails_gracefully(posted):
    ok, msg = await cc.hangup_call()
    assert not ok and "No active xAI phone call" in msg
    assert posted == []


@pytest.mark.asyncio
async def test_disconnected_session_is_not_active(posted):
    _add_call("c1", status="disconnected")
    ok, msg = await cc.hangup_call()
    assert not ok and posted == []


@pytest.mark.asyncio
async def test_single_active_call_resolved_implicitly(posted):
    _add_call("c1")
    ok, msg = await cc.hangup_call()
    assert ok
    assert posted == [("c1", "hangup", None)]


@pytest.mark.asyncio
async def test_multiple_active_calls_require_explicit_call_id(posted):
    _add_call("c1")
    _add_call("c2")
    ok, msg = await cc.hangup_call()
    assert not ok and "Multiple active calls" in msg
    ok, _ = await cc.hangup_call(call_id="c2")
    assert ok and posted == [("c2", "hangup", None)]


@pytest.mark.asyncio
async def test_explicit_unknown_call_id_rejected(posted):
    _add_call("c1")
    ok, msg = await cc.hangup_call(call_id="nope")
    assert not ok and "not an active xAI call" in msg
    assert posted == []


# ----------------------------------------------------------------- transfer

@pytest.mark.asyncio
async def test_transfer_requires_target_uri(posted):
    _add_call("c1")
    ok, msg = await cc.transfer_call("")
    assert not ok and "target_uri" in msg
    assert posted == []


@pytest.mark.asyncio
async def test_transfer_posts_refer_with_target(posted):
    _add_call("c1")
    ok, _ = await cc.transfer_call("tel:+15550100")
    assert ok
    assert posted == [("c1", "refer", {"target_uri": "tel:+15550100"})]


# --------------------------------------------------------- toolvault modules

@pytest.mark.asyncio
async def test_executors_load_and_dispatch(posted, monkeypatch):
    from Orchestrator.toolvault import registry
    from Orchestrator.toolvault.context import ToolContext

    _add_call("c1")
    ctx = ToolContext(operator="system", base_url="http://localhost:9091")

    transfer = registry.get_executor("transfer_call")
    hangup = registry.get_executor("hangup_call")
    assert transfer and hangup

    res = await transfer({"target_uri": "tel:+15550100"}, ctx)
    assert res.success
    res = await hangup({}, ctx)
    assert res.success
    assert [(c, a) for c, a, _ in posted] == [("c1", "refer"), ("c1", "hangup")]


@pytest.mark.asyncio
async def test_executor_fails_gracefully_outside_call():
    from Orchestrator.toolvault import registry
    from Orchestrator.toolvault.context import ToolContext

    hangup = registry.get_executor("hangup_call")
    res = await hangup({}, ToolContext(operator="system", base_url="http://localhost:9091"))
    assert res.success is False
    assert "No active xAI phone call" in res.result
