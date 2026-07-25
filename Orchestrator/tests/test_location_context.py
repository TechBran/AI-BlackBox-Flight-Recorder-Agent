"""M1 — location rides along with the user prompt (Orchestrator half).

Brandon's locked design: location is metadata on a turn that was happening
anyway. One location per snapshot, appended to the user text, recorded by the
ledger for free. NOT a tracking system.

The load-bearing property under test is the NO-OP: a request with no location
must produce byte-identical output to what the box produced before this feature
existed — same user text, same snapshot body, no gauge line.

Guardrail (plan): no real coordinates in fixtures. These are Google's documented
example (37.4224,-122.0841) and obvious fakes.
"""

import asyncio
import math

import pytest

from Orchestrator.location import (
    append_location_to_text,
    apply_location_to_messages,
    format_location_gauge,
    format_location_line,
    normalize,
)
from Orchestrator.monitoring import render_snapshot_body_v71

FAKE_LAT = 37.4224
FAKE_LON = -122.0841
FAKE_LINE = "[user location: 37.4224,-122.0841 · Mountain View, California]"


# ── normalize(): totality on untrusted client input ─────────────────────────

JUNK = [
    None, "", "  ", 0, 1, 1.5, [], {}, set(), object(), b"lat", True, False,
    {"lat": None, "lon": None},
    {"lat": 91.0, "lon": 0.0},           # out of range (latitude)
    {"lat": -90.001, "lon": 0.0},
    {"lat": 0.0, "lon": 180.5},          # out of range (longitude)
    {"lat": 0.0, "lon": -181.0},
    {"lat": "NaN", "lon": 0.0},
    {"lat": float("nan"), "lon": 0.0},
    {"lat": float("inf"), "lon": 0.0},
    {"lat": 0.0, "lon": float("-inf")},
    {"lat": "north", "lon": "west"},
    {"lat": True, "lon": False},         # bool is an int subclass — must not pass
    {"lat": [1], "lon": {"a": 2}},
    {"lat": 1e400, "lon": 0.0},          # overflows to inf
    {"lon": FAKE_LON},                   # missing latitude
    {"lat": FAKE_LAT},                   # missing longitude
    {"latitude": {}, "longitude": []},
    {"lat": "1,2", "lon": "3,4"},
    '{"lat": 37.4, "lon": -122.0}',      # a JSON *string* is not a dict
]


@pytest.mark.parametrize("raw", JUNK)
def test_normalize_rejects_junk_without_raising(raw):
    assert normalize(raw) is None


def test_normalize_never_raises_on_exotic_input():
    class Exploding(dict):
        def __contains__(self, k):  # pragma: no cover - defensive
            raise RuntimeError("boom")

    assert normalize(Exploding()) is None


def test_normalize_accepts_a_valid_fix():
    loc = normalize({"lat": FAKE_LAT, "lon": FAKE_LON, "accuracy_m": 12,
                     "city": "Mountain View", "state": "California"})
    assert loc["lat"] == pytest.approx(FAKE_LAT)
    assert loc["lon"] == pytest.approx(FAKE_LON)
    assert loc["place"] == "Mountain View, California"
    assert loc["accuracy_m"] == 12.0


def test_normalize_accepts_key_aliases_and_numeric_strings():
    loc = normalize({"latitude": "37.4224", "longitude": "-122.0841",
                     "locality": "Mountain View", "region": "California"})
    assert loc is not None and loc["lat"] == pytest.approx(FAKE_LAT)
    assert loc["place"] == "Mountain View, California"


def test_normalize_boundaries_are_inclusive():
    for lat, lon in ((90.0, 180.0), (-90.0, -180.0), (0.0, 12.5), (12.5, 0.0)):
        loc = normalize({"lat": lat, "lon": lon})
        assert loc is not None and math.isfinite(loc["lat"])


