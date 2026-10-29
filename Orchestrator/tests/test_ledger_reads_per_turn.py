"""How much of the ledger a chat turn, a memory search and a snapshot fetch read.

Audit rank 45 (X05-architecture-14, O02-chat-routes-A-04, O03-chat-routes-B-01):
the volume is a single append-only file (58 MB on the dev box, growing with
every mint). A /chat/stream turn read it whole five times - once decoded to a
str for a parameter every index-backed helper ignores, then once more per
section - and every search_snapshots tool call read and decoded it again only
to hand hybrid_retrieve an argument it never used. The snapshot index already
carries each snapshot's byte range, so every section now reads just the
snapshots it returns, and the keyword scan just the snapshots it scores: the
asking operator's, or every operator's for "system".

These tests run against a real ledger and index written under tmp_path and
count the bytes actually read from the volume file, so they hold whatever the
implementation reads through.
"""
from __future__ import annotations

import asyncio
import builtins
import io

import pytest

from Orchestrator import fossils
from Orchestrator.config import VOL_PATH

OPS = ("Ana", "Ben", "Cy")


def _block(snap_id: str, utc: str, operator: str, body: str) -> str:
    start = f"=== START SNAPSHOT — UTC {utc} — {snap_id} (7.1) ===\r\n"
    end = f"=== END SNAPSHOT — {snap_id} — UTC {utc} ===\r\n"
    return (f"{start}OPERATOR: {operator}\r\nTYPE: normal\r\n"
            f"Raw Session Log\r\n[1] {utc} operator={operator} user: {body}\r\n{end}")


@pytest.fixture
def ledger(isolated_stores):
    """A 120-snapshot ledger for three operators (multi-byte text throughout,
    so a char/byte offset mix-up shows) plus its byte-offset index."""
    parts = []
    for i in range(1, 121):
        op = OPS[i % 3]
        body = (f"turn {i} for {op} — reranker latency and the event loop "
                f"{'déjà vu ' * (i % 5)}🙂 " + "filler words " * 40)
        if i % 7 == 0:
            body = "[CHECKPOINT] compressed summary " + body
        parts.append(_block(f"SNAP-20261001-{i}", f"2026-10-01T{i // 60:02d}:{i % 60:02d}:00Z", op, body))
    VOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    VOL_PATH.write_bytes("".join(parts).encode("utf-8"))
    index = fossils.rebuild_snapshot_index()
    assert len(index) == 120
    assert sum(1 for m in index.values() if m["type"] == "checkpoint") == 17
    return index


@pytest.fixture
def volume_reads(monkeypatch):
    return count_volume_reads(monkeypatch)


def count_volume_reads(monkeypatch) -> dict:
    """Bytes read from the volume file from now on: through open() (open()
    and io.open are both wrapped, so a pathlib read counts too) or served
    from the shared ledger map (Orchestrator/ledger.py views: read, slice,
    and a whole-view buffer, which counts as the whole file)."""
    try:
        from Orchestrator import ledger as ledger_mod
    except ImportError:  # a tree from before the shared reader
        ledger_mod = None
    counter = {"bytes": 0}
    real_open = builtins.open

    def counting_open(file, mode="r", *a, **k):
        f = real_open(file, mode, *a, **k)
        name = str(file) if not isinstance(file, int) else ""
        if name.endswith("SNAPSHOT_VOLUME.txt") and "r" in mode and "+" not in mode:
            real_read = f.read

            def read(n=-1):
                data = real_read(n)
                counter["bytes"] += len(data)
                return data
            f.read = read
        return f

    def counted(name):
        real = getattr(ledger_mod.LedgerView, name)

        def method(self, *args):
            out = real(self, *args)
            if self.path.endswith("SNAPSHOT_VOLUME.txt"):
                counter["bytes"] += len(out)
            return out
        return method

    monkeypatch.setattr(builtins, "open", counting_open)
    monkeypatch.setattr(io, "open", counting_open)
    for name in ("read", "slice", "buffer") if ledger_mod else ():
        monkeypatch.setattr(ledger_mod.LedgerView, name, counted(name))
    return counter


