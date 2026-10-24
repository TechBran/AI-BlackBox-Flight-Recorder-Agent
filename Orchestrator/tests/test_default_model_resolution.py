"""'Auto' resolves to the configured default model on every chat path (audit rank 38).

X05-architecture-08 / W53 / O12-12: an empty model (the Portal's "(Auto - Latest)"
option sends "") fell back to hard-coded literals on both /chat/stream routes
(anthropic -> claude-sonnet-4-5, gemini -> a -customtools literal) and in the SMS
router and /sms/preferences (anthropic + claude-sonnet-4-5), while cron and the
non-stream worker used the configured *_MODEL_DEFAULT. One resolver now serves all
of them: config.resolve_default_model(provider, model).

Owner decision 2026-10-17: the built-in Anthropic default is claude-opus-5-5, and
a retired Claude id maps to the newest live model of its family (successor_for).
Android carries the identical map (another unit); the table below is the contract.

Hermetic: chat providers, retrieval and the SMS gateway are faked at module seams.
"""
import json

import pytest

from Orchestrator import config

# GET /v1/models on this account, 2026-10-17 (owner-supplied).
LIVE_IDS = (
    "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1",
    "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-opus-4-8",
    "claude-opus-4-7", "claude-sonnet-4-6", "claude-opus-4-6",
    "claude-opus-4-5-20251101", "claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929",
)
DEFAULT_FAMILY = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1")


# ── 1. built-in default + capability tables ──────────────────────────────────

def test_builtin_anthropic_default_is_opus_5_5(monkeypatch):
    # The env var still overrides; the built-in default (no ANTHROPIC_MODEL) is
    # the owner's choice.
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    assert config._anthropic_model_default() == "claude-opus-5-5"
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    assert config._anthropic_model_default() == "claude-sonnet-5-5"


@pytest.mark.parametrize("model", DEFAULT_FAMILY)
def test_current_family_capabilities(model):
    # Adaptive thinking (disabled thinking and budget_tokens 400 on all four),
    # display="summarized" (default "omitted" would blank the thinking UI), and
    # no sampling params (400 on all four).
    caps = config.anthropic_caps(model)
    assert caps.thinking == {"type": "adaptive", "display": "summarized"}
    assert not caps.sampling
    assert not config.anthropic_accepts_sampling(model)
    assert caps.effort in (None, "low", "medium", "high", "xhigh", "max")


def test_opus_5_5_pins_an_explicit_effort():
    # Opus 5.5's API default effort is "medium", one level below Opus 5's; the
    # flagship chat path pins its level instead of inheriting that.
    assert config.anthropic_caps("claude-opus-5-5").effort in ("high", "xhigh", "max")


def test_anthropic_window_guard_fits_the_default_family():
    # All four have a 1M-token window and 128K max output; the anthropic guard
    # budgets 1,000,000 - 128,000 out - 32,000 overhead.
    from Orchestrator.context_builder import window_guard_budget_tokens
    assert window_guard_budget_tokens("anthropic") <= 1_000_000 - 128_000


@pytest.mark.parametrize("stream", [False, True])
def test_default_model_payload_is_valid(monkeypatch, stream):
    """What the chat paths SEND for the default model: adaptive thinking, never
    disabled/budget_tokens, no sampling params, max_tokens within 128K."""
    import asyncio
    import Orchestrator.routes.chat_routes as cr

    monkeypatch.setattr(cr, "ANTHROPIC_API_KEY", "test-anthropic", raising=False)
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr, "read_text_safe", lambda *a, **k: "")
    captured = []
    model = config.resolve_default_model("anthropic", "")

    if not stream:
        class _Resp:
            status_code = 200
            text = "fake"

            def json(self):
                return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1, "output_tokens": 1}}

        monkeypatch.setattr(cr.requests, "post", lambda *a, json=None, **k: captured.append(json) or _Resp())
        cr.call_anthropic([{"role": "user", "content": "hi"}], model, operator="test-op")
    else:
        class _Resp:
            status_code = 400

            async def aread(self):
                return b"fake"

        class _Ctx:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *a):
                return False

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def stream(self, method, url, headers=None, json=None):
                captured.append(json)
                return _Ctx()

        monkeypatch.setattr(cr.httpx, "AsyncClient", _Client)

        async def drain():
            return [ev async for ev in cr.stream_anthropic_with_thinking(
                [{"role": "user", "content": "hi"}], model, "test-op")]

        asyncio.run(drain())

    payload = captured[0]
    assert payload["model"] == config.ANTHROPIC_MODEL_DEFAULT
    assert payload["thinking"]["type"] == "adaptive"
    assert "budget_tokens" not in payload["thinking"]
    assert not {"temperature", "top_p", "top_k"} & set(payload)
    assert payload["max_tokens"] <= 128_000
    assert "tool_choice" not in payload  # forced tool use 400s on Opus/Sonnet 5.5


