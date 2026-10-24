"""A provider's refusal is a notice for the user, not an answer for a pipeline.

Rank 58 made non-stream call_anthropic return a parenthesized decline notice on
stop_reason "refusal" (it used to return an empty reply, or the declined
partial). The notice is right for the chat worker, which shows it to the user,
but the Flight Recorder's provider chain (oversight._synthesize) moves on only
when a reply is empty: the notice was minted as the flight report and no other
provider was tried (rank 58 review). call_gemini's blocked-prompt notices had
the same shape and the same effect.

The notices are now DeclinedReply: still the same str for every caller that
shows it, and a type a caller that needs an answer can tell apart.

Hermetic: requests.post is a fake, and the provider billing state is a tmp path.
"""
import pytest

from Orchestrator import oversight
from Orchestrator import provider_billing as pb

MSGS = [{"role": "user", "content": "Summarize the record."}]


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = "fake"

    def json(self):
        return self._payload


@pytest.fixture
def cr(tmp_path, monkeypatch):
    import Orchestrator.routes.chat_routes as cr

    monkeypatch.setattr(cr, "ANTHROPIC_API_KEY", "test-anthropic", raising=False)
    monkeypatch.setattr(cr, "GOOGLE_API_KEY", "test-google", raising=False)
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr, "read_text_safe", lambda *a, **k: "")
    monkeypatch.setattr(pb, "STATE_PATH", tmp_path / "provider_billing_state.json")
    pb._reset_cache()
    yield cr
    pb._reset_cache()


def _post(cr, monkeypatch, payload):
    monkeypatch.setattr(cr.requests, "post", lambda *a, **k: _FakeResp(payload))


ANTHROPIC_REFUSAL = {"content": [], "stop_reason": "refusal",
                     "stop_details": {"type": "refusal", "category": "cyber"},
                     "usage": {"input_tokens": 3, "output_tokens": 0}}


def test_call_anthropic_refusal_is_a_declined_reply(cr, monkeypatch):
    _post(cr, monkeypatch, ANTHROPIC_REFUSAL)
    text, *_ = cr.call_anthropic(MSGS, "claude-opus-5-5", operator="test-op")
    assert isinstance(text, cr.DeclinedReply)
    # Callers that show it see the same notice as before.
    assert text == "(Claude declined to answer this request (category: cyber).)"


def test_call_anthropic_answer_is_a_plain_reply(cr, monkeypatch):
    _post(cr, monkeypatch, {"content": [{"type": "text", "text": "All quiet."}],
                            "stop_reason": "end_turn", "usage": {}})
    text, *_ = cr.call_anthropic(MSGS, "claude-opus-5-5", operator="test-op")
    assert text == "All quiet."
    assert not isinstance(text, cr.DeclinedReply)


@pytest.mark.parametrize("payload", [
    {"promptFeedback": {"blockReason": "SAFETY"}},
    {},
])
def test_call_gemini_block_is_a_declined_reply(cr, monkeypatch, payload):
    _post(cr, monkeypatch, payload)
    text, *_ = cr.call_gemini(MSGS, "gemini-3.1-pro-preview", operator="test-op")
    assert isinstance(text, cr.DeclinedReply)
    assert text.startswith("(The model")


def test_flight_recorder_tries_the_next_provider_after_a_refusal(cr, monkeypatch):
    """[flight_recorder] provider=anthropic declines; the next keyed provider
    writes the report, and the decline is recorded as the synthesis error."""
    import Orchestrator.config as cfg

    _post(cr, monkeypatch, ANTHROPIC_REFUSAL)
    monkeypatch.setattr(cfg, "GOOGLE_API_KEY", "", raising=False)
    monkeypatch.setattr(cfg, "ANTHROPIC_API_KEY", "", raising=False)
    monkeypatch.setattr(cfg, "OPENAI_API_KEY", "test-openai", raising=False)
    monkeypatch.setattr(cfg, "resolve_default_model", lambda provider, model="": model or f"{provider}-default")
    monkeypatch.setattr(oversight.CFG, "get",
                        lambda section, key, fallback="": {"provider": "anthropic"}.get(key, fallback)
                        if section == "flight_recorder" else fallback)
    tried = []

    def fake_openai(messages, model, operator):
        tried.append(model)
        return "REPORT from the next provider", {}, ""

    monkeypatch.setattr(cr, "call_openai", fake_openai)
    monkeypatch.setattr(cr, "call_custom", lambda *a, **k: ("", {}, ""))
    monkeypatch.setitem(oversight._state, "last_synthesis_error", None)

    assert oversight._synthesize("prompt") == "REPORT from the next provider"
    assert tried == ["openai-default"]


def test_flight_recorder_records_a_refusal_when_no_provider_answers(cr, monkeypatch):
    import Orchestrator.config as cfg

    _post(cr, monkeypatch, ANTHROPIC_REFUSAL)
    for key in ("GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setattr(cfg, key, "", raising=False)
    monkeypatch.setattr(cfg, "resolve_default_model", lambda provider, model="": model or f"{provider}-default")
    monkeypatch.setattr(oversight.CFG, "get",
                        lambda section, key, fallback="": {"provider": "anthropic"}.get(key, fallback)
                        if section == "flight_recorder" else fallback)
    monkeypatch.setattr(cr, "call_custom", lambda *a, **k: ("", {}, ""))
    monkeypatch.setitem(oversight._state, "last_synthesis_error", None)

    assert oversight._synthesize("prompt") is None
    assert "declined" in oversight._state["last_synthesis_error"]
    assert oversight._state["last_synthesis_error"].startswith("anthropic:")
