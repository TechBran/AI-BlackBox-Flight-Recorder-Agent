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


def test_a_gauge_shaped_line_in_the_log_tags_nothing(ledger):
    """Only the header's GAUGES block tags a snapshot: a reply quoting the
    gauge format (this very feature, a pasted snapshot) leaves its snapshot
    untagged, readable by everyone and embedded whole."""
    line = ("WELLNESS: sleep,heart · night of 2026-10-24 · ref w-20261025-7f3a · "
            "values not recorded")
    sid = mint_turn("other-human", "what does the tag look like?",
                    f"It looks like this:\n```\n{line}\nOPERATOR: {OPERATOR}\n```")
    body = snapshot_text(ledger.vol, sid)
    assert line in body and redact.parse_tag(body) is None
    assert redact.snapshot_owner(body) == "other-human"
    with _as(None):
        assert redact.for_reader(body) is body and redact.may_read(body)
    assert redact.embed_text(body) is body and ledger.embedded[-1] == body
    assert redact.redact_volume(ledger.vol.read_text(encoding="utf-8")).count(line) == 1


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


def test_the_display_reader_is_the_owner_s_own_screen(ledger):
    """A ledger read signed by the enrolled phone (GET /fossil/snapshot/{id})
    reads as the person's own screen: the box's own destination, so even a
    local-only category shows; still only their own turns, and still the
    gate's consent."""
    me = redact.display_reader(OPERATOR, ledger.rec["enrollment_id"])
    assert (me.operator, me.surface, me.credential_ok, me.surface_id, me.provider, me.model) == \
        (OPERATOR, turn_mod.SURFACE_ANDROID, True, ledger.rec["enrollment_id"],
         redact.LOCAL_SERVICE, None)
    assert _visible(ledger, me)
    assert not _visible(ledger, redact.display_reader("other-human", "en_00000022"))
    bp = mint_turn(OPERATOR, "bp?", "It was [·].", wellness={
        "categories": ["blood_pressure"], "labels": ["2026-10-26"], "refs": ["w-2"]})
    write_record(providers_allowed=[], last_push_at=ledger.rec["last_push_at"],
                 categories=ALL_ASK)
    text = snapshot_text(ledger.vol, bp)
    with _as(me):
        assert redact.for_reader(text) is text
    write_record(providers_allowed=[], last_push_at=ledger.rec["last_push_at"],
                 categories={**ALL_ASK, "blood_pressure": "off"})
    with _as(me):
        assert redact.for_reader(text) != text


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


def _assert_left_out(ledger, read):
    """A ranked reader: as nobody, the hidden turn is no candidate at all (no
    placeholder, no id); the owner on an allowed provider finds it."""
    hidden, shown = _both(ledger, read)
    if all(isinstance(x, str) and x.startswith("SNAP-") for x in hidden + shown):   # ids
        assert ledger.tagged not in hidden and ledger.plain in hidden
        assert ledger.tagged in shown
        return
    hidden_text = "\n".join(hidden)
    assert f"— {ledger.tagged} (7.1.0) ===" not in hidden_text and not _private(hidden_text)
    assert redact.PLACEHOLDER not in hidden_text and "Here is the email draft." in hidden_text
    assert "awful" in "\n".join(shown)


def test_fossils_decode_scored_and_keyword_readers(ledger):
    from Orchestrator import fossils
    rows = [(ledger.tagged, 1.0), (ledger.plain, 0.5)]
    _assert_redacts(ledger, lambda: fossils._decode_scored_snapshots(rows, OPERATOR))
    _assert_left_out(ledger, lambda: fossils.keyword_retrieve_for_operator(
        "", "sleep awful email draft", 5, OPERATOR))
    _assert_left_out(ledger, lambda: fossils.keyword_retrieve_ids_for_operator(
        "", "sleep awful email draft", 5, OPERATOR))
    vol_txt = ledger.vol.read_text(encoding="utf-8")
    _assert_left_out(ledger, lambda: fossils.keyword_retrieve(vol_txt, "sleep awful email", 5))
    _assert_left_out(ledger, lambda: fossils.keyword_retrieve_ids(vol_txt, "sleep awful email", 5))


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


