"""Custom-model-providers Tasks 4.2 + 4.3: `custom` provider wiring.

Task 4.2 seams, all additive:
  1. ToolVault injector — explicit "custom" entries in PROVIDER_FORMATS /
     PROVIDER_DEFAULT_GROUP (unknown providers already default to
     openai_rest/chat; the explicit entries keep the [TOOLVAULT-INJECT] log
     truthful and protect against future default changes).
  2. context_builder — PROVIDER_WINDOW_GUARD_TOKENS["custom"] = 19200, a
     conservative no-override floor (rounded down from 0.6 x 32,768 default
     context_tokens ~= 19,660; the live formula lives in
     custom_servers.window_guard_tokens), plus a NEW `window_guard_tokens`
     override kwarg on build_fossil_context so Task 4.3 can thread the
     resolved server's real context_tokens. Without the floor, an unknown
     provider gets the 240K default and the assembler happily overflows a
     32K-window llama.cpp server on turn one.
  3. chat_routes — "custom" in _get_tools' openai-format tuple;
     build_streaming_context threads window_guard_tokens through.

Task 4.3 seams (route-level, BOTH /chat/stream routes):
  4. custom_servers.window_guard_tokens(server) — the ONE shared formula
     max(4000, int(context_tokens * 0.6)) both routes use.
  5. GET + POST /chat/stream — provider "custom" resolves the server in the
     default-model block (BEFORE build_streaming_context — order matters),
     qualifies an empty/Auto model to the server's first discovered model,
     threads the guard, and dispatches to stream_custom_with_reasoning.

All tests hermetic (monkeypatched registry path/context builder/stream fn —
no live provider calls). Route tests use the house main-app TestClient
pattern (test_stt_onboarding.py).
"""
import json

import pytest

from Orchestrator import context_builder as cb
from Orchestrator.onboarding import custom_servers as cs


# --- injector entries ----------------------------------------------------------

def test_injector_formats_custom_as_openai_rest():
    from Orchestrator.toolvault.injector import PROVIDER_FORMATS, PROVIDER_DEFAULT_GROUP
    assert PROVIDER_FORMATS["custom"] == "openai_rest"
    assert PROVIDER_DEFAULT_GROUP["custom"] == "chat"


# --- window-guard floor --------------------------------------------------------

def test_provider_window_guard_custom_floor():
    """`custom` gets an explicit 19,200-token conservative floor (rounded down
    from 0.6 x 32,768 default context_tokens ~= 19,660; live formula:
    custom_servers.window_guard_tokens) — NEVER the 240K unknown-provider
    default."""
    assert cb.PROVIDER_WINDOW_GUARD_TOKENS["custom"] == 19200
    assert cb.window_guard_budget_tokens("custom") == 19200
    # Case-insensitive, same as every other entry (.get(provider.lower())).
    assert cb.window_guard_budget_tokens("Custom") == 19200
    # The unknown-provider default is untouched.
    assert cb.window_guard_budget_tokens("mystery") == cb.DEFAULT_WINDOW_GUARD_TOKENS


# --- build_fossil_context window_guard_tokens override --------------------------
# Hermetic fixture pattern from test_context_delivery_uncapped.py.

A = "SNAP-20260709-0001"


def _blk(sid: str, body: str = "body") -> str:
    return f"=== START SNAPSHOT — UTC 2026-07-09T00:00:00Z — {sid} ===\n{body}\n"


class _FakeCFG:
    _ints = {
        "recent_fossils_per_user": 5,
        "keyword_fossils_per_user": 3,
        "semantic_fossils_per_user": 6,
        "checkpoint_snapshots": 2,
        "max_fossil_chars": 10000,
    }

    def getint(self, section, key, fallback=0):
        return self._ints.get(key, fallback)

    def getfloat(self, section, key, fallback=0.0):
        return fallback


def _patch_builder(monkeypatch, recent_blocks):
    monkeypatch.setattr(cb, "CFG", _FakeCFG())
    monkeypatch.setattr(cb, "read_text_safe", lambda p: "")
    monkeypatch.setattr(cb, "get_recent_media_artifacts", lambda op, limit=10: [])
    monkeypatch.setattr(cb, "get_recent_fossils_for_operator",
                        lambda vol, op, n, cap: list(recent_blocks)[:n])
    monkeypatch.setattr(cb, "keyword_retrieve_for_operator",
                        lambda vol, q, k, op: [])
    monkeypatch.setattr(cb, "semantic_retrieve",
                        lambda q, operator="", k=15, threshold=0.60,
                        window_budget_chars=None, telemetry=None: [])
    monkeypatch.setattr(cb, "get_recent_checkpoints_for_operator",
                        lambda vol, op, count=1: [])


def test_build_fossil_context_accepts_window_guard_override(monkeypatch, capsys):
    """window_guard_tokens=8000 replaces the provider-table budget — observable
    in the always-printed Delivery line's window-guard budget."""
    _patch_builder(monkeypatch, [_blk(A)])
    cb.build_fossil_context("q", "TestOp", provider="anthropic",
                            window_guard_tokens=8000)
    out = capsys.readouterr().out
    assert "window-guard budget 8,000 tokens" in out
    assert "840,000" not in out  # anthropic table value NOT used


def test_build_fossil_context_none_falls_back_to_provider_table(monkeypatch, capsys):
    """window_guard_tokens=None (and omitted) = today's behavior: the budget
    comes from window_guard_budget_tokens(provider)."""
    _patch_builder(monkeypatch, [_blk(A)])
    cb.build_fossil_context("q", "TestOp", provider="custom",
                            window_guard_tokens=None)
    out = capsys.readouterr().out
    assert "window-guard budget 19,200 tokens" in out

    cb.build_fossil_context("q", "TestOp", provider=None)
    out = capsys.readouterr().out
    assert "window-guard budget 240,000 tokens" in out  # untouched default path


