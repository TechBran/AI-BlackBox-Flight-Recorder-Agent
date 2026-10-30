"""The ledger rule's mask (spec §3.7, W27, W28; check A2).

No health value is ever minted: a turn a wellness result reached records its
assistant text with every number of its delivered payloads (and every number
next to a health unit) masked as [·], its [REASONING] block omitted, and the
WELLNESS tag on its snapshot, while the user's own words stay verbatim. The
delivered rows are the durable carry: a save after a restart still masks.
Every other turn is recorded exactly as before.

The property tests (A2) run over the fixture answers that carry the leak-scan
sentinels (sleeping HR 47.3, 12,345 steps, 118/79) and over the replies
recorded in every provider golden (Orchestrator/tests/golden/providers).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import random
import re
import time
from pathlib import Path

import pytest

from Orchestrator.tests.wellness_ledger_support import (ALL_ASK, FX, deliver, envelopes,
                                                        ledger_box, snapshot_text)
from Orchestrator.tests.wellness_support import OPERATOR, enrolled_fixture, roster
from Orchestrator.wellness import mask, store, turn as turn_mod

GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "providers"
SENTINELS = FX.SENTINEL_STRINGS


def _hits(text: str):
    return [s for s in SENTINELS if s in text]


def _gauge(body: str) -> str:
    """The snapshot's WELLNESS gauge line ("" when untagged)."""
    return next((line for line in body.splitlines() if line.startswith("WELLNESS: ")), "")


@pytest.fixture(scope="module")
def answers(tmp_path_factory):
    """The fixture answers, computed once (no delivery written)."""
    mp = pytest.MonkeyPatch()
    try:
        roster(mp, OPERATOR, "other-human")
        enrolled_fixture(tmp_path_factory.mktemp("answers"), mp, categories=ALL_ASK)
        yield envelopes()
    finally:
        mp.undo()


def _golden_replies():
    """Every assistant text a provider golden recorded (stream `content`/`done`
    and `tool_result` frames, non-stream results)."""
    out = []
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        obs = json.loads(path.read_text())["observed"]
        for block in (obs.get("sse") or "").split("\n\n"):
            m = re.match(r"event: (done|tool_result|content)\ndata: (.*)", block, re.S)
            if not m:
                continue
            data = json.loads(m.group(2))
            text = data.get("content") if isinstance(data, dict) else data
            if isinstance(text, str) and text.strip():
                out.append(text)
        result = obs.get("result")
        if isinstance(result, list) and result and isinstance(result[0], str) and result[0]:
            out.append(result[0])
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------ the value set

def test_the_sentinels_are_in_the_answers(answers):
    blob = json.dumps(answers)
    assert "47.3" in blob and "12345" in blob and '"min": 118.0' in blob and '"max": 79.0' in blob


def test_value_set_holds_numbers_displays_words_durations_and_clocks(answers):
    v = mask.value_set(answers.values())
    for member in ("47.3", "47", "forty-seven", "forty seven point three", "12345", "12,345",
                   "12 345", "12.3k", "118", "79", "forty-seven beats per minute",
                   "6 h 47 min", "6:47", "6 h 47", "about seven hours", "seven hours",
                   "23:34", "11:34 pm"):
        assert member in v, member
    # Counts and coverage are not values: "worn 4 of 7 nights" stays readable,
    # and a series' coverage_pct (100) is not a reading.
    assert "4" not in v and "7" not in v
    assert "100" not in mask.value_set([answers["series"]])


def test_dates_and_sync_times_are_not_masked(answers):
    v = mask.value_set([answers["night"]])
    text = "As of 2026-10-28 (synced 11:55), the night of 2026-10-22 was worn all night."
    assert mask.mask_text(text, v) == text


# ------------------------------------------------------------------ A2 property tests

_PHRASES = (
    "{label} was {display}.", "{label}: {spoken}.", "It came to {raw}.", "roughly {rounded}",
    "{raw} {unit}", "{grouped} {unit} in total", "about {words} {unit}", "({raw})",
    "**{display}**", "- {label}: {raw}", "| {label} | {display} |", "{words}",
)


def _facts(env):
    out = []
    for m in env.get("metrics") or []:
        for key in ("value", "min", "max"):
            v = m.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append((m["label"], v, m.get("display") or "", m.get("spoken") or "",
                            m.get("unit") or ""))
    cols, units = env.get("columns") or [], env.get("units") or {}
    for row in env.get("rows") or []:
        for col, cell in zip(cols, row):
            if col in units and isinstance(cell, (int, float)) and not isinstance(cell, bool):
                out.append((col, cell, f"{cell:g}", "", units[col]))
    for s in env.get("sessions") or []:
        for key in ("avg_hr", "max_hr", "duration_min", "distance_m"):
            if isinstance(s.get(key), (int, float)):
                out.append((key, s[key], s.get("duration") or "", "", "bpm"))
    for p in env.get("points") or []:
        if isinstance(p.get("value"), (int, float)):
            out.append(("heart rate", p["value"], p.get("display") or "", "", "bpm"))
    return out


def _render(fact, rng):
    label, v, display, spoken, unit = fact
    from Orchestrator.wellness import catalog
    r = int(round(v))
    phrase = rng.choice(_PHRASES)
    return phrase.format(label=label, display=display or f"{v:g}", spoken=spoken or str(r),
                         raw=f"{v:g}", rounded=str(r), unit=unit, grouped=f"{r:,}",
                         words=catalog.number_words(r))