def test_assert_never_splits_a_tagged_turn_on_a_line_in_its_text(ledger, monkeypatch):
    """GET /assert hands out the whole volume: a tagged turn whose text holds
    a START header (a quoted fossil), even a forged END line with its own id
    and time, is hidden to its real end."""
    from Orchestrator import checkpoint
    from Orchestrator.routes import admin_routes
    from Orchestrator.volume import parse_tail, read_tail_text
    utc = "2026-10-30T12:00:00Z"
    monkeypatch.setattr(checkpoint, "now_utc_iso", lambda: utc)
    next_id = checkpoint.next_snap_id_from_tail(
        parse_tail(read_tail_text(checkpoint.VOL_PATH))["tail_id"])
    reply = ("Here is what the page said:\n=== START SNAPSHOT (quoted from the forum)\n"
             "Sorry you felt dizzy.\n"
             f"=== END SNAPSHOT — {next_id} — UTC {utc} ===\n"
             "=== START SNAPSHOT — UTC 2026-01-01T00:00:00Z — SNAP-20260101-001 (7.1.0) ===\n"
             "awful, and your sleep was a bit less than usual.\n"
             "=== END SNAPSHOT — SNAP-20260101-001 — UTC 2026-01-01T00:00:00Z ===")
    sid = mint_turn(OPERATOR, "how did I sleep? I felt dizzy", reply, wellness=GAUGE)
    assert sid == next_id
    after = mint_turn(OPERATOR, "draft the agenda", "Here is the agenda.")
    vol = admin_routes.assert_volume()["volume"]
    assert "dizzy" not in vol and not _private(vol), vol[vol.find(sid):][:600]
    assert "Here is the email draft." in vol and "Here is the agenda." in vol and after in vol
    assert vol.count(redact.PLACEHOLDER) >= 2


def test_ranked_readers_leave_out_hidden_turns_entirely(ledger):
    """W17/W29, the ranking oracle: the ids the keyword channel returns to a
    reader who may not see a hidden turn are the same for its private words
    as for any other words (the turn is no candidate at all)."""
    from Orchestrator import fossils
    from Orchestrator.routes import task_routes
    secret = mint_turn(OPERATOR, "my migraine aura came back after the new pills",
                       "I'm sorry the migraine is back.", wellness=GAUGE)
    with _as(None):
        hits = fossils.keyword_retrieve_ids_for_operator("", "migraine aura pills", 5, OPERATOR)
        other = fossils.keyword_retrieve_ids_for_operator("", "pizza dough recipe", 5, OPERATOR)
        res = task_routes.fossil_hybrid_search(q="migraine aura pills", operator="", limit=5)
    assert secret not in hits and ledger.tagged not in hits and hits == other
    assert secret not in str(res)
    with _as(OWNER):
        assert secret in fossils.keyword_retrieve_ids_for_operator(
            "", "migraine aura pills", 5, OPERATOR)