# ── 2. successor_for: retired id -> newest live model of the SAME family ──────

@pytest.mark.parametrize("retired, successor", [
    ("claude-opus-4-1", "claude-opus-5-5"),
    ("claude-opus-4-1-20250805", "claude-opus-5-5"),
    ("claude-opus-4-20250514", "claude-opus-5-5"),
    ("claude-opus-4-0", "claude-opus-5-5"),
    ("claude-opus-4", "claude-opus-5-5"),
    ("claude-3-opus-20240229", "claude-opus-5-5"),
    ("claude-sonnet-4-20250514", "claude-sonnet-5-5"),
    ("claude-sonnet-4-0", "claude-sonnet-5-5"),
    ("claude-sonnet-4", "claude-sonnet-5-5"),
    ("claude-3-7-sonnet-20250219", "claude-sonnet-5-5"),
    ("claude-3-5-sonnet-latest", "claude-sonnet-5-5"),
    ("claude-3-5-sonnet-20240620", "claude-sonnet-5-5"),
    ("claude-3-sonnet-20240229", "claude-sonnet-5-5"),
    ("claude-3-5-haiku-20241022", "claude-haiku-5-5"),
    ("claude-3-haiku-20240307", "claude-haiku-5-5"),
])
def test_successor_for_retired_ids(retired, successor):
    assert config.anthropic_model_retired(retired)
    assert config.successor_for(retired) == successor


@pytest.mark.parametrize("retired", ["claude-instant-1.2", "claude-2.1", "claude-2.0"])
def test_a_retired_id_without_a_family_runs_what_auto_runs(monkeypatch, retired):
    # The Android app rewrites a family-less retired id to Auto (""), which the
    # server runs as ANTHROPIC_MODEL_DEFAULT; the server's own mapping agrees.
    assert config.anthropic_model_retired(retired)
    assert config.successor_for(retired) == config.ANTHROPIC_MODEL_DEFAULT
    monkeypatch.setattr(config, "ANTHROPIC_MODEL_DEFAULT", "claude-sonnet-5-5")
    assert config.successor_for(retired) == "claude-sonnet-5-5"
    assert config.resolve_default_model("anthropic", retired) == config.resolve_default_model("anthropic", "")
    # A retired configured default is never what runs.
    monkeypatch.setattr(config, "ANTHROPIC_MODEL_DEFAULT", "claude-2.1")
    assert config.successor_for(retired) == "claude-opus-5-5"
    monkeypatch.setattr(config, "ANTHROPIC_MODEL_DEFAULT", "claude-3-haiku-20240307")
    assert config.successor_for(retired) == "claude-haiku-5-5"


def test_a_retired_configured_default_runs_its_successor(monkeypatch):
    # ANTHROPIC_MODEL pinned before Anthropic retired that id: Auto must not 404.
    monkeypatch.setattr(config, "ANTHROPIC_MODEL_DEFAULT", "claude-sonnet-4-20250514")
    for model in (None, "", "Auto"):
        assert config.resolve_default_model("anthropic", model) == "claude-sonnet-5-5"


@pytest.mark.parametrize("model", LIVE_IDS + (
    "claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-5", "claude-mythos-5-1",
    "claude-opus-6", "gemini-3.1-pro-preview", "gpt-5.1", "", "Auto",
))
def test_successor_for_leaves_live_and_unknown_ids_alone(model):
    assert not config.anthropic_model_retired(model)
    assert config.successor_for(model) == model


def test_successors_are_live_ids():
    assert set(config.ANTHROPIC_RETIRED_SUCCESSORS.values()) <= set(LIVE_IDS)
    assert set(config.ANTHROPIC_RETIRED_SUCCESSORS) == {"opus", "sonnet", "haiku"}


# ── 3. resolve_default_model ─────────────────────────────────────────────────

