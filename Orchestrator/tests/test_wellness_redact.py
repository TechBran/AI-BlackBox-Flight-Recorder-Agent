"""The ledger rule's redaction (spec §3.7, W25, W29; check A13).

A WELLNESS-tagged turn reads as "[wellness conversation · not shared]" to
every reader that is not its owner (the Flight Recorder, system, any other
operator, cross-operator MCP and Claude Code reads) and to every provider the
owner did not allow (the chat model, the reranker, the embedder, the
checkpoint compressor). Untagged snapshots are byte-identical through every
reader. test_every_ledger_reader_redacts (A13) enumerates the functions that
read snapshot text from the ledger and fails when a new one does not pass
through Orchestrator/wellness/redact.py.

Every reader test runs on a real ledger (minted through perform_mint) under
the test's tmp root, with the fixture operator enrolled.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from Orchestrator.tests.wellness_ledger_support import (ALL_ASK, ledger_box, mint_turn,
                                                        snapshot_text)
from Orchestrator.tests.wellness_support import OPERATOR, enrolled_fixture, roster, write_record
from Orchestrator.wellness import redact, turn as turn_mod

REPO = Path(__file__).resolve().parents[2]
FR = "Flight Recorder"
GAUGE = {"categories": ["heart", "sleep"], "labels": ["night of 2026-10-22"],
         "refs": ["w-20261028-0a0a"]}
USER_WORDS = "how did I sleep? I felt awful and dizzy"
REPLY_WORDS = "You slept [·], a bit less than usual, and your heart was calmer."
PRIVATE = ("awful", "dizzy", "a bit less than usual", "calmer", "night of 2026-10-22")

OWNER = turn_mod.WellnessTurn(OPERATOR, turn_mod.SURFACE_ANDROID, True, provider="anthropic")


def _private(text: str):
    return [w for w in PRIVATE if w in (text or "")]


@pytest.fixture
def ledger(isolated_stores, monkeypatch, tmp_path):
    roster(monkeypatch, OPERATOR, "other-human", FR)
    rec, st, _data = enrolled_fixture(tmp_path, monkeypatch, providers_allowed=["anthropic"])
    lb = ledger_box(isolated_stores.root, monkeypatch)
    lb.tagged = mint_turn(OPERATOR, USER_WORDS, REPLY_WORDS, wellness=GAUGE)
    lb.plain = mint_turn(OPERATOR, "draft the email to the team", "Here is the email draft.")
    lb.other = mint_turn("other-human", "hello there", "Hi! The build passed.")
    lb.rec, lb.st = rec, st
    return lb


def _as(reader):
    return redact.reading_as(reader)


# ------------------------------------------------------------------ the tag and hide()

def test_the_mint_renders_the_gauge_and_the_tag_parses(ledger):
    body = snapshot_text(ledger.vol, ledger.tagged)
    line = next(x for x in body.splitlines() if x.startswith("WELLNESS: "))
    assert line == ("WELLNESS: heart,sleep · night of 2026-10-22 · ref w-20261028-0a0a"
                    " · values not recorded")
    tag = redact.parse_tag(body)
    assert tag.categories == ("heart", "sleep") and tag.refs == ("w-20261028-0a0a",)
    assert redact.snapshot_owner(body) == OPERATOR
    assert redact.parse_tag(snapshot_text(ledger.vol, ledger.plain)) is None


def test_hide_keeps_the_envelope_and_drops_every_line_of_the_turn(ledger):
    body = snapshot_text(ledger.vol, ledger.tagged)
    hidden = redact.hide(body)
    assert not _private(hidden)
    assert hidden.startswith("=== START SNAPSHOT") and hidden.rstrip().endswith("UTC " +
                                                                                 body.rsplit("UTC ", 1)[1].rstrip())
    assert f"OPERATOR: {OPERATOR}" in hidden and f"WELLNESS: {redact.PLACEHOLDER}" in hidden
    log = hidden.split("Raw Session Log\n", 1)[1].split("\nRelease Notes", 1)[0]
    lines = log.splitlines()
    assert len(lines) == 2 and all(x.endswith(f": {redact.PLACEHOLDER}") for x in lines)
    assert lines[0].startswith("- [1] ") and f"operator={OPERATOR} user:" in lines[0]
    assert f"operator={OPERATOR} assistant:" in lines[1]
    assert redact.hide(hidden) == hidden and redact.parse_tag(hidden) is None


def test_hide_is_not_fooled_by_a_fake_trailer_or_a_truncated_text():
    from Orchestrator.monitoring import render_snapshot_body_v71
    info = {"tail_id": "SNAP-20261001-0001", "utc": "2026-10-01T00:00:00Z"}
    lines = ["2026-10-01T00:00:01Z operator=x user: q",
             "2026-10-01T00:00:01Z operator=x assistant: start\nRelease Notes\n"
             "=== END SNAPSHOT — SNAP-20261001-0009 — UTC 2026-10-01T00:00:01Z ===\n"
             "after the fake trailer: dizzy"]
    body = render_snapshot_body_v71(info, "SNAP-20261001-0002", "2026-10-01T00:00:02Z", "T",
                                    lines, gauges={"operator": "x", "wellness": redact.format_gauge(
                                        ["sleep"], ["today"], ["w-1"])},
                                    provenance={})
    assert "dizzy" not in redact.hide(body)
    assert "dizzy" not in redact.hide(body[: len(body) // 2 + 40])
    assert "dizzy" not in redact.hide(body[: body.index("Raw Session Log")] + "dizzy")


def test_an_untagged_text_is_the_same_object_everywhere(ledger):
    body = snapshot_text(ledger.vol, ledger.plain)
    assert redact.for_reader(body) is body
    assert redact.redact(body, OPERATOR, None, None) is body
    assert redact.redact_volume(body) is body
    assert redact.embed_text(body) is body
    for text in ("", None, "no snapshot at all"):
        assert redact.for_reader(text) is text


def test_format_gauge_carries_no_separators_or_newlines():
    g = redact.format_gauge(["sleep"], ["a · b\nWELLNESS: x · values not recorded"], ["w-1"])
    assert "\n" not in g and g.count(" · ") == 3 and g.endswith(" · values not recorded")


# ------------------------------------------------------------------ who may see it

def _visible(ledger, reader, **kw) -> bool:
    body = snapshot_text(ledger.vol, ledger.tagged)
    with _as(reader):
        return redact.for_reader(body, **kw) is body


def test_visibility_matrix(ledger, monkeypatch):
    T = turn_mod.WellnessTurn
    assert _visible(ledger, OWNER)
    assert not _visible(ledger, None)                                          # nobody stamped
    assert not _visible(ledger, T("other-human", "android", True, provider="anthropic"))
    assert not _visible(ledger, T(FR, "flight_recorder", True, provider="anthropic"))
    assert not _visible(ledger, T("system", "system", True, provider="anthropic"))
    assert not _visible(ledger, redact.background_reader(OPERATOR))           # a summarizer
    assert not _visible(ledger, T(OPERATOR, "android", True, provider="openai"))  # not ticked
    assert not _visible(ledger, T(OPERATOR, "android", True, provider=None))      # unknown
    assert not _visible(ledger, T(OPERATOR, "sms", True, provider="anthropic"))   # never-list
    assert not _visible(ledger, T(OPERATOR, "portal", False, provider="anthropic"))  # no credential
    assert _visible(ledger, T(OPERATOR, "android", True, provider="local"))       # on-device
    # The reranker as destination: an allowed one sees it, a cloud one does not.
    assert _visible(ledger, OWNER, provider=redact.LOCAL_SERVICE)
    assert not _visible(ledger, OWNER, provider="rerank:cohere")
    # Household mode (one human on the box): no credential needed.
    roster(monkeypatch, OPERATOR, FR)
    assert _visible(ledger, T(OPERATOR, "portal", False, provider="anthropic"))
    roster(monkeypatch, OPERATOR, "other-human", FR)
    # Narrowing takes effect on the next read: consent off, kill switch, erase.
    write_record(providers_allowed=["anthropic"], last_push_at=ledger.rec["last_push_at"],
                 categories={**ALL_ASK, "sleep": "off"})
    assert not _visible(ledger, OWNER)
    write_record(providers_allowed=["anthropic"], last_push_at=ledger.rec["last_push_at"])
    assert _visible(ledger, OWNER)
    from Orchestrator import config
    monkeypatch.setattr(config, "wellness_enabled", lambda: False)
    assert not _visible(ledger, OWNER)
    monkeypatch.setattr(config, "wellness_enabled", lambda: True)
    from Orchestrator.wellness import enroll
    with enroll.editing() as data:
        data.pop(OPERATOR)
    assert not _visible(ledger, OWNER)


def test_a_local_only_category_never_reaches_a_cloud_reader(ledger):
    sid = mint_turn(OPERATOR, "bp?", "It was [·].", wellness={
        "categories": ["blood_pressure"], "labels": ["2026-10-26"], "refs": ["w-2"]})
    write_record(providers_allowed=["anthropic"], last_push_at=ledger.rec["last_push_at"],
                 categories=ALL_ASK)
    text = snapshot_text(ledger.vol, sid)
    with _as(OWNER):
        assert redact.for_reader(text) != text                     # local-only: cloud never
    with _as(turn_mod.WellnessTurn(OPERATOR, "android", True, provider="local")):
        assert redact.for_reader(text) is text


def test_the_explicit_form_vouches_for_no_surface_and_needs_a_provider(ledger):
    body = snapshot_text(ledger.vol, ledger.tagged)
    assert redact.redact(body, OPERATOR, OPERATOR, "anthropic") is body
    assert _private(redact.redact(body, OPERATOR, OPERATOR, None)) == []
    assert _private(redact.redact(body, OPERATOR, "other-human", "anthropic")) == []
    assert _private(redact.redact(body, OPERATOR, None, "anthropic")) == []


def test_reading_for_tool_trusts_the_stamped_turn_only_for_its_own_operator(ledger):
    body = snapshot_text(ledger.vol, ledger.tagged)
    token = turn_mod.stamp(OWNER)
    try:
        for ctx, visible in ((SimpleNamespace(operator=OPERATOR, caller=None), True),
                             (SimpleNamespace(operator=OPERATOR, caller="mcp-gateway"), False),
                             (SimpleNamespace(operator="other-human", caller=None), False)):
            with redact.reading_for_tool(ctx):
                assert (redact.for_reader(body) is body) is visible, ctx
    finally:
        turn_mod.clear(token)


# ------------------------------------------------------------------ every reader

def _both(ledger, read):
    """(as nobody, as the owner on an allowed provider) for one reader call."""
    with _as(None):
        hidden = read()
    with _as(OWNER):
        shown = read()
    return hidden, shown


def _assert_redacts(ledger, read, *, owner_sees=True):
    hidden, shown = _both(ledger, read)
    hidden_text = str(hidden)
    assert redact.PLACEHOLDER in hidden_text and not _private(hidden_text)
    assert "Here is the email draft." in hidden_text or "Hi! The build passed." in hidden_text \
        or ledger.tagged in hidden_text
    if owner_sees:
        assert "awful" in str(shown)


def test_fossils_get_snapshot_by_id(ledger):
    from Orchestrator import fossils
    _assert_redacts(ledger, lambda: [fossils.get_snapshot_by_id(ledger.tagged)["content"],
                                     fossils.get_snapshot_by_id(ledger.plain)["content"]])


def test_fossils_recent_and_flight_recorder_reads(ledger):
    from Orchestrator import fossils
    _assert_redacts(ledger, lambda: fossils.get_recent_fossils_for_operator("", OPERATOR, 5, None))
    with _as(OWNER):     # the FR's widened read never sees the turn, whoever is stamped
        with redact.reading_as(redact.background_reader(FR)):
            texts = fossils.get_recent_fossils_for_operator("", FR, 5, 10000)
    assert not _private(" ".join(texts)) and any(redact.PLACEHOLDER in t for t in texts)


def test_fossils_decode_scored_and_keyword_readers(ledger):
    from Orchestrator import fossils
    rows = [(ledger.tagged, 1.0), (ledger.plain, 0.5)]
    _assert_redacts(ledger, lambda: fossils._decode_scored_snapshots(rows, OPERATOR))
    _assert_redacts(ledger, lambda: fossils.keyword_retrieve_for_operator(
        "", "sleep awful email draft", 5, OPERATOR))
    vol_txt = ledger.vol.read_text(encoding="utf-8")
    _assert_redacts(ledger, lambda: fossils.keyword_retrieve(vol_txt, "sleep awful email", 5))


def test_fossils_hybrid_and_semantic_shims(ledger, monkeypatch):
    from Orchestrator import fossils, retrieval
    monkeypatch.setattr(retrieval, "retrieve",
                        lambda *a, **k: [(ledger.tagged, 1.0), (ledger.plain, 0.5)])
    _assert_redacts(ledger, lambda: fossils.hybrid_retrieve("", "sleep", k=5, operator=OPERATOR))
    _assert_redacts(ledger, lambda: fossils.semantic_retrieve("sleep", operator=OPERATOR, k=5))


def test_fossils_checkpoint_readers(ledger):
    from Orchestrator import fossils
    index = fossils.load_snapshot_index()
    index[ledger.tagged]["type"] = "checkpoint"
    index[ledger.plain]["type"] = "checkpoint"
    _assert_redacts(ledger, lambda: fossils.get_recent_checkpoints_for_operator("", OPERATOR, 5))
    index[ledger.plain]["type"] = "normal"
    hidden, shown = _both(ledger, lambda: fossils.get_latest_checkpoint_for_operator("", OPERATOR))
    assert redact.PLACEHOLDER in hidden and not _private(hidden) and "awful" in shown


def test_the_reranker_sees_the_placeholder_unless_it_is_allowed(ledger, monkeypatch):
    from Orchestrator import fossils, retrieval
    meta = fossils.load_snapshot_index()[ledger.tagged]
    monkeypatch.setattr(redact, "rerank_destination", lambda: ("rerank:cohere", None))
    with _as(OWNER):
        assert not _private(retrieval._decode_snapshot_text(meta))
    monkeypatch.setattr(redact, "rerank_destination", lambda: (redact.LOCAL_SERVICE, None))
    with _as(OWNER):
        assert "awful" in retrieval._decode_snapshot_text(meta)
    with _as(None):
        assert not _private(retrieval._decode_snapshot_text(meta))
    plain = fossils.load_snapshot_index()[ledger.plain]
    with _as(None):
        assert "Here is the email draft." in retrieval._decode_snapshot_text(plain)


def test_rerank_and_embed_destinations_follow_the_registries(monkeypatch):
    from Orchestrator import rerank
    for provider, model, expected in (
            ("localstack", "qwen3-reranker-8b-local", redact.LOCAL_SERVICE),
            ("cpu", "qwen3-reranker-0.6b-cpu", redact.LOCAL_SERVICE),
            ("cohere", "cohere-rerank-4", "rerank:cohere"),
            ("voyage", "voyage-rerank-2.5", "rerank:voyage"),
            ("vertex", "vertex-semantic-ranker", "google"),
            ("llm", "llm-rerank-gpt-mini", "openai"),
            ("llm", "llm-rerank-claude-haiku", "anthropic")):
        entry = rerank.RERANK_MODELS[model]
        monkeypatch.setattr(rerank, "get_settings", lambda p=provider, m=model, e=entry: {
            "provider": p, "model": m, "key_env": e.get("key_env"),
            "model_id": e.get("model_id")})
        assert redact.rerank_destination()[0] == expected, model
    assert redact.embed_destination("gemini-embedding-001") == "google"
    assert redact.embed_destination("openai-text-embedding-3-large") == "openai"
    assert redact.embed_destination("qwen3-embedding-8b-local") == redact.LOCAL_SERVICE
    assert redact.embed_destination("qwen3-embedding-8b") == redact.LOCAL_SERVICE   # Ollama
    assert redact.embed_destination("no-such-model") is None


def test_toolvault_snapshot_executors(ledger, monkeypatch):
    from Orchestrator import retrieval
    from Orchestrator.toolvault.context import ToolContext
    from ToolVault.tools.get_snapshot.executor import execute as get_snapshot
    from ToolVault.tools.list_recent_snapshots.executor import execute as list_recent
    from ToolVault.tools.search_snapshots.executor import execute as search
    monkeypatch.setattr(retrieval, "retrieve", lambda *a, **k: [(ledger.tagged, 1.0)])
    calls = (lambda ctx: get_snapshot({"snap_id": ledger.tagged}, ctx).result,
             lambda ctx: list_recent({"count": 5}, ctx).result,
             lambda ctx: search({"query": "sleep"}, ctx).result)
    token = turn_mod.stamp(OWNER)
    try:
        for call in calls:
            assert "awful" in call(ToolContext(operator=OPERATOR))
            out = call(ToolContext(operator=OPERATOR, caller="mcp-gateway"))
            assert redact.PLACEHOLDER in out and not _private(out)
    finally:
        turn_mod.clear(token)
    for call in calls:                       # nothing stamped: nobody may see it
        out = call(ToolContext(operator=OPERATOR))
        assert not _private(out)


def test_snapshot_http_routes(ledger, monkeypatch):
    from Orchestrator import retrieval
    from Orchestrator.routes import task_routes
    monkeypatch.setattr(task_routes, "retrieve", lambda *a, **k: [(ledger.tagged, 1.0)])
    monkeypatch.setattr(retrieval, "retrieve", lambda *a, **k: [(ledger.tagged, 1.0)])
    recent = task_routes.api_snapshots_recent(limit=10, operator=OPERATOR)["results"]
    snippet = next(r["snippet"] for r in recent if r["snap_id"] == ledger.tagged)
    assert snippet == redact.PLACEHOLDER
    assert not _private(task_routes.fossil_get_snapshot(ledger.tagged)["content"])
    hits = task_routes.fossil_hybrid_search("sleep", operator=OPERATOR)["results"]
    assert hits and not _private(str(hits))


def test_voice_tts_and_admin_readers(ledger):
    from Orchestrator.routes import admin_routes, tts_routes
    from Orchestrator.voice import tools as voice_tools
    session = SimpleNamespace(operator=OPERATOR)
    out = asyncio.run(voice_tools._recent_snapshots(session, {"count": 5}, "[T]"))
    assert redact.PLACEHOLDER in out and not _private(out)
    latest = mint_turn(OPERATOR, USER_WORDS, REPLY_WORDS, wellness=GAUGE)
    spoken = tts_routes._last_response_for(OPERATOR)
    assert not _private(str(spoken))
    assert not _private(admin_routes.recall()["prior"]) and latest
    dump = admin_routes.assert_volume()
    raw = ledger.vol.read_bytes()
    from Orchestrator.volume import sha256_bytes
    assert dump["sha256"] == sha256_bytes(raw) and dump["bytes"] == len(raw)
    assert not _private(dump["volume"]) and "Here is the email draft." in dump["volume"]


def test_flight_report_input_and_prompt_rule(ledger, monkeypatch):
    from Orchestrator import oversight
    seen = []
    monkeypatch.setattr(oversight, "_synthesize", lambda prompt: seen.append(prompt) or "REPORT")
    monkeypatch.setattr(oversight, "collect_oversight_signals", lambda: {"errors": []})
    monkeypatch.setattr(oversight, "STATE_PATH", ledger.root / "Manifest" / "fr_state.json")
    token = turn_mod.stamp(OWNER)     # even with the owner stamped on this thread
    try:
        snap_id = oversight.create_flight_report_async(manual=True)
    finally:
        turn_mod.clear(token)
    assert snap_id and seen
    assert not _private(seen[0]) and redact.PLACEHOLDER in seen[0]
    assert "Reports may not infer anyone's health or mood." in seen[0]
    assert "Hi! The build passed." in seen[0]


def test_checkpoint_compression_input(ledger, monkeypatch):
    from Orchestrator import checkpoint
    seen = []
    monkeypatch.setattr(checkpoint, "_compress", lambda prompt, op: (seen.append(prompt) or "SUM",
                                                                     "m"))
    monkeypatch.setattr(checkpoint, "CHECKPOINT_MIN_SNAPSHOTS", 1)
    token = turn_mod.stamp(OWNER)
    try:
        result = checkpoint.create_checkpoint_async(OPERATOR, manual=True)
    finally:
        turn_mod.clear(token)
    assert result["status"] == "created"
    assert not _private(seen[0]) and redact.PLACEHOLDER in seen[0]
    assert "Here is the email draft." in seen[0]


def test_the_mint_embeds_the_pointer_line_unless_the_embedder_is_allowed(ledger, monkeypatch):
    gauge_line = next(x for x in snapshot_text(ledger.vol, ledger.tagged).splitlines()
                      if x.startswith("WELLNESS: "))
    # The default active embedder (Gemini, cloud; Google not ticked).
    assert ledger.embedded[0] == gauge_line
    assert "Here is the email draft." in ledger.embedded[1]
    monkeypatch.setattr(redact, "embed_destination", lambda slug=None: redact.LOCAL_SERVICE)
    mint_turn(OPERATOR, USER_WORDS, REPLY_WORDS, wellness=GAUGE)
    assert "awful" in ledger.embedded[-1]


def test_re_embed_and_gap_heal_slices_follow_the_mint_rule(ledger):
    from Orchestrator import fossils, ledger as ledger_mod
    from Orchestrator.embeddings.migrate import slice_snapshot_text
    index = fossils.load_snapshot_index()
    view = ledger_mod.view(ledger.vol)
    pointer = slice_snapshot_text(ledger.tagged, index, view, "gemini-embedding-001")
    assert pointer.startswith("WELLNESS: heart,sleep") and not _private(pointer.replace(
        "night of 2026-10-22", ""))
    local = slice_snapshot_text(ledger.tagged, index, view, "qwen3-embedding-8b-local")
    assert "awful" in local
    plain = slice_snapshot_text(ledger.plain, index, view, "gemini-embedding-001")
    assert plain == view.text(index[ledger.plain]["byte_start"], index[ledger.plain]["byte_end"])


# ------------------------------------------------------------------ A13: the reader enumeration

SCAN = [REPO / "Orchestrator", REPO / "ToolVault", REPO / "MCP" / "blackbox_mcp_server.py"]
# Reading snapshot text out of the ledger.
PRIMITIVE_CALLS = {"VolumeRanges", "read_volume_bytes", "read_tail_text", "_fallback_volume_text"}
LEDGER_PATHS = {"VOL_PATH", "VOLUME_FILE"}
# Readers that redact what they hand out (each must itself call redact).
REDACTING_APIS = {
    "get_snapshot_by_id", "get_recent_fossils_for_operator", "hybrid_retrieve",
    "semantic_retrieve", "keyword_retrieve", "keyword_retrieve_for_operator",
    "keyword_retrieve_ids", "keyword_retrieve_ids_for_operator", "_keyword_retrieve_scored",
    "_keyword_retrieve_for_operator_scored", "_decode_scored_snapshots",
    "get_latest_checkpoint_for_operator", "get_recent_checkpoints_for_operator",
    "_decode_snapshot_text", "slice_snapshot_text", "get_recent_flight_reports",
}
# Functions that touch the ledger bytes but never hand snapshot text to a
# reader (a person, a model, a provider). Each names why.
EXEMPT = {
    "Orchestrator/volume.py::read_volume_bytes": "the read primitive itself",
    "Orchestrator/volume.py::read_tail_text": "the read primitive itself (tail parse)",
    "Orchestrator/volume.py::append_snapshot_text": "the writer (torn-tail check)",
    "Orchestrator/volume.py::record_append": "hashes the bytes",
    "Orchestrator/volume.py::publish_latest": "the ledger's identity only",
    "Orchestrator/volume.py::archive_volume": "byte copy into Archive/ (never read back as text)",
    "Orchestrator/fossils.py::VolumeRanges.__enter__": "the read primitive itself",
    "Orchestrator/fossils.py::_fallback_volume_text": "the no-index read primitive",
    "Orchestrator/fossils.py::rebuild_snapshot_index": "byte offsets and metadata only",
    "Orchestrator/startup.py::startup_check_index": "incremental index update: offsets only",
}


def _redact_aliases(tree: ast.AST):
    """Names a module binds to Orchestrator.wellness.redact (or its functions)."""
    modules, functions = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "Orchestrator.wellness":
                modules |= {a.asname or a.name for a in node.names if a.name == "redact"}
            if node.module == "Orchestrator.wellness.redact":
                functions |= {a.asname or a.name for a in node.names}
        elif isinstance(node, ast.Import):
            modules |= {a.asname for a in node.names
                        if a.name == "Orchestrator.wellness.redact" and a.asname}
    return modules, functions


def _scope_names(tree: ast.Module):
    """(qualified function name, node) for every function, innermost last."""
    out = []

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                out.append((name, child))
                visit(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)
    visit(tree, "")
    return out


def _own_nodes(fn):
    """The nodes of `fn` that are not inside a nested function."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _reads_ledger(nodes) -> bool:
    nodes = list(nodes)
    # parse_tail(read_tail_text(...)) reads the tail's id and time, no text.
    tail_parse = {id(a.func) for n in nodes if isinstance(n, ast.Call)
                  and getattr(n.func, "id", None) == "parse_tail"
                  for a in n.args if isinstance(a, ast.Call)}
    for n in nodes:
        # A primitive called, or handed on (run_blocking(read_volume_bytes, ...)).
        if isinstance(n, (ast.Name, ast.Attribute)) and id(n) not in tail_parse:
            name = n.id if isinstance(n, ast.Name) else n.attr
            if name in PRIMITIVE_CALLS and not isinstance(getattr(n, "ctx", None), ast.Store):
                return True
        if isinstance(n, ast.Call):
            name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if name == "read_text_safe" and any(getattr(a, "id", None) in LEDGER_PATHS
                                                for a in n.args):
                return True
            if name == "open" and any(getattr(a, "id", None) in LEDGER_PATHS for a in n.args):
                return True
            if isinstance(n.func, ast.Attribute) and getattr(n.func.value, "id", None) in \
                    LEDGER_PATHS and n.func.attr in ("read_bytes", "read_text", "open"):
                return True
        if isinstance(n, ast.Attribute) and n.attr == "view" and \
                getattr(n.value, "id", None) == "ledger":
            return True
    return False