@pytest.mark.parametrize("seed", range(6))
def test_no_sentinel_and_no_payload_number_survives_the_mask(answers, seed):
    """A2: replies built from the fixture answers (their raw values, display
    and spoken strings, rounded, grouped and in words), spliced into every
    reply a provider golden recorded, come out of mask() with no sentinel and
    no number of the payload left."""
    rng = random.Random(seed)
    goldens = _golden_replies()
    assert goldens
    envs = list(answers.values())
    for _case in range(60):
        chosen = rng.sample(envs, rng.randint(1, 3))
        rows = [{"ref": e["ref"], "payload": e, "categories": ["heart"]} for e in chosen]
        facts = [f for e in chosen for f in _facts(e)]
        said = [_render(f, rng) for f in rng.sample(facts, min(len(facts), rng.randint(3, 12)))]
        names = {name for name, env in answers.items() if env in chosen}
        if "bp" in names:
            said.append(rng.choice(("Your blood pressure was 118/79 this morning.", "118/79")))
        if "night" in names:
            said.append(rng.choice(("Sleeping heart rate 47.3", "47.3", "(47.3)")))
        if "steps" in names:
            said.append(rng.choice(("You walked 12,345 steps.", "12345", "12,345!")))
        reply = f"{rng.choice(goldens)}\n\n" + " ".join(said) + f"\n\n{rng.choice(goldens)}"
        snap = f"[REASONING]\nThe tool said 47.3 and 12345.\n\n[RESPONSE]\n{reply}"
        out, cats, refs = mask.mask(snap, rows)
        assert not _hits(out), (seed, out)
        assert out.startswith(mask.REASONING_OMITTED)
        assert refs == [e["ref"] for e in chosen] and cats == ["heart"]
        for _label, v, _d, _s, _u in facts:
            if abs(v) >= 10 or not float(v).is_integer():
                for form in {f"{v:g}", str(int(round(v)))}:
                    assert not re.search(rf"(?<![\d.,]){re.escape(form)}(?![\d])", out), \
                        (form, out)


def test_recorded_provider_replies_pass_through_unchanged():
    """The masker has no false positives on ordinary replies: every golden
    reply (tool results with ports, counts and ids included) is unchanged."""
    for text in _golden_replies():
        assert mask.mask_text(text, frozenset()) == text, text


def test_the_unit_rule_masks_numbers_the_model_computed():
    cases = {
        "about 5% lower than usual": "about [·]% lower than usual",
        "a 6.1 hour night of sleep": "a [·] hour night of sleep",
        "you burned 2,345 kcal": "you burned [·] kcal",
        "a resting HR of 61": "a resting HR of [·]",
        "you slept 7 hours": "you slept [·] hours",
        "around eleven thousand steps": "around [·] steps",
        "it read 118/79 mmHg": "it read [·] mmHg",
        "SpO2 was 97": "SpO2 was [·]",
        "weight 81.4 kg": "weight [·] kg",
    }
    for text, expected in cases.items():
        assert mask.mask_text(text, frozenset()) == expected, text
    for keep in ("call me in 10 minutes", "3 meetings at 14:00", "version 2.0 of the app",
                 "port 8765 on the box"):
        assert mask.mask_text(keep, frozenset()) == keep


def test_reasoning_block_is_omitted_in_both_layouts():
    save = "[REASONING]\nsecret 47.3\n\n[RESPONSE]\nanswer"
    assert mask.drop_reasoning(save) == f"{mask.REASONING_OMITTED}\n\n[RESPONSE]\nanswer"
    worker = "answer\n\nKeywords: a, b\n\n[REASONING]\nthought 12345\nmore"
    assert mask.drop_reasoning(worker) == f"answer\n\nKeywords: a, b\n\n{mask.REASONING_OMITTED}"
    assert mask.drop_reasoning("no block here") == "no block here"


def test_the_gauge_line_renders_only_when_present():
    from Orchestrator.monitoring import render_snapshot_body_v71
    info = {"tail_id": "SNAP-20261001-0001", "utc": "2026-10-01T00:00:00Z"}
    gauges = {"drift": "green", "p": 1, "c": 2, "t": 3, "model": "m", "operator": "op",
              "location": "1.0,2.0 · Town"}

    def body(g):
        return render_snapshot_body_v71(info, "SNAP-20261001-0002", "2026-10-01T00:00:01Z",
                                        "TURNS", ["line"], gauges=g, provenance={"gm": True})

    today = body(gauges)
    assert body({**gauges, "wellness": ""}) == today and "WELLNESS" not in today
    tagged = body({**gauges, "wellness": mask.gauge_text(
        {"categories": ["sleep"], "labels": ["today"], "refs": ["w-1"]})})
    assert tagged.index("LOCATION:") < tagged.index("WELLNESS: sleep · today · ref w-1 · "
                                                    "values not recorded") < tagged.index("OPERATOR:")
    assert tagged.replace("WELLNESS: sleep · today · ref w-1 · values not recorded\n", "") == today


def test_merge_gauge_keeps_every_turn_once():
    a = {"categories": ["sleep"], "labels": ["x"], "refs": ["w-1"]}
    b = {"categories": ["heart", "sleep"], "labels": ["y"], "refs": ["w-2"]}
    assert mask.merge_gauge(None, a) == a and mask.merge_gauge(a, None) == a
    assert mask.merge_gauge(a, b) == {"categories": ["sleep", "heart"], "labels": ["x", "y"],
                                      "refs": ["w-1", "w-2"]}


# ------------------------------------------------------------------ the save path

class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def box(isolated_stores, monkeypatch, tmp_path):
    """The enrolled fixture operator, a real ledger, and /chat/save minting
    every turn through the real perform_mint."""
    import Orchestrator.routes.chat_routes as cr
    from Orchestrator.routes.chat import save as save_mod
    roster(monkeypatch, OPERATOR, "other-human", "Flight Recorder")
    rec, st, _data = enrolled_fixture(tmp_path, monkeypatch, categories=ALL_ASK)
    lb = ledger_box(isolated_stores.root, monkeypatch)
    for mod in (cr, save_mod):
        monkeypatch.setattr(mod, "AUTO_ENABLE", True, raising=False)
        monkeypatch.setattr(mod, "TURNS_THRESHOLD", 1, raising=False)
        monkeypatch.setattr(mod, "DEBOUNCE_MS", 0, raising=False)
        monkeypatch.setattr(mod, "should_create_checkpoint", lambda *a, **k: False, raising=False)
    lb.rec, lb.st, lb.cr = rec, st, cr
    lb.envs = envelopes()
    return lb


