"""Tests for the v2-rewired meta-tool (Task 6.4a).

``meta_tool.execute`` now sources tools from the v2 module registry
(``registry.load_canonical`` / ``registry.get_tool``) and ranks search via the
embeddings.json store (``embeddings.hybrid_search_store``) — no more v1
volume/manifest byte-offset machinery.

Tests are hermetic: ``registry.TOOLS_DIR`` is pointed at a ``tmp_path`` populated
with module ``schema.json`` files (plus an ``executor.py`` for the dispatch
test), and ``embeddings.embed_query`` is monkeypatched so search is
network-free + deterministic. The embeddings store is supplied directly via a
monkeypatched ``load_embeddings_store``.
"""

import json

import pytest

from Orchestrator.toolvault import meta_tool, registry, embeddings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_module(tools_dir, name, *, schema, executor_src=None):
    folder = tools_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "schema.json").write_text(json.dumps(schema))
    if executor_src is not None:
        (folder / "executor.py").write_text(executor_src)


@pytest.fixture
def tools_dir(tmp_path, monkeypatch):
    """A tmp ToolVault/tools with a handful of modules + the toolvault meta-tool."""
    td = tmp_path / "tools"
    td.mkdir()

    _write_module(td, "send_sms", schema={
        "name": "send_sms",
        "description": "Send an SMS text message via the cellular gateway.",
        "category": "communication",
        "groups": ["chat", "phone"],
        "tier": 2,
        "parameters": {
            "type": "object",
            "properties": {
                "phone_number": {"type": "string", "description": "E.164 number"},
                "message": {"type": "string", "description": "The text to send"},
            },
            "required": ["phone_number", "message"],
        },
        "example": "send_sms(phone_number=\"...\", message=\"...\")",
        "notes": "Look up contact number with search_contacts first.",
    })

    _write_module(td, "search_snapshots", schema={
        "name": "search_snapshots",
        "description": "Search the BlackBox memory snapshots semantically.",
        "category": "memory",
        "groups": ["chat", "mcp"],
        "tier": 1,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query"},
                "operator": {
                    "type": "string",
                    "x-source": "operators",
                    "description": "Optional operator scope",
                },
            },
            "required": ["query"],
        },
    })

    _write_module(td, "generate_image", schema={
        "name": "generate_image",
        "description": "Generate an image from a text prompt.",
        "category": "media_generation",
        "groups": ["chat"],
        "tier": 2,
        "parameters": {
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "Prompt"}},
            "required": ["prompt"],
        },
    })

    # The toolvault meta-tool itself, with a module executor (for the dispatch test).
    _write_module(
        td, "toolvault",
        schema={
            "name": "toolvault",
            "description": "Tool discovery system.",
            "category": "uncategorized",
            "groups": ["chat", "mcp"],
            "tier": 2,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["search", "read", "list"]},
                    "query": {"type": "string"},
                    "tool_name": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["action"],
            },
        },
        executor_src=(
            "from Orchestrator.toolvault.context import ToolContext, ToolResult\n"
            "from Orchestrator.toolvault.meta_tool import execute as _meta_execute\n"
            "\n"
            "async def execute(params: dict, ctx: ToolContext) -> ToolResult:\n"
            "    action = params.get('action', '')\n"
            "    action_params = {k: v for k, v in params.items() if k != 'action'}\n"
            "    result = _meta_execute(action, **action_params)\n"
            "    return ToolResult(success=result.success, result=result.result,\n"
            "                      data=result.data if result.data else None)\n"
        ),
    )

    monkeypatch.setattr(registry, "TOOLS_DIR", td)
    registry.invalidate_cache()
    yield td
    registry.invalidate_cache()


@pytest.fixture
def fake_store(monkeypatch):
    """Deterministic embeddings store + query embedder (no network).

    send_sms vector aligns with the query vector; others are orthogonal-ish so
    send_sms ranks first semantically. Keyword overlap reinforces it.
    """
    store = {
        "send_sms": {"vector": [1.0, 0.0, 0.0]},
        "search_snapshots": {"vector": [0.0, 1.0, 0.0]},
        "generate_image": {"vector": [0.0, 0.0, 1.0]},
    }
    monkeypatch.setattr(embeddings, "load_embeddings_store", lambda *a, **k: store)
    monkeypatch.setattr(embeddings, "embed_query", lambda q: [1.0, 0.0, 0.0])
    return store


@pytest.fixture
def patched_operators(monkeypatch):
    """Deterministic operator list so the x-source:operators resolver is hermetic."""
    from Orchestrator.toolvault import resolvers
    monkeypatch.setattr(resolvers, "_list_operators", lambda ctx: ["Brandon", "system"])


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_returns_categories_from_registry(tools_dir):
    r = meta_tool.execute("list")
    assert r.success
    assert r.data["total"] == 4
    cats = set(r.data["categories"])
    assert {"communication", "memory", "media_generation", "uncategorized"} <= cats
    assert "send_sms" in r.result
    assert "generate_image" in r.result


def test_list_filters_by_category(tools_dir):
    r = meta_tool.execute("list", category="communication")
    assert r.success
    assert r.data["categories"] == ["communication"]
    assert "send_sms" in r.result
    assert "generate_image" not in r.result


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

def test_read_returns_schema(tools_dir):
    r = meta_tool.execute("read", tool_name="send_sms")
    assert r.success
    assert r.data["name"] == "send_sms"
    assert r.data["schema"]["required"] == ["phone_number", "message"]
    assert r.data["tier"] == 2
    assert "phone_number" in r.result
    assert "Look up contact number" in r.result  # notes surfaced


def test_read_unknown_tool_not_found(tools_dir):
    r = meta_tool.execute("read", tool_name="does_not_exist")
    assert not r.success
    assert "not found" in r.result.lower()


def test_read_resolves_x_source_enum(tools_dir, patched_operators):
    """read applies resolve_schema, so x-source:operators becomes a live enum."""
    r = meta_tool.execute("read", tool_name="search_snapshots")
    assert r.success
    op_prop = r.data["schema"]["properties"]["operator"]
    assert "x-source" not in op_prop  # marker stripped
    assert op_prop["enum"] == ["Brandon", "system"]
    assert "Brandon" in r.result  # enum surfaced in human-readable text


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def test_search_returns_ranked_matches(tools_dir, fake_store):
    r = meta_tool.execute("search", query="send a text message")
    assert r.success
    matches = r.data["matches"]
    assert matches, "expected ranked matches"
    assert matches[0]["name"] == "send_sms"
    assert "send_sms" in r.result


def test_search_missing_query_fails(tools_dir):
    r = meta_tool.execute("search", query="")
    assert not r.success
    assert "query" in r.result.lower()


# ---------------------------------------------------------------------------
# dispatch through the executor pipeline
# ---------------------------------------------------------------------------

def test_dispatch_toolvault_module_executor(tools_dir):
    import asyncio
    from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor

    ex = BlackBoxToolExecutor()
    r = asyncio.run(ex.execute("toolvault", {"action": "list"}))
    assert r.success
    assert "send_sms" in r.result
