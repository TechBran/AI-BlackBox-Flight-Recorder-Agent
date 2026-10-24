"""Audit rank 46 (O18-03): no ToolVault executor blocks the event loop.

Every executor is awaited on the server's one event loop (chat streams, voice
bridges, the phone bridge). An executor whose work is blocking I/O is a plain
``def execute``, which the dispatcher runs in a worker thread; an ``async def
execute`` may only await, or hand blocking work to a thread itself.

Two layers:
  * a static guard over every ToolVault/tools/*/executor.py: an async executor
    module may not call a blocking library or one of the box's sync I/O helpers
    directly (or through a local sync helper) inside its async functions;
  * runtime checks on representative real tools: with the blocking helper
    stubbed to sleep 0.5 s, a 20 ms ticker on the loop keeps ticking.
"""

import ast
import asyncio
import time
from pathlib import Path

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor

TOOLS_DIR = Path(__file__).resolve().parents[3] / "ToolVault" / "tools"

# Calls that block the calling thread: network clients, subprocesses, sleeps,
# file reads, and the box's own sync I/O helpers (provider HTTP via requests,
# the 58 MB ledger, the embed + rerank retrieval, the media index scan).
BLOCKING_PREFIXES = (
    "requests.",
    "urllib.request.",
    "http.client.",
    "subprocess.",
    "socket.",
    "time.sleep",
    "Orchestrator.web_tools.",
    "Orchestrator.gmail.",
    "Orchestrator.google_workspace.",
    "Orchestrator.elevenlabs.",
    "Orchestrator.xai_voices.",
    "Orchestrator.fossils.",
    "Orchestrator.volume.",
    "Orchestrator.retrieval.",
    "Orchestrator.rerank.",
    "Orchestrator.qwen_tts.",
    "Orchestrator.stt.",
    "Orchestrator.media_index.",
    "Orchestrator.embeddings.search.",
    "Orchestrator.toolvault.meta_tool.",
    "Orchestrator.routes.admin_routes.get_available_models",
    "Orchestrator.routes.chat_routes.execute_",
)
BLOCKING_BUILTINS = {"open"}

# Pure helpers under a blocking prefix (no I/O): calling them on the loop is fine.
PURE = {
    "Orchestrator.xai_voices.voice_id_of",
    "Orchestrator.fossils.format_snapshot_for_delivery",
    "Orchestrator.fossils.cap_chars",
    "Orchestrator.gmail.service.is_error_result",
}


