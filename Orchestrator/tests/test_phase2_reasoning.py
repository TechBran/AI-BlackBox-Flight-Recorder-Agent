"""Phase 2 reasoning-capture tests (2-reasoning).

Gates that the THREE migrated non-stream sync callers separate the model's native
reasoning from the user-facing answer:

  call_anthropic  -> Anthropic `thinking` / `redacted_thinking` content blocks
  call_gemini     -> Gemini thought-summary parts (`thought: true` + `text`)
  call_xai        -> xAI OpenAI-compatible `reasoning_content`

For each provider we build a FAKE non-stream HTTP response carrying BOTH a
reasoning element AND an answer element, monkeypatch the network call, and assert:

  (1) the returned answer (`raw`) contains ONLY the answer (no reasoning text);
  (2) reasoning is captured in the NEW return element;
  (3) through process_chat_task, result_data["reply"] == answer only, and
      snap_text == answer + Keywords, with `[REASONING]` appearing AFTER the
      answer/keywords (or absent when no reasoning) -- never an empty header.

call_openai is intentionally NOT migrated (Phase 3 / OD-3 deferred).

Plan: docs/plans/2026-06-22-pure-production-reply-snapshot-parsing.md (Phase 2,
section 2.5). Builds on f04b931.
"""

import pytest


ANSWER = (
    "The Orchestrator runs FastAPI on port 9091 and proxies the Portal. "
    "Snapshots are immutable and embedded for semantic search."
)
REASONING = (
    "My Thought Process. Okay, here's the deal: the user wants to know how the "
    "Orchestrator works, so I should explain the FastAPI server and the ledger."
)


# --------------------------------------------------------------------------- #
# Fake provider responses                                                     #
# --------------------------------------------------------------------------- #

class _FakeResp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "fake"

    def json(self):
        return self._payload


def _anthropic_payload():
    # Anthropic non-stream Messages: content is a list of blocks; thinking block +
    # text block, stop_reason "end_turn" (no tool use).
    return {
        "content": [
            {"type": "thinking", "thinking": REASONING},
            {"type": "text", "text": ANSWER},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 11, "output_tokens": 22},
    }


def _gemini_payload():
    # Gemini non-stream: candidates[0].content.parts; a thought-summary part
    # (thought: true) + an answer part. No functionCall -> loop breaks.
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": REASONING, "thought": True},
                        {"text": ANSWER},
                    ]
                }
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 11,
            "candidatesTokenCount": 22,
            "totalTokenCount": 33,
        },
    }


def _xai_payload():
    # xAI OpenAI-compatible: choices[0].message with reasoning_content + content.
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": ANSWER,
                    "reasoning_content": REASONING,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
    }


@pytest.fixture
def _provider_keys(monkeypatch):
    """Ensure the *_API_KEY guards in each call_* don't short-circuit."""
    import Orchestrator.routes.chat_routes as cr

    monkeypatch.setattr(cr, "ANTHROPIC_API_KEY", "test-anthropic", raising=False)
    monkeypatch.setattr(cr, "GOOGLE_API_KEY", "test-google", raising=False)
    monkeypatch.setattr(cr, "XAI_API_KEY", "test-xai", raising=False)
    # _get_tools makes a ToolVault round-trip; stub it so requests is the only dep.
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    return cr


# --------------------------------------------------------------------------- #
# (1)+(2) Direct call_* unit tests                                            #
# --------------------------------------------------------------------------- #

def test_call_anthropic_separates_reasoning(monkeypatch, _provider_keys):
    cr = _provider_keys
    monkeypatch.setattr(cr.requests, "post", lambda *a, **k: _FakeResp(_anthropic_payload()))

    text, usage, reasoning = cr.call_anthropic(
        [{"role": "user", "content": "How does it work?"}], "claude-test"
    )

    assert text == ANSWER
    assert REASONING not in text, "reasoning leaked into the anthropic answer"
    assert reasoning == REASONING
    assert ANSWER not in reasoning


def test_call_gemini_separates_reasoning(monkeypatch, _provider_keys):
    cr = _provider_keys
    monkeypatch.setattr(cr.requests, "post", lambda *a, **k: _FakeResp(_gemini_payload()))

    text, usage, media_parts, reasoning = cr.call_gemini(
        [{"role": "user", "content": "How does it work?"}], "gemini-test"
    )

    assert text == ANSWER
    assert REASONING not in text, "reasoning leaked into the gemini answer"
    assert reasoning == REASONING
    assert media_parts == []


def test_call_xai_separates_reasoning(monkeypatch, _provider_keys):
    cr = _provider_keys
    monkeypatch.setattr(cr.requests, "post", lambda *a, **k: _FakeResp(_xai_payload()))

    text, usage, reasoning = cr.call_xai(
        [{"role": "user", "content": "How does it work?"}], "grok-test"
    )

    assert text == ANSWER
    assert REASONING not in text, "reasoning leaked into the xai answer"
    assert reasoning == REASONING


def test_call_anthropic_no_reasoning_is_empty(monkeypatch, _provider_keys):
    cr = _provider_keys
    payload = {
        "content": [{"type": "text", "text": ANSWER}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 6},
    }
    monkeypatch.setattr(cr.requests, "post", lambda *a, **k: _FakeResp(payload))

    text, usage, reasoning = cr.call_anthropic(
        [{"role": "user", "content": "hi"}], "claude-test"
    )
    assert text == ANSWER
    assert reasoning == "", "reasoning must be empty string when model did not think"


def test_call_gemini_no_reasoning_is_empty(monkeypatch, _provider_keys):
    cr = _provider_keys
    payload = {
        "candidates": [{"content": {"parts": [{"text": ANSWER}]}}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 6, "totalTokenCount": 11},
    }
    monkeypatch.setattr(cr.requests, "post", lambda *a, **k: _FakeResp(payload))

    text, usage, media_parts, reasoning = cr.call_gemini(
        [{"role": "user", "content": "hi"}], "gemini-test"
    )
    assert text == ANSWER
    assert reasoning == ""