def test_normalize_rejects_null_island():
    """(0, 0) is a real place but an unreal reading.

    It is the classic signature of a bad fix — zeroed provider output, an
    uninitialised struct, a stubbed client. The Android capture path rejects it;
    the backend must agree, or a future non-Android client can stamp a bogus
    position into an immutable ledger. A zero in ONE axis is still legitimate.
    """
    assert normalize({"lat": 0, "lon": 0}) is None
    assert normalize({"lat": 0.0, "lon": 0.0}) is None
    assert normalize({"lat": 0.0, "lon": 12.5}) is not None
    assert normalize({"lat": 12.5, "lon": 0.0}) is not None


def test_normalize_scrubs_and_caps_untrusted_place_text():
    loc = normalize({
        "lat": FAKE_LAT, "lon": FAKE_LON,
        "city": "Ev\nil]City[\x00",
        "state": "S" * 500,
    })
    place = loc["place"]
    assert "\n" not in place and "\x00" not in place
    assert "[" not in place and "]" not in place
    assert len(place) <= 120


def test_normalize_place_only_coordinates_when_no_names():
    loc = normalize({"lat": FAKE_LAT, "lon": FAKE_LON})
    assert loc["place"] is None


# ── the rendered line ───────────────────────────────────────────────────────

def test_format_location_line():
    loc = normalize({"lat": FAKE_LAT, "lon": FAKE_LON,
                     "city": "Mountain View", "state": "California"})
    assert format_location_line(loc) == FAKE_LINE


def test_format_location_line_degrades_to_coordinates_alone():
    loc = normalize({"lat": FAKE_LAT, "lon": FAKE_LON})
    assert format_location_line(loc) == "[user location: 37.4224,-122.0841]"


def test_format_location_line_accepts_an_explicit_place_override():
    loc = normalize({"lat": FAKE_LAT, "lon": FAKE_LON})
    assert format_location_line(loc, place="Mountain View, California") == FAKE_LINE


@pytest.mark.parametrize("bad", [None, {}, "x", 3, [], {"lat": "x", "lon": "y"}])
def test_format_location_line_is_empty_for_anything_invalid(bad):
    assert format_location_line(bad) == ""
    assert format_location_gauge(bad) == ""


# ── the append lands in the user text ───────────────────────────────────────

def test_append_lands_at_the_bottom_of_the_user_text():
    loc = normalize({"lat": FAKE_LAT, "lon": FAKE_LON,
                     "city": "Mountain View", "state": "California"})
    out = append_location_to_text("find me a restaurant nearby", loc)
    assert out == "find me a restaurant nearby\n" + FAKE_LINE


def test_append_never_double_stamps():
    loc = normalize({"lat": FAKE_LAT, "lon": FAKE_LON,
                     "city": "Mountain View", "state": "California"})
    once = append_location_to_text("hi", loc)
    assert append_location_to_text(once, loc) == once


def test_append_with_no_location_returns_the_same_string_object():
    text = "unchanged"
    assert append_location_to_text(text, None) is text


def test_apply_to_messages_appends_to_the_last_user_message():
    loc = normalize({"lat": FAKE_LAT, "lon": FAKE_LON,
                     "city": "Mountain View", "state": "California"})
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "where am i"},
    ]
    out = apply_location_to_messages(messages, loc)
    assert out[3]["content"] == "where am i\n" + FAKE_LINE
    assert out[1]["content"] == "first"          # earlier turns untouched
    assert messages[3]["content"] == "where am i"  # caller's dicts not mutated


def test_apply_to_messages_handles_multipart_content():
    loc = normalize({"lat": FAKE_LAT, "lon": FAKE_LON})
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        {"type": "text", "text": "what is this"},
    ]}]
    out = apply_location_to_messages(messages, loc)
    assert out[0]["content"][1]["text"].endswith("[user location: 37.4224,-122.0841]")
    assert out[0]["content"][0]["type"] == "image_url"


