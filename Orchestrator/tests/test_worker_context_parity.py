"""The non-stream worker delivers the same fossil context as /chat/stream.

Audit O04-tasks-retrieval-04 / rank 68: process_chat_task serves /chat, every
cron job that posts there and every inbound SMS, and it hand-built its own
copy of the four-section context. It had drifted from the shared builder:
raw snapshots with their ~1,000-char bookkeeping envelopes instead of the
fenced, numbered, body-only rendering; keyword before semantic (so the drop
ladder's "section order is drop precedence" was false); a window guard only
for provider "custom"; no Flight Recorder pins; and different config
fallbacks. It now calls context_builder.build_fossil_context, so for the same
inputs the worker's fossil message IS the builder's output.
"""
from __future__ import annotations

import pytest

from Orchestrator import config


def _snap(sid: str, op: str, user: str) -> str:
    return (f"=== START SNAPSHOT — UTC 2026-10-01T00:00:00Z — {sid} (7.1) ===\n"
            f"OPERATOR: {op}\nTYPE: normal\nCROSS-FILE BEACON: bookkeeping\n"
            f"VOLUME TRACKER: COUNT=1\n"
            f"Raw Session Log\n[1] 2026-10-01T00:00:00Z operator={op} user: {user}\n"
            f"=== END SNAPSHOT — {sid} — UTC 2026-10-01T00:00:00Z ===")


RECENT = [_snap(f"SNAP-20261001-{i}", "ParityOp", f"recent {i}") for i in (1, 2, 3)]
KEYWORD = [_snap("SNAP-20261001-2", "ParityOp", "dupe of recent"),
           _snap("SNAP-20261001-7", "ParityOp", "keyword hit")]
SEMANTIC = [_snap("SNAP-20261001-8", "ParityOp", "semantic hit")]
CHECKPOINT = [_snap("SNAP-20261001-9", "ParityOp", "[CHECKPOINT] summary")]


@pytest.fixture
def retrieval(monkeypatch):
    """Fixed retriever outputs at the shared builder's seams (no ledger, no
    embedding provider)."""
    from Orchestrator import context_builder as cb
    monkeypatch.setattr(cb, "get_recent_fossils_for_operator", lambda v, op, n, cap: RECENT[-n:])
    monkeypatch.setattr(cb, "keyword_retrieve_for_operator", lambda v, q, k, op: KEYWORD[:k])
    monkeypatch.setattr(cb, "semantic_retrieve", lambda q, **kw: SEMANTIC)
    monkeypatch.setattr(cb, "get_recent_checkpoints_for_operator", lambda v, op, count=1: CHECKPOINT[:count])
    monkeypatch.setattr(cb, "get_recent_media_artifacts", lambda op, limit=10: [])