def _save(cr, **body):
    payload = {"operator": OPERATOR, "user_message": "how did I sleep? my BP was 118/79",
               "assistant_response": "ok", "model": "model-test",
               "tokens": {"prompt": 1, "completion": 2}}
    payload.update(body)
    return asyncio.run(cr.chat_save(_Req(payload)))


REPLY = ("Your sleeping heart rate was 47.3 bpm (47 bpm), forty-seven beats per minute. "
         "You walked 12,345 steps; your blood pressure was 118/79.")


def _last_turns(operator=OPERATOR):
    from Orchestrator.state import get_state
    log = get_state(operator).conversation_log
    return log[-2], log[-1]


def test_save_masks_tags_and_keeps_the_user_words(box, capsys):
    refs = [deliver(box.st, box.envs[k], "wt_turn1") for k in ("night", "steps", "bp")]
    res = _save(box.cr, assistant_response=REPLY, wellness_turn="wt_turn1",
                reasoning="The tool returned 47.3 and 12345 and 118/79.")
    assert res["minted"] is True
    user, assistant = _last_turns()
    assert user["text"] == "how did I sleep? my BP was 118/79"       # verbatim
    assert not _hits(assistant["text"]) and not _hits(assistant["snap_text"])
    assert assistant["snap_text"].startswith(f"{mask.REASONING_OMITTED}\n\n[RESPONSE]\n")
    body = snapshot_text(box.vol, res["snap_id"])
    gauge = next(line for line in body.splitlines() if line.startswith("WELLNESS: "))
    assert gauge.startswith("WELLNESS: activity,blood_pressure,heart,sleep · ")
    assert gauge.endswith(" · values not recorded")
    assert all(r in gauge for r in refs)
    assert "my BP was 118/79" in body                                  # the user's own words
    assert not _hits(body.replace("my BP was 118/79", ""))
    assert body.index("MODEL:") < body.index("WELLNESS:") < body.index("OPERATOR:")
    assert all(row["masked_at"] for row in box.st.deliveries_for_turn("wt_turn1"))
    printed = capsys.readouterr().out
    assert "Response preview" not in printed and not _hits(printed)
    assert "Wellness turn: preview skipped, 3 result(s) masked" in printed
    # The active embedder (Gemini, cloud) is not on the owner's allowed list:
    # the vector is built from the pointer line only.
    assert box.embedded[-1] == gauge


def test_a_save_with_no_key_masks_against_recent_unmasked_results(box, monkeypatch):
    now = int(time.time() * 1000)
    deliver(box.st, box.envs["night"], None, ts=now - 25 * 3600 * 1000)   # too old
    deliver(box.st, box.envs["steps"], "wt_other")                        # unmatched, recent
    res = _save(box.cr, assistant_response="You walked 12,345 steps on that day.")
    _user, assistant = _last_turns()
    assert assistant["text"] == "You walked [·] steps on that day."
    assert "activity" in _gauge(snapshot_text(box.vol, res["snap_id"]))
    # Consumed: an hour later, an ordinary save is recorded as is.
    monkeypatch.setattr(mask, "_now_ms", lambda: now + 3600 * 1000)
    res2 = _save(box.cr, assistant_response="The meeting moved to 12:30.")
    _user, assistant = _last_turns()
    assert assistant["text"] == "The meeting moved to 12:30."
    assert "WELLNESS" not in snapshot_text(box.vol, res2["snap_id"])


@pytest.mark.parametrize("reply", ["Your sleeping heart rate was 47.3.",
                                   "Your sleeping heart rate was 47 bpm."])
def test_a_concurrent_turn_whose_save_lands_second_is_still_masked(box, reply):
    """Two chats at once, neither echoing its key: B's save takes A's rows
    first (over-masked, the safe way) and stamps them; A's save then quotes
    them, so the recall rule masks and tags it. (A reply that quotes only a
    duration is the residual until the clients echo the key: wave 3.)"""
    deliver(box.st, box.envs["night"], "wt_a")
    _save(box.cr, assistant_response="Unrelated: the build passed.")     # B, no key, first
    res = _save(box.cr, assistant_response=reply)                        # A, no key, second
    _user, assistant = _last_turns()
    assert not _hits(assistant["text"]) and "47" not in assistant["text"]
    assert "sleep" in _gauge(snapshot_text(box.vol, res["snap_id"]))


def test_a_non_wellness_turn_is_byte_identical(box, capsys):
    """No delivered row for the echoed key (and none recent): the turn is
    recorded and minted exactly as before, with its reasoning and preview."""
    res = _save(box.cr, assistant_response="Sleep well! The 3 tasks are done.",
                wellness_turn="wt_none", reasoning="Plain thinking.")
    _user, assistant = _last_turns()
    assert assistant["text"] == "Sleep well! The 3 tasks are done."
    assert assistant["snap_text"] == ("[REASONING]\nPlain thinking.\n\n[RESPONSE]\n"
                                      "Sleep well! The 3 tasks are done.")
    body = snapshot_text(box.vol, res["snap_id"])
    assert "WELLNESS" not in body and "[REASONING]\nPlain thinking." in body
    assert "Response preview: Sleep well!" in capsys.readouterr().out
    # An operator with no enrollment at all: the same.
    _save(box.cr, operator="other-human", assistant_response="Ran 5 km? Nice.")
    _user, assistant = _last_turns("other-human")
    assert assistant["text"] == "Ran 5 km? Nice."