def _fake_retrieve(index):
    """retrieve() without an embedding provider: each operator's newest ids."""
    def retrieve(query, operator="", k=10, **kw):
        ids = sorted((s for s, m in index.items() if not operator or m["operator"] == operator),
                     key=lambda s: int(s.rsplit("-", 1)[1]), reverse=True)[:k]
        rows = [(s, 1.0 / (n + 1)) for n, s in enumerate(ids)]
        return [(s, sc, None) for s, sc in rows] if kw.get("return_provenance") else rows
    return retrieve


def _size(index, ids):
    return sum(index[s]["byte_end"] - index[s]["byte_start"] for s in ids)


def _owned(index, op):
    return [s for s, m in index.items() if m["operator"] == op]


@pytest.fixture
def whole_reads(monkeypatch):
    """Calls that read the whole volume file, through either helper, from any
    module that imported them by name."""
    import importlib
    calls = []
    for mod_name in ("Orchestrator.fossils", "Orchestrator.volume", "Orchestrator.context_builder",
                     "Orchestrator.retrieval", "Orchestrator.tasks"):
        mod = importlib.import_module(mod_name)
        for fn_name in ("read_volume_bytes", "read_text_safe"):
            real = getattr(mod, fn_name, None)
            if real is None:
                continue

            def spy(p, *a, _real=real, _name=fn_name, **k):
                if str(p).endswith("SNAPSHOT_VOLUME.txt"):
                    calls.append(_name)
                return _real(p, *a, **k)
            monkeypatch.setattr(mod, fn_name, spy)
    return calls


# -- the per-turn context build ------------------------------------------------


def _run(call):
    """The snapshot executors are plain defs since M4 U23 (the dispatcher runs
    them in a worker thread); an async executor is awaited the same way."""
    import inspect
    return asyncio.run(call) if inspect.iscoroutine(call) else call

def test_stream_turn_reads_only_the_operators_snapshots(ledger, volume_reads, whole_reads, monkeypatch):
    """Every section reads just its own snapshots, and the keyword channel just
    the ones it scores - this operator's, by their byte ranges. Before: the
    volume was read whole 5 times per turn, then (first pass of this fix) once
    for the keyword scan."""
    from Orchestrator import context_builder, retrieval
    monkeypatch.setattr(retrieval, "retrieve", _fake_retrieve(ledger))
    vol_size = VOL_PATH.stat().st_size
    ana = _size(ledger, _owned(ledger, "Ana"))

    _ctx, prov = context_builder.build_fossil_context(
        "reranker latency", "Ana", provider="anthropic", include_media=False)

    assert prov["recent"] and prov["semantic"] and prov["checkpoint"] and prov["keyword"]
    assert whole_reads == []
    assert ana <= volume_reads["bytes"] < 2 * ana < vol_size, (
        f"read {volume_reads['bytes']:,} bytes of a {vol_size:,}-byte volume "
        f"(the operator owns {ana:,})")


def test_keyword_scan_reads_exactly_the_snapshots_it_scores(ledger, volume_reads, whole_reads):
    got = fossils.keyword_retrieve_for_operator("", "reranker latency déjà", 6, "Ben")
    assert got and whole_reads == []
    assert volume_reads["bytes"] == _size(ledger, _owned(ledger, "Ben"))

    volume_reads["bytes"] = 0
    # "system" scores every operator's snapshots: one range read each.
    assert fossils.keyword_retrieve_for_operator("", "reranker latency", 6, "system")
    assert volume_reads["bytes"] == _size(ledger, ledger)

    volume_reads["bytes"] = 0
    # A query with nothing to match reads nothing.
    assert fossils.keyword_retrieve_for_operator("", "", 6, "Ana") == []
    assert volume_reads["bytes"] == 0
    assert whole_reads == []


