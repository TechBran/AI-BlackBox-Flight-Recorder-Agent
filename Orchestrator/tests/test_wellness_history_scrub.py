"""The history scrub on a provider switch (spec §3.7, W30; check A3b).

/chat/stream takes the chat history and the provider from the client, so a
model switch mid-chat would re-send earlier health bubbles to a provider the
operator never allowed. Before the provider call, an earlier assistant message
tagged `wellness_ref` is masked against its stored payload when the provider
is not allowed for that ref's categories; for a client that sends no
`wellness_ref`, every health number in its assistant messages is masked
whenever an enrolled operator is on a provider that is not allowed. The user's
own messages stay verbatim and `wellness_ref` never reaches the provider.

The A3b test replays POST /chat/stream for every provider through the golden
harness (Orchestrator/tests/provider_goldens.py) and inspects the request body
the provider would have received.
"""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from Orchestrator.tests import provider_goldens as pg
from Orchestrator.tests.wellness_ledger_support import ALL_ASK, FX, deliver, envelopes
from Orchestrator.tests.wellness_support import OPERATOR, enrolled_fixture, roster
from Orchestrator.wellness import mask

SENTINELS = FX.SENTINEL_STRINGS
BUBBLE = ("Last night your sleeping heart rate was 47.3, and on Sunday you walked "
          "12,345 steps. This morning: 118/79.")
# The bubble as Anthropic may receive it: sleep, heart and activity are ticked
# for it; blood pressure is off now (and local-only), so its reading goes.
BUBBLE_FOR_ANTHROPIC = BUBBLE.replace("118/79", "[·]/[·]")
NO_BP = "Last night your sleeping heart rate was 47.3, and on Sunday you walked 12,345 steps."


CATS = {"night": ["heart", "sleep"], "steps": ["activity"], "bp": ["blood_pressure"]}


@pytest.fixture
def op(isolated_stores, monkeypatch, tmp_path):
    """The fixture operator with sleep, heart and activity on and Anthropic
    ticked; its answers (blood pressure included, read while it was on) were
    delivered earlier in the chat."""
    from Orchestrator.tests.wellness_support import write_record
    roster(monkeypatch, OPERATOR, "other-human", "Flight Recorder")
    rec, st, _ = enrolled_fixture(tmp_path, monkeypatch, categories=ALL_ASK)
    envs = envelopes()
    rec = write_record(providers_allowed=["anthropic"], last_push_at=rec["last_push_at"])
    refs = {k: deliver(st, envs[k], "wt_hist", categories=CATS[k]) for k in CATS}
    # The operator's own credentialed chat turn: its key may deliver, so the
    # scrub judges the history against the stored values (turn.may_deliver).
    from Orchestrator.wellness import turn as turn_mod
    turn = turn_mod.WellnessTurn(OPERATOR, turn_mod.SURFACE_ANDROID, True, provider="anthropic",
                                 turn_key=turn_mod.new_turn_key())
    turn_mod.remember(turn, True)
    token = turn_mod.stamp(turn)
    try:
        yield {"rec": rec, "st": st, "envs": envs, "refs": refs}
    finally:
        turn_mod.clear(token)


def _history(ref=None, content=BUBBLE):
    bubble = {"role": "assistant", "content": content}
    if ref:
        bubble["wellness_ref"] = ref
    return [{"role": "user", "content": "how did I sleep? my BP was 118/79"}, bubble,
            {"role": "user", "content": "and what should I do today?"}]


def _hits(obj):
    text = json.dumps(obj, ensure_ascii=False)
    return [s for s in SENTINELS if s in text]


def test_a_tagged_bubble_is_masked_for_a_provider_that_is_not_allowed(op):
    msgs = _history(op["refs"]["night"])
    out = mask.scrub_history(msgs, OPERATOR, "openai")
    assert "47.3" not in out[1]["content"] and "[·]" in out[1]["content"]
    assert all("wellness_ref" not in m for m in out)
    assert out[0] == msgs[0] and out[2] == msgs[2]                 # user words verbatim
    assert "wellness_ref" in msgs[1]                               # the caller's list untouched


def test_an_allowed_provider_gets_the_bubble_without_the_ref_key(op):
    msgs = _history(op["refs"]["night"], NO_BP)
    out = mask.scrub_history(msgs, OPERATOR, "anthropic")           # ticked for sleep, heart
    assert out[1] == {"role": "assistant", "content": NO_BP}