def test_a_later_turn_that_repeats_a_result_is_masked_too(box):
    """The chat history carries the earlier, unmasked bubble back to the
    model, so a reply can repeat a result although no tool ran this turn: it
    is masked and tagged. A reply with only a bare "47" or a clock is not."""
    deliver(box.st, box.envs["night"], "wt_r1")
    _save(box.cr, assistant_response="It was 47.3 bpm.", wellness_turn="wt_r1")
    res = _save(box.cr, assistant_response="As I said, 47.3 is your number.",
                wellness_turn="wt_r2")
    _user, assistant = _last_turns()
    assert assistant["text"] == "As I said, [·] is your number."
    assert "sleep" in _gauge(snapshot_text(box.vol, res["snap_id"]))
    res = _save(box.cr, assistant_response="Done: 47 files moved at 23:34.",
                wellness_turn="wt_r3")
    _user, assistant = _last_turns()
    assert assistant["text"] == "Done: 47 files moved at 23:34."
    assert not _gauge(snapshot_text(box.vol, res["snap_id"]))


def test_the_tag_waits_for_the_mint_and_is_consumed(box, monkeypatch):
    from Orchestrator.routes.chat import save as save_mod
    monkeypatch.setattr(save_mod, "TURNS_THRESHOLD", 2)
    deliver(box.st, box.envs["night"], "wt_t1")
    first = _save(box.cr, assistant_response="It was 47.3.", wellness_turn="wt_t1")
    assert first["minted"] is False
    second = _save(box.cr, assistant_response="Done with the email.", wellness_turn="wt_t2")
    body = snapshot_text(box.vol, second["snap_id"])
    assert "sleep" in _gauge(body) and not _hits(body.replace("118/79", ""))
    third = _save(box.cr, assistant_response="a", wellness_turn="wt_t3")
    fourth = _save(box.cr, assistant_response="b", wellness_turn="wt_t4")
    assert third["minted"] is False and fourth["minted"] is True
    assert "WELLNESS" not in snapshot_text(box.vol, fourth["snap_id"])


def test_restart_between_stream_and_save(box, monkeypatch):
    """W28: the rows are on disk, not in memory. A fresh process state (the
    mask module reloaded, the store's open-once cache and the operator state
    gone) still masks the save that echoes the key."""
    from collections import defaultdict

    from Orchestrator import state
    deliver(box.st, box.envs["night"], "wt_restart")
    importlib.reload(mask)
    store._INITIALIZED.clear()
    monkeypatch.setattr(state, "state_by_op", defaultdict(state.OpState))
    res = _save(box.cr, assistant_response="Your sleeping heart rate was 47.3 bpm.",
                wellness_turn="wt_restart")
    _user, assistant = _last_turns()
    assert assistant["text"] == "Your sleeping heart rate was [·] bpm."
    assert "sleep" in _gauge(snapshot_text(box.vol, res["snap_id"]))


def test_artifacts_on_a_wellness_turn_carry_no_values(box, monkeypatch):
    """W26: an artifact a wellness turn asks for is written from the masked text."""
    from Orchestrator.routes.chat import save as save_mod
    seen = []
    monkeypatch.setattr(save_mod, "parse_and_process_artifacts_with_meta",
                        lambda text, op: (seen.append(text) or text, []))
    deliver(box.st, box.envs["steps"], "wt_art")
    _save(box.cr, assistant_response="[ARTIFACT: steps.csv]\nday,steps\nsun,12345\n",
          wellness_turn="wt_art")
    assert seen and not _hits(seen[0])


# ------------------------------------------------------------------ on-device and worker paths

def test_local_turn_complete_masks_and_replaces_wellness_tool_lines(box):
    from Orchestrator.routes.chat_routes import persist_local_turn_and_mint
    deliver(box.st, box.envs["night"], None)
    result = asyncio.run(persist_local_turn_and_mint(
        OPERATOR, "how did I sleep?", "Your sleeping heart rate was 47.3 bpm.",
        tool_transcript=[
            {"name": "wellness_summary", "args": {"period": "last_night"},
             "result": json.dumps(box.envs["night"])},
            {"name": "web_search", "args": {"query": "is 47.3 bpm low"}, "result": "ok"},
        ]))
    body = snapshot_text(box.vol, result["snap_id"])
    assert mask.TOOLS_LINE in body and "wellness_summary" not in body
    assert "WELLNESS: " in body and not _hits(body)


def test_local_turn_with_a_wellness_call_but_no_row_is_still_tagged(box):
    from Orchestrator.routes.chat_routes import persist_local_turn_and_mint
    result = asyncio.run(persist_local_turn_and_mint(
        OPERATOR, "steps?", "You walked 9,876 steps.",
        tool_transcript=[{"name": "wellness_series", "args": {}, "result": "9876"}]))
    body = snapshot_text(box.vol, result["snap_id"])
    assert "WELLNESS: activity,blood_pressure,heart,sleep · on-device · " in body
    assert "9,876" not in body and "9876" not in body