def test_retrieve_keyword_channel_reads_no_whole_volume(ledger, volume_reads, whole_reads):
    """search_snapshots and every turn reach the keyword scan through
    retrieve()'s keyword channel."""
    ids = fossils.keyword_retrieve_ids_for_operator("", "reranker latency", 20, "Cy")
    assert ids and all(ledger[s]["operator"] == "Cy" for s in ids)
    assert whole_reads == []
    assert volume_reads["bytes"] == _size(ledger, _owned(ledger, "Cy"))


def test_context_without_the_keyword_channel_never_reads_the_whole_volume(
        ledger, volume_reads, monkeypatch):
    from Orchestrator import context_builder, retrieval
    monkeypatch.setattr(retrieval, "retrieve", _fake_retrieve(ledger))

    _ctx, prov = context_builder.build_fossil_context(
        "reranker latency", "Ben", provider="anthropic", include_keyword=False,
        include_media=False)

    delivered = [s for ids in prov.values() for s in ids]
    assert delivered
    # The semantic over-fetch decodes a few more ids than survive dedupe.
    assert volume_reads["bytes"] < VOL_PATH.stat().st_size // 2
    assert volume_reads["bytes"] >= _size(ledger, delivered)


# -- memory search tool calls --------------------------------------------------

def test_search_snapshots_tool_reads_no_volume_text(ledger, volume_reads, monkeypatch):
    """The ToolVault executor (MCP, chat tool loops, phone) used to read and
    decode the whole volume before every search for an argument that is unused."""
    from Orchestrator.toolvault.context import ToolContext
    from ToolVault.tools.search_snapshots import executor
    seen = []

    def fake_hybrid(vol_txt, query, k=3, operator="", window_budget_chars=None):
        seen.append(vol_txt)
        return []
    monkeypatch.setattr(fossils, "hybrid_retrieve", fake_hybrid)

    out = _run(executor.execute({"query": "reranker"}, ToolContext(operator="Ana")))

    assert out.success and seen == [""]
    assert volume_reads["bytes"] == 0


def test_list_recent_snapshots_tool_reads_only_what_it_returns(ledger, volume_reads):
    from Orchestrator.toolvault.context import ToolContext
    from ToolVault.tools.list_recent_snapshots import executor

    out = _run(executor.execute({"count": 3}, ToolContext(operator="Ben")))

    assert out.success and out.data["count"] == 3
    newest_ben = sorted((s for s, m in ledger.items() if m["operator"] == "Ben"),
                        key=lambda s: int(s.rsplit("-", 1)[1]))[-3:]
    assert volume_reads["bytes"] == _size(ledger, newest_ben)


_VOICE_EXECUTORS = [
    ("Orchestrator.routes.grok_live_routes", "_execute_grok_tool"),
    ("Orchestrator.routes.realtime_routes", "_execute_openai_tool"),
    ("Orchestrator.routes.gemini_live_routes", "_execute_gemini_tool"),
]


@pytest.mark.parametrize("operator", ["Ana", "system"])
@pytest.mark.parametrize("module,executor", _VOICE_EXECUTORS)
def test_voice_recent_snapshots_read_only_what_they_return(ledger, volume_reads, module, executor,
                                                           operator):
    """The live-voice prompts open every session with list_recent_snapshots.
    Its Grok, OpenAI and Gemini handlers slice their snapshots out of
    read_volume_bytes, which read the whole ledger to return three of them
    (audit O05-ledger-persistence-18); it serves the shared map now. "system"
    sees every operator's snapshots."""
    import importlib
    from types import SimpleNamespace
    raw = VOL_PATH.read_bytes()
    newest = sorted((s for s, m in ledger.items() if operator == "system" or m["operator"] == operator),
                    key=lambda s: int(s.rsplit("-", 1)[1]))[-3:]
    volume_reads["bytes"] = 0  # the test's own read above

    run = getattr(importlib.import_module(module), executor)
    out = asyncio.run(run(SimpleNamespace(operator=operator), "list_recent_snapshots", {"count": 3}))

    for sid in newest:
        lo, hi = ledger[sid]["byte_start"], ledger[sid]["byte_end"]
        assert f"--- {sid} (operator: {ledger[sid]['operator']}) ---\n" + raw[lo:hi].decode() in out
    assert volume_reads["bytes"] == _size(ledger, newest), (
        f"read {volume_reads['bytes']:,} bytes of a {len(raw):,}-byte ledger "
        f"for 3 snapshots ({_size(ledger, newest):,} bytes)")