@pytest.mark.parametrize("model", [None, "", "   ", "Auto", "auto", "AUTO"])
def test_auto_resolves_to_each_providers_configured_default(model):
    assert config.resolve_default_model("anthropic", model) == config.ANTHROPIC_MODEL_DEFAULT
    assert config.resolve_default_model("google", model) == config.GEMINI_MODEL_DEFAULT
    assert config.resolve_default_model("gemini", model) == config.GEMINI_MODEL_DEFAULT
    assert config.resolve_default_model("openai", model) == config.OPENAI_MODEL_DEFAULT
    assert config.resolve_default_model("xai", model) == config.XAI_MODEL_DEFAULT
    assert config.resolve_default_model("computer-use", model) == config.CU_MODEL_DEFAULT


@pytest.mark.parametrize("alias", ["claude", "gpt", "grok", "cu", "mistral"])
def test_a_provider_no_dispatch_knows_resolves_like_its_gemini_fallthrough(alias):
    # /chat/stream sends any provider it does not know to Gemini; another
    # provider's model id must not go with it.
    assert config.resolve_default_model(alias, "") == config.GEMINI_MODEL_DEFAULT


@pytest.fixture
def keys(monkeypatch):
    """Set exactly the chat keys named (the rest empty) and unset DEFAULT_PROVIDER."""
    monkeypatch.delenv("DEFAULT_PROVIDER", raising=False)

    def _set(**present):
        for name in ("GOOGLE", "ANTHROPIC", "OPENAI", "XAI"):
            monkeypatch.setattr(config, f"{name}_API_KEY", present.get(name.lower(), ""))
    return _set


@pytest.mark.parametrize("present, expected", [
    ({"google": "g", "anthropic": "a"}, "google"),
    ({"anthropic": "a"}, "anthropic"),
    ({"anthropic": "a", "openai": "o"}, "anthropic"),
    ({"openai": "o", "xai": "x"}, "openai"),
    ({"xai": "x"}, "xai"),
    ({}, "google"),          # no key at all: the historical default
])
def test_default_provider_is_the_first_keyed_provider(keys, present, expected):
    keys(**present)
    assert config._default_provider() == expected


def test_an_explicit_default_provider_wins(keys, monkeypatch):
    keys(google="g")
    monkeypatch.setenv("DEFAULT_PROVIDER", " OpenAI ")
    assert config._default_provider() == "openai"


def test_an_anthropic_only_box_boots_with_anthropic_as_its_default_provider():
    """A fresh BYOK box with only an Anthropic key (onboarding never writes
    DEFAULT_PROVIDER): provider-less turns run Anthropic's default, not a
    Gemini that has no key."""
    import os
    import subprocess
    import sys

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = dict(os.environ, PYTHONPATH=root)
    # Empty = unset for these reads, and load_dotenv never overrides a set
    # variable, so this box's own .env cannot leak into the fresh-box boot.
    env.update({"DEFAULT_PROVIDER": "", "GOOGLE_API_KEY": "", "GEMINI_API_KEY": "",
                "OPENAI_API_KEY": "", "XAI_API_KEY": "", "ANTHROPIC_API_KEY": "test-anthropic",
                "ANTHROPIC_MODEL": ""})
    out = subprocess.run(
        [sys.executable, "-c",
         "from Orchestrator import config; print('PROVIDER=%s MODEL=%s' % ("
         "config.DEFAULT_PROVIDER, config.resolve_default_model('', '')))"],
        cwd=root, env=env, capture_output=True, text=True, timeout=120,
    )
    assert "PROVIDER=anthropic MODEL=claude-opus-5-5" in out.stdout, out.stdout[-800:] + out.stderr[-800:]