def _import_map(tree):
    """Local name -> dotted origin for every import anywhere in the module
    (executors import lazily inside functions)."""
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names[(a.asname or a.name).split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                names[a.asname or a.name] = f"{node.module}.{a.name}"
    return names


def _dotted(func):
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
        return list(reversed(parts))
    return None


class _Calls(ast.NodeVisitor):
    """Calls made directly in one function body (nested defs/lambdas excluded:
    a nested sync def handed to a thread is not run on the loop)."""

    def __init__(self):
        self.calls = []

    def visit_FunctionDef(self, node):
        pass

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def visit_Call(self, node):
        d = _dotted(node.func)
        if d:
            self.calls.append((node.lineno, d))
        self.generic_visit(node)


def _body_calls(fn):
    v = _Calls()
    for stmt in fn.body:
        v.visit(stmt)
    return v.calls


def _resolve(parts, imports):
    head = imports.get(parts[0])
    if head is None:
        return ".".join(parts)
    return ".".join([head] + parts[1:])


def _is_blocking(name):
    if name in PURE:
        return False
    if name in BLOCKING_BUILTINS:
        return True
    if name.startswith("ToolVault.tools.") and ".executor." in name:
        # A helper imported from another executor module (edit_cron_job reuses
        # create_cron_job's validators): blocking if it blocks over there.
        module, _, fn = name.rpartition(".")
        path = TOOLS_DIR / module.split(".")[2] / "executor.py"
        return path.exists() and fn in _blocking_helpers_of(path.read_text(encoding="utf-8"))
    return any(name.startswith(p) or name == p.rstrip(".") for p in BLOCKING_PREFIXES)


def _blocking_helpers_of(source, filename="executor.py"):
    tree = ast.parse(source, filename)
    return _blocking_helpers(tree, _import_map(tree))


def _top_functions(tree):
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def blocking_calls_on_loop(source, filename="executor.py"):
    """Return [(line, call)] for blocking calls an async function in ``source``
    makes directly, or through a module-level sync helper (transitively)."""
    tree = ast.parse(source, filename)
    imports = _import_map(tree)
    blocking_helpers = _blocking_helpers(tree, imports)
    hits = []
    for fn in _top_functions(tree).values():
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for line, parts in _body_calls(fn):
            full = _resolve(parts, imports)
            if _is_blocking(full) or (len(parts) == 1 and parts[0] in blocking_helpers):
                hits.append((line, full))
    return hits


def _blocking_helpers(tree, imports):
    """Names of the module-level sync functions that block (fixed point over
    the module's own calls)."""
    top = _top_functions(tree)
    blocking_helpers = set()
    changed = True
    while changed:
        changed = False
        for name, fn in top.items():
            if isinstance(fn, ast.AsyncFunctionDef) or name in blocking_helpers:
                continue
            for _, parts in _body_calls(fn):
                callee = parts[0] if len(parts) == 1 else None
                if _is_blocking(_resolve(parts, imports)) or callee in blocking_helpers:
                    blocking_helpers.add(name)
                    changed = True
                    break
    return blocking_helpers


def _executor_files():
    return sorted(TOOLS_DIR.glob("*/executor.py"))


def test_the_guard_sees_the_blocking_shapes_it_must_catch():
    """The guard itself: direct calls, lazy imports, module aliases and local
    sync helpers are caught; work handed to a thread is not."""
    src = (
        "import asyncio\n"
        "import requests\n"
        "def _helper():\n"
        "    from Orchestrator.routes.admin_routes import get_available_models\n"
        "    return get_available_models('x')\n"
        "def _outer():\n"
        "    return _helper()\n"
        "async def execute(params, ctx):\n"
        "    from Orchestrator.web_tools import perform_provider_search\n"
        "    from Orchestrator.google_workspace import docs\n"
        "    perform_provider_search('ddg', 'q')\n"
        "    docs.create_doc('op', 't')\n"
        "    requests.get('http://x')\n"
        "    _outer()\n"
        "    with open('f') as fh: fh.read()\n"
        "    await asyncio.to_thread(perform_provider_search, 'ddg', 'q')\n"
        "    await asyncio.to_thread(lambda: docs.create_doc('op', 't'))\n"
        "    from ToolVault.tools.create_cron_job.executor import _validate_model\n"
        "    _validate_model('m', 'p', 'op')\n"
    )
    hits = [c for _, c in blocking_calls_on_loop(src)]
    assert "Orchestrator.web_tools.perform_provider_search" in hits
    assert "Orchestrator.google_workspace.docs.create_doc" in hits
    assert "requests.get" in hits
    assert "_outer" in hits
    assert "open" in hits
    assert "ToolVault.tools.create_cron_job.executor._validate_model" in hits
    assert len(hits) == 6, hits


def test_a_sync_executor_module_is_not_flagged():
    src = (
        "def execute(params, ctx):\n"
        "    from Orchestrator.web_tools import perform_provider_search\n"
        "    return perform_provider_search('ddg', 'q')\n"
    )
    assert blocking_calls_on_loop(src) == []


def test_no_async_executor_blocks_the_event_loop():
    offenders = []
    for path in _executor_files():
        for line, call in blocking_calls_on_loop(path.read_text(encoding="utf-8"), str(path)):
            offenders.append(f"{path.parent.name}/executor.py:{line} {call}")
    assert not offenders, (
        "async executors calling blocking code on the event loop (make "
        "execute a plain def, or hand the call to asyncio.to_thread):\n  "
        + "\n  ".join(offenders))


# ---------------------------------------------------------------------------
# Runtime: the real executors, their blocking helper stubbed to sleep
# ---------------------------------------------------------------------------

def _sleeps(ret):
    def stub(*a, **kw):
        time.sleep(0.5)
        return ret
    return stub


def _patch_web(monkeypatch):
    import Orchestrator.web_tools as web_tools
    monkeypatch.setattr(web_tools, "perform_provider_search", _sleeps("results"))


def _patch_gmail(monkeypatch):
    import Orchestrator.gmail.service as gmail
    monkeypatch.setattr(gmail, "list_messages", _sleeps([]))


def _patch_calendar(monkeypatch):
    import Orchestrator.gmail.service as gmail
    import Orchestrator.google_workspace.calendar as calendar
    monkeypatch.setattr(gmail, "workspace_connected", lambda op: True)
    monkeypatch.setattr(calendar, "list_events", _sleeps([]))


def _patch_search(monkeypatch):
    import Orchestrator.fossils as fossils
    import Orchestrator.volume as volume
    monkeypatch.setattr(volume, "read_text_safe", lambda p: "")
    monkeypatch.setattr(fossils, "hybrid_retrieve", _sleeps([]))


def _patch_recent(monkeypatch):
    import Orchestrator.fossils as fossils
    import Orchestrator.volume as volume
    monkeypatch.setattr(volume, "read_text_safe", lambda p: "")
    monkeypatch.setattr(fossils, "get_recent_fossils_for_operator", _sleeps([]))


def _patch_el_voices(monkeypatch):
    import Orchestrator.elevenlabs.catalog as catalog
    monkeypatch.setattr(catalog, "get_voices", _sleeps(None))


def _patch_toolvault_search(monkeypatch):
    import Orchestrator.toolvault.embeddings as emb
    monkeypatch.setattr(emb, "embed_query", _sleeps(None))
    monkeypatch.setattr(emb, "load_embeddings_store", lambda path=None: {})


@pytest.mark.parametrize("tool,params,patch", [
    ("perplexity_web_search", {"query": "rovers"}, _patch_web),
    ("duckduckgo_web_search", {"query": "rovers"}, _patch_web),
    ("gmail_search", {"query": "from:me"}, _patch_gmail),
    ("list_events", {"time_min": "2026-01-01T00:00:00Z",
                     "time_max": "2026-01-02T00:00:00Z"}, _patch_calendar),
    ("search_snapshots", {"query": "rovers"}, _patch_search),
    ("list_recent_snapshots", {"count": 2}, _patch_recent),
    ("elevenlabs_list_voices", {}, _patch_el_voices),
    ("toolvault", {"action": "search", "query": "send a text"}, _patch_toolvault_search),
])
def test_real_executor_leaves_the_loop_running(monkeypatch, tool, params, patch):
    patch(monkeypatch)
    registry.invalidate_cache()

    async def go():
        ticks = 0
        stop = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not stop.is_set():
                await asyncio.sleep(0.02)
                ticks += 1

        t = asyncio.create_task(ticker())
        try:
            result = await BlackBoxToolExecutor(operator="LoopTester").execute(tool, params)
        finally:
            stop.set()
            await t
        return result, ticks

    result, ticks = asyncio.run(go())
    assert "Unknown tool" not in (result.result or "")
    assert ticks >= 15, f"{tool} stalled the event loop for its whole call (ticks={ticks})"
