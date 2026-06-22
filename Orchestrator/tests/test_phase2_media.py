"""Phase 2 (2-media) tests: non-stream native media renders/polls + ledger hygiene.

Gates the two-part 2-media cutover (builds on e3f9eed):

  PART 1 - ledger hygiene: the non-stream worker computes the snapshot body +
    keyword digest from a TAG-STRIPPED copy of the reply. Media loading-placeholder
    <div>s (regex path + native surfacing) stay in the live ui_reply so the Portal
    pollers fire, but NEVER pollute the immutable snapshot body or its keywords.

  PART 2 - native media convergence: when a model natively calls
    generate_image/video/lyria_music inside a call_* tool loop, the loop now
    surfaces the created (task_id,type,prompt); the worker appends the EXACT
    existing loading-placeholder div (so Portal's existing pollImageTasks /
    pollVideoTasks / pollMusicTasks fire) AND a result_data['media_tasks'] array.

Also covers _unpack_call arity normalization (new + legacy shapes) and an
import-smoke that every call_* caller module still loads.

Plan: docs/plans/2026-06-22-pure-production-reply-snapshot-parsing.md (Phase 2,
section 3.2; adversarial option C). Builds on e3f9eed.
"""

import pytest


PROSE = (
    "Here is the image you asked for. The Orchestrator runs FastAPI on port 9091 "
    "and proxies the Portal; snapshots are immutable and embedded for search."
)

# A realistic media loading-placeholder div (image), as injected into ui_reply.
IMG_DIV = (
    '<div class="image-loading-placeholder" data-task-id="img-task-123" '
    'style="padding: 12px; background: #000000; border-radius: 8px; margin: 8px 0;">'
    '\U0001f5bc️ Image generation in progress<span class="thinking-dots" '
    'style="display:inline-flex; gap:4px; margin-left:8px;">'
    '<span></span><span></span><span></span></span></div>'
)

HTML_TOKENS = ("<div", "data-task-id", "class=", "thinking-dots",
              "image-loading-placeholder", "</div>", "<span")


# --------------------------------------------------------------------------- #
# Shared worker harness (mirrors test_phase2_reasoning._run_worker)            #
# --------------------------------------------------------------------------- #

def _run_worker(monkeypatch, provider, fake_call, *, task_id, user="make an image"):
    import Orchestrator.tasks as tasks
    import Orchestrator.routes.chat_routes as cr
    from Orchestrator.models import Task, TaskStatus, TaskType, task_db
    from Orchestrator.volume import now_utc_iso

    captured = {"turns": []}

    suffix = {"anthropic": "anthropic", "google": "gemini", "xai": "xai"}[provider]
    monkeypatch.setattr(cr, "call_" + suffix, fake_call)

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
        operator="MediaTester",
        result_data={
            "messages": [{"role": "user", "content": user}],
            "operator": "MediaTester",
            "provider": provider,
            "model": "model-test",
        },
    )
    task_db.save_task(task)
    tasks.process_chat_task(task)

    final = task_db.get_task(task_id)
    assistant = next((t for t in captured["turns"] if t.get("role") == "assistant"), None)
    return final, assistant


# --------------------------------------------------------------------------- #
# PART 1 - ledger hygiene: a placeholder div in the reply must be stripped     #
#          from the snapshot body + keywords, but kept in ui_reply.            #
# --------------------------------------------------------------------------- #