def test_empty_provider_uses_default_provider(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_PROVIDER", "anthropic")
    assert config.resolve_default_model(None) == config.ANTHROPIC_MODEL_DEFAULT
    assert config.resolve_default_model("", "") == config.ANTHROPIC_MODEL_DEFAULT
    monkeypatch.setattr(config, "DEFAULT_PROVIDER", "google")
    assert config.resolve_default_model("") == config.GEMINI_MODEL_DEFAULT


def test_resolver_reads_the_defaults_at_call_time(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_MODEL_DEFAULT", "claude-sonnet-5-5")
    assert config.resolve_default_model("anthropic") == "claude-sonnet-5-5"


def test_concrete_models_pass_through_and_retired_claude_ids_get_successors():
    assert config.resolve_default_model("anthropic", "claude-opus-4-8") == "claude-opus-4-8"
    assert config.resolve_default_model("anthropic", "claude-opus-4-1") == "claude-opus-5-5"
    assert config.resolve_default_model("openai", "gpt-5.5") == "gpt-5.5"
    # Only an anthropic dispatch is rewritten.
    assert config.resolve_default_model("openai", "claude-opus-4-1") == "claude-opus-4-1"


def test_custom_provider_is_left_to_its_registry_branch():
    # The custom branches resolve "", "Auto" and alias::model from the registry.
    assert config.resolve_default_model("custom", "") == ""
    assert config.resolve_default_model("custom", "Auto") == "Auto"
    assert config.resolve_default_model("custom", "lab::qwen") == "lab::qwen"


def test_scheduler_default_map_is_the_config_resolver():
    from Orchestrator.scheduler.executor import _provider_default_model
    for provider in ("anthropic", "google", "openai", "xai", "computer-use"):
        assert _provider_default_model(provider) == config.resolve_default_model(provider)


# ── 4. /chat/stream GET + POST: anthropic + empty model dispatches the default ─

def _get_stream_route_exists():
    # U20 (M3) deletes the GET twin; its tests skip once that is merged.
    from Orchestrator.checkpoint import app
    import Orchestrator.app  # noqa: F401 — registers routes on the shared app
    return any(getattr(r, "path", None) == "/chat/stream" and "GET" in (getattr(r, "methods", None) or ())
               for r in app.routes)


def _client():
    import Orchestrator.app  # noqa: F401 — registers routes on the shared app
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    return TestClient(app)


@pytest.fixture
def stream_fakes(monkeypatch):
    from Orchestrator.routes import chat_routes as cr
    seen = {}

    def fake_bsc(messages, operator, provider="openai", window_guard_tokens=None):
        seen["context_provider"] = provider
        return list(messages), {"recent": [], "keyword": [], "semantic": [], "checkpoint": []}, {}

    def _recorder(name):
        async def fake(messages, model, operator=None, **kw):
            seen["stream"] = name
            seen["model"] = model
            yield {"type": "content", "data": "ok"}
        return fake

    monkeypatch.setattr(cr, "build_streaming_context", fake_bsc)
    monkeypatch.setattr(cr, "stream_anthropic_with_thinking", _recorder("anthropic"))
    monkeypatch.setattr(cr, "stream_gemini_with_thinking", _recorder("gemini"))
    monkeypatch.setattr(cr, "stream_openai_with_reasoning", _recorder("openai"))
    monkeypatch.setattr(cr, "stream_xai_with_reasoning", _recorder("xai"))
    return seen


_MSGS = [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize("model", [None, "", "Auto"])
def test_get_stream_anthropic_auto_dispatches_configured_default(stream_fakes, model):
    if not _get_stream_route_exists():
        pytest.skip("no GET /chat/stream on this tree")
    params = {"messages": json.dumps(_MSGS), "provider": "anthropic", "operator": "TestOp"}
    if model is not None:
        params["model"] = model
    r = _client().get("/chat/stream", params=params)
    assert r.status_code == 200
    assert stream_fakes["stream"] == "anthropic"
    assert stream_fakes["model"] == config.ANTHROPIC_MODEL_DEFAULT
    assert f'"model": "{config.ANTHROPIC_MODEL_DEFAULT}"' in r.text


@pytest.mark.parametrize("model", [None, "", "Auto"])
def test_post_stream_anthropic_auto_dispatches_configured_default(stream_fakes, model):
    body = {"messages": _MSGS, "provider": "anthropic", "operator": "TestOp"}
    if model is not None:
        body["model"] = model
    r = _client().post("/chat/stream", json=body)
    assert r.status_code == 200
    assert stream_fakes["stream"] == "anthropic"
    assert stream_fakes["model"] == config.ANTHROPIC_MODEL_DEFAULT


def test_stream_routes_resolve_every_provider_from_config(stream_fakes):
    client = _client()
    for provider, stream, default in (
        ("gemini", "gemini", config.GEMINI_MODEL_DEFAULT),
        ("openai", "openai", config.OPENAI_MODEL_DEFAULT),
        ("xai", "xai", config.XAI_MODEL_DEFAULT),
    ):
        r = client.post("/chat/stream", json={"messages": _MSGS, "provider": provider,
                                              "model": "", "operator": "TestOp"})
        assert r.status_code == 200
        assert (stream_fakes["stream"], stream_fakes["model"]) == (stream, default)


@pytest.mark.parametrize("alias", ["claude", "gpt", "grok", "cu"])
def test_stream_routes_send_an_unknown_provider_gemini_with_geminis_model(stream_fakes, alias):
    r = _client().post("/chat/stream", json={"messages": _MSGS, "provider": alias,
                                             "model": "", "operator": "TestOp"})
    assert r.status_code == 200
    assert (stream_fakes["stream"], stream_fakes["model"]) == ("gemini", config.GEMINI_MODEL_DEFAULT)


def test_stream_routes_map_a_retired_claude_id_to_its_successor(stream_fakes):
    r = _client().post("/chat/stream", json={"messages": _MSGS, "provider": "anthropic",
                                             "model": "claude-opus-4-1", "operator": "TestOp"})
    assert r.status_code == 200
    assert stream_fakes["model"] == "claude-opus-5-5"


def test_stream_routes_take_a_missing_provider_from_default_provider(stream_fakes, monkeypatch):
    from Orchestrator.routes import chat_routes as cr
    monkeypatch.setattr(config, "DEFAULT_PROVIDER", "anthropic")
    monkeypatch.setattr(cr, "DEFAULT_PROVIDER", "anthropic", raising=False)
    client = _client()
    r = client.post("/chat/stream", json={"messages": _MSGS, "operator": "TestOp"})
    assert r.status_code == 200
    assert (stream_fakes["stream"], stream_fakes["model"]) == ("anthropic", config.ANTHROPIC_MODEL_DEFAULT)
    if not _get_stream_route_exists():
        return
    r = client.get("/chat/stream", params={"messages": json.dumps(_MSGS), "operator": "TestOp"})
    assert r.status_code == 200
    assert (stream_fakes["stream"], stream_fakes["model"]) == ("anthropic", config.ANTHROPIC_MODEL_DEFAULT)


# ── 5. tasks.process_chat_task (cron, SMS, /chat) ────────────────────────────

def _run_worker(monkeypatch, *, provider, model, task_id, messages=None):
    import Orchestrator.tasks as tasks
    import Orchestrator.routes.chat_routes as cr
    from Orchestrator.models import Task, TaskStatus, TaskType, task_db
    from Orchestrator.volume import now_utc_iso

    recorded = {}

    def fake_call_anthropic(msgs, mdl, operator="?"):
        recorded["model"] = mdl
        return ("answer", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, "", [])

    def fake_call_gemini(msgs, mdl, operator="?"):
        recorded["gemini_model"] = mdl
        return ("answer", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, [], "", [])

    monkeypatch.setattr(cr, "call_anthropic", fake_call_anthropic)
    monkeypatch.setattr(cr, "call_gemini", fake_call_gemini)
    monkeypatch.setattr(tasks, "TOOLVAULT_ENABLED", False)
    monkeypatch.setattr(tasks, "build_fossil_context", lambda *a, **k: (
        "", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []}))
    monkeypatch.setattr(tasks, "get_recent_fossils_for_operator", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "keyword_retrieve_for_operator", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "semantic_retrieve", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "get_recent_checkpoints_for_operator", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "get_recent_media_artifacts", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "AUTO_ENABLE", False)
    monkeypatch.setattr(tasks, "should_create_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "perform_mint", lambda *a, **k: {"snap_id": "SNAP-TEST"})
    monkeypatch.setattr(tasks, "save_operator_state", lambda *a, **k: None)

    task = Task(
        task_id=task_id, task_type=TaskType.CHAT, status=TaskStatus.PENDING,
        created_at=now_utc_iso(), updated_at=now_utc_iso(), operator="ModelTester",
        result_data={"messages": messages or [{"role": "user", "content": "hello"}],
                     "operator": "ModelTester", "provider": provider, "model": model},
    )
    task_db.save_task(task)
    tasks.process_chat_task(task)
    return task_db.get_task(task_id), recorded


@pytest.mark.parametrize("model", ["", "Auto"])
def test_worker_anthropic_auto_dispatches_configured_default(monkeypatch, model):
    from Orchestrator.models import TaskStatus
    final, recorded = _run_worker(monkeypatch, provider="anthropic", model=model,
                                  task_id=f"t-u15-auto-{model or 'empty'}")
    assert final.status == TaskStatus.COMPLETED, final.result_data
    assert recorded["model"] == config.ANTHROPIC_MODEL_DEFAULT


def test_worker_maps_a_retired_claude_id_to_its_successor(monkeypatch):
    # A cron job or SMS preference saved before Anthropic retired its model.
    from Orchestrator.models import TaskStatus
    final, recorded = _run_worker(monkeypatch, provider="anthropic",
                                  model="claude-sonnet-4-20250514", task_id="t-u15-retired")
    assert final.status == TaskStatus.COMPLETED, final.result_data
    assert recorded["model"] == "claude-sonnet-5-5"


def test_worker_media_auto_route_runs_the_new_providers_default(monkeypatch):
    # Audio on anthropic switches the turn to Gemini; the Claude id the client
    # sent must not be dispatched to Gemini.
    from Orchestrator.models import TaskStatus
    audio = [{"role": "user", "content": [
        {"type": "text", "text": "What does this say?"},
        {"type": "audio_url", "audio_url": {"url": "/ui/uploads/clip.mp3"}},
    ]}]
    final, recorded = _run_worker(monkeypatch, provider="anthropic", model="claude-opus-4-8",
                                  task_id="t-u15-autoroute", messages=audio)
    assert final.status == TaskStatus.COMPLETED, final.result_data
    assert recorded["gemini_model"] == config.GEMINI_MODEL_DEFAULT
    assert "model" not in recorded


@pytest.mark.parametrize("cfg_model, expected", [
    ("", None), ("Auto", None), ("claude-opus-4-1", "claude-opus-5-5"),
])
def test_flight_recorder_provider_without_a_model_runs_its_default(monkeypatch, cfg_model, expected):
    import configparser
    import Orchestrator.oversight as oversight
    import Orchestrator.routes.chat_routes as cr

    cp = configparser.ConfigParser()
    cp.read_dict({"flight_recorder": {"provider": "anthropic", "model": cfg_model}})
    monkeypatch.setattr(oversight, "CFG", cp)
    seen = []
    monkeypatch.setattr(cr, "call_anthropic",
                        lambda messages, model, operator: seen.append(model) or ("report", {}, "", []))
    assert oversight._synthesize("digest") == "report"
    assert seen == [expected or config.ANTHROPIC_MODEL_DEFAULT]


def test_flight_recorder_keyed_fallbacks_run_each_providers_auto_model(monkeypatch, keys):
    # No [flight_recorder] provider: the keyed fallbacks run what Auto runs, so
    # an ANTHROPIC_MODEL pinned to a retired id gets its successor there too.
    import configparser
    import Orchestrator.oversight as oversight
    import Orchestrator.routes.chat_routes as cr

    monkeypatch.setattr(oversight, "CFG", configparser.ConfigParser())
    keys(anthropic="a")
    monkeypatch.setattr(config, "ANTHROPIC_MODEL_DEFAULT", "claude-sonnet-4-20250514")
    seen = []
    monkeypatch.setattr(cr, "call_anthropic",
                        lambda messages, model, operator: seen.append(model) or ("report", {}, "", []))
    assert oversight._synthesize("digest") == "report"
    assert seen == ["claude-sonnet-5-5"]


def test_model_catalog_preselects_what_auto_runs(monkeypatch):
    from Orchestrator.routes.admin_routes import _wrap
    monkeypatch.setattr(config, "ANTHROPIC_MODEL_DEFAULT", "claude-sonnet-4-20250514")
    assert _wrap("anthropic", [], "live")["default_id"] == "claude-sonnet-5-5"
    monkeypatch.setattr(config, "ANTHROPIC_MODEL_DEFAULT", "claude-opus-5-5")
    assert _wrap("anthropic", [], "live")["default_id"] == "claude-opus-5-5"
    for provider in ("google", "openai", "xai", "computer-use"):
        assert _wrap(provider, [], "live")["default_id"] == config.resolve_default_model(provider)
    assert _wrap("custom", [], "live")["default_id"] == ""


# ── 6. SMS: router + /sms/preferences ────────────────────────────────────────

class _Resp:
    def __init__(self, data):
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._data


class _Session:
    posted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, timeout=None):
        _Session.posted.append((url, json))
        return _Resp({"task_id": "t-sms"})

    def get(self, url, timeout=None):
        return _Resp({"status": "completed", "result_data": {"ui_reply": "ok"}})


class _Manager:
    def set_sms_callback(self, cb):
        pass

    def gateways(self):
        return {}


class _Store:
    def get_messages(self, *a, **k):
        return []

    def get_thread(self, *a, **k):
        return []


@pytest.fixture
def sms_router(monkeypatch):
    import aiohttp
    import Orchestrator.sms.router as router_mod
    import Orchestrator.state as state

    _Session.posted = []
    monkeypatch.setattr(aiohttp, "ClientSession", _Session)

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(router_mod.asyncio, "sleep", _no_sleep)
    prefs = {}
    monkeypatch.setattr(state, "get_operator_preference",
                        lambda op, key, default=None: prefs.get(key, default))
    r = router_mod.SMSRouter(_Manager(), _Store())
    monkeypatch.setattr(r, "_build_thread_messages", lambda *a, **k: [])
    return r, prefs


@pytest.mark.asyncio
@pytest.mark.parametrize("provider, model, expect_provider, expect_model", [
    ("anthropic", "", "anthropic", None),        # None = ANTHROPIC_MODEL_DEFAULT
    ("anthropic", "claude-opus-4-1", "anthropic", "claude-opus-5-5"),
    ("anthropic", "claude-opus-4-8", "anthropic", "claude-opus-4-8"),
])
async def test_sms_router_resolves_the_reply_model(sms_router, provider, model,
                                                  expect_provider, expect_model):
    r, prefs = sms_router
    prefs.update({"sms_provider": provider, "sms_model": model})
    reply = await r._route_through_chat("+15555550123", "hi", "TestOp", "Pat")
    assert reply == "ok"
    url, payload = _Session.posted[0]
    assert payload["provider"] == expect_provider
    assert payload["model"] == (expect_model or config.ANTHROPIC_MODEL_DEFAULT)
    assert url == config.orchestrator_url("/chat")


@pytest.mark.asyncio
async def test_sms_router_unset_preferences_use_default_provider(sms_router, monkeypatch):
    r, _prefs = sms_router
    monkeypatch.setattr(config, "DEFAULT_PROVIDER", "anthropic")
    await r._route_through_chat("+15555550123", "hi", "TestOp", "Pat")
    _url, payload = _Session.posted[0]
    assert payload["provider"] == "anthropic"
    assert payload["model"] == config.ANTHROPIC_MODEL_DEFAULT


@pytest.mark.asyncio
async def test_sms_on_an_anthropic_only_box_replies_with_anthropic(sms_router, monkeypatch, keys):
    # The default provider the box boots with (DEFAULT_PROVIDER unset).
    r, _prefs = sms_router
    keys(anthropic="a")
    monkeypatch.setattr(config, "DEFAULT_PROVIDER", config._default_provider())
    await r._route_through_chat("+15555550123", "hi", "TestOp", "Pat")
    _url, payload = _Session.posted[0]
    assert (payload["provider"], payload["model"]) == ("anthropic", config.ANTHROPIC_MODEL_DEFAULT)


@pytest.mark.asyncio
async def test_sms_preferences_defaults_come_from_config(monkeypatch):
    import Orchestrator.state as state
    from Orchestrator.routes.sms_routes import get_sms_preferences

    monkeypatch.setattr(state, "get_operator_preference", lambda op, key, default=None: default)
    monkeypatch.setattr(config, "DEFAULT_PROVIDER", "anthropic")
    prefs = await get_sms_preferences(operator="TestOp")
    assert prefs["sms_provider"] == "anthropic"
    assert prefs["sms_model"] == ""  # Auto: follows the configured default
    assert prefs["sms_model_resolved"] == config.ANTHROPIC_MODEL_DEFAULT


@pytest.mark.asyncio
async def test_sms_preferences_report_the_successor_of_a_retired_choice(monkeypatch):
    import Orchestrator.state as state
    from Orchestrator.routes.sms_routes import get_sms_preferences

    saved = {"sms_provider": "anthropic", "sms_model": "claude-sonnet-4-20250514"}
    monkeypatch.setattr(state, "get_operator_preference",
                        lambda op, key, default=None: saved.get(key, default))
    prefs = await get_sms_preferences(operator="TestOp")
    assert prefs["sms_model"] == "claude-sonnet-4-20250514"   # what was saved
    assert prefs["sms_model_resolved"] == "claude-sonnet-5-5"  # what replies run on