def test_a_bubble_tagged_with_an_allowed_ref_still_loses_what_the_provider_may_not_get(op):
    """A bubble can quote more than its own ref (an on-device reply covering
    last night and this morning's blood pressure, tagged with the night ref):
    the provider gets the night's numbers, never the reading it may not have."""
    out = mask.scrub_history(_history(op["refs"]["night"]), OPERATOR, "anthropic")
    assert out[1] == {"role": "assistant", "content": BUBBLE_FOR_ANTHROPIC}
    out = mask.scrub_history(_history(), OPERATOR, "anthropic")      # and without a ref
    assert out[1] == {"role": "assistant", "content": BUBBLE_FOR_ANTHROPIC}


@pytest.mark.parametrize("state", ("revoked", "paused"))
def test_a_disconnect_that_kept_the_history_still_scrubs(op, state):
    """Disconnect with Keep history: the results stay in the store, the
    operator is no longer live, so no provider may receive them, and an old
    client that never sends refs gets every health number masked."""
    from Orchestrator.tests.wellness_support import write_record
    write_record(state=state, providers_allowed=["anthropic"],
                 last_push_at=op["rec"]["last_push_at"])
    for provider in ("openai", "anthropic"):
        out = mask.scrub_history(_history(), OPERATOR, provider)
        if state == "revoked" or provider == "openai":
            assert not _hits(out[1]), (state, provider, out[1])
        else:                                    # paused is still live: as before
            assert out[1]["content"] == BUBBLE_FOR_ANTHROPIC
        assert out[0]["content"] == "how did I sleep? my BP was 118/79"


def test_a_local_only_category_ref_is_masked_even_on_an_allowed_cloud(op, monkeypatch):
    from Orchestrator.tests.wellness_support import write_record
    write_record(providers_allowed=["anthropic"], categories=ALL_ASK,
                 last_push_at=op["rec"]["last_push_at"])
    bp_ref = op["refs"]["bp"]
    out = mask.scrub_history(_history(bp_ref, "Your blood pressure was 118/79."), OPERATOR,
                             "anthropic")
    assert "118/79" not in out[1]["content"]
    assert mask.scrub_history(_history(bp_ref, "It was 118/79."), OPERATOR, "local")[1][
        "content"] == "It was 118/79."                               # on-device: allowed


def test_an_unknown_ref_is_masked_with_the_unit_rule(op):
    out = mask.scrub_history(_history("w-20990101-dead", "You slept 7 hours, 61 bpm."),
                             OPERATOR, "openai")
    assert out[1]["content"] == "You slept [·] hours, [·] bpm."


def test_a_client_that_never_sends_refs_is_masked_on_a_disallowed_provider(op):
    msgs = _history()
    out = mask.scrub_history(msgs, OPERATOR, "openai")
    assert not _hits(out[1]) and out[0] == msgs[0]
    # Multipart content too.
    parts = [{"role": "assistant", "content": [{"type": "text", "text": BUBBLE}]}]
    assert not _hits(mask.scrub_history(parts, OPERATOR, "xai"))
    # On an allowed provider nothing changes: the same list object.
    plain = _history(content=NO_BP)
    assert mask.scrub_history(plain, OPERATOR, "anthropic") is plain


def test_operators_without_health_get_the_same_list(op):
    msgs = _history()
    assert mask.scrub_history(msgs, "other-human", "openai") is msgs
    assert mask.scrub_history(msgs, "", "openai") is msgs
    # A stray ref is still never sent upstream.
    tagged = _history("w-1")
    assert all("wellness_ref" not in m for m in mask.scrub_history(tagged, "other-human",
                                                                   "openai"))


def test_a_failed_scrub_masks_instead_of_sending(op, monkeypatch):
    from Orchestrator.wellness import destinations

    def boom(*a, **k):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(destinations, "allowed", boom)
    out = mask.scrub_history(_history(), OPERATOR, "openai")
    assert "[·]" in out[1]["content"] and "12,345" not in out[1]["content"]


# ------------------------------------------------------------------ A3b through /chat/stream

PROVIDERS = ("openai", "anthropic", "google", "xai", "custom")


def _stream(spec, monkeypatch, tmp_path, messages, provider=None):
    import Orchestrator.routes.chat_routes as cr
    golden_root = tmp_path / "golden"
    golden_root.mkdir(exist_ok=True)
    cap = pg.install(spec, monkeypatch, golden_root)
    body = {"messages": copy.deepcopy(messages), "provider": provider or spec["provider"],
            "model": spec["model"], "operator": OPERATOR}

    async def go():
        resp = await cr.chat_stream_post(pg._Req(body))
        return await pg._drain(resp)

    sse = asyncio.run(go())
    assert "event: stream_end" in sse
    return cap.upstream