def test_window_guard_override_actually_binds(monkeypatch, capsys):
    """The override is the REAL guard budget, not just a log label: a tiny
    override forces the whole-snapshot drop path on an oversized fossil."""
    big = _blk(A, "FACT-" + "x" * 30000 + "-ENDFACT")
    _patch_builder(monkeypatch, [big])
    text, prov = cb.build_fossil_context("q", "TestOp", provider="anthropic",
                                         window_guard_tokens=10)
    out = capsys.readouterr().out
    assert "window guard dropped" in out
    assert A not in text  # the over-budget snapshot was dropped whole
    assert prov["recent"] == []  # provenance reflects what is DELIVERED


# --- chat_routes: _get_tools tuple + build_streaming_context threading -----------

def test_get_tools_static_fallback_custom_is_openai_format(monkeypatch):
    from Orchestrator.routes import chat_routes as cr
    monkeypatch.setattr(cr, "TOOLVAULT_ENABLED", False)
    assert cr._get_tools("custom", prompt="") is cr.CHAT_TOOLS_OPENAI


def test_build_streaming_context_threads_window_guard_tokens(monkeypatch):
    from Orchestrator.routes import chat_routes as cr
    from Orchestrator import tasks as tasks_mod

    recorded = {}

    def _fake_bfc(user_text, operator, log_prefix="[CONTEXT]", provider=None,
                  window_guard_tokens=None, **kw):
        recorded["window_guard_tokens"] = window_guard_tokens
        recorded["provider"] = provider
        return "", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []}

    # build_streaming_context lazily does `from Orchestrator.context_builder
    # import build_fossil_context` at call time — patching the module attr works.
    monkeypatch.setattr(cb, "build_fossil_context", _fake_bfc)
    monkeypatch.setattr(cr, "TOOLVAULT_ENABLED", False)
    monkeypatch.setattr(tasks_mod, "build_core_system_prompt",
                        lambda instructions, operator=None: "SP")

    msgs = [{"role": "user", "content": "hello"}]

    cr.build_streaming_context(msgs, "TestOp", provider="custom",
                               window_guard_tokens=4096)
    assert recorded["window_guard_tokens"] == 4096
    assert recorded["provider"] == "custom"

    cr.build_streaming_context(msgs, "TestOp", provider="custom")
    assert recorded["window_guard_tokens"] is None  # default = today's behavior


# --- Task 4.3: shared window-guard helper (custom_servers.window_guard_tokens) ---

def test_window_guard_tokens_default_context():
    """Default 32,768-token window -> int(0.6 x 32,768) = 19,660."""
    assert cs.window_guard_tokens({"context_tokens": 32768}) == 19660


def test_window_guard_tokens_none_is_default_derived():
    """Unresolved server (None) and a record missing the key both derive from
    DEFAULT_CONTEXT_TOKENS — never crash, never 240K."""
    assert cs.window_guard_tokens(None) == 19660
    assert cs.window_guard_tokens({}) == 19660


def test_window_guard_tokens_floor_binds():
    """Tiny window: int(0.6 x 2,048) = 1,228 -> the 4,000 floor binds so a
    nonsense context_tokens can't starve retrieval entirely."""
    assert cs.window_guard_tokens({"context_tokens": 2048}) == 4000


def test_window_guard_tokens_custom_16k():
    assert cs.window_guard_tokens({"context_tokens": 16384}) == 9830  # int(0.6 x 16,384)


# --- Task 4.3: /chat/stream route wiring (GET + POST, symmetric) -----------------
# House main-app TestClient pattern (test_stt_onboarding.py). The route's
# collaborators are patched at the chat_routes module seam: a recording
# build_streaming_context (captures window_guard_tokens) and a fake
# stream_custom_with_reasoning (captures the dispatched model/operator).

def _client():
    import Orchestrator.app  # noqa: F401 — side-effect: registers routes onto the shared app
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    return TestClient(app)


@pytest.fixture
def custom_registry(tmp_path, monkeypatch):
    """One enabled server: alias 'lab', 16K window, two discovered models."""
    reg = tmp_path / "custom_models.json"
    reg.write_text(json.dumps({"version": 1, "servers": [{
        "id": "srv-test0001",
        "alias": "lab",
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key": "",
        "context_tokens": 16384,
        "enabled": True,
        "added_at": "2026-07-09T00:00:00+00:00",
        "validated_at": None,
        "last_models": ["llama-3-8b", "qwen-2"],
    }]}))
    monkeypatch.setattr(cs, "REGISTRY_PATH", str(reg))
    return reg


@pytest.fixture
def route_fakes(monkeypatch):
    from Orchestrator.routes import chat_routes as cr
    recorded = {}

    def fake_bsc(messages, operator, provider="openai", window_guard_tokens=None):
        recorded["window_guard_tokens"] = window_guard_tokens
        # 3-tuple: build_streaming_context now also returns The Signal's
        # presentation-only telemetry sink (the route sets telemetry["model"]).
        return list(messages), {"recent": [], "keyword": [], "semantic": [], "checkpoint": []}, {}

    async def fake_custom_stream(messages, model, operator):
        recorded["dispatch_model"] = model
        recorded["dispatch_operator"] = operator
        yield {"type": "content", "data": "ok"}

    monkeypatch.setattr(cr, "build_streaming_context", fake_bsc)
    monkeypatch.setattr(cr, "stream_custom_with_reasoning", fake_custom_stream)
    return recorded


