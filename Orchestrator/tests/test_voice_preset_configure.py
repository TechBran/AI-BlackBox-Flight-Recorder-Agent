"""configure_openai_session: custom_role persona replacement + tool_group_override (P4)."""
import asyncio, json
import pytest

from Orchestrator.models import RealtimeSession
from Orchestrator.routes import realtime_routes as rt


class FakeWS:
    def __init__(self):
        self.sent = []
    async def send(self, payload):
        self.sent.append(json.loads(payload))


@pytest.fixture
def quiet_context(monkeypatch):
    # Skip the heavy fossil-context build — not under test here.
    monkeypatch.setattr(rt, "build_context_for_operator",
                        lambda operator, user_text="": ("", {}))


def _configure(**kwargs):
    session = RealtimeSession(session_id="t-p4")
    session.openai_ws = FakeWS()
    asyncio.run(rt.configure_openai_session(session, "system", "ash", **kwargs))
    return session.openai_ws.sent[0]


def test_custom_role_replaces_persona(quiet_context):
    cfg = _configure(custom_role="You are Pepper the pizza-order bot.")
    assert cfg["type"] == "session.update"
    instructions = cfg["session"]["instructions"]
    assert instructions.startswith("You are Pepper the pizza-order bot.")
    assert "IDENTITY:\nYou are the voice interface" not in instructions


def test_tool_group_override_swaps_tool_group(quiet_context):
    cfg = _configure(tool_group_override="gemini_live")
    sent = [t["name"] for t in cfg["session"]["tools"]]
    expected = [t["name"] for t in rt.get_openai_realtime_tools("gemini_live")]
    assert sent == expected


def test_no_override_keeps_default_tools(quiet_context):
    # P1.28 deleted the frozen REALTIME_TOOLS constant (tools are read at
    # configure time) — compare against the live group read, same as the route.
    cfg = _configure()
    assert [t["name"] for t in cfg["session"]["tools"]] == \
        [t["name"] for t in rt.get_openai_realtime_tools("realtime")]


# ---------------------------------------------------------------- gemini live

from Orchestrator.models import GeminiLiveSession
from Orchestrator.routes import gemini_live_routes as gm


@pytest.fixture
def quiet_gemini_context(monkeypatch):
    monkeypatch.setattr(gm, "build_context_for_operator",
                        lambda operator, user_text="": ("", {}))


def test_gemini_custom_role_and_tool_group_override(quiet_gemini_context):
    session = GeminiLiveSession(session_id="t-p4-gm")
    session.gemini_ws = FakeWS()
    asyncio.run(gm.configure_gemini_session(
        session, "system", "Orus",
        custom_role="You are Pepper the pizza-order bot.",
        tool_group_override="realtime"))
    cfg = session.gemini_ws.sent[0]
    assert "setup" in cfg
    assert "You are Pepper the pizza-order bot." in json.dumps(cfg)
    assert cfg["setup"]["tools"] == gm.get_gemini_live_tools("realtime")