def test_hybrid_retrieve_defaults_vol_txt_and_reads_only_its_results(
        ledger, volume_reads, monkeypatch):
    from Orchestrator import retrieval
    monkeypatch.setattr(retrieval, "retrieve", _fake_retrieve(ledger))

    texts = fossils.hybrid_retrieve(query="reranker", k=4, operator="Ana")

    ids = fossils.extract_snap_ids(texts)
    assert len(ids) == 4
    assert volume_reads["bytes"] == _size(ledger, ids)


# Files whose hot paths fed vol_txt to helpers that ignore it. A whole-volume
# read must not come back into any of them.
_NO_VOLUME_TEXT = [
    "Orchestrator/context_builder.py",
    "Orchestrator/tasks.py",
    "Orchestrator/routes/chat_routes.py",
    "Orchestrator/routes/chat/context.py",
    "Orchestrator/routes/chat/cu.py",
    "Orchestrator/routes/chat/media.py",
    "Orchestrator/routes/chat/nonstream.py",
    "Orchestrator/routes/chat/providers.py",
    "Orchestrator/routes/chat/save.py",
    "Orchestrator/routes/chat/stream.py",
    "Orchestrator/providers/tool_dispatch.py",
    "Orchestrator/routes/gemini_live_routes.py",
    "Orchestrator/routes/grok_live_routes.py",
    "Orchestrator/routes/realtime_routes.py",
    "Orchestrator/voice/tools.py",
    "Orchestrator/browser/driver_anthropic.py",
    "ToolVault/tools/search_snapshots/executor.py",
    "ToolVault/tools/list_recent_snapshots/executor.py",
]


@pytest.mark.parametrize("rel", _NO_VOLUME_TEXT)
def test_no_whole_volume_text_read_on_the_hot_paths(rel):
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / rel).read_text(encoding="utf-8")
    for needle in ("read_text_safe(VOL_PATH)", "read_text_safe, VOL_PATH"):
        assert needle not in src, f"{rel} still reads the whole volume as text ({needle})"


# -- the index-backed helpers ignore vol_txt, the no-index fallback reads lazily

def _all_helpers(vol_txt):
    out = {}
    for op in OPS + ("system",):
        out[f"recent:{op}"] = fossils.get_recent_fossils_for_operator(vol_txt, op, 5, None)
        out[f"recent_all:{op}"] = fossils.get_recent_fossils_for_operator(vol_txt, op, 0, None)
        out[f"recent_cap:{op}"] = fossils.get_recent_fossils_for_operator(vol_txt, op, 3, 120)
        out[f"ckpt:{op}"] = fossils.get_recent_checkpoints_for_operator(vol_txt, op, count=2)
        out[f"latest_ckpt:{op}"] = fossils.get_latest_checkpoint_for_operator(vol_txt, op)
        out[f"kw:{op}"] = fossils.keyword_retrieve_for_operator(vol_txt, "reranker latency déjà", 6, op)
    return out


def test_index_backed_helpers_give_the_same_answer_without_vol_txt(ledger):
    full = fossils.read_text_safe(VOL_PATH)
    with_text = _all_helpers(full)
    assert with_text["recent:Ana"] and with_text["ckpt:Ben"] and with_text["kw:Ana"]
    assert _all_helpers("") == with_text


def test_no_index_fallback_reads_the_volume_itself(ledger, monkeypatch):
    """A box whose index is missing still answers when the caller passes ""."""
    full = fossils.read_text_safe(VOL_PATH)
    monkeypatch.setattr(fossils, "load_snapshot_index", lambda: {})
    with_text = _all_helpers(full)
    assert with_text["recent:Ana"] and with_text["kw:Ana"]
    assert _all_helpers("") == with_text


