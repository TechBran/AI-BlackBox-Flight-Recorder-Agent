"""Tests for the ToolVault v2 injector (Tasks 3.1 + 3.2).

The v2 injector selects + formats tools from the module ``registry`` +
``resolvers`` + the ``embeddings.json`` store, replacing the old byte-offset
volume/manifest path. It also builds the human-readable AVAILABLE TOOLS section
for the system prompt (``build_tool_instructions``).

Tests are hermetic:
  * ``registry.TOOLS_DIR`` points at a ``tmp_path`` of schema.json modules.
  * ``embeddings.load_embeddings_store`` reads a tmp store JSON.
  * ``embeddings.embed_query`` is monkeypatched (no network).
  * ``resolvers._list_operators`` is monkeypatched (no backend).
"""

import json

import pytest

from Orchestrator.toolvault import registry, embeddings, resolvers
from Orchestrator.toolvault import injector


# A 2-D vector space makes cosine ordering trivial to reason about.
# query_vec points along +x; tools aligned with +x score higher.
QUERY_VEC = [1.0, 0.0]


def _schema(name, *, groups=("chat",), tier=2, description="A tool.",
            properties=None, required=None, example=None, notes=None):
    props = properties if properties is not None else {
        "q": {"type": "string", "description": "a query"},
    }
    schema = {
        "name": name,
        "description": description,
        "category": "communication",
        "groups": list(groups),
        "tier": tier,
        "parameters": {
            "type": "object",
            "properties": props,
        },
    }
    if required is not None:
        schema["parameters"]["required"] = required
    if example is not None:
        schema["example"] = example
    if notes is not None:
        schema["notes"] = notes
    return schema