# --------------------------------------------------------------------------- #
# (3) End-to-end through process_chat_task                                     #
# --------------------------------------------------------------------------- #

def provider_fn(provider):
    """Map the result_data provider tag to the call_* function suffix."""
    return {"anthropic": "anthropic", "google": "gemini", "xai": "xai"}[provider]


def _run_worker(monkeypatch, provider, fake_call, *, task_id):
    """Drive process_chat_task with a monkeypatched call_* and capture the
    assistant turn + result_data."""
    import Orchestrator.tasks as tasks
    import Orchestrator.routes.chat_routes as cr
    from Orchestrator.models import Task, TaskStatus, TaskType, task_db
    from Orchestrator.volume import now_utc_iso

    captured = {"turns": []}

    monkeypatch.setattr(cr, "call_" + provider_fn(provider), fake_call)

    monkeypatch.setattr(tasks, "read_text_safe", lambda *a, **k: "")
    monkeypatch.setattr(tasks, "get_recent_fossils_for_operator", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "keyword_retrieve_for_operator", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "semantic_retrieve", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "get_recent_checkpoints_for_operator", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "hybrid_retrieve", lambda *a, **k: [])

    monkeypatch.setattr(tasks, "AUTO_ENABLE", False)
    monkeypatch.setattr(tasks, "should_create_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "perform_mint", lambda *a, **k: {"snap_id": "SNAP-TEST"})
    monkeypatch.setattr(tasks, "save_operator_state", lambda *a, **k: None)

    orig_get_state = tasks.get_state

    def spy_get_state(op):
        st = orig_get_state(op)
        real_add = st.add_conversation_turn

        def capturing_add(turn, max_turns=100):
            captured["turns"].append(dict(turn))
            return real_add(turn, max_turns)

        st.add_conversation_turn = capturing_add
        return st

    monkeypatch.setattr(tasks, "get_state", spy_get_state)

    task = Task(
        task_id=task_id,
        task_type=TaskType.CHAT,
        status=TaskStatus.PENDING,
        created_at=now_utc_iso(),
        updated_at=now_utc_iso(),
        operator="ReasonTester",
        result_data={
            "messages": [{"role": "user", "content": "How does the Orchestrator work?"}],
            "operator": "ReasonTester",
            "provider": provider,
            "model": "model-test",
        },
    )
    task_db.save_task(task)
    tasks.process_chat_task(task)

    final = task_db.get_task(task_id)
    assistant = next((t for t in captured["turns"] if t.get("role") == "assistant"), None)
    return final, assistant


def _fake_anthropic(messages, model, operator="Brandon"):
    return ANSWER, {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}, REASONING


def _fake_gemini(messages, model, operator="Brandon"):
    return ANSWER, {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}, [], REASONING


def _fake_xai(messages, model, operator="Brandon"):
    return ANSWER, {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}, REASONING


def _fake_anthropic_no_reasoning(messages, model, operator="Brandon"):
    return ANSWER, {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}, ""


@pytest.mark.parametrize(
    "provider,fake",
    [("anthropic", _fake_anthropic), ("google", _fake_gemini), ("xai", _fake_xai)],
)
def test_worker_reply_is_answer_only(monkeypatch, provider, fake):
    final, _ = _run_worker(monkeypatch, provider, fake, task_id="reason-" + provider)
    rd = final.result_data
    assert rd["reply"] == ANSWER
    assert rd["ui_reply"] == ANSWER
    assert rd["text"] == ANSWER
    assert REASONING not in rd["reply"], "reasoning leaked into the user-facing reply"


@pytest.mark.parametrize(
    "provider,fake",
    [("anthropic", _fake_anthropic), ("google", _fake_gemini), ("xai", _fake_xai)],
)
def test_worker_snap_text_answer_first_reasoning_last(monkeypatch, provider, fake):
    _, assistant = _run_worker(monkeypatch, provider, fake, task_id="reason-snap-" + provider)
    assert assistant is not None, "no assistant turn captured"
    snap_text = assistant["snap_text"]

    # Answer first.
    assert snap_text.startswith(ANSWER)
    # Keywords second.
    assert "\n\nKeywords: " in snap_text
    kw_idx = snap_text.index("\n\nKeywords: ")
    # Reasoning last (AFTER the answer AND the keywords).
    assert "[REASONING]" in snap_text
    reason_idx = snap_text.index("[REASONING]")
    assert reason_idx > kw_idx, "[REASONING] must come after the Keywords line"
    assert snap_text.index(ANSWER) < reason_idx, "[REASONING] must come after the answer"
    assert REASONING in snap_text
    # No empty header form.
    assert "[REASONING]\n\n" not in snap_text


def test_worker_no_reasoning_has_no_header(monkeypatch):
    final, assistant = _run_worker(
        monkeypatch, "anthropic", _fake_anthropic_no_reasoning, task_id="reason-empty"
    )
    snap_text = assistant["snap_text"]
    assert snap_text.startswith(ANSWER)
    assert "\n\nKeywords: " in snap_text
    assert "[REASONING]" not in snap_text, "no [REASONING] header when reasoning is empty"
    assert final.result_data["reply"] == ANSWER


def test_openai_not_migrated_still_two_tuple():
    """call_openai must remain answer-only (Phase 3 deferred): no reasoning element."""
    import inspect
    import Orchestrator.routes.chat_routes as cr

    src = inspect.getsource(cr.call_openai)
    assert "return text, total_usage, reasoning" not in src
    assert "return text, total_usage" in src