def test_recent_fossils_cap_and_count_match_the_whole_read(ledger):
    newest = fossils.get_recent_fossils_for_operator("", "Ana", 2, None)
    assert fossils.extract_snap_ids(newest) == ["SNAP-20261001-117", "SNAP-20261001-120"]
    capped = fossils.get_recent_fossils_for_operator("", "Ana", 2, 120)
    assert [len(t) <= 120 + 64 for t in capped] == [True, True]


def test_index_past_eof_keeps_the_old_partial_and_missing_rules(ledger, monkeypatch, volume_reads):
    """Index/volume drift (a truncated volume): a snapshot that starts inside
    the file comes back partial, one that starts past it comes back empty."""
    size = VOL_PATH.stat().st_size
    drifted = {
        "SNAP-20261001-1": dict(ledger["SNAP-20261001-1"]),
        "SNAP-20261002-1": {"byte_start": size - 10, "byte_end": size + 500,
                            "operator": "Ben", "timestamp": "x", "type": "normal"},
        "SNAP-20261002-2": {"byte_start": size + 10, "byte_end": size + 900,
                            "operator": "Ben", "timestamp": "x", "type": "normal"},
    }
    drifted["SNAP-20261001-1"]["operator"] = "Ben"
    monkeypatch.setattr(fossils, "load_snapshot_index", lambda: drifted)
    raw = VOL_PATH.read_bytes()

    texts = fossils.get_recent_fossils_for_operator("", "Ben", 0, None)

    lo, hi = ledger["SNAP-20261001-1"]["byte_start"], ledger["SNAP-20261001-1"]["byte_end"]
    assert texts == [raw[lo:hi].decode(), raw[size - 10:].decode("utf-8", "replace"), ""]
    assert fossils.get_snapshot_by_id("SNAP-20261002-1") is None
    assert fossils.get_snapshot_by_id("SNAP-20261002-2") is None


# -- the per-id snapshot fetch (Portal Timeline, Android Timeline, SNAP-id links)

def test_get_snapshot_by_id_reads_only_that_snapshot(ledger, volume_reads):
    meta = ledger["SNAP-20261001-17"]
    got = fossils.get_snapshot_by_id("SNAP-20261001-17")

    raw = VOL_PATH.read_bytes()
    volume_reads["bytes"] -= len(raw)  # the test's own comparison read
    assert got["content"] == raw[meta["byte_start"]:meta["byte_end"]].decode("utf-8")
    assert got["metadata"] == {"operator": "Cy", "timestamp": "2026-10-01T00:17:00Z",
                               "type": "normal", "byte_start": meta["byte_start"],
                               "byte_end": meta["byte_end"]}
    assert volume_reads["bytes"] == meta["byte_end"] - meta["byte_start"]
    assert fossils.get_snapshot_by_id("SNAP-19990101-1") is None


def test_flight_recorder_pin_reads_only_its_reports(ledger, volume_reads, monkeypatch):
    """Every Flight Recorder turn pins its latest reports; that read only
    their byte ranges now, not the whole volume."""
    from Orchestrator import oversight
    from Orchestrator.config import FLIGHT_RECORDER_OPERATOR
    reports = {}
    for sid in ("SNAP-20261001-50", "SNAP-20261001-60", "SNAP-20261001-70"):
        reports[sid] = dict(ledger[sid], operator=FLIGHT_RECORDER_OPERATOR, type="flight_report")
    monkeypatch.setattr(fossils, "load_snapshot_index", lambda: {**ledger, **reports})

    pinned = oversight.get_recent_flight_reports(count=2)

    assert fossils.extract_snap_ids(pinned) == ["SNAP-20261001-60", "SNAP-20261001-70"]
    assert volume_reads["bytes"] == _size(ledger, ["SNAP-20261001-60", "SNAP-20261001-70"])
