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
                        window_budget_chars=None: [])
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
        return list(messages), {"recent": [], "keyword": [], "semantic": [], "checkpoint": []}

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