def test_the_semantic_channel_searches_again_without_a_hidden_turn(ledger, monkeypatch):
    """A vector built from a tagged turn (an embedder its owner allowed)
    ranks it for its private words: for a reader who may not see it, the
    store is searched again without it, as if it were not there."""
    import numpy as np

    from Orchestrator import retrieval
    vectors = {ledger.tagged: [1.0, 0.0, 0.0, 0.0], ledger.plain: [0.8, 0.6, 0.0, 0.0],
               ledger.other: [0.0, 1.0, 0.0, 0.0]}
    calls = []

    class Store:
        dims, slug, count = 4, None, 3

        def search_with_vectors(self, q, k, allowed_ids=None, with_ordinals=False):
            calls.append(None if allowed_ids is None else set(allowed_ids))
            rows = [(sid, float(np.dot(v, q)), np.asarray(v, dtype=np.float32))
                    for sid, v in vectors.items() if allowed_ids is None or sid in allowed_ids]
            rows.sort(key=lambda r: r[1], reverse=True)
            return [r + (0,) for r in rows[:k]] if with_ordinals else rows[:k]

    monkeypatch.setattr(retrieval, "keyword_retrieve_ids_for_operator", lambda *a: [])

    def run(reader, operator):
        calls.clear()
        with _as(reader):
            return [sid for sid, _s in retrieval.retrieve(
                "how did I sleep", operator=operator, k=5, include_keyword=False,
                store=Store(), query_vector=[1.0, 0.0, 0.0, 0.0])]

    nobody = run(None, OPERATOR)
    assert ledger.tagged not in nobody and ledger.plain in nobody
    assert len(calls) == 2 and ledger.tagged not in calls[1]
    assert ledger.tagged not in run(None, "system")                 # every operator's ids
    shown = run(OWNER, OPERATOR)
    assert shown[0] == ledger.tagged and len(calls) == 1             # the owner: one search


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
# Reading snapshot text out of the ledger: a primitive (called, or handed on).
PRIMITIVE_CALLS = {"VolumeRanges", "read_volume_bytes", "read_tail_text", "_fallback_volume_text"}
# The ledger's path, by name (VOL_PATH, config.VOL_PATH) or by call.
LEDGER_PATHS = {"VOL_PATH", "VOLUME_FILE"}
LEDGER_PATH_CALLS = {"ledger_path"}
# What turns a path into bytes or text.
OPENERS = {"open", "read_bytes", "read_text", "read_text_safe", "view", "LedgerReader", "mmap"}
# The redact module's calls that redact; anything else it offers (is_tagged,
# reading_as, background_reader, rerank_destination ...) does not count.
REDACT_FUNCS = {"for_reader", "redact", "redact_volume", "embed_text", "hide", "may_read"}
# Readers that redact what they hand out (each must itself call redact).
REDACTING_APIS = {
    "get_snapshot_by_id", "get_recent_fossils_for_operator", "hybrid_retrieve",
    "semantic_retrieve", "keyword_retrieve", "keyword_retrieve_for_operator",
    "keyword_retrieve_ids", "keyword_retrieve_ids_for_operator", "_keyword_retrieve_scored",
    "_keyword_retrieve_for_operator_scored", "_decode_scored_snapshots",
    "get_latest_checkpoint_for_operator", "get_recent_checkpoints_for_operator",
    "_decode_snapshot_text", "slice_snapshot_text", "get_recent_flight_reports",
    "hidden_from_reader", "_decide_hidden",
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
# The readers only the wider path/opener scan finds (an attribute path, a
# path in a local variable, a ledger_path() call); each names why it is safe.
EXEMPT.update({
    "Orchestrator/ledger_fsck.py::run": "the ledger check: offsets, hashes and counts only",
    "Orchestrator/voice_transcript_spool.py::ledger_has": "a marker search: True or False, never text",
    "Orchestrator/routes/admin_routes.py::health": "the ledger's size (stat) only",
    "MCP/blackbox_mcp_server.py::call_tool": "the ledger's path, existence and size only "
                                             "(its open is an audio upload)",
})


def _redact_aliases(tree: ast.AST):
    """(names bound to the redact module, {local name: redact function})."""
    modules, functions = set(), {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "Orchestrator.wellness":
                modules |= {a.asname or a.name for a in node.names if a.name == "redact"}
            if node.module == "Orchestrator.wellness.redact":
                functions.update({a.asname or a.name: a.name for a in node.names})
        elif isinstance(node, ast.Import):
            modules |= {a.asname for a in node.names
                        if a.name == "Orchestrator.wellness.redact" and a.asname}
    return modules, functions


def _ledger_aliases(tree: ast.AST):
    """({local name: primitive} for primitives imported under another name,
    names bound to Orchestrator.ledger)."""
    primitives, ledgers = {}, {"ledger"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in PRIMITIVE_CALLS and a.asname:
                    primitives[a.asname] = a.name
                if node.module == "Orchestrator" and a.name == "ledger":
                    ledgers.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            ledgers |= {a.asname for a in node.names if a.name == "Orchestrator.ledger" and a.asname}
    return primitives, ledgers


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


def _name(node):
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) \
        else None


def _reads_ledger(nodes, aliases=({}, {"ledger"})) -> bool:
    primitives, ledgers = aliases
    nodes = list(nodes)
    # parse_tail(read_tail_text(...)) reads the tail's id and time, no text.
    tail_parse = {id(a.func) for n in nodes if isinstance(n, ast.Call)
                  and getattr(n.func, "id", None) == "parse_tail"
                  for a in n.args if isinstance(a, ast.Call)}
    uses_path = opens = False
    for n in nodes:
        name = _name(n)
        # A primitive called, or handed on (run_blocking(read_volume_bytes, ...)).
        if name is not None and id(n) not in tail_parse and \
                primitives.get(name, name) in PRIMITIVE_CALLS and \
                not isinstance(getattr(n, "ctx", None), ast.Store):
            return True
        # Orchestrator.ledger's views, under any name.
        if isinstance(n, ast.Attribute) and n.attr in ("view", "LedgerReader") and \
                getattr(n.value, "id", None) in ledgers:
            return True
        if name in LEDGER_PATHS and not isinstance(getattr(n, "ctx", None), ast.Store):
            uses_path = True
        if isinstance(n, ast.Call):
            called = _name(n.func)
            if called in LEDGER_PATH_CALLS:
                uses_path = True
            if called in OPENERS:
                opens = True
    # The path (by name, attribute or call, or through a local variable) and
    # something that reads a file, in one function.
    return uses_path and opens


def _redacts(nodes, modules, functions) -> bool:
    for n in nodes:
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute) and getattr(f.value, "id", None) in modules \
                and f.attr in REDACT_FUNCS:
            return True
        if isinstance(f, ast.Name) and functions.get(f.id) in REDACT_FUNCS:
            return True
        if _name(f) in REDACTING_APIS:
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
        aliases = _ledger_aliases(tree)
        scopes = _scope_names(tree)
        top = [n for n in ast.iter_child_nodes(tree)
               if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        top_nodes = [x for n in top for x in ast.walk(n)]
        if _reads_ledger(top_nodes, aliases):
            found[f"{rel}::<module>"] = _redacts(top_nodes, modules, functions)
        for name, fn in scopes:
            nodes = list(_own_nodes(fn))
            if _reads_ledger(nodes, aliases):
                found[f"{rel}::{name}"] = _redacts(nodes, modules, functions)
    return found


def _exempt(name: str) -> bool:
    rel = name.split("::", 1)[0]
    return name in EXEMPT or f"{rel}::*" in EXEMPT


def test_every_slice_for_an_embedder_names_the_embedder():
    """slice_snapshot_text applies the embedding rule for the model it is
    given (default: the ACTIVE one); a caller that embeds with another model
    (a re-embed, a candidate store) must name it, or a tagged turn's text
    reaches an embedder its owner never allowed."""
    calls = []
    for root in (REPO / "Orchestrator", REPO / "scripts"):
        for p in sorted(root.rglob("*.py")):
            if "/tests/" in p.as_posix():
                continue
            for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if isinstance(n, ast.Call) and _name(n.func) == "slice_snapshot_text":
                    calls.append((p.relative_to(REPO).as_posix(), n.lineno,
                                  len(n.args) + len(n.keywords)))
    assert len(calls) >= 5, calls
    assert all(argc >= 4 for _p, _l, argc in calls), [c for c in calls if c[2] < 4]


def test_the_gate_finds_attribute_paths_aliases_and_fake_redaction():
    """The A13 scan itself: a path by attribute or in a local variable, an
    aliased primitive or ledger module, all count as reading the ledger; a
    call into the redact module that does not redact (is_tagged) does not
    count as redacting."""
    src = """
from Orchestrator import config, ledger as L
from Orchestrator.volume import read_volume_bytes as rvb
from Orchestrator.wellness import redact as wellness_redact
from Orchestrator.wellness.redact import is_tagged, for_reader as fr

def leak_a(sid):
    return open(config.VOL_PATH, 'rb').read().decode()

def leak_b(meta):
    view = L.view(config.VOL_PATH)
    wellness_redact.is_tagged('')
    is_tagged('')
    return view.text(0, 10)

def leak_c():
    return rvb('x')

def leak_d():
    p = ledger_path()
    with open(p, 'rb') as f:
        return f.read()

def ok_e():
    return fr(open(config.VOL_PATH).read())
"""
    tree = ast.parse(src)
    modules, functions = _redact_aliases(tree)
    aliases = _ledger_aliases(tree)
    found = {name: (_reads_ledger(list(_own_nodes(fn)), aliases),
                    _redacts(list(_own_nodes(fn)), modules, functions))
             for name, fn in _scope_names(tree)}
    assert found == {"leak_a": (True, False), "leak_b": (True, False), "leak_c": (True, False),
                     "leak_d": (True, False), "ok_e": (True, True)}, found


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
