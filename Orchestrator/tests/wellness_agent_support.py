"""Shared helpers for the agent-access tests (wave 2, not a test module).

    def test_x(agent_box):                   # OPERATOR enrolled, continuous_120 pushed
        with stamped():                      # a signed Android turn on Anthropic (ticked)
            r = run_tool("wellness_summary", {"period": "last_night"})

agent_box is a pytest fixture: import it into the test module. Everything is
hermetic: a temp box (isolated_stores), the fixture store, a pinned clock,
no embedding provider, no network.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from Orchestrator.tests.wellness_support import OPERATOR, enrolled_fixture, fixtures, roster
from Orchestrator.wellness import access, turn as turn_mod

FX = fixtures()
OTHER = "other-human"
SENTINELS = FX.SENTINEL_STRINGS
WELLNESS_TOOLS = ("wellness_history", "wellness_series", "wellness_sessions", "wellness_sources",
                  "wellness_summary")
LAN_ALIAS = "lan-box"
LAN_MODEL = f"{LAN_ALIAS}::fake-model"


@pytest.fixture
def agent_box(isolated_stores, tmp_path, monkeypatch):
    """OPERATOR enrolled with Anthropic ticked and the 120-day fixture
    ingested; two humans on the roster (credentialed mode); the tools'
    clock pinned to the fixture's push time; no embedding provider."""
    roster(monkeypatch, OPERATOR, OTHER, "Flight Recorder")
    rec, st, data = enrolled_fixture(tmp_path, monkeypatch, providers_allowed=["anthropic"])
    monkeypatch.setattr(access, "_now_ms", lambda: FX.now_ms())
    no_embeddings(monkeypatch)
    return {"record": rec, "store": st, "data": data}


def no_embeddings(monkeypatch, semantic=None):
    """No embedding provider; semantic discovery returns ``semantic(names)``
    (default: every wellness tool in the pool, as for a health question)."""
    from Orchestrator.toolvault import embeddings, injector
    monkeypatch.setattr(embeddings, "embed_query", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "load_embeddings_store", lambda *a, **k: {})
    monkeypatch.setattr(injector, "load_embeddings_store", lambda *a, **k: {})

    def search(query, descriptions, store, limit=8, threshold=0.0):
        pick = semantic or (lambda names: [n for n in names if n.startswith("wellness_")])
        return [(n, 0.9) for n in pick(sorted(descriptions))][:limit]

    monkeypatch.setattr(injector, "hybrid_search_store", search)
    monkeypatch.setattr(embeddings, "hybrid_search_store", search)


def make_turn(**kw) -> turn_mod.WellnessTurn:
    base = {"operator": OPERATOR, "surface": turn_mod.SURFACE_ANDROID, "credential_ok": True,
            "surface_id": "en_0f1c0f1c", "provider": "anthropic", "model": "claude-test",
            "turn_key": turn_mod.new_turn_key()}
    base.update(kw)
    return turn_mod.WellnessTurn(**base)


@contextlib.contextmanager
def stamped(turn: turn_mod.WellnessTurn = None, **kw):
    t = turn if turn is not None else make_turn(**kw)
    token = turn_mod.stamp(t)
    try:
        yield t
    finally:
        turn_mod.clear(token)


def run_tool(name: str, params: dict, *, operator: str = OPERATOR, caller=None):
    from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor
    return asyncio.run(BlackBoxToolExecutor(operator=operator, caller=caller).execute(name, params))


def tool_names(tools) -> set:
    from Orchestrator.routes.chat.providers import _extract_tool_names
    out = set(_extract_tool_names(tools))
    for t in tools or []:
        for fd in (t.get("function_declarations") or []) if isinstance(t, dict) else []:
            out.add(fd.get("name"))
    return out


def set_enabled(value: bool) -> None:
    """[wellness] enabled in the test box's config.ini (read fresh)."""
    from Orchestrator.utils.config_ini import update_config_ini

    def edit(parser):
        if not parser.has_section("wellness"):
            parser.add_section("wellness")
        parser.set("wellness", "enabled", "true" if value else "false")
    update_config_ini(edit)


def custom_server(alias: str = LAN_ALIAS, base_url: str = "http://127.0.0.1:8099/v1", *,
                  context_tokens: int = 32768, models=("fake-model",)) -> None:
    """One enabled custom model server in the box's registry."""
    from Orchestrator.onboarding import custom_servers
    data = {"servers": [{"id": f"srv-{alias}", "alias": alias, "base_url": base_url,
                         "api_key": "", "context_tokens": context_tokens, "model_context": {},
                         "enabled": True, "added_at": "2026-10-01T00:00:00+00:00",
                         "validated_at": "2026-10-01T00:00:00+00:00",
                         "last_models": list(models), "model_modalities": {}, "audio": {}}]}
    custom_servers.REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    custom_servers.REGISTRY_PATH.write_text(json.dumps(data))


def edit_record(**fields) -> None:
    from Orchestrator.wellness import enroll
    with enroll.editing() as data:
        rec = dict(data[OPERATOR])
        rec.update(fields)
        data[OPERATOR] = enroll._normalize(rec)


def stub_retrieval(monkeypatch) -> None:
    """No ledger reads in the context build (the shared builder's seams)."""
    from Orchestrator import context_builder as cb
    monkeypatch.setattr(cb, "get_recent_fossils_for_operator", lambda *a, **k: [])
    monkeypatch.setattr(cb, "keyword_retrieve_for_operator", lambda *a, **k: [])
    monkeypatch.setattr(cb, "semantic_retrieve", lambda *a, **k: [])
    monkeypatch.setattr(cb, "get_recent_checkpoints_for_operator", lambda *a, **k: [])
    monkeypatch.setattr(cb, "get_recent_media_artifacts", lambda *a, **k: [])


def sentinel_hits(text: str) -> list:
    return [s for s in SENTINELS if s in (text or "")]