_MSGS = [{"role": "user", "content": "hi"}]


def test_get_stream_custom_qualifies_default_model_and_threads_guard(custom_registry, route_fakes):
    """GET /chat/stream, empty model: resolves 'lab', qualifies its first
    discovered model, threads guard = int(0.6 x 16,384) = 9,830, dispatches."""
    r = _client().get("/chat/stream", params={
        "messages": json.dumps(_MSGS), "provider": "custom", "operator": "TestOp",
    })
    assert r.status_code == 200
    assert route_fakes["window_guard_tokens"] == 9830
    assert route_fakes["dispatch_model"] == "lab::llama-3-8b"
    assert route_fakes["dispatch_operator"] == "TestOp"
    assert '"model": "lab::llama-3-8b"' in r.text  # stream_start advertises the resolved model
    assert "event: content" in r.text  # keepalive-wrapped dispatch relayed the fake stream


def test_post_stream_custom_auto_model_qualifies_default(custom_registry, route_fakes):
    """POST /chat/stream, model='Auto' (picker sentinel): same qualified
    default + guard as the GET route — the two routes stay symmetric."""
    r = _client().post("/chat/stream", json={
        "messages": _MSGS, "provider": "custom", "model": "Auto", "operator": "TestOp",
    })
    assert r.status_code == 200
    assert route_fakes["window_guard_tokens"] == 9830
    assert route_fakes["dispatch_model"] == "lab::llama-3-8b"
    assert '"model": "lab::llama-3-8b"' in r.text


def test_post_stream_custom_explicit_model_passes_through(custom_registry, route_fakes):
    """An explicit qualified model is NOT rewritten to the server's first
    discovered model; the guard still comes from the resolved server."""
    r = _client().post("/chat/stream", json={
        "messages": _MSGS, "provider": "custom", "model": "lab::qwen-2", "operator": "TestOp",
    })
    assert r.status_code == 200
    assert route_fakes["dispatch_model"] == "lab::qwen-2"
    assert route_fakes["window_guard_tokens"] == 9830


def test_get_stream_custom_no_servers_floor_guard_model_untouched(tmp_path, monkeypatch, route_fakes):
    """No enabled servers: guard falls back to the default-derived floor
    (19,660) and the model is left as-is — stream_custom_with_reasoning owns
    the clean error event (route must not invent a model)."""
    monkeypatch.setattr(cs, "REGISTRY_PATH", str(tmp_path / "absent.json"))
    r = _client().get("/chat/stream", params={
        "messages": json.dumps(_MSGS), "provider": "custom", "operator": "TestOp",
    })
    assert r.status_code == 200
    assert route_fakes["window_guard_tokens"] == 19660
    assert route_fakes["dispatch_model"] is None


# --- per-model context windows (model_context) route threading -------------------
# A llama-swap server hosts models with DIFFERENT real windows behind one
# record; the guard must budget from the DISPATCHED model's window, not the
# server-wide context_tokens (which over-budgets the smaller models and gets
# the whole turn 400'd far-end by llama.cpp).

@pytest.fixture
def per_model_registry(tmp_path, monkeypatch):
    """One enabled llama-swap-style server: 128K server-wide window, first
    discovered model 'm-small' learned at 16K, 'm-big' not in the map."""
    reg = tmp_path / "custom_models.json"
    reg.write_text(json.dumps({"version": 1, "servers": [{
        "id": "srv-permodel1",
        "alias": "swap",
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key": "",
        "context_tokens": 131072,
        "model_context": {"m-small": 16384},
        "enabled": True,
        "added_at": "2026-07-09T00:00:00+00:00",
        "validated_at": None,
        "last_models": ["m-small", "m-big"],
    }]}))
    monkeypatch.setattr(cs, "REGISTRY_PATH", str(reg))
    return reg


def test_post_stream_custom_per_model_guard_override(per_model_registry, route_fakes):
    """Dispatching a model with a learned window budgets from THAT window:
    int(0.6 x 16,384) = 9,830 — not the server-wide 78,643."""
    r = _client().post("/chat/stream", json={
        "messages": _MSGS, "provider": "custom", "model": "swap::m-small", "operator": "TestOp",
    })
    assert r.status_code == 200
    assert route_fakes["window_guard_tokens"] == 9830
    assert route_fakes["dispatch_model"] == "swap::m-small"


def test_post_stream_custom_model_not_in_map_uses_server_window(per_model_registry, route_fakes):
    """A model absent from model_context keeps the server-wide budget:
    int(0.6 x 131,072) = 78,643."""
    r = _client().post("/chat/stream", json={
        "messages": _MSGS, "provider": "custom", "model": "swap::m-big", "operator": "TestOp",
    })
    assert r.status_code == 200
    assert route_fakes["window_guard_tokens"] == 78643
    assert route_fakes["dispatch_model"] == "swap::m-big"


def test_get_stream_custom_per_model_guard_override(per_model_registry, route_fakes):
    """GET route symmetric with POST for the per-model override."""
    r = _client().get("/chat/stream", params={
        "messages": json.dumps(_MSGS), "provider": "custom",
        "model": "swap::m-small", "operator": "TestOp",
    })
    assert r.status_code == 200
    assert route_fakes["window_guard_tokens"] == 9830