def _spec(provider):
    return next(s for s in pg.scenario_specs() if s["name"] == f"{provider}-stream-text")


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("with_ref", [True, False])
def test_no_earlier_number_reaches_a_disallowed_provider(op, monkeypatch, tmp_path, provider,
                                                         with_ref):
    """A3b: the chat was answered on an allowed provider (the bubble holds the
    numbers); the picker moves to one the operator never ticked. The request
    body that provider receives carries none of them, with or without the
    client echoing wellness_ref."""
    from Orchestrator.tests.wellness_support import write_record
    write_record(providers_allowed=[], last_push_at=op["rec"]["last_push_at"])
    ref = op["refs"]["night"] if with_ref else None
    upstream = _stream(_spec(provider), monkeypatch, tmp_path, _history(ref))
    assert upstream, "no provider request was recorded"
    blob = json.dumps([u["json"] for u in upstream], ensure_ascii=False)
    for s in ("47.3", "12,345"):
        assert s not in blob, (provider, s)
    assert "wellness_ref" not in blob
    assert "how did I sleep? my BP was 118/79" in blob                 # the user's own words


@pytest.mark.parametrize("alias,ticked", [("local", []), ("claude", ["anthropic"]),
                                          ("grok", ["xai"]), ("openai_rest", ["openai"]),
                                          ("custom:gemma-box", [])])
def test_a_provider_string_the_dispatcher_sends_to_gemini_is_judged_as_gemini(
        op, monkeypatch, tmp_path, alias, ticked):
    """/chat/stream sends every provider string it does not dispatch by name
    to Gemini, whatever the allow-list would make of it ("local" reads as the
    on-device model, "claude" as Anthropic): the scrub judges Google, the
    provider that really receives the history."""
    from Orchestrator.routes.chat import stream as stream_mod
    from Orchestrator.tests.wellness_support import write_record
    write_record(providers_allowed=ticked, last_push_at=op["rec"]["last_push_at"])
    assert stream_mod.dispatched_provider(alias) == "google"
    upstream = _stream(_spec("google"), monkeypatch, tmp_path, _history(), provider=alias)
    assert upstream and all("gemini" in (u.get("url") or "") for u in upstream)
    blob = json.dumps([u["json"] for u in upstream], ensure_ascii=False)
    for s in ("47.3", "12,345"):
        assert s not in blob, (alias, s)


def test_dispatched_provider_names_what_open_provider_stream_calls(monkeypatch):
    """dispatched_provider and _open_provider_stream agree on every name."""
    from types import SimpleNamespace

    from Orchestrator.routes.chat import stream as stream_mod
    calls = {"stream_openai_with_reasoning": "openai", "stream_anthropic_with_thinking": "anthropic",
             "stream_xai_with_reasoning": "xai", "stream_custom_with_reasoning": "custom",
             "stream_gemini_with_thinking": "google"}
    for fn, name in calls.items():
        monkeypatch.setattr(stream_mod, fn, lambda *a, _n=name, **k: _n)
    for p in ("openai", "anthropic", "xai", "custom", "google", "gemini", "claude", "local",
              "grok", "on_device", "", "OPENAI"):
        req = SimpleNamespace(provider=p, model="m", operator=OPERATOR, cu_session_id=None,
                              cu_device_id="blackbox")
        assert stream_mod._open_provider_stream(req, []) == stream_mod.dispatched_provider(p), p
    assert stream_mod.dispatched_provider("computer-use") == "computer-use"


def test_a_computer_use_turn_gets_no_health_numbers(op):
    """A CU agent is never a destination for health data (an unknown provider
    to the allow-list): the history it is handed is masked."""
    out = mask.scrub_history(_history(), OPERATOR, "computer-use")
    assert not _hits(out[1])


@pytest.mark.parametrize("provider", ("anthropic", "openai"))
def test_an_allowed_provider_still_receives_the_history_as_is(op, monkeypatch, tmp_path,
                                                              provider):
    """On a turn that could deliver (here household mode: one human on the
    box, so the unsigned stream is the operator's own) an allowed provider
    gets the history as the client sent it, minus the ref key."""
    from Orchestrator.tests.wellness_support import write_record
    roster(monkeypatch, OPERATOR, "Flight Recorder")
    write_record(providers_allowed=[provider], last_push_at=op["rec"]["last_push_at"])
    upstream = _stream(_spec(provider), monkeypatch, tmp_path, _history(op["refs"]["night"]))
    blob = json.dumps([u["json"] for u in upstream], ensure_ascii=False)
    assert "47.3" in blob and "wellness_ref" not in blob


# ── An unproven turn never reads the store ─────────────────────────────────

def _unproven(monkeypatch):
    """No turn stamped (a direct call), or a stamped unsigned turn: the scrub
    cannot know the history is this operator's own."""
    from Orchestrator.wellness import turn as turn_mod
    monkeypatch.setattr(turn_mod, "current", lambda: None)


