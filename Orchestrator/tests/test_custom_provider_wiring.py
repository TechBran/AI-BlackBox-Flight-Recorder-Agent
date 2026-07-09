"""Custom-model-providers Task 4.2: `custom` provider wiring.

Three seams, all additive:
  1. ToolVault injector — explicit "custom" entries in PROVIDER_FORMATS /
     PROVIDER_DEFAULT_GROUP (unknown providers already default to
     openai_rest/chat; the explicit entries keep the [TOOLVAULT-INJECT] log
     truthful and protect against future default changes).
  2. context_builder — PROVIDER_WINDOW_GUARD_TOKENS["custom"] = 19200
     (0.6 x 32768 default context_tokens) as the no-override floor, plus a
     NEW `window_guard_tokens` override kwarg on build_fossil_context so
     Task 4.3 can thread the resolved server's real context_tokens. Without
     the floor, an unknown provider gets the 240K default and the assembler
     happily overflows a 32K-window llama.cpp server on turn one.
  3. chat_routes — "custom" in _get_tools' openai-format tuple;
     build_streaming_context threads window_guard_tokens through.

All tests hermetic (monkeypatched retrievers/system-prompt, no live service).
"""

from Orchestrator import context_builder as cb


# --- injector entries ----------------------------------------------------------

def test_injector_formats_custom_as_openai_rest():
    from Orchestrator.toolvault.injector import PROVIDER_FORMATS, PROVIDER_DEFAULT_GROUP
    assert PROVIDER_FORMATS["custom"] == "openai_rest"
    assert PROVIDER_DEFAULT_GROUP["custom"] == "chat"


# --- window-guard floor --------------------------------------------------------

def test_provider_window_guard_custom_floor():
    """`custom` gets an explicit 19,200-token floor (0.6 x 32768 default
    context_tokens) — NEVER the 240K unknown-provider default."""
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