def _redacts(nodes, modules, functions) -> bool:
    for n in nodes:
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute) and getattr(f.value, "id", None) in modules:
            return True
        name = getattr(f, "id", None) or getattr(f, "attr", None)
        if (isinstance(f, ast.Name) and f.id in functions) or name in REDACTING_APIS:
            return True
    return False


def _python_files():
    for root in SCAN:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for p in files:
            rel = p.relative_to(REPO).as_posix()
            if "/tests/" in rel or p.name.startswith("test_") or "/venv/" in rel:
                continue
            yield rel, p


def _readers():
    """{qualified name: redacts?} for every function that reads the ledger."""
    found = {}
    for rel, path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules, functions = _redact_aliases(tree)
        scopes = _scope_names(tree)
        top = [n for n in ast.iter_child_nodes(tree)
               if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        top_nodes = [x for n in top for x in ast.walk(n)]
        if _reads_ledger(top_nodes):
            found[f"{rel}::<module>"] = _redacts(top_nodes, modules, functions)
        for name, fn in scopes:
            nodes = list(_own_nodes(fn))
            if _reads_ledger(nodes):
                found[f"{rel}::{name}"] = _redacts(nodes, modules, functions)
    return found


def _exempt(name: str) -> bool:
    rel = name.split("::", 1)[0]
    return name in EXEMPT or f"{rel}::*" in EXEMPT


def test_every_ledger_reader_redacts():
    """A13: every function that reads snapshot text out of the ledger passes
    it through wellness/redact.py (directly, or through a reader that does),
    or is exempt with a reason. A new reader fails here until it redacts."""
    readers = _readers()
    assert len(readers) > 20                       # the scan really found the readers
    bypass = sorted(n for n, ok in readers.items() if not ok and not _exempt(n))
    assert not bypass, "ledger readers that bypass wellness/redact.py: " + ", ".join(bypass)


def test_the_redacting_readers_really_redact_and_exemptions_are_live():
    readers = _readers()
    defined = {}
    for rel, path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules, functions = _redact_aliases(tree)
        for name, fn in _scope_names(tree):
            if name.split(".")[-1] in REDACTING_APIS and rel.startswith("Orchestrator/"):
                nodes = list(_own_nodes(fn))
                defined[name.split(".")[-1]] = _redacts(nodes, modules, functions)
    assert set(defined) == REDACTING_APIS, REDACTING_APIS - set(defined)
    assert all(defined.values()), [n for n, ok in defined.items() if not ok]
    stale = [n for n in EXEMPT if not n.endswith("::*") and n not in readers]
    assert not stale, f"exemptions for functions that no longer read the ledger: {stale}"