def _write(tools_dir, schema):
    folder = tools_dir / schema["name"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "schema.json").write_text(json.dumps(schema))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Build a hermetic registry + embeddings store + stubbed network/backend.

    Modules:
      * core_tool       — tier 1, group chat (always injected)
      * send_sms        — tier 2, group chat, aligned with +x (semantic match)
      * generate_image  — tier 2, group chat, orthogonal (no semantic match)
      * realtime_only   — tier 1, group realtime (NOT in chat group)
      * realtime_search — tier 2, group realtime (NOT chat), aligned +x vector
      * internal_seek   — tier 3, group chat, aligned +x vector
      * pick_operator   — tier 2, group chat, x-source: operators property
    """
    d = tmp_path / "tools"
    d.mkdir()

    _write(d, _schema("core_tool", groups=("chat",), tier=1,
                       description="Core always-on capability."))
    _write(d, _schema("send_sms", groups=("chat",), tier=2,
                       description="send a text message via sms",
                       example="send_sms(to='123', body='hi')",
                       notes="US numbers only."))
    _write(d, _schema("generate_image", groups=("chat",), tier=2,
                       description="create an image from a prompt"))
    _write(d, _schema("realtime_only", groups=("realtime",), tier=1,
                       description="A realtime-only tool."))
    # Tier-2 tool in the realtime group (NOT chat) with a +x vector — proves
    # cross-group semantic discovery into a chat request.
    _write(d, _schema("realtime_search", groups=("realtime",), tier=2,
                       description="search realtime stream data"))
    # Tier-3 internal tool with a +x vector — proves tier-3 surfaces via
    # semantic search even though it's never an always-on baseline tool.
    _write(d, _schema("internal_seek", groups=("chat",), tier=3,
                       description="seek snapshot direct internal"))
    _write(d, _schema(
        "pick_operator", groups=("chat",), tier=2,
        description="do something for an operator",
        properties={
            "operator": {
                "type": "string",
                "description": "which operator",
                "x-source": "operators",
            },
        },
    ))

    monkeypatch.setattr(registry, "TOOLS_DIR", d)
    registry.invalidate_cache()

    # Embeddings store: send_sms / realtime_search / internal_seek aligned with
    # the query (+x); generate_image / pick_operator orthogonal.
    store = {
        "send_sms": {"hash": "h", "model": "m", "vector": [1.0, 0.0]},
        "generate_image": {"hash": "h", "model": "m", "vector": [0.0, 1.0]},
        "realtime_search": {"hash": "h", "model": "m", "vector": [1.0, 0.0]},
        "internal_seek": {"hash": "h", "model": "m", "vector": [1.0, 0.0]},
        "pick_operator": {"hash": "h", "model": "m", "vector": [0.0, 1.0]},
    }
    store_path = tmp_path / "embeddings.json"
    store_path.write_text(json.dumps(store))
    monkeypatch.setattr(embeddings, "EMBEDDINGS_PATH", store_path)

    # No network: query embeds to +x.
    monkeypatch.setattr(embeddings, "embed_query", lambda q: list(QUERY_VEC))
    # No backend: fixed operator list.
    monkeypatch.setattr(resolvers, "_list_operators", lambda ctx: ["Brandon", "system"])

    yield d
    registry.invalidate_cache()


# ---------------------------------------------------------------------------
# Selection: meta + tier 1 always present
# ---------------------------------------------------------------------------

def test_meta_and_tier1_always_present_even_empty_prompt(env):
    names = [n for n, _ in injector.get_injected_tool_names("", group="chat")]
    assert names[0] == "toolvault"
    assert "core_tool" in names
    # No prompt → no semantic tier-2 selection.
    assert "send_sms" not in names


def test_meta_and_tier1_present_with_prompt(env):
    names = [n for n, _ in injector.get_injected_tool_names(
        "totally unrelated gibberish zzz", group="chat")]
    assert "toolvault" in names
    assert "core_tool" in names


# ---------------------------------------------------------------------------
# Tier 2 semantic selection: threshold + max
# ---------------------------------------------------------------------------

def test_tier2_semantic_match_selected(env):
    pairs = injector.get_injected_tool_names(
        "send sms text message", group="chat", similarity_threshold=0.1)
    names = [n for n, _ in pairs]
    assert "send_sms" in names
    # generate_image is orthogonal + no keyword overlap → excluded.
    assert "generate_image" not in names
    reason = dict(pairs)["send_sms"]
    assert reason.startswith("semantic(")


def test_tier2_threshold_excludes_low_scores(env):
    # generate_image is orthogonal to the query vector (semantic 0) and shares
    # no keywords with this prompt → combined score 0, dropped by any positive
    # threshold. send_sms (aligned + keyword match) stays in.
    pairs = injector.get_injected_tool_names(
        "send sms text message", group="chat", similarity_threshold=0.3)
    names = [n for n, _ in pairs]
    assert "generate_image" not in names
    assert "send_sms" in names
    # meta + tier1 still present regardless of threshold.
    assert "toolvault" in names and "core_tool" in names


def test_tier2_threshold_above_one_excludes_all(env):
    # Combined scores are capped at 1.0; a threshold above that drops every
    # tier-2 candidate while meta + tier1 remain.
    pairs = injector.get_injected_tool_names(
        "send sms text message", group="chat", similarity_threshold=1.01)
    names = [n for n, _ in pairs]
    assert "send_sms" not in names
    assert "generate_image" not in names
    assert "toolvault" in names and "core_tool" in names


def test_max_semantic_tools_caps_tier2(env):
    pairs = injector.get_injected_tool_names(
        "send sms text message create image operator",
        group="chat", max_semantic_tools=1, similarity_threshold=0.0)
    tier2 = [n for n, r in pairs if r.startswith("semantic(")]
    assert len(tier2) <= 1


# ---------------------------------------------------------------------------
# Group filter — governs the always-on baseline ONLY (tier-1). Semantic search
# spans the FULL catalog (all groups + all tiers), so an out-of-group tool is
# not an always-on, but DOES surface when its vector matches the prompt.
# ---------------------------------------------------------------------------

def test_out_of_group_tier1_not_always_on_but_semantic_can_surface(env):
    # (a) realtime_only is tier-1 in the realtime group. With an EMPTY prompt
    #     (no semantic pass) it must NOT appear in a chat request — group filters
    #     the tier-1 always-on baseline.
    names_empty = [n for n, _ in injector.get_injected_tool_names(
        "", group="chat")]
    assert "realtime_only" not in names_empty
    # core_tool (chat tier-1) IS the always-on baseline.
    assert "core_tool" in names_empty

    # (b) realtime_search is tier-2 in the realtime group with a +x vector. WITH
    #     a matching prompt it DOES appear in a chat request via global semantic.
    pairs = injector.get_injected_tool_names(
        "search realtime stream data", group="chat", similarity_threshold=0.1)
    names_prompt = [n for n, _ in pairs]
    assert "realtime_search" in names_prompt
    assert dict(pairs)["realtime_search"].startswith("semantic(")


def test_realtime_group_includes_realtime_tool(env):
    names = [n for n, _ in injector.get_injected_tool_names(
        "", group="realtime")]
    assert "realtime_only" in names
    assert "core_tool" not in names  # chat-only tier1


# ---------------------------------------------------------------------------
# Global semantic pool: cross-group + tier-3 inclusion, no slot starvation
# ---------------------------------------------------------------------------

def test_cross_group_tier2_injected_via_semantic_into_chat(env):
    # realtime_search is tier-2, group realtime (NOT chat). Its +x vector means
    # a chat request with a matching prompt pulls it in via global semantic.
    pairs = injector.get_injected_tool_names(
        "search realtime stream data", group="chat", similarity_threshold=0.1)
    names = [n for n, _ in pairs]
    assert "realtime_search" in names
    assert dict(pairs)["realtime_search"].startswith("semantic(")
    # always-on baseline still present
    assert "toolvault" in names and "core_tool" in names


def test_tier3_tool_injected_via_semantic_into_chat(env):
    # internal_seek is tier-3 with a +x vector — surfaces via global semantic.
    pairs = injector.get_injected_tool_names(
        "seek snapshot direct internal", group="chat", similarity_threshold=0.1)
    names = [n for n, _ in pairs]
    assert "internal_seek" in names
    assert dict(pairs)["internal_seek"].startswith("semantic(")


def test_no_slot_starvation_tier1_does_not_consume_semantic_slot(tmp_path,
                                                                 monkeypatch):
    # A tier-1 tool that ALSO has a store vector must NOT eat the single semantic
    # slot — it's already selected (always-on), hence excluded from the
    # searchable/scoped store passed to hybrid_search_store. With
    # max_semantic_tools=1, a separate tier-2 tool still gets its slot.
    #
    # Deterministic trap: core_tool (tier-1) vector is PERFECTLY aligned with the
    # query (cos=1.0) while semtool (tier-2) is off-axis (cos<1.0). The prompt
    # shares NO keywords with either description, so ranking is semantic-only.
    # An UNSCOPED store ranks core_tool #1 and — since it's already selected —
    # the lone slot is consumed and semtool is dropped (the old bug). A SCOPED
    # store omits core_tool, so semtool wins the slot.
    d = tmp_path / "starve_tools"
    d.mkdir()
    _write(d, _schema("core_tool", groups=("chat",), tier=1, description="zzz core"))
    _write(d, _schema("semtool", groups=("chat",), tier=2, description="qqq tool"))
    monkeypatch.setattr(registry, "TOOLS_DIR", d)
    registry.invalidate_cache()

    store = {
        "core_tool": {"hash": "h", "model": "m", "vector": [1.0, 0.0]},   # cos=1.0
        "semtool": {"hash": "h", "model": "m", "vector": [0.8, 0.6]},     # cos=0.8
    }
    store_path = tmp_path / "embeddings_starve.json"
    store_path.write_text(json.dumps(store))
    monkeypatch.setattr(embeddings, "EMBEDDINGS_PATH", store_path)
    monkeypatch.setattr(embeddings, "embed_query", lambda q: list(QUERY_VEC))
    monkeypatch.setattr(resolvers, "_list_operators", lambda ctx: ["Brandon"])

    pairs = injector.get_injected_tool_names(
        "xyz unrelated", group="chat",
        max_semantic_tools=1, similarity_threshold=0.1)
    names = [n for n, _ in pairs]
    registry.invalidate_cache()

    # tier-1 always present (as always-on, NOT as a semantic match)
    assert "core_tool" in names
    assert dict(pairs)["core_tool"] == "tier1"
    # the lone semantic slot went to the tier-2 tool, not the already-selected
    # tier-1 tool that would otherwise out-rank it.
    assert "semtool" in names
    assert dict(pairs)["semtool"].startswith("semantic(")


# ---------------------------------------------------------------------------
# x-source resolution (operators) — provider-clean schema
# ---------------------------------------------------------------------------

def test_xsource_resolved_and_marker_stripped(env):
    tools, _ = injector.inject_for_prompt(
        "do something for an operator pick_operator",
        "anthropic", "chat", similarity_threshold=0.0)
    by_name = {t["name"]: t for t in tools}
    assert "pick_operator" in by_name
    prop = by_name["pick_operator"]["input_schema"]["properties"]["operator"]
    assert prop["enum"] == ["Brandon", "system"]
    assert "x-source" not in prop


# ---------------------------------------------------------------------------
# Provider format correctness
# ---------------------------------------------------------------------------

def test_anthropic_format(env):
    tools, _ = injector.inject_for_prompt("hello", "anthropic", "chat")
    # Each tool has anthropic shape.
    for t in tools:
        assert set(t.keys()) == {"name", "description", "input_schema"}
    assert any(t["name"] == "toolvault" for t in tools)
    assert any(t["name"] == "core_tool" for t in tools)


def test_gemini_format_wraps_function_declarations(env):
    tools, _ = injector.inject_for_prompt("hello", "gemini", "chat")
    assert isinstance(tools, list) and len(tools) == 1
    assert "function_declarations" in tools[0]
    decls = tools[0]["function_declarations"]
    names = {d["name"] for d in decls}
    assert "toolvault" in names and "core_tool" in names


# ---------------------------------------------------------------------------
# No UGV logic anywhere
# ---------------------------------------------------------------------------

def test_no_ugv_logic_in_source():
    import inspect
    src = inspect.getsource(injector)
    assert "ugv" not in src.lower()


def test_no_ugv_expansion_behavior(env):
    # Injecting a tool whose name starts with a prefix does NOT pull siblings.
    # (Purely tier/semantic/group driven — nothing special-cased.)
    tools, _ = injector.inject_for_prompt(
        "send sms text message", "anthropic", "chat", similarity_threshold=0.1)
    names = {t["name"] for t in tools}
    # generate_image is not pulled in just because send_sms matched.
    assert "generate_image" not in names


# ---------------------------------------------------------------------------
# build_tool_instructions (Task 3.2)
# ---------------------------------------------------------------------------

def test_build_tool_instructions_includes_tool_excludes_meta(env):
    text = injector.build_tool_instructions(["core_tool", "toolvault"])
    assert "core_tool" in text
    assert "Core always-on capability." in text
    assert "AVAILABLE TOOLS" in text
    # The meta-tool gets no described section of its own (it still appears in
    # the fixed header sentence about discovering more tools).
    assert "Tool: toolvault" not in text


def test_build_tool_instructions_reflects_xsource_enum(env):
    text = injector.build_tool_instructions(["pick_operator"])
    assert "pick_operator" in text
    # x-source resolved → operator enum should be visible in the param summary.
    assert "Brandon" in text and "system" in text


def test_build_tool_instructions_empty():
    assert injector.build_tool_instructions([]) == ""


# ---------------------------------------------------------------------------
# Backward compatibility with live callers
# ---------------------------------------------------------------------------

def test_inject_for_prompt_backward_compat_two_positional(env):
    result = injector.inject_for_prompt("hello", "anthropic")
    assert isinstance(result, tuple) and len(result) == 2
    tools, instructions = result
    assert isinstance(tools, list)
    assert isinstance(instructions, str)


def test_inject_for_prompt_no_group_no_ctx(env):
    # provider default group inference still works (anthropic → chat).
    tools, _ = injector.inject_for_prompt("send sms", "anthropic")
    names = {t["name"] for t in tools}
    assert "toolvault" in names and "core_tool" in names