def test_post_stream_custom_default_model_uses_final_bare_for_guard(per_model_registry, route_fakes):
    """Empty model defaults to the FIRST discovered model ('m-small') — the
    guard must budget from that FINAL dispatched bare id's learned window,
    not from the pre-default empty model."""
    r = _client().post("/chat/stream", json={
        "messages": _MSGS, "provider": "custom", "operator": "TestOp",
    })
    assert r.status_code == 200
    assert route_fakes["dispatch_model"] == "swap::m-small"
    assert route_fakes["window_guard_tokens"] == 9830


# =================================================================================
# Task 5.1: call_custom (non-stream) + tasks.py dispatch + guard exemptions
# =================================================================================
# call_custom is a clone of call_xai (same OpenAI-compatible wire shape + the
# 4-field reasoning probe + (text, usage, reasoning, media_tasks) 4-tuple) with
# the streaming clone's server-resolution deltas. tasks.process_chat_task gains:
# provider normalization + registry-default-model resolution + dispatch for
# "custom", a media auto-route exemption (audio/video must NOT switch custom ->
# google), and a fossil window guard — the stream guard's exact metric,
# estimate_tokens(assembled) vs window_guard_tokens(server), whole oldest-first
# snapshot drops — on the otherwise-uncapped inline fossil block.

ANSWER_51 = "Port 9091 serves the Orchestrator."
REASONING_51 = "Thinking hard about ports and orchestration."


class _FakeResp:
    """Minimal stand-in for a requests.Response (test_phase2_reasoning pattern)."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _compat_payload():
    """OpenAI-compatible non-stream body the way llama.cpp emits it
    (reasoning_content always present on reasoning models)."""
    return {
        "choices": [{
            "message": {"role": "assistant", "content": ANSWER_51,
                        "reasoning_content": REASONING_51},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16},
    }


@pytest.fixture
def call_custom_env(monkeypatch):
    """chat_routes with _get_tools stubbed and a recording requests.post."""
    from Orchestrator.routes import chat_routes as cr
    recorded = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        recorded["url"] = url
        recorded["headers"] = headers or {}
        recorded["payload"] = json
        recorded["timeout"] = timeout
        return _FakeResp(_compat_payload())

    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr.requests, "post", fake_post)
    return cr, recorded


# --- call_custom unit tests -------------------------------------------------------

def test_call_custom_returns_xai_shape_and_separates_reasoning(custom_registry, call_custom_env):
    """4-tuple return (call_xai parity), reasoning probed out of
    reasoning_content, answer never contaminated."""
    cr, recorded = call_custom_env
    text, usage, reasoning, media_tasks = cr.call_custom(
        [{"role": "user", "content": "hi"}], "lab::qwen-2", operator="TestOp")

    assert text == ANSWER_51
    assert REASONING_51 not in text
    assert reasoning == REASONING_51
    assert media_tasks == []
    assert usage["total_tokens"] == 16
    assert recorded["timeout"] == 200  # call_xai parity


def test_call_custom_payload_shape(custom_registry, call_custom_env):
    """Bare model in the payload, no stream/stream_options (non-stream), no
    max_tokens (INTEGRATION.md §6), keyless server -> no Authorization header,
    endpoint = <base_url>/chat/completions."""
    cr, recorded = call_custom_env
    cr.call_custom([{"role": "user", "content": "hi"}], "lab::qwen-2", operator="TestOp")

    assert recorded["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    assert recorded["payload"]["model"] == "qwen-2"  # bare, never alias-qualified
    assert "stream" not in recorded["payload"]
    assert "stream_options" not in recorded["payload"]
    assert "max_tokens" not in recorded["payload"]
    assert "Authorization" not in recorded["headers"]


def test_call_custom_empty_model_uses_first_discovered(custom_registry, call_custom_env):
    """Empty model resolves to the first enabled server's first discovered
    model (same fallback as the stream clone)."""
    cr, recorded = call_custom_env
    cr.call_custom([{"role": "user", "content": "hi"}], "", operator="TestOp")
    assert recorded["payload"]["model"] == "llama-3-8b"


def test_call_custom_bearer_only_when_key(tmp_path, monkeypatch, call_custom_env):
    cr, recorded = call_custom_env
    reg = tmp_path / "custom_models.json"
    reg.write_text(json.dumps({"version": 1, "servers": [{
        "id": "srv-key00001", "alias": "keyed",
        "base_url": "http://127.0.0.1:8081/v1", "api_key": "sk-test-123",
        "context_tokens": 32768, "enabled": True,
        "added_at": "2026-07-09T00:00:00+00:00", "validated_at": None,
        "last_models": ["m1"],
    }]}))
    monkeypatch.setattr(cs, "REGISTRY_PATH", str(reg))

    cr.call_custom([{"role": "user", "content": "hi"}], "keyed::m1", operator="TestOp")
    assert recorded["headers"].get("Authorization") == "Bearer sk-test-123"


def test_call_custom_audio_part_becomes_text_note(custom_registry, call_custom_env):
    """audio_url parts are converted to a text note at the message-processing
    seam (mirrors the video_url branch) — NEVER forwarded raw. llama.cpp 500s
    on raw audio parts, and custom is the only provider whose call_* ever
    receives them (the tasks.py media exemption keeps audio turns on custom)."""
    cr, recorded = call_custom_env
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "what does this say?"},
        {"type": "audio_url", "audio_url": {"url": "/ui/uploads/clip.mp3"}},
    ]}]
    cr.call_custom(msgs, "lab::qwen-2", operator="TestOp")

    sent = recorded["payload"]["messages"][0]["content"]
    assert all(p.get("type") != "audio_url" for p in sent)  # nothing raw
    note = next((p["text"] for p in sent
                 if p.get("type") == "text" and "Audio file attached" in p.get("text", "")), None)
    assert note is not None
    assert "/ui/uploads/clip.mp3" in note
    assert "doesn't support audio input" in note


def test_call_custom_exceed_context_learns_and_raises_friendly(custom_registry, call_custom_env, monkeypatch):
    """llama.cpp's exceed_context_size_error 400 carries the model's REAL
    window (n_ctx). call_custom must (a) persist it into the server's
    model_context map so every subsequent guard fits, and (b) raise a
    FRIENDLY actionable error — never the raw JSON body."""
    from fastapi import HTTPException
    cr, _ = call_custom_env
    exceed_body = {"error": {
        "code": 400,
        "message": "request (18079 tokens) exceeds the available context size (16384 tokens), try increasing it",
        "type": "exceed_context_size_error",
        "n_prompt_tokens": 18079, "n_ctx": 16384,
    }}
    monkeypatch.setattr(cr.requests, "post",
                        lambda *a, **k: _FakeResp(exceed_body, status_code=400))

    with pytest.raises(HTTPException) as ei:
        cr.call_custom([{"role": "user", "content": "hi"}], "lab::qwen-2", operator="TestOp")

    assert ei.value.status_code == 400
    assert "learned this limit" in ei.value.detail
    assert "16,384-token context window" in ei.value.detail
    assert "qwen-2" in ei.value.detail and "lab" in ei.value.detail
    assert "exceed_context_size_error" not in ei.value.detail  # raw body stays in the LOG only
    # learned limit persisted (legacy record had no model_context field at all)
    srv = cs.list_servers()[0]
    assert srv["model_context"] == {"qwen-2": 16384}


def test_call_custom_exceed_context_merges_existing_map(custom_registry, call_custom_env, monkeypatch):
    """Learning a second model's window must MERGE, not clobber, the map."""
    from fastapi import HTTPException
    cr, _ = call_custom_env
    srv_id = cs.list_servers()[0]["id"]
    cs.update_server(srv_id, {"model_context": {"llama-3-8b": 8192}})
    exceed_body = {"error": {"type": "exceed_context_size_error",
                             "n_prompt_tokens": 18079, "n_ctx": 16384, "code": 400,
                             "message": "request (18079 tokens) exceeds the available context size (16384 tokens), try increasing it"}}
    monkeypatch.setattr(cr.requests, "post",
                        lambda *a, **k: _FakeResp(exceed_body, status_code=400))
    with pytest.raises(HTTPException):
        cr.call_custom([{"role": "user", "content": "hi"}], "lab::qwen-2", operator="TestOp")
    assert cs.list_servers()[0]["model_context"] == {"llama-3-8b": 8192, "qwen-2": 16384}