def test_worker_turn_records_masked_text_in_tasks_db_and_the_ledger(box, monkeypatch):
    """The non-stream path (cron, /chat): result_data (tasks.db and the cron
    rows that copy it) and the snapshot body are masked, the reasoning is
    omitted, and the task is on the store's erase list."""
    import Orchestrator.routes.chat_routes as cr
    import Orchestrator.tasks as tasks
    from Orchestrator.models import Task, TaskStatus, TaskType, task_db
    from Orchestrator.volume import now_utc_iso
    monkeypatch.setattr(tasks, "build_fossil_context", lambda *a, **k: (
        "", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []}))
    monkeypatch.setattr(tasks, "AUTO_ENABLE", True)
    monkeypatch.setattr(tasks, "TURNS_THRESHOLD", 1)
    monkeypatch.setattr(tasks, "DEBOUNCE_MS", 0)
    monkeypatch.setattr(tasks, "should_create_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "TOOLVAULT_ENABLED", False)

    def fake_call(messages, model, operator="x"):
        # The tool ran mid-turn, for the turn stamped on this task.
        deliver(box.st, box.envs["steps"], turn_mod.current().turn_key)
        return (REPLY, {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                "I read 12345 and 47.3.")

    monkeypatch.setattr(cr, "call_anthropic", fake_call)
    task = Task(task_id="wellness-worker-1", task_type=TaskType.CHAT, status=TaskStatus.PENDING,
                created_at=now_utc_iso(), updated_at=now_utc_iso(), operator=OPERATOR,
                result_data={"messages": [{"role": "user", "content": "steps?"}],
                             "operator": OPERATOR, "provider": "anthropic", "model": "m"})
    task_db.save_task(task)
    token = turn_mod.stamp(turn_mod.WellnessTurn(OPERATOR, turn_mod.SURFACE_CRON, True,
                                                 provider="anthropic", turn_key="wt_worker"))
    try:
        tasks.process_chat_task(task)
    finally:
        turn_mod.clear(token)
    final = task_db.get_task("wellness-worker-1")
    assert final.status == TaskStatus.COMPLETED
    blob = json.dumps(final.result_data) + str(final.reasoning_text)
    assert not _hits(blob) and "[·]" in final.result_data["reply"]
    body = Path("Volumes/SNAPSHOT_VOLUME.txt").read_text(encoding="utf-8")
    assert "activity" in _gauge(body) and mask.REASONING_OMITTED in body
    assert not _hits(body)
    assert "wellness-worker-1" in box.st.task_refs()
    # The cron rows copy this reply (cron_job_history.result, the notification).
    from Orchestrator.scheduler.executor import _extract_reply
    assert not _hits(_extract_reply({"result_data": final.result_data}, "job"))


# ------------------------------------------------------------------ the stream key

def test_stream_start_names_the_turn_key_only_for_a_stamped_enrolled_turn(box, monkeypatch):
    import Orchestrator.routes.chat_routes as cr

    def fake_build(messages, operator, provider="openai", window_guard_tokens=None):
        return [{"role": "system", "content": "sys"}] + list(messages), {}, {}

    seen = []

    async def fake_stream(msgs, model, operator=None, **kw):
        seen.append(turn_mod.current())          # the turn this stream runs as
        yield {"type": "content", "data": "ok"}

    monkeypatch.setattr(cr, "build_streaming_context", fake_build)
    monkeypatch.setattr(cr, "stream_openai_with_reasoning", fake_stream)

    def start_event(operator, stamped):
        body = {"messages": [{"role": "user", "content": "hi"}], "provider": "openai",
                "model": "m", "operator": operator}
        token = turn_mod.stamp(stamped) if stamped else None

        async def go():
            resp = await cr.chat_stream_post(_Req(body))
            out = []
            async for chunk in resp.body_iterator:
                out.append(chunk if isinstance(chunk, str) else chunk.decode())
            return "".join(out)
        try:
            sse = asyncio.run(go())
        finally:
            if token:
                turn_mod.clear(token)
        block = next(b for b in sse.split("\n\n") if b.startswith("event: stream_start"))
        return json.loads(block.split("data: ", 1)[1])

    def stamped_key():
        t = seen[-1]
        return t.turn_key if t is not None and t.operator == OPERATOR else None

    # The key named is the stamped turn's (the test's here; the route's own
    # once the entry points stamp every request), and none when none is.
    stamped = turn_mod.WellnessTurn(OPERATOR, turn_mod.SURFACE_ANDROID, True, provider="openai",
                                    turn_key="wt_stream_1")
    event = start_event(OPERATOR, stamped)
    assert event["wellness_turn"] == stamped_key() and event["wellness_turn"]
    assert start_event(OPERATOR, None).get("wellness_turn") == stamped_key()
    other = turn_mod.WellnessTurn("other-human", turn_mod.SURFACE_ANDROID, True,
                                  provider="openai", turn_key="wt_stream_2")
    assert start_event("other-human", other) == {"provider": "openai", "model": "m",
                                                 "provenance": {}}


# ------------------------------------------------------------------ what is NOT a wellness turn

# Ordinary replies: durations, percents, milliseconds, a plan's step, a clock.
ORDINARY = ("I'll check back in one hour.", "The build takes 30 minutes.",
            "Give it 45 minutes to finish.", "That's about two hours of work.",
            "The deploy took 20 min.", "CPU sits at 95% for 120 ms bursts.",
            "Raised the timeout to 500 ms; tests pass at 100%.",
            "Heading out now, home in 30 minutes.", "Step 12 of the plan is done.",
            "The meeting moved to 12:30.")


def _answered_earlier_today(box, hours=3):
    """The operator's health answers from earlier today, each already saved
    (rows masked): sleep stages and workouts put plain durations in V."""
    now = int(time.time() * 1000)
    refs = [deliver(box.st, box.envs[k], f"wt_{k}_earlier", ts=now - hours * 3600 * 1000)
            for k in ("night", "steps", "bp", "week", "sleeps", "workouts")]
    box.st.mark_masked(refs, now - hours * 3600 * 1000)
    return mask.value_set(box.envs[k] for k in ("night", "steps", "bp", "week", "sleeps",
                                                 "workouts"))


def test_the_recall_rule_needs_a_distinctive_quote(box):
    """The recall rule fires on a decimal, grouped thousands, a pair or a
    number with a reading's own unit; never on a duration, a percent, a
    millisecond count or a clock time, though V holds "30 min" and "one hour"."""
    v = _answered_earlier_today(box)
    assert {"30 min", "one hour"} & v or {"45 minutes", "20 min", "two hours"} & v
    for text in ORDINARY:
        assert not mask.quotes_results(text, v), text
    for text in ("As I said, 47.3 is your number.", "You did 12,345 that day.",
                 "It was 118/79.", "Your sleeping heart rate was 47 bpm.",
                 "you logged 12345 steps", "Tu pulso fue 47,3."):
        assert mask.quotes_results(text, v), text
    assert not mask.quotes_results("Step 47 of the runbook.", v)      # steps are thousands


def test_ordinary_saves_after_a_health_answer_are_byte_identical(box, capsys):
    _answered_earlier_today(box)
    for text in ORDINARY:
        res = _save(box.cr, assistant_response=text, reasoning="Plain thinking.")
        _user, assistant = _last_turns()
        assert assistant["text"] == text
        assert assistant["snap_text"] == f"[REASONING]\nPlain thinking.\n\n[RESPONSE]\n{text}"
        body = snapshot_text(box.vol, res["snap_id"])
        assert not _gauge(body) and box.embedded[-1] == body
    assert "Wellness turn" not in capsys.readouterr().out


def test_a_save_after_the_wellness_turn_saved_itself_is_byte_identical(box, monkeypatch):
    """No grace window: once a wellness turn's own save masked its rows, the
    next turn (ten minutes on, with or without a key) is recorded as is."""
    now = int(time.time() * 1000)
    deliver(box.st, box.envs["steps"], "wt_s1")
    _save(box.cr, assistant_response="You walked 12,345 steps.")            # its own save, no key
    deliver(box.st, box.envs["night"], "wt_s2")
    _save(box.cr, assistant_response="It was 47.3.", wellness_turn="wt_s2")  # its own, keyed
    monkeypatch.setattr(mask, "_now_ms", lambda: now + 10 * 60 * 1000)
    text = "Raised the timeout to 500 ms; tests pass at 100%."
    for key in (None, "wt_next"):
        res = _save(box.cr, assistant_response=text, reasoning="r",
                    **({"wellness_turn": key} if key else {}))
        _user, assistant = _last_turns()
        assert assistant["text"] == text and "[REASONING]\nr" in assistant["snap_text"]
        assert not _gauge(snapshot_text(box.vol, res["snap_id"]))


@pytest.mark.parametrize("key", ["wt_stale", True, 1, ["wt_x"], {"key": "wt_x"}, ""])
def test_a_key_that_matches_nothing_still_falls_back(box, key):
    """W28: an echoed key that names no row (stale, mistyped, not a string)
    is no reason to skip the safety net: the save is masked against the
    unmasked deliveries of the last 24 hours."""
    deliver(box.st, box.envs["night"], "wt_x")
    res = _save(box.cr, wellness_turn=key, assistant_response=(
        "Last night you reached 46 at the lowest; overall it was 52 on a usual night."))
    _user, assistant = _last_turns()
    assert "46" not in assistant["text"] and "52" not in assistant["text"], assistant["text"]
    assert "sleep" in _gauge(snapshot_text(box.vol, res["snap_id"]))


@pytest.mark.parametrize("failure", ["corrupt", "io"])
def test_an_unreadable_health_store_never_loses_the_turn(box, monkeypatch, capsys, failure):
    """A broken store must not stop chat persistence: the turn is recorded,
    masked by the unit rule and tagged (it may have been a wellness turn),
    and the journal names only the error class."""
    import sqlite3

    if failure == "corrupt":
        box.st.path.write_bytes(b"not a database" * 64)
        store._INITIALIZED.clear()
    else:
        def broken(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(store.Store, "read", broken)
    res = _save(box.cr, assistant_response="The meeting moved to Tuesday; you slept 7 hours.")
    assert res["success"] is True and res["minted"] is True
    _user, assistant = _last_turns()
    assert assistant["text"] == "The meeting moved to Tuesday; you slept [·] hours."
    gauge = _gauge(snapshot_text(box.vol, res["snap_id"]))
    assert gauge.startswith("WELLNESS: activity,blood_pressure,heart,sleep · health data · ")
    out = capsys.readouterr().out
    assert "health store unreadable (" in out and "not a database" not in out


# ------------------------------------------------------------------ locale forms (A2)

_LOCALE_PHRASES = (
    "Anoche tu frecuencia cardíaca mientras dormías fue de {comma} lpm.",
    "Tu pulso en reposo fue {comma}.", "Last night you got to {comma} while asleep.",
    "You averaged {two} overnight.", "({comma})", "{two}",
)


def test_decimal_commas_trailing_zeros_and_thousands_are_masked(answers):
    """A2, locale forms: the same reading with a decimal comma (a Spanish
    reply), with a trailing zero, or rounded to thousands in words."""
    v = mask.value_set(answers.values())
    for env in answers.values():
        for _label, x, _d, _s, _u in _facts(env):
            if float(x).is_integer() and abs(x) < 1000:
                continue
            if not float(x).is_integer():
                forms = {"comma": f"{x:.1f}".replace(".", ","), "two": f"{x:.2f}"}
                for phrase in _LOCALE_PHRASES:
                    text = phrase.format(**forms)
                    out = mask.mask_text(text, v)
                    for form in forms.values():
                        assert form not in out, (text, out)
            else:
                from Orchestrator.wellness import catalog
                words = catalog.number_words(int(round(x, -3)))
                out = mask.mask_text(f"You walked over {words} yesterday.", v)
                assert words not in out, out
                out = mask.mask_text(f"Diste {int(x):,} pasos ayer.".replace(",", "."), v)
                assert f"{int(x):,}".replace(",", ".") not in out, out


def test_a_spanish_wellness_reply_mints_no_value(box):
    deliver(box.st, box.envs["night"], "wt_es")
    deliver(box.st, box.envs["week"], "wt_es")
    reply = ("Anoche tu frecuencia cardíaca mientras dormías fue de 47,3 lpm, un poco por "
             "debajo de lo habitual (52,1). Dormiste 6,8 horas y diste 12.345 pasos.")
    res = _save(box.cr, assistant_response=reply, wellness_turn="wt_es")
    body = snapshot_text(box.vol, res["snap_id"])
    log = body.split("Raw Session Log\n", 1)[1].split("\nRelease Notes", 1)[0]
    assistant = next(line for line in log.splitlines() if " assistant: " in line)
    said = assistant.split(" assistant: ", 1)[1]
    assert not re.search(r"\d", said), said
    assert "frecuencia cardíaca" in said and "WELLNESS: " in body


# ------------------------------------------------------------------ the worker path

def _worker(box, monkeypatch, reply, *, deliver_envs=(), stamp_key=None, extra=None,
            task_id=None):
    import Orchestrator.routes.chat_routes as cr
    import Orchestrator.tasks as tasks
    from Orchestrator.models import Task, TaskStatus, TaskType, task_db
    from Orchestrator.volume import now_utc_iso
    monkeypatch.setattr(tasks, "build_fossil_context", lambda *a, **k: (
        "", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []}))
    monkeypatch.setattr(tasks, "AUTO_ENABLE", True)
    monkeypatch.setattr(tasks, "TURNS_THRESHOLD", 1)
    monkeypatch.setattr(tasks, "DEBOUNCE_MS", 0)
    monkeypatch.setattr(tasks, "should_create_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "TOOLVAULT_ENABLED", False)

    def fake_call(messages, model, operator="x"):
        for env in deliver_envs:      # the tool ran mid-turn, for the turn stamped on this task
            deliver(box.st, box.envs[env], turn_mod.current().turn_key)
        return reply, {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}, ""

    monkeypatch.setattr(cr, "call_anthropic", fake_call)
    data = {"messages": [{"role": "user", "content": "and then?"}], "operator": OPERATOR,
            "provider": "anthropic", "model": "m", **(extra or {})}
    task = Task(task_id=task_id or f"wellness-worker-{time.time_ns()}", task_type=TaskType.CHAT,
                status=TaskStatus.PENDING, created_at=now_utc_iso(), updated_at=now_utc_iso(),
                operator=OPERATOR, result_data=data)
    task_db.save_task(task)
    token = turn_mod.stamp(turn_mod.WellnessTurn(
        OPERATOR, turn_mod.SURFACE_CRON, True, provider="anthropic",
        turn_key=stamp_key)) if stamp_key else None
    try:
        tasks.process_chat_task(task)
    finally:
        if token:
            turn_mod.clear(token)
    final = task_db.get_task(task.task_id)
    assert final.status == TaskStatus.COMPLETED, final.result_data
    return final


def test_an_ordinary_sms_or_cron_reply_after_a_health_answer_is_untouched(box, monkeypatch):
    """The reply the user gets (an SMS to a peer, a cron notification) is
    never masked on a guess, and the ledger records it as is."""
    _answered_earlier_today(box)
    for extra in ({"sms_mode": True, "sms_sender": "+15550000000", "sms_peer": True}, {}):
        final = _worker(box, monkeypatch, "Heading out now, home in 30 minutes.", extra=extra)
        assert final.result_data["ui_reply"] == "Heading out now, home in 30 minutes."
        assert final.task_id not in box.st.task_refs()
        body = Path("Volumes/SNAPSHOT_VOLUME.txt").read_text(encoding="utf-8")
        last = body[body.rindex("=== START SNAPSHOT"):]
        assert not _gauge(last) and "home in 30 minutes" in last


def test_a_recalled_result_masks_the_ledger_copy_only(box, monkeypatch):
    """A reply quoting an earlier result (the history carried it): the user
    gets it as written; the snapshot records it masked and tagged."""
    _answered_earlier_today(box)
    final = _worker(box, monkeypatch, "As I said this morning, it was 47.3.")
    assert final.result_data["ui_reply"] == "As I said this morning, it was 47.3."
    body = Path("Volumes/SNAPSHOT_VOLUME.txt").read_text(encoding="utf-8")
    last = body[body.rindex("=== START SNAPSHOT"):]
    assert _gauge(last) and "47.3" not in last and "it was [·]" in last
    assert final.task_id in box.st.task_refs()       # the row holds the number: erase scrubs it


def test_worker_artifacts_and_media_prompts_are_made_from_the_masked_reply(box, monkeypatch):
    """W26/A11: an [ARTIFACT] file (Portal/artifacts) and a generate_image
    prompt (tasks.db) of a wellness turn carry no value."""
    import Orchestrator.tasks as tasks
    from Orchestrator.models import TaskType, task_db
    monkeypatch.setattr(tasks, "task_queue", type(tasks.task_queue)())   # nothing really runs
    reply = ("Here is your file.\n[ARTIFACT:sleep_report.txt:text]Sleeping heart rate 47.3 bpm; "
             "12,345 steps; BP 118/79[/ARTIFACT]\n\ngenerate_image: a chart of 12,345 steps")
    final = _worker(box, monkeypatch, reply, deliver_envs=("night", "bp", "steps"),
                    stamp_key="wt_art_worker")
    assert final.result_data["artifacts"]
    files = [p for p in Path("Portal").rglob("*") if p.is_file()]
    assert files and not any(_hits(p.read_text(errors="replace")) for p in files)
    assert not _hits(json.dumps(final.result_data))
    prompts = [t.prompt or "" for t in task_db.get_all_tasks(OPERATOR)
               if t.task_type == TaskType.IMAGE_GENERATION]
    assert prompts and not any(_hits(p) for p in prompts), prompts


# ------------------------------------------------------------------ the tag rides with the turn

def test_a_mint_between_the_appends_still_tags_the_turn(box, monkeypatch):
    """A mint on another thread that lands between a wellness turn's two
    appends (its user words, its reply) tags whatever it mints of the turn,
    and the next mint tags the rest; an unrelated turn after it is untagged."""
    from Orchestrator import checkpoint, state, turn_persistence
    from Orchestrator.routes.chat import save as save_mod
    from Orchestrator.turn_persistence import MintGate
    minted = []
    orig = state.OpState.add_conversation_turn

    def add_then_concurrent_mint(self, item, *a, **k):
        orig(self, item, *a, **k)
        if not minted:
            minted.append(checkpoint.perform_mint(OPERATOR, "CONCURRENT")["snap_id"])

    monkeypatch.setattr(state.OpState, "add_conversation_turn", add_then_concurrent_mint)
    gauge = {"categories": ["sleep"], "labels": ["night of 2026-10-22"], "refs": ["w-1"]}
    turn_persistence._record_turn(
        state.get_state(OPERATOR), OPERATOR, "I felt dizzy and awful last night",
        "Your sleep was [·], a bit less than usual.", gate=MintGate.of(save_mod), snap_text=None,
        prompt_tokens=0, completion_tokens=0, total_tokens=0, model="m", location_gauge="",
        provenance=None, count_context=False, tag="[T]", wellness=gauge)
    monkeypatch.setattr(state.OpState, "add_conversation_turn", orig)
    first = snapshot_text(box.vol, minted[0])
    assert "dizzy" in first and _gauge(first).startswith("WELLNESS: sleep · ")
    second = snapshot_text(box.vol, checkpoint.perform_mint(OPERATOR, "NEXT")["snap_id"])
    assert "a bit less than usual" in second and _gauge(second)
    third = snapshot_text(box.vol, _save(box.cr, assistant_response="Done.")["snap_id"])
    assert not _gauge(third)


# ------------------------------------------------------------------ merge safety

def test_process_chat_task_binds_no_module_level_name_locally():
    """A function-local import binds its name for the WHOLE function: one
    that reuses a module-level name (wave 2's H2 imports `wellness_turn` at
    the top of tasks.py and uses it before this unit's lazy import) turns
    every earlier use into UnboundLocalError. The worker's lazy imports use
    private names."""
    import ast

    import Orchestrator.tasks as tasks
    tree = ast.parse(Path(tasks.__file__).read_text(encoding="utf-8"))
    module_names = {a.asname or a.name.split(".")[0] for n in tree.body
                    if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    module_names |= {"wellness_turn", "wellness_access", "wellness_mask", "wellness_redact"}
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "process_chat_task")
    local = {a.asname or a.name.split(".")[0] for n in ast.walk(fn)
             if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    wellness_locals = {n for n in local if "wellness" in n}
    assert wellness_locals and not (wellness_locals & module_names), wellness_locals


def test_a_range_whose_high_end_was_masked_first_still_loses_its_low_end():
    """Live on dev (SNAP-20261030-9503): "(3.4 to 7.3 km)" minted as
    "(3.4 to [·] km)". The value pass masked the high end, and the glyph it
    left behind was three tokens to the unit rule, which pushed "km" out of
    the low end's reach. The glyph is one number token now."""
    assert mask.mask_text("above your usual range (3.4 to 7.3 km).", frozenset({"7.3"})) \
        == "above your usual range ([·] to [·] km)."
    assert mask.mask_text("range ([·] to 7.3 km)") == "range ([·] to [·] km)"
    # "and" joins number phrases, so the glyph and the next number mask as
    # one span (over-masking, the safe direction).
    assert mask.mask_text("between [·] and 3.4 km, about [·] more") \
        == "between [·] km, about [·] more"
    # A glyph is not a unit: it never pulls an unrelated count into reach
    # (no sleep or workout context word here, "runs" would be one, so "minutes" is plain time).
    plain = "[·] bpm. The build takes 30 minutes in 3 of 7 attempts."
    assert mask.mask_text(plain) == plain


def test_metre_values_are_in_v_as_kilometres():
    """A usual range in the payload is plain metres; a reply writes it in km."""
    v = mask.value_set([{"metric": "distance", "unit": "m", "value": 7612.0,
                         "display": "7.6 km",
                         "usual": {"median": 5120.4, "low": 3412.3, "high": 7312.0, "n": 60}}])
    assert {"3.4", "3.41", "3,4", "7.3", "7.31", "5.1", "5.12", "7.6", "7.61"} <= v
    # The catalog's own display ("7.3 km") is a member too, unit included.
    assert mask.mask_values("usual range (3.4 to 7.3 km), median 5,12 km", v) \
        == "usual range ([·] to [·]), median [·] km"
    # Small metre values (a 40 m elevation) and other units gain nothing.
    assert not {f for f in mask.value_set([{"unit": "m", "value": 40.0}]) if "." in f}
    assert "0.4" not in mask.value_set([{"metric": "steps", "unit": "steps", "value": 412.0}])


def test_a_count_of_awakenings_is_masked_by_the_unit_rule():
    """E1 found the ledger keeps "you woke up 3 times": no unit word marks a
    count of awakenings. "times" / "veces" counts as one within reach of a
    wake word, and the nouns (wake-ups, awakenings, despertares) are units of
    their own. A tally with no wake word stays readable."""
    cases = {
        "You woke up 3 times last night.": "You woke up [·] times last night.",
        "There were 3 wake-ups after midnight.": "There were [·] wake-ups after midnight.",
        "You were awake 3 times overnight.": "You were awake [·] times overnight.",
        "You had four awakenings.": "You had [·] awakenings.",
        "Te despertaste 3 veces anoche.": "Te despertaste [·] veces anoche.",
        "Tuviste 3 despertares.": "Tuviste [·] despertares.",
        # The glyph is still one number token: "[·] and 3" stays one phrase.
        "You woke [·] and 3 times on the nights after.": "You woke [·] times on the nights after.",
    }
    for text, expected in cases.items():
        assert mask.mask_text(text, frozenset()) == expected, text
    for keep in ("I called the office 3 times today.", "Retry the build 3 times.",
                 "Lo intenté 3 veces.", "Wake me at 7 for the 3 meetings."):
        assert mask.mask_text(keep, frozenset()) == keep, keep


def test_the_grounding_check_sees_the_awakenings_the_mask_masks():
    """evals.grounding's candidates are the mask's: every count of awakenings
    the unit rule now masks is a health number to A1 (offending when no
    result of the turn carries it), and it is counted once."""
    from Orchestrator.wellness import evals
    for text in ("You woke up 14 times last night.", "There were 14 wake-ups.",
                 "You were awake 14 times.", "Te despertaste 14 veces anoche.",
                 "Tuviste 14 despertares.", "You had fourteen awakenings."):
        assert "[·]" in mask.mask_text(text, frozenset()), text
        assert len(evals.grounding(text).offending) == 1, text
    assert evals.grounding("I called the office 14 times.").ok
