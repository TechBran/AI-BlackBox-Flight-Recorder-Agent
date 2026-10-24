"""Audit rank 46 (O17-toolvault-runtime-02/-03/-05): tool injection does its
expensive work once per turn.

A chat turn injects twice with the same prompt: build_streaming_context (or the
/chat worker) for the system-prompt tool instructions, then _get_tools for the
provider's tools array. Each pass embedded the prompt through the provider
(a network round trip, with retries when the provider is down), re-parsed the
5.8 MB embeddings.json and re-resolved every selected schema twice. Now:

  * the prompt's query vector is reused for the same text under the same
    embedding model (a failure only briefly, so a down provider is not paid
    for twice in one turn, yet a recovery is seen within seconds);
  * the parsed store is cached until the file changes, with each stored
    vector's norm computed once;
  * each selected schema is resolved once per injection.

Hermetic: tmp registry + store, the shared embed layer stubbed.
"""

import json
import math
import random

import pytest

from Orchestrator.embeddings import search as shared_search
from Orchestrator.toolvault import embeddings, injector, registry, resolvers

VEC = [1.0, 0.0]


def _schema(name, tier=2, description="A tool."):
    return {
        "name": name, "description": description, "category": "communication",
        "groups": ["chat"], "tier": tier,
        "parameters": {"type": "object", "properties": {
            "q": {"type": "string", "description": "a query"}}},
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = tmp_path / "tools"
    for s in (_schema("core_tool", tier=1, description="Core always-on capability."),
              _schema("send_sms", description="send a text message via sms"),
              _schema("generate_image", description="create an image from a prompt")):
        (d / s["name"]).mkdir(parents=True)
        (d / s["name"] / "schema.json").write_text(json.dumps(s))
    monkeypatch.setattr(registry, "TOOLS_DIR", d)
    registry.invalidate_cache()

    store_path = tmp_path / "embeddings.json"
    store_path.write_text(json.dumps({
        "send_sms": {"hash": "h", "model": "m", "vector": [1.0, 0.0]},
        "generate_image": {"hash": "h", "model": "m", "vector": [0.0, 1.0]},
    }))
    monkeypatch.setattr(embeddings, "EMBEDDINGS_PATH", store_path)
    monkeypatch.setattr(resolvers, "_list_operators", lambda ctx: ["system"])
    monkeypatch.setattr(embeddings, "_active_slug", lambda: "test-slug")

    calls = []

    def fake_embed(text, purpose="document", slug=None):
        calls.append((text, purpose))
        return list(VEC)

    monkeypatch.setattr(shared_search, "generate_embedding_sync", fake_embed)
    yield {"calls": calls, "store_path": store_path}
    registry.invalidate_cache()


def _query_calls(env):
    return [c for c in env["calls"] if c[1] == "query"]


# ---------------------------------------------------------------------------
# The prompt is embedded once per turn
# ---------------------------------------------------------------------------

def test_one_turns_two_injections_embed_the_prompt_once(env):
    prompt = "please send a text to the team"
    # build_streaming_context: the tool instructions for the system prompt.
    _, instructions = injector.inject_for_prompt(prompt, "openai")
    # _get_tools: the provider's tools array, same prompt and group.
    tools, _ = injector.inject_for_prompt(prompt, "openai", "chat")
    assert len(_query_calls(env)) == 1, env["calls"]
    assert "send_sms" in instructions
    assert any(t["function"]["name"] == "send_sms" for t in tools)


def test_another_prompt_or_model_embeds_again(env, monkeypatch):
    injector.inject_for_prompt("send a text", "openai")
    injector.inject_for_prompt("make a picture", "openai")
    assert len(_query_calls(env)) == 2
    monkeypatch.setattr(embeddings, "_active_slug", lambda: "other-slug")
    injector.inject_for_prompt("send a text", "openai")
    assert len(_query_calls(env)) == 3


def test_a_failed_embed_is_reused_only_briefly(env, monkeypatch):
    outcomes = [None, list(VEC)]

    def flaky(text, purpose="document", slug=None):
        env["calls"].append((text, purpose))
        return outcomes.pop(0)

    monkeypatch.setattr(shared_search, "generate_embedding_sync", flaky)
    clock = [1000.0]
    monkeypatch.setattr(embeddings, "_monotonic", lambda: clock[0])

    assert embeddings.embed_query("q") is None
    assert embeddings.embed_query("q") is None      # same turn: not retried
    assert len(_query_calls(env)) == 1
    clock[0] += embeddings.QUERY_VEC_FAIL_TTL_S + 1
    assert embeddings.embed_query("q") == VEC         # provider back: re-embedded
    assert len(_query_calls(env)) == 2


def test_a_reused_vector_expires(env, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(embeddings, "_monotonic", lambda: clock[0])
    embeddings.embed_query("q")
    embeddings.embed_query("q")
    assert len(_query_calls(env)) == 1
    clock[0] += embeddings.QUERY_VEC_TTL_S + 1
    embeddings.embed_query("q")
    assert len(_query_calls(env)) == 2


def test_a_swapped_embedder_is_never_served_an_old_vector(env, monkeypatch):
    embeddings.embed_query("q")
    monkeypatch.setattr(shared_search, "generate_embedding_sync",
                        lambda text, purpose="document", slug=None: [0.0, 1.0])
    assert embeddings.embed_query("q") == [0.0, 1.0]


def test_a_freed_embedder_address_is_never_mistaken_for_a_new_one(env):
    """Keyed on id() alone, a double freed after use and a new one allocated at
    the same address shared a memo entry: the new one got the old vector."""
    stale = []
    for i in range(100):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(shared_search, "generate_embedding_sync",
                       lambda text, purpose="document", slug=None, i=i: [float(i), 1.0])
            got = embeddings.embed_query("same prompt")
        if got != [float(i), 1.0]:
            stale.append((i, got))
    assert not stale, f"{len(stale)} of 100 embeds served another double's vector: {stale[:3]}"


def test_the_first_query_after_a_restart_is_reused(env, monkeypatch):
    """The embed itself creates the slug's provider instance on a box's first
    query, so the vector must be stored under the instance that made it, or
    the same turn's second injection embeds again."""
    from Orchestrator.embeddings import providers
    monkeypatch.setattr(providers, "_instances", {})
    embeddings._query_vecs.clear()
    calls = []

    def first_embed(text, purpose="document", slug=None):
        calls.append(text)
        providers._instances.setdefault("test-slug", object())  # what get_provider does
        return list(VEC)

    monkeypatch.setattr(shared_search, "generate_embedding_sync", first_embed)
    assert embeddings.embed_query("send a text") == VEC
    assert embeddings.embed_query("send a text") == VEC
    assert calls == ["send a text"]


# ---------------------------------------------------------------------------
# The parsed store is cached until the file changes
# ---------------------------------------------------------------------------

def test_store_is_parsed_once_until_the_file_changes(env):
    a = embeddings.load_embeddings_store()
    b = embeddings.load_embeddings_store()
    assert a == b
    assert a["send_sms"] is b["send_sms"], "the 5.8 MB store was re-parsed"

    a["injected"] = {"vector": [9.0]}             # a caller's own copy
    assert "injected" not in embeddings.load_embeddings_store()

    env["store_path"].write_text(json.dumps({
        "only_tool": {"hash": "h", "model": "m", "vector": [1.0, 1.0, 1.0]}}))
    assert list(embeddings.load_embeddings_store()) == ["only_tool"]


def test_saving_the_store_is_seen_by_the_next_load(env):
    embeddings.load_embeddings_store()
    embeddings.save_embeddings_store({"x": {"hash": "h", "model": "m", "vector": [1.0]}})
    assert list(embeddings.load_embeddings_store()) == ["x"]


def test_missing_and_corrupt_stores_stay_empty(env):
    env["store_path"].write_text("{not json")
    assert embeddings.load_embeddings_store() == {}
    env["store_path"].unlink()
    assert embeddings.load_embeddings_store() == {}


def test_cached_norm_scores_match_plain_cosine():
    rng = random.Random(7)
    store = {f"t{i}": {"vector": [rng.uniform(-1, 1) for _ in range(16)]} for i in range(20)}
    store["zero"] = {"vector": [0.0] * 16}
    store["short"] = {"vector": [1.0, 2.0]}         # mid-migration dims mismatch
    store["empty"] = {"vector": []}
    q = [rng.uniform(-1, 1) for _ in range(16)]
    got = dict(embeddings.semantic_search_store(q, store, limit=100))
    for name, entry in store.items():
        if not entry["vector"]:
            assert name not in got
            continue
        assert math.isclose(got[name], embeddings.cosine_similarity(q, entry["vector"]),
                            rel_tol=1e-9, abs_tol=1e-12), name


# ---------------------------------------------------------------------------
# Each selected schema is resolved once
# ---------------------------------------------------------------------------

def test_each_selected_schema_is_resolved_once(env, monkeypatch):
    seen = []
    real = injector.resolve_schema

    def counting(schema, ctx=None):
        seen.append(schema.get("name"))
        return real(schema, ctx)

    monkeypatch.setattr(injector, "resolve_schema", counting)
    tools, instructions = injector.inject_for_prompt("please send a text", "openai")
    names = [t["function"]["name"] for t in tools if t["function"]["name"] != "toolvault"]
    assert sorted(seen) == sorted(names), seen
    assert "Tool: send_sms" in instructions
