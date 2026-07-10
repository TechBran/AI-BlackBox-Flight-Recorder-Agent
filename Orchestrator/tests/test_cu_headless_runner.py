"""Unit tests for browser/headless.run_cu_task — the event-queue drain that
folds driver events into the task-result contract dict.

The Anthropic driver is replaced with a scripted fake that pushes events to
session.event_queue (exactly the seam the real driver uses); everything
between the queue and the returned dict is REAL runner code.
"""
import asyncio

import pytest

from Orchestrator.browser import headless
from Orchestrator.browser.session_manager import ComputerUseSession
from Orchestrator.routes import chat_routes


@pytest.fixture
def runner_env(monkeypatch):
    """Stub the runner's external seams; leave the drain logic real."""
    session_holder = {}

    def fake_get_or_create(operator, session_id=None, device_id="blackbox"):
        s = ComputerUseSession(operator, device_id=device_id)
        session_holder["session"] = s
        return s

    monkeypatch.setattr(headless, "get_or_create_session", fake_get_or_create)
    # Working-box default: an Anthropic key is present. This is deliberately NOT
    # narrowed per-test — but it is exactly why the fresh-box defect was
    # invisible (every gemini/openai test inherits an Anthropic key). Fresh-box /
    # wrong-vendor scenarios MUST override this to "" to exercise the
    # no-Anthropic-key path (see test_google_only_box_..._without_anthropic_key).
    monkeypatch.setattr(headless, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(headless, "NATIVE_MODE", True)

    async def _ensure_browser(self, url="about:blank"):
        return True

    monkeypatch.setattr(ComputerUseSession, "ensure_browser", _ensure_browser)
    monkeypatch.setattr(ComputerUseSession, "destroy", lambda self: None)

    monkeypatch.setattr(headless, "capture_screenshot", lambda *a, **k: b"\x89PNG-fake")
    monkeypatch.setattr(
        headless, "save_screenshot_to_uploads",
        lambda png, ident, step: f"/ui/uploads/{ident}_step{step:03d}.png")
    monkeypatch.setattr(
        headless, "screenshot_to_base64", lambda png: "ZmFrZQ==")

    # chat_routes helpers are imported lazily by the runner at call time
    monkeypatch.setattr(chat_routes, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(chat_routes, "build_cu_context", lambda *a, **k: ("", {}))

    async def _instant(_secs):
        return None

    monkeypatch.setattr(headless.asyncio, "sleep", _instant)
    return session_holder


def _scripted_driver(events, final_step):
    """Build a fake run_anthropic_cu_loop that replays scripted events."""
    async def fake_driver(session, history, system_prompt, tools, headers,
                          model, operator, user_text):
        session.current_step = final_step
        for evt in events:
            await session.event_queue.put(evt)
        await session.event_queue.put(None)  # sentinel
    return fake_driver


def _scripted_agen(events, final_step):
    """Build a fake run_gemini_cu_loop / run_openai_cu_loop: an async GENERATOR
    that yields scripted events (the real Gemini/OpenAI loops yield; the runner
    bridges them onto the queue via _pump_generator). No sentinel — the bridge
    appends it."""
    async def fake_loop(session, *args, **kwargs):
        session.current_step = final_step
        for evt in events:
            yield evt
    return fake_loop


def _fresh_gemini_session(operator="system", device_id="blackbox", environment="desktop"):
    from Orchestrator.gemini_cu.session_manager import GeminiCUSession
    return GeminiCUSession(operator, device_id, environment)


@pytest.mark.asyncio
async def test_drain_maps_events_to_contract(runner_env, monkeypatch):
    events = [
        {"type": "cu_step", "data": {"step": 1, "total": 40}},
        {"type": "content", "data": "ignored streaming delta"},
        {"type": "cu_screenshot", "data": {"url": "/ui/uploads/s1.png", "step": 1}},
        {"type": "usage", "data": {"prompt_tokens": 10, "completion_tokens": 4}},
        {"type": "cu_screenshot", "data": {"url": "/ui/uploads/s2.png", "step": 2}},
        {"type": "usage", "data": {"prompt_tokens": 7, "completion_tokens": 2}},
        {"type": "done", "data": {"thinking": "", "content": "All done"}},
    ]
    monkeypatch.setattr(headless, "run_anthropic_cu_loop", _scripted_driver(events, 3))

    result = await headless.run_cu_task("t1", "system", "do the thing")

    assert result["success"] is True
    assert result["result_text"] == "All done"
    # initial screenshot (saved by the runner) + the driver's two
    assert result["screenshots"] == [
        "/ui/uploads/cu_system_step001.png", "/ui/uploads/s1.png", "/ui/uploads/s2.png"]
    assert result["final_screenshot"] == "/ui/uploads/s2.png"
    assert result["steps"] == 3
    assert result["tokens"] == {"input": 17, "output": 6}


@pytest.mark.asyncio
async def test_drain_error_event_fails_task(runner_env, monkeypatch):
    events = [
        {"type": "cu_screenshot", "data": {"url": "/ui/uploads/s1.png", "step": 1}},
        {"type": "error", "data": "API error 400: bad model"},
    ]
    monkeypatch.setattr(headless, "run_anthropic_cu_loop", _scripted_driver(events, 1))

    result = await headless.run_cu_task("t2", "system", "explode")

    assert result["success"] is False
    assert result["result_text"] == "API error 400: bad model"
    assert result["final_screenshot"] == "/ui/uploads/s1.png"
    assert result["tokens"] == {"input": 0, "output": 0}


@pytest.mark.asyncio
async def test_drain_stop_event_fails_task(runner_env, monkeypatch):
    events = [
        {"type": "cu_stopped", "data": {"step": 2, "reason": "User requested stop"}},
        {"type": "done", "data": {"thinking": "", "content": "[Task stopped by user at step 2]"}},
    ]
    monkeypatch.setattr(headless, "run_anthropic_cu_loop", _scripted_driver(events, 2))

    result = await headless.run_cu_task("t3", "system", "stop me")

    assert result["success"] is False
    assert "stopped" in result["result_text"].lower()


@pytest.mark.asyncio
async def test_single_display_conflict_returns_failed_dict(runner_env, monkeypatch):
    """get_or_create_session raises RuntimeError when the display arbiter denies
    the claim — the runner must return a clean FAILED contract dict, not crash the
    worker. The stubbed RuntimeError carries the REAL production message (built
    from the arbiter's DisplayOwner.describe()), so the assertion tracks the
    shipping string, not a hand-written fixture substring (M1-T6 review Issue 4)."""
    from Orchestrator.browser.display_arbiter import DisplayOwner
    real_message = f"Cannot start session: {DisplayOwner('browser', 'alice', 'abcd1234ef').describe()}"

    def boom(operator, session_id=None, device_id="blackbox"):
        raise RuntimeError(real_message)

    monkeypatch.setattr(headless, "get_or_create_session", boom)

    result = await headless.run_cu_task("t4", "system", "conflict")

    assert result["success"] is False
    assert result["result_text"] == real_message                 # exact production string
    assert "Computer Use" in result["result_text"] and "running" in result["result_text"].lower()
    assert result["screenshots"] == [] and result["final_screenshot"] is None


@pytest.mark.asyncio
async def test_gemini_backend_dispatches_and_succeeds(runner_env, monkeypatch):
    """T5 (was test_non_anthropic_backend_rejected, which asserted the now-DELETED
    Anthropic-only guard). Intent preserved and inverted per the task: a Gemini
    model + Google key is no longer REJECTED — it DISPATCHES to the Gemini CU
    driver over its OWN GeminiCUSession (never the browser session, never the
    Anthropic driver) and returns that driver's result."""
    holder = {}

    def fake_gem(operator, device_id="blackbox", environment="desktop", session_id=None):
        s = _fresh_gemini_session(operator, device_id, environment)
        holder["gemini_session"] = s
        return s

    monkeypatch.setattr(headless, "gemini_create_task_session", fake_gem)
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake-google-key")

    def _boom(*a, **k):
        raise AssertionError("Anthropic driver invoked for a Gemini task")

    monkeypatch.setattr(headless, "run_anthropic_cu_loop", _boom)

    events = [
        {"type": "cu_screenshot", "data": {"url": "/ui/uploads/g1.png", "step": 1}},
        {"type": "content", "data": {"text": "looking", "step": 1}},
        {"type": "done", "data": {"content": "Gemini finished the task"}},
        {"type": "usage", "data": {"input": 30, "output": 12}},
    ]
    monkeypatch.setattr(headless, "run_gemini_cu_loop", _scripted_agen(events, 2))

    result = await headless.run_cu_task(
        "t5", "system", "hi", model="gemini-2.5-computer-use-preview-10-2025")

    assert result["success"] is True
    assert result["result_text"] == "Gemini finished the task"
    assert result["screenshots"] == ["/ui/uploads/g1.png"]
    assert result["final_screenshot"] == "/ui/uploads/g1.png"
    assert result["steps"] == 2
    assert result["tokens"] == {"input": 30, "output": 12}
    # It really used a GeminiCUSession, not the browser ComputerUseSession.
    from Orchestrator.gemini_cu.session_manager import GeminiCUSession
    assert isinstance(holder["gemini_session"], GeminiCUSession)
    assert "session" not in runner_env  # browser get_or_create was never called


@pytest.mark.asyncio
async def test_anthropic_missing_key_fails(runner_env, monkeypatch):
    """Anthropic model + no ANTHROPIC_API_KEY -> failure naming the Anthropic key."""
    monkeypatch.setattr(headless, "ANTHROPIC_API_KEY", "")
    result = await headless.run_cu_task(
        "t-anth", "system", "hi", model="claude-opus-4-6")

    assert result["success"] is False
    assert "ANTHROPIC_API_KEY" in result["result_text"]


@pytest.mark.asyncio
async def test_gemini_missing_google_key_names_google_not_anthropic(runner_env, monkeypatch):
    """Gemini model + no GOOGLE_API_KEY must fail naming the GOOGLE key — NOT the
    Anthropic one. Before the gate reorder this failed with the Anthropic-only
    backend message (which names the WRONG vendor), so this test went red first."""
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "")
    result = await headless.run_cu_task(
        "t-gem", "system", "hi", model="gemini-2.5-computer-use-preview-10-2025")

    assert result["success"] is False
    assert "GOOGLE_API_KEY" in result["result_text"]
    # The failure must not name the wrong vendor's key.
    assert "anthropic" not in result["result_text"].lower()


@pytest.mark.asyncio
async def test_openai_missing_key_fails(runner_env, monkeypatch):
    """OpenAI model + no OPENAI_API_KEY -> failure naming the OPENAI key."""
    monkeypatch.setattr(headless, "OPENAI_API_KEY", "")
    result = await headless.run_cu_task(
        "t-oai", "system", "hi", model="gpt-5.5")

    assert result["success"] is False
    assert "OPENAI_API_KEY" in result["result_text"]
    assert "anthropic" not in result["result_text"].lower()


@pytest.mark.asyncio
async def test_gemini_token_fold_reads_input_output_keys(runner_env, monkeypatch):
    """FOLD 2a (was test_gemini_with_google_key_reaches_backend_guard). The Gemini
    loop emits usage as session.total_tokens {input, output}
    (gemini_cu/agent_loop.py:641), NOT the Anthropic driver's {prompt_tokens,
    completion_tokens}. The fold must read the {input, output} keys or every
    Gemini CU task silently records tokens {0, 0} while still passing the
    Anthropic-only golden test."""
    monkeypatch.setattr(headless, "gemini_create_task_session",
                        lambda *a, **k: _fresh_gemini_session())
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake-google-key")
    events = [
        {"type": "usage", "data": {"input": 21, "output": 9}},
        {"type": "done", "data": {"content": "done"}},
    ]
    monkeypatch.setattr(headless, "run_gemini_cu_loop", _scripted_agen(events, 1))

    result = await headless.run_cu_task(
        "t-gtok", "system", "hi", model="gemini-2.5-computer-use-preview-10-2025")

    assert result["success"] is True
    assert result["tokens"] == {"input": 21, "output": 9}


@pytest.mark.asyncio
async def test_anthropic_key_present_proceeds_past_both_gates(runner_env, monkeypatch):
    """Anthropic model + key present -> proceeds past BOTH gates and reaches the
    driver (scripted here so no real browser work happens)."""
    events = [{"type": "done", "data": {"thinking": "", "content": "reached the driver"}}]
    monkeypatch.setattr(headless, "run_anthropic_cu_loop", _scripted_driver(events, 1))

    result = await headless.run_cu_task(
        "t-anth2", "system", "hi", model="claude-opus-4-6")

    assert result["success"] is True
    assert result["result_text"] == "reached the driver"


@pytest.mark.asyncio
async def test_google_only_box_dispatches_without_anthropic_key(runner_env, monkeypatch):
    """Pins the fresh-box PORTABILITY invariant — the whole point of M1-T4/T5
    (was test_google_only_box_reaches_backend_guard_without_anthropic_key, which
    asserted the deleted Anthropic-only guard).

    A customer box provisioned with ONLY a Google key (NO Anthropic credential)
    must RUN a Gemini CU task to completion — not die naming the wrong vendor's
    key, and (post-T5) not hit any Anthropic-only guard. Moving the
    ANTHROPIC_API_KEY check back above resolve_backend() makes this fail (the
    task would die on 'ANTHROPIC_API_KEY not set')."""
    monkeypatch.setattr(headless, "gemini_create_task_session",
                        lambda *a, **k: _fresh_gemini_session())
    monkeypatch.setattr(headless, "ANTHROPIC_API_KEY", "")   # fresh box: no Anthropic credential
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake")  # only Google is provisioned
    events = [{"type": "done", "data": {"content": "ran on a google-only box"}}]
    monkeypatch.setattr(headless, "run_gemini_cu_loop", _scripted_agen(events, 1))

    result = await headless.run_cu_task(
        "t-google-only", "system", "hi", model="gemini-2.5-computer-use-preview-10-2025")

    assert result["success"] is True
    assert result["result_text"] == "ran on a google-only box"
    assert "ANTHROPIC_API_KEY not set" not in result["result_text"]   # did NOT die on the wrong vendor's key


@pytest.mark.asyncio
async def test_openai_only_box_dispatches_without_anthropic_key(runner_env, monkeypatch):
    """Same fresh-box portability invariant on the OpenAI backend: an OpenAI-only
    box (no Anthropic credential) must RUN an OpenAI CU task, not die on the
    missing Anthropic key."""
    monkeypatch.setattr(headless, "ANTHROPIC_API_KEY", "")   # fresh box: no Anthropic credential
    monkeypatch.setattr(headless, "OPENAI_API_KEY", "fake")  # only OpenAI is provisioned
    events = [{"type": "done", "data": {"content": "ran on an openai-only box"}}]
    monkeypatch.setattr(headless, "run_openai_cu_loop", _scripted_agen(events, 1))

    result = await headless.run_cu_task(
        "t-openai-only", "system", "hi", model="gpt-5.5")

    assert result["success"] is True
    assert result["result_text"] == "ran on an openai-only box"
    assert "ANTHROPIC_API_KEY not set" not in result["result_text"]   # did NOT die on the wrong vendor's key


@pytest.mark.asyncio
async def test_unmapped_backend_fails_loudly_not_silent(runner_env, monkeypatch):
    """A backend with NO entry in the key map must fail LOUD, never slip through
    with no API-key gate. This is the guard that keeps T5 honest: after T5
    deletes the backend!=anthropic backstop, a 4th CU_MODEL_FILTERS family added
    without extending the map would otherwise run ungated. resolve_backend is
    stubbed to return an unmapped 'xai' (it can't produce one today); the runner
    must reject it by name, not silently continue. Mutation-verified: reverting
    to the old `.get(backend)`/falsy-None short-circuit makes this test fail."""
    monkeypatch.setattr(headless, "resolve_backend", lambda model: "xai")
    result = await headless.run_cu_task("t-xai", "system", "hi", model="grok-cu-1")

    assert result["success"] is False
    assert "No API-key gate configured" in result["result_text"]
    assert "backend 'xai'" in result["result_text"]


@pytest.mark.asyncio
async def test_openai_backend_dispatches_and_token_fold(runner_env, monkeypatch):
    """FOLD 2a for OpenAI + dispatch. The OpenAI loop reuses the browser
    ComputerUseSession and emits usage as session.total_tokens {input, output}
    (openai_cu/agent_loop.py:572). Proves the openai backend dispatches through
    _pump_generator and its {input, output} usage is normalized (not {0, 0})."""
    monkeypatch.setattr(headless, "OPENAI_API_KEY", "fake-openai-key")
    events = [
        {"type": "cu_screenshot", "data": {"url": "/ui/uploads/o1.png", "step": 0}},
        {"type": "content", "data": {"text": "clicking", "step": 1}},
        {"type": "usage", "data": {"input": 33, "output": 14}},
        {"type": "done", "data": {"content": "OpenAI finished"}},
    ]
    monkeypatch.setattr(headless, "run_openai_cu_loop", _scripted_agen(events, 3))

    result = await headless.run_cu_task("t-otok", "system", "hi", model="gpt-5.5")

    assert result["success"] is True
    assert result["result_text"] == "OpenAI finished"
    assert result["tokens"] == {"input": 33, "output": 14}
    assert result["screenshots"] == ["/ui/uploads/o1.png"]
    assert result["steps"] == 3


@pytest.mark.asyncio
async def test_gemini_iteration_capped_synthesizes_result(runner_env, monkeypatch):
    """FOLD 2b for Gemini. On MAX_ITERATIONS exhaustion the Gemini loop yields
    `usage` with NO `done` (gemini_cu/agent_loop.py:641). Without synthesis the
    runner reports FAILED with empty result_text despite real work; the fold must
    synthesize the result from accumulated `content` and report success."""
    monkeypatch.setattr(headless, "gemini_create_task_session",
                        lambda *a, **k: _fresh_gemini_session())
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake-google-key")
    events = [  # note: NO `done` event — the loop exhausted its step budget
        {"type": "content", "data": {"text": "Step one done. ", "step": 1}},
        {"type": "content", "data": {"text": "Step two done.", "step": 2}},
        {"type": "usage", "data": {"input": 50, "output": 20}},
    ]
    monkeypatch.setattr(headless, "run_gemini_cu_loop", _scripted_agen(events, 40))

    result = await headless.run_cu_task(
        "t-gcap", "system", "big task",
        model="gemini-2.5-computer-use-preview-10-2025")

    assert result["success"] is True                       # not an empty failure
    assert result["result_text"] == "Step one done. Step two done."  # synthesized
    assert result["steps"] == 40
    assert result["tokens"] == {"input": 50, "output": 20}


@pytest.mark.asyncio
async def test_openai_iteration_capped_synthesizes_result(runner_env, monkeypatch):
    """FOLD 2b for OpenAI. The for-else exhaustion path
    (openai_cu/agent_loop.py:566-572) yields `usage` with no `done`. Synthesize
    from accumulated `content`."""
    monkeypatch.setattr(headless, "OPENAI_API_KEY", "fake-openai-key")
    events = [  # NO `done`
        {"type": "content", "data": {"text": "partial progress", "step": 1}},
        {"type": "usage", "data": {"input": 12, "output": 5}},
    ]
    monkeypatch.setattr(headless, "run_openai_cu_loop", _scripted_agen(events, 40))

    result = await headless.run_cu_task(
        "t-ocap", "system", "big task", model="gpt-5.5")

    assert result["success"] is True
    assert result["result_text"] == "partial progress"
    assert result["steps"] == 40
    assert result["tokens"] == {"input": 12, "output": 5}


def test_key_map_backends_match_cu_model_filters():
    """Item-4 static coupling. _BACKEND_KEY_NAMES must stay in lockstep with
    CU_MODEL_FILTERS at COMMIT time — a 4th CU family (e.g. 'xai') added to
    CU_MODEL_FILTERS without a key-name entry would otherwise only surface at
    RUNTIME (the 'No API-key gate configured' path in
    test_unmapped_backend_fails_loudly_not_silent). Key NAMES are a static module
    constant; the key VALUES are still read from module globals at call time, so
    this static assertion does not disturb the per-call monkeypatch seam."""
    from Orchestrator.config import CU_MODEL_FILTERS
    assert set(headless._BACKEND_KEY_NAMES) == set(CU_MODEL_FILTERS)


@pytest.mark.asyncio
async def test_headless_gemini_uses_own_session_not_cached_chat(runner_env, monkeypatch):
    """Issue 1 (a) + (c). A headless Gemini task must NOT borrow the operator's
    cached CHAT session. Pre-seed a cached ANDROID chat session with history, then
    run a headless task for device_id='blackbox': the task must run on a DISTINCT
    session with the REQUESTED (blackbox/desktop) device+environment — NOT the
    cached android one — and the operator's chat session must SURVIVE untouched.
    Uses the REAL create_task_session (no mock) so the isolation is genuine.
    Mutation-verified: swapping create_task_session for get_or_create_session
    (borrowing) makes assertion (a) fail — the task runs against the phone."""
    import Orchestrator.gemini_cu.session_manager as gem_sm
    op = "gem-own-session-op"
    gem_sm.destroy_session(op)  # clean slate
    try:
        # Pre-seed a cached chat session: android device, with prior history.
        chat = gem_sm.get_or_create_session(op, "pixel-9", "android")
        chat.conversation_history = ["prior chat turn"]
        chat_id = chat.session_id

        monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake-google-key")
        captured = {}

        async def fake_loop(session, *a, **k):
            captured["session"] = session
            captured["device_id"] = session.device_id
            captured["environment"] = session.environment
            captured["history_len"] = len(session.conversation_history)
            session.current_step = 1
            yield {"type": "done", "data": {"content": "ok"}}

        monkeypatch.setattr(headless, "run_gemini_cu_loop", fake_loop)

        result = await headless.run_cu_task(
            "t-own", op, "hi", device_id="blackbox",
            model="gemini-2.5-computer-use-preview-10-2025")

        assert result["success"] is True
        # (a) ran on the REQUESTED device/environment, not the cached android one
        assert captured["device_id"] == "blackbox"
        assert captured["environment"] == "desktop"
        # ran on a DISTINCT session object, not the cached chat session
        assert captured["session"] is not chat
        assert captured["session"].session_id != chat_id
        # (b) fresh history -> driver's first-turn fossil retrieval fires
        #     (is_first_turn = not conversation_history)
        assert captured["history_len"] == 0
        # (c) the operator's chat session SURVIVES the headless run, intact
        surviving = gem_sm.get_session(op)
        assert surviving is chat
        assert surviving.conversation_history == ["prior chat turn"]
    finally:
        gem_sm.destroy_session(op)  # cleanup


@pytest.mark.asyncio
async def test_headless_gemini_teardown_on_driver_raise(runner_env, monkeypatch):
    """Issue 3 (failure path). When the Gemini loop RAISES, the task's own session
    must still be dropped from _sessions (the teardown lives in a finally) and the
    task reported as a clean failure — no leaked session, no leaked pump."""
    import Orchestrator.gemini_cu.session_manager as gem_sm
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake-google-key")
    captured = {}

    async def boom_loop(session, *a, **k):
        captured["session"] = session
        session.current_step = 1
        raise RuntimeError("driver exploded")
        yield  # unreachable — makes this an async generator

    monkeypatch.setattr(headless, "run_gemini_cu_loop", boom_loop)

    result = await headless.run_cu_task(
        "t-gem-raise", "gem-raise-op", "hi",
        model="gemini-2.5-computer-use-preview-10-2025")

    assert result["success"] is False
    assert "driver exploded" in result["result_text"]
    assert captured["session"].session_id not in gem_sm._sessions  # dropped


@pytest.mark.asyncio
async def test_headless_gemini_teardown_on_cancellation(runner_env, monkeypatch):
    """Issue 3 (cancellation path — the mutation-discriminating one). When the
    outer run is CANCELLED mid-drain (the wait_for-timeout case), the task's own
    session must STILL be dropped. A driver raise returns a failure DICT so the
    teardown runs whether it is in the finally or inline; only cancellation, which
    propagates an exception THROUGH the return, distinguishes them. Mutation-
    verified: moving destroy_task_session out of the finally leaves the session in
    _sessions here."""
    import Orchestrator.gemini_cu.session_manager as gem_sm
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake-google-key")
    captured = {}
    started = asyncio.Event()

    async def hanging_loop(session, *a, **k):
        captured["session"] = session
        session.current_step = 1
        yield {"type": "cu_step", "data": {"step": 1, "total": 40}}
        started.set()
        await asyncio.Event().wait()  # block until cancelled
        yield {"type": "done", "data": {"content": "never reached"}}

    monkeypatch.setattr(headless, "run_gemini_cu_loop", hanging_loop)

    task = asyncio.create_task(headless.run_cu_task(
        "t-gem-cancel", "gem-cancel-op", "hi",
        model="gemini-2.5-computer-use-preview-10-2025"))
    await started.wait()          # loop started + session created
    await asyncio.sleep(0)        # let the drain settle on queue.get()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert captured["session"].session_id not in gem_sm._sessions  # dropped despite cancel


@pytest.mark.asyncio
async def test_openai_headless_injects_fossils(runner_env, monkeypatch):
    """Issue 2. The OpenAI driver does NO internal fossil retrieval (unlike
    Gemini's), so the runner must build the system prompt WITH fossils before
    launching it — exactly as the Anthropic path and interactive OpenAI CU do.
    Otherwise headless OpenAI is the only CU cohort running without memory."""
    monkeypatch.setattr(headless, "OPENAI_API_KEY", "fake-openai-key")
    monkeypatch.setattr(
        chat_routes, "build_cu_context",
        lambda prompt, operator: ("## RELEVANT MEMORY\nremember-this-fossil", {"snap": []}))
    captured = {}

    async def fake_loop(session, prompt, model=None, system_prompt=None, url=None):
        captured["system_prompt"] = system_prompt
        session.current_step = 1
        yield {"type": "done", "data": {"content": "ok"}}

    monkeypatch.setattr(headless, "run_openai_cu_loop", fake_loop)

    result = await headless.run_cu_task("t-ofoss", "system", "do X", model="gpt-5.5")

    assert result["success"] is True
    assert "remember-this-fossil" in (captured["system_prompt"] or "")


@pytest.mark.asyncio
async def test_gemini_desktop_blocked_by_running_browser_cu(runner_env, monkeypatch):
    """Issue 2 / M1-T6: a same-operator Gemini DESKTOP task must not run while that
    operator's Anthropic/OpenAI CU task (both use the browser ComputerUseSession)
    is driving the one physical display. Now routed through the shared display
    arbiter: register a REAL running browser session (not a get_operator_session
    mock) and prove the arbiter refuses the Gemini task before its driver launches."""
    import Orchestrator.browser.session_manager as bsm
    from Orchestrator.browser.session_manager import ComputerUseSession

    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake-google-key")

    # A REAL running browser CU session holds the local display.
    browser_sess = ComputerUseSession("system", device_id="blackbox")
    browser_sess.status = "running"
    bsm._sessions[browser_sess.session_id] = browser_sess
    bsm._operator_sessions["system"] = browser_sess.session_id

    def _no_gemini_session(*a, **k):
        raise AssertionError("Gemini session created despite desktop conflict")

    monkeypatch.setattr(headless, "gemini_create_task_session", _no_gemini_session)

    def _boom(*a, **k):
        raise AssertionError("Gemini driver launched despite desktop conflict")

    monkeypatch.setattr(headless, "run_gemini_cu_loop", _boom)

    try:
        result = await headless.run_cu_task(
            "t-gem-conflict", "system", "hi",
            model="gemini-2.5-computer-use-preview-10-2025")

        assert result["success"] is False
        assert "running" in result["result_text"].lower()
        assert "Computer Use" in result["result_text"]
    finally:
        bsm._sessions.pop(browser_sess.session_id, None)
        bsm._operator_sessions.pop("system", None)


def test_driver_signatures_match_runner_call():
    """Mock-fidelity guard. The Gemini/OpenAI mocks take (session, *args,
    **kwargs), so a driver signature change could sail through green. Binding the
    REAL driver signatures with EXACTLY the args headless.py passes narrows that
    hole — but only partially. What this DOES catch at commit time (without
    running the driver): a REMOVED or REORDERED required parameter, or a required
    parameter added such that the arg count no longer binds. What it does NOT
    catch: a parameter RENAME (positional binding ignores names), nor a new middle
    parameter WITH a default (the 5 positional args still bind to a 6-param
    signature, silently shifting values). Keep these bind calls byte-for-byte in
    sync with the call sites (headless._run_gemini_cu_task / run_cu_task openai
    branch)."""
    import inspect
    from Orchestrator.gemini_cu.agent_loop import run_gemini_cu_loop as real_gem
    from Orchestrator.openai_cu.agent_loop import run_openai_cu_loop as real_oai

    # _run_gemini_cu_task: run_gemini_cu_loop(session, prompt, model, system_prompt, url)
    inspect.signature(real_gem).bind(object(), "prompt", "model", "sys", "url")
    # run_cu_task openai branch: run_openai_cu_loop(session, prompt, model, system_prompt, None)
    inspect.signature(real_oai).bind(object(), "prompt", "model", "sys", None)


@pytest.mark.asyncio
async def test_gemini_dict_error_extracts_message(runner_env, monkeypatch):
    """FOLD gap 4: Gemini/OpenAI wrap errors as {"message": ...} (Anthropic sends
    a bare str). The fold must surface the message, not str(dict)."""
    monkeypatch.setattr(headless, "gemini_create_task_session",
                        lambda *a, **k: _fresh_gemini_session())
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake-google-key")
    events = [{"type": "error", "data": {"message": "Gemini API error: quota exhausted"}}]
    monkeypatch.setattr(headless, "run_gemini_cu_loop", _scripted_agen(events, 1))

    result = await headless.run_cu_task(
        "t-gerr", "system", "hi", model="gemini-2.5-computer-use-preview-10-2025")

    assert result["success"] is False
    assert result["result_text"] == "Gemini API error: quota exhausted"


@pytest.mark.asyncio
async def test_openai_reasonless_cu_stopped_fails(runner_env, monkeypatch):
    """FOLD: Gemini/OpenAI emit cu_stopped as {step} with NO `reason`; Anthropic
    emits {step, reason}. A stopped task is FAILED and the fold defaults the
    reason so the reason-less shape still fails cleanly (not crashes)."""
    monkeypatch.setattr(headless, "OPENAI_API_KEY", "fake-openai-key")
    events = [
        {"type": "cu_screenshot", "data": {"url": "/ui/uploads/o1.png", "step": 1}},
        {"type": "cu_stopped", "data": {"step": 2}},  # no "reason"
        {"type": "usage", "data": {"input": 4, "output": 1}},
    ]
    monkeypatch.setattr(headless, "run_openai_cu_loop", _scripted_agen(events, 2))

    result = await headless.run_cu_task("t-ostop", "system", "hi", model="gpt-5.5")

    assert result["success"] is False
    assert "stopped" in result["result_text"].lower()
    assert result["final_screenshot"] == "/ui/uploads/o1.png"


def test_fresh_event_queue_unbinds_dead_worker_loop():
    """Regression (Task 12 review C1): a session whose event_queue was bound
    inside a worker thread's asyncio.run() must be reusable from a NEW loop
    after fresh_event_queue() — without it, the chat path's next CU turn dies
    with "bound to a different event loop"."""
    import asyncio

    from Orchestrator.browser.session_manager import ComputerUseSession

    session = ComputerUseSession("queue-rebind-op")

    async def _bind_and_drain():
        # Bind the queue to this (soon-to-be-dead) loop the same way the
        # runner does: await get() against it.
        session.event_queue.put_nowait(None)
        await session.event_queue.get()

    asyncio.run(_bind_and_drain())  # worker-thread style run; loop now closed

    async def _chat_turn():
        # What stream_computer_use now does before launching its driver:
        session.fresh_event_queue()
        session.event_queue.put_nowait({"type": "done"})
        return await session.event_queue.get()

    event = asyncio.run(_chat_turn())  # second, distinct loop
    assert event == {"type": "done"}