def test_apply_to_messages_is_identity_without_a_location():
    messages = [{"role": "user", "content": "hi"}]
    assert apply_location_to_messages(messages, None) is messages
    assert apply_location_to_messages(messages, {"lat": "junk"}) is messages


# ── snapshot gauge line ─────────────────────────────────────────────────────

INFO = {"tail_id": "SNAP-20260724-0001", "utc": "2026-07-24T00:00:00Z"}
BASE_GAUGES = {"drift": "green", "p": 1, "c": 2, "t": 3,
               "model": "test-model", "operator": "LocTester"}
PROV = {"gm": True, "recent": [], "relevant": []}


def _body(gauges):
    return render_snapshot_body_v71(INFO, "SNAP-20260724-0002", "2026-07-24T00:00:01Z",
                                    "TURNS", ["line one"], gauges=gauges, provenance=PROV)


def test_snapshot_body_has_the_location_gauge_when_present():
    gauge = format_location_gauge(normalize({
        "lat": FAKE_LAT, "lon": FAKE_LON, "city": "Mountain View", "state": "California"}))
    body = _body({**BASE_GAUGES, "location": gauge})
    assert "LOCATION: 37.4224,-122.0841 · Mountain View, California" in body
    # sits with the other gauges, between MODEL and OPERATOR
    assert body.index("MODEL:") < body.index("LOCATION:") < body.index("OPERATOR:")


def test_snapshot_body_has_no_location_line_when_absent():
    for gauges in ({**BASE_GAUGES}, {**BASE_GAUGES, "location": ""},
                   {**BASE_GAUGES, "location": None}, {**BASE_GAUGES, "location": "   "}):
        assert "LOCATION" not in _body(gauges)


def test_snapshot_body_without_location_is_byte_identical_to_today():
    """`gauges` with no `location` key at all is exactly the pre-M1 call shape."""
    today = _body({**BASE_GAUGES})
    for gauges in ({**BASE_GAUGES, "location": ""}, {**BASE_GAUGES, "location": None}):
        assert _body(gauges) == today


# ── POST /chat/stream: the line reaches the MODEL ───────────────────────────

@pytest.fixture
def stream_env(monkeypatch):
    import Orchestrator.routes.chat_routes as cr

    seen = {}

    def fake_build(messages, operator, provider="openai", window_guard_tokens=None):
        return ([{"role": "system", "content": "sys"}] + list(messages), {}, {})

    async def fake_stream(msgs, model, operator=None, **kw):
        seen["messages"] = msgs
        seen["model"] = model
        yield {"type": "content", "data": "ok"}

    monkeypatch.setattr(cr, "build_streaming_context", fake_build)
    monkeypatch.setattr(cr, "stream_openai_with_reasoning", fake_stream)
    return cr, seen


def _drain(response):
    async def go():
        async for _ in response.body_iterator:
            pass
    asyncio.run(go())


def _post_stream(cr, body):
    return asyncio.run(cr.chat_stream_post(_Req(body)))


def test_stream_ships_the_location_inside_the_user_prompt(stream_env):
    cr, seen = stream_env
    _drain(_post_stream(cr, {
        "messages": [{"role": "user", "content": "any good tacos near me?"}],
        "provider": "openai", "model": "test", "operator": "LocTester-stream",
        "location": {"lat": FAKE_LAT, "lon": FAKE_LON,
                     "city": "Mountain View", "state": "California"},
    }))
    assert seen["messages"][-1]["content"] == "any good tacos near me?\n" + FAKE_LINE
    # and it was carried for the /chat/save that records this turn
    assert cr._take_turn_location("LocTester-stream") is not None


def test_stream_without_location_is_byte_identical(stream_env):
    cr, seen = stream_env
    _drain(_post_stream(cr, {
        "messages": [{"role": "user", "content": "any good tacos near me?"}],
        "provider": "openai", "model": "test", "operator": "LocTester-stream-absent",
    }))
    assert seen["messages"][-1]["content"] == "any good tacos near me?"
    assert cr._take_turn_location("LocTester-stream-absent") is None