def _fake_anthropic_with_div(messages, model, operator="Brandon"):
    # The model's prose plus an already-present loading-placeholder div (e.g. the
    # legacy regex path injected one). 2-media arity: (text, usage, reasoning,
    # media_tasks).
    return (PROSE + IMG_DIV,
            {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            "", [])


def test_part1_snap_text_has_no_html_tokens(monkeypatch):
    _, assistant = _run_worker(
        monkeypatch, "anthropic", _fake_anthropic_with_div, task_id="media-part1-snap"
    )
    assert assistant is not None
    snap_text = assistant["snap_text"]
    for tok in HTML_TOKENS:
        assert tok not in snap_text, f"snapshot body leaked HTML token {tok!r}"
    # The clean prose still leads the snapshot body.
    assert snap_text.startswith(PROSE.split(".")[0])


def test_part1_keywords_have_no_html_noise(monkeypatch):
    _, assistant = _run_worker(
        monkeypatch, "anthropic", _fake_anthropic_with_div, task_id="media-part1-kw"
    )
    snap_text = assistant["snap_text"]
    assert "\n\nKeywords: " in snap_text
    kw_line = snap_text.split("\n\nKeywords: ", 1)[1].split("\n", 1)[0]
    for noise in ("div", "class", "data-task-id", "image-loading-placeholder",
                 "thinking-dots", "span", "task-id", "loading"):
        assert noise not in kw_line.split(", "), f"keyword noise: {noise!r} in {kw_line!r}"


def test_part1_ui_reply_keeps_the_div(monkeypatch):
    final, _ = _run_worker(
        monkeypatch, "anthropic", _fake_anthropic_with_div, task_id="media-part1-ui"
    )
    rd = final.result_data
    # The live UI still gets the placeholder so it can render + poll.
    assert 'image-loading-placeholder' in rd["ui_reply"]
    assert 'data-task-id="img-task-123"' in rd["ui_reply"]
    assert rd["reply"] == rd["ui_reply"]
    assert rd["text"] == rd["ui_reply"]


# --------------------------------------------------------------------------- #
# PART 2 - native non-stream media: a surfaced created task becomes a          #
#          placeholder div in ui_reply + a media_tasks entry + clean snapshot. #
# --------------------------------------------------------------------------- #

def _fake_anthropic_native_image(messages, model, operator="Brandon"):
    # The model natively called generate_image; the loop created a task and
    # surfaced it. The PROSE carries NO div (the worker injects it from
    # media_tasks). 2-media arity: (text, usage, reasoning, media_tasks).
    return (PROSE,
            {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            "",
            [{"task_id": "img-999", "type": "image", "prompt": "a red fox"}])


def _fake_gemini_native_image(messages, model, operator="Brandon"):
    # gemini arity: (text, usage, media_parts, reasoning, media_tasks).
    return (PROSE,
            {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            [],
            "",
            [{"task_id": "img-999", "type": "image", "prompt": "a red fox"}])


def _fake_xai_native_image(messages, model, operator="Brandon"):
    return (PROSE,
            {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            "",
            [{"task_id": "img-999", "type": "image", "prompt": "a red fox"}])


@pytest.mark.parametrize(
    "provider,fake",
    [("anthropic", _fake_anthropic_native_image),
     ("google", _fake_gemini_native_image),
     ("xai", _fake_xai_native_image)],
)
def test_part2_native_image_injects_placeholder(monkeypatch, provider, fake):
    final, _ = _run_worker(
        monkeypatch, provider, fake, task_id="media-part2-ui-" + provider
    )
    rd = final.result_data
    # Placeholder div with the REAL task_id is in ui_reply so Portal polls it.
    assert 'class="image-loading-placeholder"' in rd["ui_reply"]
    assert 'data-task-id="img-999"' in rd["ui_reply"]


@pytest.mark.parametrize(
    "provider,fake",
    [("anthropic", _fake_anthropic_native_image),
     ("google", _fake_gemini_native_image),
     ("xai", _fake_xai_native_image)],
)
def test_part2_media_tasks_in_result_data(monkeypatch, provider, fake):
    final, _ = _run_worker(
        monkeypatch, provider, fake, task_id="media-part2-rd-" + provider
    )
    rd = final.result_data
    assert "media_tasks" in rd
    assert rd["media_tasks"] == [
        {"task_id": "img-999", "type": "image", "prompt": "a red fox"}
    ]


@pytest.mark.parametrize(
    "provider,fake",
    [("anthropic", _fake_anthropic_native_image),
     ("google", _fake_gemini_native_image),
     ("xai", _fake_xai_native_image)],
)
def test_part2_snapshot_is_div_free(monkeypatch, provider, fake):
    _, assistant = _run_worker(
        monkeypatch, provider, fake, task_id="media-part2-snap-" + provider
    )
    snap_text = assistant["snap_text"]
    for tok in HTML_TOKENS:
        assert tok not in snap_text, f"native-injected div leaked into ledger: {tok!r}"
    assert snap_text.startswith(PROSE.split(".")[0])


def test_part2_video_and_music_placeholders(monkeypatch):
    def fake(messages, model, operator="Brandon"):
        return (PROSE,
                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "",
                [{"task_id": "vid-1", "type": "video", "prompt": "a sunset"},
                 {"task_id": "mus-1", "type": "music", "prompt": "lofi beats"}])

    final, assistant = _run_worker(
        monkeypatch, "anthropic", fake, task_id="media-part2-vm"
    )
    rd = final.result_data
    assert 'class="video-loading-placeholder"' in rd["ui_reply"]
    assert 'data-task-id="vid-1"' in rd["ui_reply"]
    assert 'class="music-loading-placeholder"' in rd["ui_reply"]
    assert 'data-task-id="mus-1"' in rd["ui_reply"]
    assert rd["media_tasks"] == [
        {"task_id": "vid-1", "type": "video", "prompt": "a sunset"},
        {"task_id": "mus-1", "type": "music", "prompt": "lofi beats"},
    ]
    # Ledger stays clean.
    for tok in HTML_TOKENS:
        assert tok not in assistant["snap_text"]


def test_part2_no_media_means_empty_array_and_no_div(monkeypatch):
    def fake(messages, model, operator="Brandon"):
        return (PROSE, {"total_tokens": 1}, "", [])

    final, _ = _run_worker(monkeypatch, "anthropic", fake, task_id="media-part2-none")
    rd = final.result_data
    assert rd["media_tasks"] == []
    assert "loading-placeholder" not in rd["ui_reply"]


# --------------------------------------------------------------------------- #
# _unpack_call arity normalization (new 2-media + legacy shapes)               #
# --------------------------------------------------------------------------- #

def test_unpack_call_nongemini_new_arity():
    from Orchestrator.tasks import _unpack_call

    text, usage, reasoning, media_tasks = _unpack_call(
        ("ans", {"total_tokens": 3}, "why", [{"task_id": "t1", "type": "image"}])
    )
    assert text == "ans"
    assert usage == {"total_tokens": 3}
    assert reasoning == "why"
    assert media_tasks == [{"task_id": "t1", "type": "image"}]


def test_unpack_call_gemini_new_arity():
    from Orchestrator.tasks import _unpack_call

    text, usage, media_parts, reasoning, media_tasks = _unpack_call(
        ("ans", {"total_tokens": 3}, [{"mime_type": "image/png"}], "why",
         [{"task_id": "t2", "type": "video"}]),
        media=True,
    )
    assert text == "ans"
    assert media_parts == [{"mime_type": "image/png"}]
    assert reasoning == "why"
    assert media_tasks == [{"task_id": "t2", "type": "video"}]


def test_unpack_call_legacy_two_tuple():
    from Orchestrator.tasks import _unpack_call

    # openai-shaped 2-tuple -> reasoning "" + media_tasks [].
    t, u, r, mt = _unpack_call(("ans", {"total_tokens": 1}))
    assert (t, u, r, mt) == ("ans", {"total_tokens": 1}, "", [])

    # gemini legacy 2-tuple -> media_parts [] + reasoning "" + media_tasks [].
    t, u, mp, r, mt = _unpack_call(("ans", {"total_tokens": 1}), media=True)
    assert (t, u, mp, r, mt) == ("ans", {"total_tokens": 1}, [], "", [])


def test_unpack_call_legacy_three_tuple_reasoning_only():
    from Orchestrator.tasks import _unpack_call

    # pre-2-media non-gemini stub: (text, usage, reasoning) -> media_tasks [].
    t, u, r, mt = _unpack_call(("ans", {}, "why"))
    assert (t, u, r, mt) == ("ans", {}, "why", [])


def test_unpack_call_legacy_gemini_four_tuple():
    from Orchestrator.tasks import _unpack_call

    # pre-2-media gemini stub: (text, usage, media_parts, reasoning) -> mt [].
    t, u, mp, r, mt = _unpack_call(("ans", {}, [{"x": 1}], "why"), media=True)
    assert (t, u, mp, r, mt) == ("ans", {}, [{"x": 1}], "why", [])


def test_unpack_call_none_defaults_collapse():
    from Orchestrator.tasks import _unpack_call

    # Explicit None reasoning / media_tasks collapse to "" / [].
    t, u, r, mt = _unpack_call(("ans", {}, None, None))
    assert r == "" and mt == []
    t, u, mp, r, mt = _unpack_call(("ans", {}, None, None, None), media=True)
    assert mp == [] and r == "" and mt == []


# --------------------------------------------------------------------------- #
# Caller import-smoke: every module that unpacks a call_* still imports.       #
# --------------------------------------------------------------------------- #

def test_all_callers_import_clean():
    import importlib

    for mod in ("Orchestrator.routes.chat_routes",
               "Orchestrator.tasks",
               "Orchestrator.checkpoint"):
        importlib.import_module(mod)  # must not raise


def test_checkpoint_unpacks_five_tuple():
    # checkpoint.py must strict-unpack the 5-element gemini return.
    import inspect
    import Orchestrator.checkpoint as ckpt

    src = inspect.getsource(ckpt)
    assert "_, _, _ = call_gemini(" in src, "checkpoint must unpack 5-tuple gemini return"


def test_call_openai_stays_two_tuple():
    # call_openai is Phase-3 deferred: no reasoning, no media_tasks element.
    import inspect
    import Orchestrator.routes.chat_routes as cr

    src = inspect.getsource(cr.call_openai)
    assert "return text, total_usage\n" in src
    assert "media_tasks" not in src