def test_an_unproven_turn_loses_every_number_of_a_tagged_bubble_hit_or_miss(op, monkeypatch):
    """/chat/stream takes the operator, the history and its refs from an
    unsigned body, and what the provider receives comes back through the
    model. A masked history that depended on the stored values would tell
    such a caller which numbers are the operator's. So for an unproven turn
    the scrub reads nothing: a bubble tagged wellness_ref loses every number,
    whichever provider, and a hit reads like a miss."""
    _unproven(monkeypatch)
    hit = _history(op["refs"]["night"], "Last night it was 47.3; also 12,345 and 118/79.")
    miss = _history(op["refs"]["night"], "Last night it was 48.3; also 12,346 and 118/80.")
    for provider in ("anthropic", "custom:lan-box", "openai"):
        out_hit = mask.scrub_history(hit, OPERATOR, provider)[1]["content"]
        out_miss = mask.scrub_history(miss, OPERATOR, provider)[1]["content"]
        assert out_hit == out_miss == "Last night it was [·]; also [·] and [·]."
    # Untagged: the unit rule, and only for a provider that may not have the
    # operator's data (openai is not ticked; anthropic is).
    out = mask.scrub_history(_history(), OPERATOR, "openai")
    assert out[1]["content"] == mask.mask_text(BUBBLE, frozenset()) and "wellness_ref" not in out[1]
    assert "47.3" not in out[1]["content"] and "12,345" not in out[1]["content"]
    assert mask.scrub_history(_history(), OPERATOR, "anthropic")[1]["content"] == BUBBLE


def test_an_unproven_turn_never_looks_a_ref_up(op, monkeypatch):
    """Refs are client-supplied names: a guessed one (or a real one) is not
    read for an unproven turn, so the store is never opened for a lookup."""
    from Orchestrator.wellness import store as store_mod
    _unproven(monkeypatch)

    def never(*a, **k):
        raise AssertionError("the store was read for an unproven turn")
    monkeypatch.setattr(store_mod.Store, "get_delivery", never)
    monkeypatch.setattr(mask, "_recent_history_rows", never)
    for ref in (op["refs"]["night"], "w-20261029-ffff", "not-a-ref"):
        out = mask.scrub_history(_history(ref, "It was 47.3 and 12,345."), OPERATOR, "anthropic")
        assert out[1] == {"role": "assistant", "content": "It was [·] and [·]."}


def test_a_proven_turn_still_judges_by_the_stored_values(op):
    """The operator's own turn (the fixture stamps one that may deliver): the
    allowed provider gets the numbers it may have, the ref key dropped."""
    out = mask.scrub_history(_history(op["refs"]["night"], NO_BP), OPERATOR, "anthropic")
    assert out[1] == {"role": "assistant", "content": NO_BP}


def test_an_unproven_turn_keeps_ordinary_numbers_for_an_allowed_provider(op, monkeypatch):
    """The unit rule on an unproven turn's untagged bubbles applies only when
    the provider may not have the operator's data (the enrollment record
    decides, never the store): an allowed provider keeps "250 ms" and "0.7"."""
    from Orchestrator.tests.wellness_support import write_record
    _unproven(monkeypatch)
    plain = "The request took 250 ms and coverage rose to 87%. Set temperature to 0.7."
    assert mask.scrub_history(_history(None, plain), OPERATOR, "anthropic")[1]["content"] == plain
    write_record(providers_allowed=[], last_push_at=op["rec"]["last_push_at"])
    out = mask.scrub_history(_history(None, plain), OPERATOR, "anthropic")[1]["content"]
    assert out == mask.mask_text(plain, frozenset()) and "250 ms" not in out


def test_the_scrubs_failure_path_masks_a_tagged_bubble_entirely(op, monkeypatch):
    """When the scrub itself fails (the store locked, a broken record), a
    bubble tagged wellness_ref loses every number and the rest get the unit
    rule: the failure path can never say more than the unproven path."""
    from Orchestrator.wellness import enroll
    monkeypatch.setattr(enroll, "effective_state", lambda rec: (_ for _ in ()).throw(RuntimeError("x")))
    out = mask.scrub_history(_history(op["refs"]["night"], "47.3 is within range, 12,345 too."),
                             OPERATOR, "anthropic")
    assert out[1] == {"role": "assistant", "content": "[·] is within range, [·] too."}
    out = mask.scrub_history(_history(None, "Your sleeping heart rate was 47.3 and 12,345 steps."),
                             OPERATOR, "anthropic")
    assert "47.3" not in out[1]["content"] and "12,345" not in out[1]["content"]