def test_stream_with_garbage_location_still_streams(stream_env):
    cr, seen = stream_env
    for junk in ({"lat": 999, "lon": 0}, "nope", 5, [], {"lat": None}):
        _drain(_post_stream(cr, {
            "messages": [{"role": "user", "content": "hi"}],
            "provider": "openai", "model": "test", "operator": "LocTester-stream-junk",
            "location": junk,
        }))
        assert seen["messages"][-1]["content"] == "hi"


# ── end-to-end through POST /chat/save (the path that records the turn) ─────

class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def save_env(monkeypatch):
    """POST /chat/save with the mint + persistence side effects stubbed."""
    import Orchestrator.routes.chat_routes as cr
    import Orchestrator.checkpoint as ckpt
    import Orchestrator.tasks as tasks

    monkeypatch.setattr(cr, "AUTO_ENABLE", False)
    monkeypatch.setattr(cr, "should_create_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(cr, "save_operator_state", lambda *a, **k: None)
    monkeypatch.setattr(ckpt, "save_operator_state", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "collect_pending_media_artifacts", lambda *a, **k: [])
    return cr


def _save(cr, operator, body):
    payload = {"operator": operator, "assistant_response": "ok",
               "model": "test-model", "tokens": {"prompt": 1, "completion": 2}}
    payload.update(body)
    return asyncio.run(cr.chat_save(_Req(payload)))


def _user_turns(operator):
    from Orchestrator.state import get_state
    return [t for t in get_state(operator).conversation_log if t.get("role") == "user"]


def test_save_appends_the_location_to_the_recorded_user_turn(save_env):
    op = "LocTester-present"
    _save(save_env, op, {
        "user_message": "any good tacos near me?",
        "location": {"lat": FAKE_LAT, "lon": FAKE_LON,
                     "city": "Mountain View", "state": "California"},
    })
    turns = _user_turns(op)
    assert turns[-1]["text"] == "any good tacos near me?\n" + FAKE_LINE

    from Orchestrator.state import get_state
    assert get_state(op).last_context_meta["location"] == \
        "37.4224,-122.0841 · Mountain View, California"


def test_save_without_location_is_a_perfect_noop(save_env):
    op = "LocTester-absent"
    _save(save_env, op, {"user_message": "any good tacos near me?"})
    turns = _user_turns(op)
    assert turns[-1]["text"] == "any good tacos near me?"  # byte-identical
    assert "[user location" not in turns[-1]["text"]

    from Orchestrator.state import get_state
    assert get_state(op).last_context_meta["location"] == ""


def test_save_with_garbage_location_never_fails_the_turn(save_env):
    op = "LocTester-garbage"
    for junk in ({"lat": 999, "lon": 999}, "not-an-object", [], 7, {"lat": "NaN"}):
        _save(save_env, op, {"user_message": "hello", "location": junk})
    turns = _user_turns(op)
    assert len(turns) == 5
    assert all(t["text"] == "hello" for t in turns)

    from Orchestrator.state import get_state
    assert get_state(op).last_context_meta["location"] == ""


def test_stream_location_carries_into_the_recorded_turn(save_env):
    """The stream route only streams; /chat/save records. The single-slot carry
    hands this turn's fix across, and a later locationless stream CLEARS it so a
    Portal turn can never inherit a phone turn's fix."""
    cr = save_env
    op = "LocTester-carry"
    loc = {"lat": FAKE_LAT, "lon": FAKE_LON, "city": "Mountain View", "state": "California"}

    cr._stash_turn_location(op, cr.user_location.normalize(loc))
    _save(cr, op, {"user_message": "where am i"})
    assert _user_turns(op)[-1]["text"] == "where am i\n" + FAKE_LINE

    # carry is consumed exactly once
    _save(cr, op, {"user_message": "and now"})
    assert _user_turns(op)[-1]["text"] == "and now"

    # a locationless stream request clears any stale carry
    cr._stash_turn_location(op, cr.user_location.normalize(loc))
    cr._stash_turn_location(op, None)
    _save(cr, op, {"user_message": "portal turn"})
    assert _user_turns(op)[-1]["text"] == "portal turn"


def test_stale_carry_expires(save_env, monkeypatch):
    cr = save_env
    op = "LocTester-stale"
    cr._stash_turn_location(op, cr.user_location.normalize(
        {"lat": FAKE_LAT, "lon": FAKE_LON}))
    monkeypatch.setattr(cr, "_LOCATION_CARRY_TTL_S", -1.0)
    _save(cr, op, {"user_message": "much later"})
    assert _user_turns(op)[-1]["text"] == "much later"


# ── end-to-end: /chat/save -> perform_mint -> snapshot body ─────────────────

@pytest.fixture
def mint_env(save_env, monkeypatch, tmp_path):
    """As save_env, plus auto-mint ON with the REAL perform_mint over stubbed
    ledger/embedding I/O, so the rendered snapshot body can be inspected."""
    import Orchestrator.checkpoint as ckpt

    bodies = []
    monkeypatch.setattr(save_env, "AUTO_ENABLE", True)
    monkeypatch.setattr(save_env, "TURNS_THRESHOLD", 1)
    monkeypatch.setattr(save_env, "DEBOUNCE_MS", 0)
    monkeypatch.setattr(ckpt, "verify_gm_or_halt", lambda: None)
    monkeypatch.setattr(ckpt, "read_text_safe", lambda _p: "")
    monkeypatch.setattr(ckpt, "parse_tail", lambda _t: dict(INFO))
    monkeypatch.setattr(ckpt, "next_snap_id_from_tail", lambda _t: "SNAP-20260724-0002")
    monkeypatch.setattr(ckpt, "append_snapshot_text", lambda b: bodies.append(b))
    monkeypatch.setattr(ckpt, "VOL_PATH", tmp_path / "no-such-volume.txt")
    monkeypatch.setattr(ckpt, "_embed_for_index", lambda *a, **k: {})
    monkeypatch.setattr(ckpt, "update_snapshot_index", lambda *a, **k: None)
    monkeypatch.setattr(ckpt, "archive_volume", lambda: ("/tmp/arc.txt", "deadbeef", "utc"))
    return save_env, bodies


def test_mint_records_both_the_prompt_line_and_the_gauge(mint_env):
    cr, bodies = mint_env
    _save(cr, "LocTester-mint", {
        "user_message": "any good tacos near me?",
        "location": {"lat": FAKE_LAT, "lon": FAKE_LON,
                     "city": "Mountain View", "state": "California"},
    })
    assert len(bodies) == 1
    body = bodies[0]
    assert FAKE_LINE in body                                        # ledger prose
    assert "LOCATION: 37.4224,-122.0841 · Mountain View, California" in body  # gauge


def test_mint_consumes_the_location_so_it_cannot_leak_to_a_later_snapshot(mint_env):
    """A cron/CU/on-device mint stamps no location; the previous turn's fix must
    not survive into it."""
    cr, bodies = mint_env
    op = "LocTester-leak"
    _save(cr, op, {"user_message": "near me?",
                   "location": {"lat": FAKE_LAT, "lon": FAKE_LON, "city": "Mountain View"}})
    from Orchestrator.state import get_state
    assert get_state(op).last_context_meta["location"] == ""

    from Orchestrator.checkpoint import perform_mint
    get_state(op).add_conversation_turn({"role": "user", "utc": "u", "text": "cron turn"})
    perform_mint(op, "TURNS")
    assert "LOCATION" not in bodies[-1]


def test_mint_without_location_has_neither(mint_env):
    cr, bodies = mint_env
    _save(cr, "LocTester-mint-absent", {"user_message": "any good tacos near me?"})
    assert len(bodies) == 1
    body = bodies[0]
    assert "LOCATION" not in body
    assert "[user location" not in body