def test_call_custom_exceed_context_learn_is_fail_soft(custom_registry, call_custom_env, monkeypatch):
    """A server vanishing between resolve and learn must not mask the
    friendly error — the learn is fail-soft, the raise still happens."""
    from fastapi import HTTPException
    cr, _ = call_custom_env
    exceed_body = {"error": {"type": "exceed_context_size_error", "n_ctx": 16384,
                             "code": 400, "message": "exceeds the available context size"}}
    monkeypatch.setattr(cr.requests, "post",
                        lambda *a, **k: _FakeResp(exceed_body, status_code=400))

    def _vanished(*a, **k):
        raise KeyError("No custom server with id 'srv-test0001'")
    monkeypatch.setattr(cs, "update_server", _vanished)

    with pytest.raises(HTTPException) as ei:
        cr.call_custom([{"role": "user", "content": "hi"}], "lab::qwen-2", operator="TestOp")
    assert "learned this limit" in ei.value.detail


def test_call_custom_non_context_400_keeps_raw_error(custom_registry, call_custom_env, monkeypatch):
    """Any other 400 keeps today's alias-prefixed raw error and learns
    NOTHING — the exceed branch must not swallow unrelated failures."""
    from fastapi import HTTPException
    cr, _ = call_custom_env
    other_body = {"error": {"code": 400, "message": "unknown field 'tools'",
                            "type": "invalid_request_error"}}
    monkeypatch.setattr(cr.requests, "post",
                        lambda *a, **k: _FakeResp(other_body, status_code=400))
    with pytest.raises(HTTPException) as ei:
        cr.call_custom([{"role": "user", "content": "hi"}], "lab::qwen-2", operator="TestOp")
    assert "API error" in ei.value.detail
    assert "unknown field 'tools'" in ei.value.detail
    assert "learned this limit" not in ei.value.detail
    assert cs.list_servers()[0].get("model_context", {}) == {}


def test_call_custom_error_cases(tmp_path, monkeypatch, custom_registry, call_custom_env):
    """The stream clone's three resolution error cases, raised (non-stream
    context) instead of yielded."""
    cr, _ = call_custom_env

    # (1) qualified alias matching no ENABLED server (servers DO exist)
    with pytest.raises(Exception, match="unknown or disabled server alias"):
        cr.call_custom([{"role": "user", "content": "hi"}], "ghost::m", operator="TestOp")

    # (2) enabled server but zero discovered models, empty model requested
    reg = tmp_path / "empty_models.json"
    reg.write_text(json.dumps({"version": 1, "servers": [{
        "id": "srv-nomodels", "alias": "bare",
        "base_url": "http://127.0.0.1:8082/v1", "api_key": "",
        "context_tokens": 32768, "enabled": True,
        "added_at": "2026-07-09T00:00:00+00:00", "validated_at": None,
        "last_models": [],
    }]}))
    monkeypatch.setattr(cs, "REGISTRY_PATH", str(reg))
    with pytest.raises(Exception, match="no discovered models"):
        cr.call_custom([{"role": "user", "content": "hi"}], "", operator="TestOp")

    # (3) no servers configured at all
    monkeypatch.setattr(cs, "REGISTRY_PATH", str(tmp_path / "absent.json"))
    with pytest.raises(Exception, match="No custom model servers configured"):
        cr.call_custom([{"role": "user", "content": "hi"}], "", operator="TestOp")