def _run_worker(monkeypatch, *, provider, model="", messages=None, task_id):
    import Orchestrator.tasks as tasks
    import Orchestrator.routes.chat_routes as cr
    from Orchestrator.models import Task, TaskStatus, TaskType, task_db
    from Orchestrator.volume import now_utc_iso

    seen = {"builder_calls": []}
    real_build = tasks.build_fossil_context

    def spy_build(*a, **k):
        out = real_build(*a, **k)
        seen["builder_calls"].append((a, k, out))
        return out

    def fake_call(name):
        def call(msgs, mdl, operator="?"):
            seen.update(call=name, messages=msgs, model=mdl)
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            if name == "openai":
                return "answer", usage
            return ("answer", usage, [], "", []) if name == "gemini" else ("answer", usage, "", [])
        return call

    monkeypatch.setattr(tasks, "build_fossil_context", spy_build)
    for name in ("anthropic", "gemini", "openai", "xai"):
        monkeypatch.setattr(cr, f"call_{name}", fake_call(name))
    monkeypatch.setattr(tasks, "TOOLVAULT_ENABLED", False)
    monkeypatch.setattr(tasks, "AUTO_ENABLE", False)
    monkeypatch.setattr(tasks, "should_create_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "perform_mint", lambda *a, **k: {"snap_id": "SNAP-TEST"})
    monkeypatch.setattr(tasks, "save_operator_state", lambda *a, **k: None)

    task = Task(task_id=task_id, task_type=TaskType.CHAT, status=TaskStatus.PENDING,
                created_at=now_utc_iso(), updated_at=now_utc_iso(), operator="ParityOp",
                result_data={"messages": messages or [{"role": "user", "content": "keyword question"}],
                             "operator": "ParityOp", "provider": provider, "model": model})
    task_db.save_task(task)
    tasks.process_chat_task(task)
    final = task_db.get_task(task_id)
    assert final.status == TaskStatus.COMPLETED, final.result_data
    return seen


def test_worker_fossil_message_is_the_shared_builders_output(retrieval, monkeypatch):
    from Orchestrator.context_builder import build_fossil_context
    seen = _run_worker(monkeypatch, provider="anthropic", task_id="parity-anthropic")

    expected, provenance = build_fossil_context(
        "keyword question", "ParityOp", log_prefix="[CONTEXT]", provider="anthropic")
    # msg_list = [core system prompt, fossil context, *conversation]
    assert seen["messages"][1] == {"role": "system", "content": expected}
    assert provenance == {"recent": ["SNAP-20261001-1", "SNAP-20261001-2", "SNAP-20261001-3"],
                          "keyword": ["SNAP-20261001-7"], "semantic": ["SNAP-20261001-8"],
                          "checkpoint": ["SNAP-20261001-9"]}


def test_worker_context_is_fenced_body_only_and_in_drop_order(retrieval, monkeypatch):
    seen = _run_worker(monkeypatch, provider="anthropic", task_id="parity-shape")
    ctx = seen["messages"][1]["content"]
    assert "=== RECENT SNAPSHOTS (3 total) ===" in ctx
    assert "=== Recent #2: SNAP-20261001-2 ===" in ctx
    assert "CROSS-FILE BEACON" not in ctx and "VOLUME TRACKER" not in ctx
    assert "Recent snapshots:\n" not in ctx
    order = [ctx.index(h) for h in ("=== CHECKPOINT SNAPSHOTS", "=== RECENT SNAPSHOTS",
                                    "=== SEMANTIC SNAPSHOTS", "=== KEYWORD-MATCHED SNAPSHOTS")]
    assert order == sorted(order)


@pytest.mark.parametrize("provider, model, routed", [
    ("anthropic", "", "anthropic"),
    ("gemini", "", "google"),
    ("computer-use", "", "anthropic"),   # CU is stream-only; the worker runs Anthropic
])
def test_worker_builds_context_for_the_provider_that_runs(retrieval, monkeypatch, provider, model, routed):
    seen = _run_worker(monkeypatch, provider=provider, model=model, task_id=f"parity-{provider}")
    (args, kwargs, _out), = seen["builder_calls"]
    assert kwargs["provider"] == routed
    assert kwargs["window_guard_tokens"] is None


def test_worker_audio_turn_builds_context_for_google(retrieval, monkeypatch):
    audio = [{"role": "user", "content": [
        {"type": "text", "text": "keyword question"},
        {"type": "audio_url", "audio_url": {"url": "/ui/uploads/clip.wav"}}]}]
    seen = _run_worker(monkeypatch, provider="openai", messages=audio, task_id="parity-audio")
    (args, kwargs, _out), = seen["builder_calls"]
    assert seen["call"] == "gemini"
    assert seen["model"] == config.resolve_default_model("google", None)
    assert kwargs["provider"] == "google"
    assert args[0] == "keyword question"


def test_worker_guards_every_cloud_window(retrieval, monkeypatch):
    """The cloud window guard now runs on the worker too (it ran only for
    custom): a context over the provider's budget drops whole snapshots -
    here all of them, so no fossil message is sent at all."""
    from Orchestrator import context_builder as cb
    monkeypatch.setitem(cb.PROVIDER_WINDOW_GUARD_TOKENS, "anthropic", 1)
    seen = _run_worker(monkeypatch, provider="anthropic", task_id="parity-guard")
    (_a, _k, (ctx, provenance)), = seen["builder_calls"]
    assert ctx == "" and not any(provenance.values())
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]