# --- process_chat_task dispatch + guards (house _run_worker pattern) ---------------

class _TasksCFG:
    """Wrap the real CFG, pinning the [context] retrieval counts so the worker
    tests don't depend on this box's config.ini values. Everything else
    delegates to the real object."""

    _ints = {"recent_fossils_per_user": 10, "keyword_fossils_per_user": 0,
             "semantic_fossils_per_user": 0, "checkpoint_snapshots": 0}

    def __init__(self, real):
        self._real = real

    def getint(self, section, key, fallback=0):
        if section == "context" and key in self._ints:
            return self._ints[key]
        return self._real.getint(section, key, fallback=fallback)

    def getfloat(self, section, key, fallback=0.0):
        return self._real.getfloat(section, key, fallback=fallback)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _run_chat_worker(monkeypatch, *, provider, model, task_id, messages,
                     recent_blocks=(), forbidden_calls=()):
    """Drive process_chat_task hermetically (test_phase2_reasoning pattern) with
    a recording fake call_custom at the chat_routes seam. Returns
    (final_task, recorded, captured_turns)."""
    import Orchestrator.tasks as tasks
    import Orchestrator.routes.chat_routes as cr
    from Orchestrator.models import Task, TaskStatus, TaskType, task_db
    from Orchestrator.volume import now_utc_iso

    monkeypatch.setattr(tasks, "CFG", _TasksCFG(tasks.CFG))

    recorded = {}
    captured = {"turns": []}

    def fake_call_custom(msgs, mdl, operator="?"):
        recorded["messages"] = msgs
        recorded["model"] = mdl
        recorded["operator"] = operator
        return (ANSWER_51,
                {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                REASONING_51, [])

    def fake_call_anthropic(msgs, mdl, operator="?"):
        recorded["messages"] = msgs
        recorded["model"] = mdl
        return (ANSWER_51,
                {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                "", [])

    # raising=False: red-phase, call_custom may not exist yet on chat_routes.
    monkeypatch.setattr(cr, "call_custom", fake_call_custom, raising=False)
    monkeypatch.setattr(cr, "call_anthropic", fake_call_anthropic)
    for name in forbidden_calls:
        def _boom(*a, _n=name, **k):
            raise AssertionError(f"{_n} must not be called for provider {provider!r}")
        monkeypatch.setattr(cr, name, _boom)

    monkeypatch.setattr(tasks, "TOOLVAULT_ENABLED", False)
    monkeypatch.setattr(tasks, "read_text_safe", lambda *a, **k: "")
    monkeypatch.setattr(tasks, "get_recent_fossils_for_operator",
                        lambda vol, op, n, cap: list(recent_blocks)[:n])
    monkeypatch.setattr(tasks, "keyword_retrieve_for_operator", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "semantic_retrieve", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "get_recent_checkpoints_for_operator", lambda *a, **k: [])
    monkeypatch.setattr(tasks, "get_recent_media_artifacts", lambda *a, **k: [])
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
        operator="CustomTester",
        result_data={
            "messages": messages,
            "operator": "CustomTester",
            "provider": provider,
            "model": model,
        },
    )
    task_db.save_task(task)
    tasks.process_chat_task(task)
    return task_db.get_task(task_id), recorded, captured["turns"]


_TEXT_MSGS = [{"role": "user", "content": "How does the Orchestrator work?"}]
_AUDIO_MSGS = [{"role": "user", "content": [
    {"type": "text", "text": "What does this recording say?"},
    {"type": "audio_url", "audio_url": {"url": "/ui/uploads/clip.mp3"}},
]}]


def test_worker_custom_dispatch_registry_default_model(custom_registry, monkeypatch):
    """provider 'custom' routes to call_custom; empty model resolves to the
    registry default (alias-qualified first discovered model — no hardcoded
    literal); the 4-tuple unpacks via _unpack_call so reasoning lands in the
    reasoning slot (snap_text) and NEVER in the user-facing reply."""
    from Orchestrator.models import TaskStatus
    final, recorded, turns = _run_chat_worker(
        monkeypatch, provider="custom", model="", task_id="t51-dispatch",
        messages=_TEXT_MSGS)

    assert final.status == TaskStatus.COMPLETED, final.result_data
    assert recorded["model"] == "lab::llama-3-8b"
    assert recorded["operator"] == "CustomTester"
    rd = final.result_data
    assert rd["reply"] == ANSWER_51
    assert REASONING_51 not in rd["reply"]
    assistant = next((t for t in turns if t.get("role") == "assistant"), None)
    assert assistant is not None
    assert "[REASONING]" in assistant["snap_text"]
    assert REASONING_51 in assistant["snap_text"]


def test_worker_custom_explicit_model_passes_through(custom_registry, monkeypatch):
    from Orchestrator.models import TaskStatus
    final, recorded, _ = _run_chat_worker(
        monkeypatch, provider="custom", model="lab::qwen-2",
        task_id="t51-explicit", messages=_TEXT_MSGS)
    assert final.status == TaskStatus.COMPLETED, final.result_data
    assert recorded["model"] == "lab::qwen-2"


def test_worker_custom_audio_skips_media_autoroute(custom_registry, monkeypatch):
    """has_audio + provider custom must NOT switch to google (a custom-only box
    has no Google key — the switch would hard-fail instead of replying)."""
    from Orchestrator.models import TaskStatus
    final, recorded, _ = _run_chat_worker(
        monkeypatch, provider="custom", model="", task_id="t51-audio",
        messages=_AUDIO_MSGS, forbidden_calls=("call_gemini",))
    assert final.status == TaskStatus.COMPLETED, final.result_data
    assert recorded["model"] == "lab::llama-3-8b"  # still call_custom
    assert final.result_data["reply"] == ANSWER_51


def test_worker_audio_autoroute_untouched_for_other_providers(custom_registry, monkeypatch):
    """The exemption is custom-ONLY: audio on any other provider still switches
    to google (byte-identical existing behavior)."""
    import Orchestrator.routes.chat_routes as cr
    from Orchestrator.models import TaskStatus

    hit = {}

    def fake_call_gemini(msgs, mdl, operator="?"):
        hit["model"] = mdl
        return (ANSWER_51,
                {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                [], "", [])

    monkeypatch.setattr(cr, "call_gemini", fake_call_gemini)
    final, _, _ = _run_chat_worker(
        monkeypatch, provider="xai", model="", task_id="t51-audio-xai",
        messages=_AUDIO_MSGS, forbidden_calls=("call_xai",))
    assert final.status == TaskStatus.COMPLETED, final.result_data
    assert "model" in hit  # google took the turn


# --- fossil window guard (non-stream inline block) ---------------------------------

def _big_blk(i: int, size: int = 10000) -> str:
    sid = f"SNAP-20260709-{i:04d}"
    body = "x" * (size - 60)
    return f"=== START SNAPSHOT — UTC 2026-07-09T00:00:00Z — {sid} ===\n{body}\n"


def test_worker_custom_fossil_guard_trims_oldest_first(custom_registry, monkeypatch, capsys):
    """provider custom + huge fossil corpus: the delivered fossil block obeys
    the STREAM guard's exact metric — estimate_tokens(assembled) <=
    window_guard_tokens(server) (lab = 16,384-token window -> 9,830 tokens;
    tokenization floor = 2 chars/token) — dropping OLDEST whole recent
    snapshots first, with the est/budget overage logged per drop."""
    from Orchestrator.models import TaskStatus
    from Orchestrator.tokenization import estimate_tokens
    blocks = [_big_blk(i) for i in range(1, 11)]  # oldest (0001) -> newest (0010)
    final, recorded, _ = _run_chat_worker(
        monkeypatch, provider="custom", model="", task_id="t51-guard",
        messages=_TEXT_MSGS, recent_blocks=blocks)

    assert final.status == TaskStatus.COMPLETED, final.result_data
    # msg_list = [core system, fossil context, *user messages]
    context = recorded["messages"][1]["content"]
    budget = cs.window_guard_tokens({"context_tokens": 16384})  # 9,830 tokens
    assert estimate_tokens(context) <= budget
    assert "SNAP-20260709-0010" in context  # newest survives
    assert "SNAP-20260709-0001" not in context  # oldest dropped whole
    out = capsys.readouterr().out
    assert "custom window guard dropped" in out
    assert "> budget" in out  # overage logged per drop (context_builder style)


def test_worker_custom_fossil_guard_uses_per_model_window(per_model_registry, monkeypatch, capsys):
    """Empty model on the llama-swap registry defaults to 'm-small' (first
    discovered) whose LEARNED 16K window must size the guard (9,830 tokens) —
    the server-wide 131,072 (78,643 budget) would deliver the whole ~50k-token
    corpus and get the turn 400'd far-end."""
    from Orchestrator.models import TaskStatus
    from Orchestrator.tokenization import estimate_tokens
    blocks = [_big_blk(i) for i in range(1, 11)]  # ~100k chars ≈ 50k tokens
    final, recorded, _ = _run_chat_worker(
        monkeypatch, provider="custom", model="", task_id="t-permodel-guard",
        messages=_TEXT_MSGS, recent_blocks=blocks)

    assert final.status == TaskStatus.COMPLETED, final.result_data
    assert recorded["model"] == "swap::m-small"
    context = recorded["messages"][1]["content"]
    assert estimate_tokens(context) <= 9830  # per-model budget, NOT 78,643
    assert "SNAP-20260709-0001" not in context  # oldest dropped whole
    assert "custom window guard dropped" in capsys.readouterr().out


def test_worker_custom_fossil_guard_model_not_in_map_server_window(per_model_registry, monkeypatch):
    """'m-big' has no learned window: the server-wide 78,643-token budget
    holds and the ~50k-token corpus is delivered WHOLE (nothing dropped)."""
    from Orchestrator.models import TaskStatus
    blocks = [_big_blk(i) for i in range(1, 11)]
    final, recorded, _ = _run_chat_worker(
        monkeypatch, provider="custom", model="swap::m-big",
        task_id="t-permodel-big", messages=_TEXT_MSGS, recent_blocks=blocks)

    assert final.status == TaskStatus.COMPLETED, final.result_data
    context = recorded["messages"][1]["content"]
    for i in range(1, 11):
        assert f"SNAP-20260709-{i:04d}" in context  # ALL delivered


def test_worker_fossil_guard_other_providers_untouched(custom_registry, monkeypatch):
    """Same huge corpus on a non-custom provider: nothing is trimmed (the
    cloud non-stream path stays cap-free, WI-10)."""
    from Orchestrator.models import TaskStatus
    blocks = [_big_blk(i) for i in range(1, 11)]
    final, recorded, _ = _run_chat_worker(
        monkeypatch, provider="anthropic", model="claude-test",
        task_id="t51-guard-anthropic", messages=_TEXT_MSGS, recent_blocks=blocks)

    assert final.status == TaskStatus.COMPLETED, final.result_data
    context = recorded["messages"][1]["content"]
    for i in range(1, 11):
        assert f"SNAP-20260709-{i:04d}" in context  # ALL delivered, none dropped


# --- unknown provider contract ------------------------------------------------------

def test_worker_unknown_provider_still_raises(custom_registry, monkeypatch):
    """Genuinely unknown provider strings keep the 'unknown provider' failure
    contract — accepting 'custom' must not open the floodgates."""
    from Orchestrator.models import TaskStatus
    final, _, _ = _run_chat_worker(
        monkeypatch, provider="mystery", model="", task_id="t51-unknown",
        messages=_TEXT_MSGS)
    assert final.status == TaskStatus.FAILED
    assert "unknown provider: mystery" in (final.result_data or {}).get("error", "")


# =================================================================================
# Task 5.2: cron scheduler — executor default-model resolution for "custom"
# =================================================================================
# _provider_default_model held only static config strings and .get(provider,
# GEMINI_MODEL_DEFAULT)-fell-through for anything unknown — a custom cron job
# with an Auto model would SILENTLY run on Gemini. Custom's default lives in
# the customer's server registry (can change between fires), so it is resolved
# lazily at fire time: first enabled server's first discovered model,
# alias-qualified (the exact /chat/stream + call_custom Auto semantics). An
# unusable registry RAISES RuntimeError — the manager's _attempt_once records
# it as an error history row (a failed run, with the reason) — NEVER Gemini.


def test_cron_provider_default_custom_resolves_registry_default(custom_registry):
    from Orchestrator.scheduler.executor import _provider_default_model
    assert _provider_default_model("custom") == "lab::llama-3-8b"


def test_cron_resolve_model_name_custom_auto_and_bare_word(custom_registry):
    """Auto (empty model) + provider custom resolves the registry default; the
    bare provider word 'custom' behaves like every other bare alias; a specific
    qualified id passes through verbatim."""
    from Orchestrator.scheduler.executor import _resolve_model_name
    assert _resolve_model_name("", "custom") == "lab::llama-3-8b"
    assert _resolve_model_name("custom", "custom") == "lab::llama-3-8b"
    assert _resolve_model_name("lab::qwen-2", "custom") == "lab::qwen-2"


def test_cron_custom_no_servers_raises_never_gemini(tmp_path, monkeypatch):
    """Empty registry: RAISE with the customer-facing wizard message — the
    silent Gemini fallthrough is dead for custom."""
    from Orchestrator.scheduler.executor import _resolve_model_name
    monkeypatch.setattr(cs, "REGISTRY_PATH", str(tmp_path / "absent.json"))
    with pytest.raises(RuntimeError, match="No custom model servers configured"):
        _resolve_model_name("", "custom")


def test_cron_custom_no_discovered_models_raises(tmp_path, monkeypatch):
    """Enabled server with zero discovered models: same failure contract as
    call_custom — raise naming the server, never fall back to Gemini."""
    from Orchestrator.scheduler.executor import _provider_default_model
    reg = tmp_path / "custom_models.json"
    reg.write_text(json.dumps({"version": 1, "servers": [{
        "id": "srv-nomodels", "alias": "bare",
        "base_url": "http://127.0.0.1:8082/v1", "api_key": "",
        "context_tokens": 32768, "enabled": True,
        "added_at": "2026-07-09T00:00:00+00:00", "validated_at": None,
        "last_models": [],
    }]}))
    monkeypatch.setattr(cs, "REGISTRY_PATH", str(reg))
    with pytest.raises(RuntimeError, match="no discovered models"):
        _provider_default_model("custom")


def test_cron_other_provider_defaults_untouched():
    """Every other provider (and the unknown-provider Gemini fallthrough)
    keeps today's static-map behavior byte-identical."""
    from Orchestrator.config import GEMINI_MODEL_DEFAULT, ANTHROPIC_MODEL_DEFAULT
    from Orchestrator.scheduler.executor import _provider_default_model
    assert _provider_default_model("google") == GEMINI_MODEL_DEFAULT
    assert _provider_default_model("anthropic") == ANTHROPIC_MODEL_DEFAULT
    assert _provider_default_model("mystery") == GEMINI_MODEL_DEFAULT


def test_cron_model_to_provider_qualified_custom_id():
    """An alias-qualified custom id ('alias::model') must derive provider
    'custom' — only custom ids ever contain '::', and the google fallthrough
    would run the job on the wrong provider (blank-provider legacy path +
    create_cron_job tool validation both consume this)."""
    from Orchestrator.scheduler.executor import _model_to_provider
    assert _model_to_provider("lab::qwen-2") == "custom"
    # The '::' check runs FIRST: a vendor substring inside the alias or bare
    # model half (e.g. a CU-looking model served from a custom box) must not
    # win the derivation.
    assert _model_to_provider("mybox::computer-use-preview") == "custom"
    # No regression on the existing derivations.
    assert _model_to_provider("claude-opus-4-8") == "anthropic"
    assert _model_to_provider("gemini-3.1-pro-preview") == "google"
    assert _model_to_provider("computer-use-preview") == "computer-use"
