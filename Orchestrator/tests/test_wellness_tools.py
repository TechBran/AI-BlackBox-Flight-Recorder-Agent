"""The wellness_* ToolVault tools (spec §4.2, §10): envelopes, the rendering the
model reads, caps by destination class, coarsening, as_of / stale, the display
and spoken strings, the operator from ctx only, the delivered rows written with
the turn key, and every error shape. Run through BlackBoxToolExecutor (the
dispatcher every chat loop's catch-all uses) on the 120-day fixture."""
from __future__ import annotations

import json
import logging

import pytest

from Orchestrator.tests.wellness_agent_support import (  # noqa: F401 - agent_box is a fixture
    FX, LAN_ALIAS, LAN_MODEL, OTHER, WELLNESS_TOOLS, agent_box, custom_server, edit_record,
    run_tool, sentinel_hits, stamped)
from Orchestrator.tests.wellness_support import OPERATOR
from Orchestrator.toolvault import registry
from Orchestrator.wellness import access, api, catalog, gate

DAY_SLEEPING_HR = FX.day_of(FX.SENTINEL_DAYS["sleeping_hr"])
DAY_STEPS = FX.day_of(FX.SENTINEL_DAYS["steps"])


def _tokens(text: str) -> int:
    return (len(text) + api.CHARS_PER_TOKEN - 1) // api.CHARS_PER_TOKEN


def _metric(env, name):
    return next(m for m in env["metrics"] if m["metric"] == name)


# ------------------------------------------------------------------ the modules

def test_the_five_modules_are_valid_and_shaped_as_the_spec_says():
    from Orchestrator.toolvault import validate
    report = validate.validate_all()
    assert not {t: e for t, e in report["errors"].items() if t.startswith("wellness_")}
    for name in WELLNESS_TOOLS:
        spec = registry.get_tool(name)
        assert spec["category"] == "wellness" and spec["groups"] == ["chat"]
        assert spec["x-availability"] == {"feature": "wellness"}
        assert "operator" not in spec["parameters"]["properties"]
        assert spec["notes"] == catalog.WELLNESS_RULES          # the rules ride in the notes
        assert callable(registry.get_executor(name))
    assert registry.get_tool("wellness_summary")["tier"] == 1   # pinned
    assert all(registry.get_tool(n)["tier"] == 2 for n in WELLNESS_TOOLS
               if n != "wellness_summary")
    series = registry.get_tool("wellness_series")["parameters"]["properties"]
    assert series["metric"]["x-source"] == "wellness_metrics"
    for name in ("wellness_summary", "wellness_history"):
        props = registry.get_tool(name)["parameters"]["properties"]
        assert props["categories"]["x-source"] == "wellness_categories"


def test_the_lint_refuses_an_operator_parameter_and_public_groups():
    from Orchestrator.toolvault import availability, resolvers, schema_spec
    good = registry.get_tool("wellness_sources")

    def errors(**over):
        d = dict(good, **over)
        return schema_spec.validate_module_dict(d, "wellness_sources", resolvers.KNOWN_SOURCES,
                                                availability.FEATURES)

    assert errors() == []
    with_op = {"type": "object", "properties": {"operator": {"type": "string"}}, "required": []}
    assert any("W18" in e for e in errors(parameters=with_op))
    for group in ("mcp", "phone", "chat_cu"):
        assert any("must not be in group" in e for e in errors(groups=["chat", group]))
    assert any("x-availability" in e for e in errors(**{"x-availability": None}))
    stray = dict(registry.get_tool("roll_dice"), **{"x-availability": {"feature": "wellness"}})
    assert any("category" in e for e in schema_spec.validate_module_dict(
        stray, "roll_dice", resolvers.KNOWN_SOURCES, availability.FEATURES))


# ------------------------------------------------------------------ envelopes + rendering

def test_summary_one_day_envelope_and_rendering(agent_box):
    with stamped() as t:
        r = run_tool("wellness_summary", {"period": "custom", "start": DAY_SLEEPING_HR,
                                          "end": DAY_SLEEPING_HR})
    assert r.success, r.result
    env = r.data
    assert env["ok"] and env["tool"] == "wellness_summary" and env["ref"].startswith("w-")
    assert env["categories"] == ["sleep", "heart", "activity"]
    hr = _metric(env, "sleeping_hr_median")
    assert hr["value"] == FX.SENTINEL_SLEEPING_HR and hr["display"] == "47 bpm"
    assert hr["usual"]["n"] > 0 and hr["coverage"]["pct"] == 100
    text = r.result
    # The model reads display strings, the usual range, how it compares and
    # the coverage, never the raw value.
    assert "sleeping_hr_median (Sleeping heart rate): 47 bpm" in text
    assert 'spoken "forty-seven beats per minute"' in text
    assert "usual " in text and "your usual range" in text and "coverage worn all night" in text
    assert env["ref"] in text and "as of " in text
    assert str(FX.SENTINEL_SLEEPING_HR) not in text          # the raw value never: W6
    assert t.turn_key


def test_display_and_spoken_strings_for_steps(agent_box):
    with stamped():
        r = run_tool("wellness_summary", {"period": "custom", "start": DAY_STEPS,
                                          "end": DAY_STEPS, "categories": ["activity"]})
    steps = _metric(r.data, "steps")
    assert steps["value"] == FX.SENTINEL_STEPS
    assert steps["display"] == "12,345 steps" and steps["spoken"].startswith("about twelve thousand")
    assert "steps (Steps): 12,345 steps" in r.result
    assert r.data["categories"] == ["activity"]
    assert not any(m["category"] != "activity" for m in r.data["metrics"])


def test_series_sessions_history_and_sources_render(agent_box):
    with stamped():
        series = run_tool("wellness_series", {"metric": "resting_hr", "range": "last_30_days"})
        sessions = run_tool("wellness_sessions", {"kind": "exercise", "limit": 3})
        history = run_tool("wellness_history", {"start": FX.day_of(-6), "end": FX.day_of(0)})
        sources = run_tool("wellness_sources", {})
    assert series.success and series.data["metric"] == "resting_hr"
    assert "time | value | coverage" in series.result and " bpm | " in series.result
    assert sessions.success and len(sessions.data["sessions"]) == 3
    assert sessions.result.count("\n- s-") == 3 and "avg HR" in sessions.result
    assert history.success and history.data["resolution"] == "day"
    assert len(history.data["rows"]) == 7
    assert "resting_hr (bpm)" in history.result and FX.day_of(-3) in history.result
    assert "12,345" in history.result                       # display grouping, not 12345
    assert sources.success and "heart-rate mode" in sources.result
    assert str(FX.SENTINEL_STEPS) not in history.result      # display strings only


def test_operator_comes_from_ctx_only(agent_box):
    with stamped():
        # A model-supplied operator never re-points the tool: the dispatcher
        # refuses another operator outright, and the session's own is a no-op.
        other = run_tool("wellness_summary", {"period": "last_7_days", "operator": OTHER})
        own = run_tool("wellness_summary", {"period": "last_7_days", "operator": OPERATOR})
    assert not other.success and "cannot act as" in other.result
    assert own.success
    # The turn is OPERATOR's: a session running as anyone else gets nothing.
    with stamped():
        r = run_tool("wellness_summary", {"period": "last_7_days"}, operator=OTHER)
    assert not r.success and r.data["error"] == "not_allowed"


def test_delivered_rows_carry_the_turn_key(agent_box):
    st = agent_box["store"]
    with stamped(provider="anthropic", model="claude-test") as t:
        refs = [run_tool(name, params).data["ref"] for name, params in (
            ("wellness_summary", {"period": "yesterday"}),
            ("wellness_series", {"metric": "steps", "range": "last_7_days"}),
            ("wellness_sessions", {"kind": "sleep", "limit": 2}),
            ("wellness_history", {"start": FX.day_of(-2), "end": FX.day_of(0)}),
            ("wellness_sources", {}))]
    rows = st.deliveries_for_turn(t.turn_key)
    assert [r["ref"] for r in rows] == refs
    assert [r["tool"] for r in rows] == ["wellness_summary", "wellness_series",
                                         "wellness_sessions", "wellness_history",
                                         "wellness_sources"]
    for row in rows:
        assert (row["surface"], row["provider"], row["model"]) == \
            ("android", "anthropic", "claude-test")
        assert row["payload"]["ref"] == row["ref"] and row["masked_at"] is None
    assert rows[1]["categories"] == ["activity"] and rows[2]["categories"] == ["sleep"]
    assert st.get_delivery(refs[0])["payload"]["tool"] == "wellness_summary"


# ------------------------------------------------------------------ caps + coarsening

WORST = (("wellness_summary", {"period": "last_30_days"}),
         ("wellness_series", {"metric": "resting_hr", "range": "last_365_days",
                              "granularity": "day"}),
         ("wellness_sessions", {"kind": "sleep", "range": "last_90_days", "limit": 20}),
         ("wellness_history", {"start": FX.day_of(-91), "end": FX.day_of(0)}))


@pytest.mark.parametrize("provider, surface, cap", [
    ("anthropic", "android", api.CAP_TOKENS["cloud"]),
    ("local", "on_device", api.CAP_TOKENS["on_device"]),
])
def test_every_answer_fits_its_destination_cap(agent_box, provider, surface, cap):
    with stamped(provider=provider, model=None, surface=surface):
        assert access.budget_tokens(access.turn_mod.current()) == cap
        for name, params in WORST:
            r = run_tool(name, params)
            assert r.success, (name, r.result)
            assert _tokens(r.result) <= cap, (name, _tokens(r.result))
            assert api.estimate_tokens(r.data) <= cap
            json.loads(json.dumps(r.data))                 # always whole JSON


def test_on_device_cap_coarsens_and_says_so(agent_box):
    with stamped(provider="local", model=None, surface="on_device"):
        r = run_tool("wellness_history", {"start": FX.day_of(-91), "end": FX.day_of(0)})
    assert r.data["coarsened"] and r.data["resolution"] in ("week", "month")
    assert "shown coarser to fit" in r.result and "one row per week" in r.result
    with stamped(provider="anthropic"):
        full = run_tool("wellness_history", {"start": FX.day_of(-91), "end": FX.day_of(0)})
    assert full.data["resolution"] == "day" and len(full.data["rows"]) == 92


def test_custom_server_cap_stays_inside_half_its_window_guard(agent_box):
    from Orchestrator.onboarding import custom_servers
    custom_server(context_tokens=8192)
    srv, bare = custom_servers.resolve_model(LAN_MODEL)
    guard = custom_servers.window_guard_tokens(srv, bare)
    with stamped(provider="custom", model=LAN_MODEL) as t:
        cap = access.budget_tokens(t)
        r = run_tool("wellness_history", {"start": FX.day_of(-91), "end": FX.day_of(0)})
    assert cap == int(guard * access.CUSTOM_GUARD_SHARE) < api.CAP_TOKENS["local"]
    assert r.success and _tokens(r.result) <= cap
    custom_server(context_tokens=131072)
    with stamped(provider="custom", model=LAN_MODEL) as t:
        assert access.budget_tokens(t) == api.CAP_TOKENS["local"]


# ------------------------------------------------------------------ as_of / stale / paused

def test_as_of_and_stale(agent_box, monkeypatch):
    with stamped():
        fresh = run_tool("wellness_summary", {"period": "yesterday"})
    assert fresh.data["as_of"].startswith(FX.day_of(0)) and fresh.data["stale"] is False
    assert "STALE" not in fresh.result
    monkeypatch.setattr(access, "_now_ms", lambda: FX.now_ms() + 3 * 86_400_000)
    with stamped():
        stale = run_tool("wellness_summary", {"period": "last_7_days"})
    assert stale.data["stale"] is True
    assert 'STALE: say "as of" when you use it' in stale.result
    assert f"as of {stale.data['as_of']}" in stale.result


def test_paused_answers_from_the_store_and_says_so(agent_box):
    edit_record(state="paused")
    with stamped():
        r = run_tool("wellness_summary", {"period": "last_7_days"})
        empty = run_tool("wellness_summary", {"period": "custom", "start": "2026-01-01",
                                              "end": "2026-01-02"})
    assert r.success and r.data["paused"] and "collection paused" in r.result
    assert not empty.success and empty.data["error"] == "paused"
    assert empty.data["fix"] == access.PAUSED_FIX


# ------------------------------------------------------------------ error shapes

def _err(r):
    assert not r.success and r.data["ok"] is False, r.result
    assert r.data["fix"] and r.data["fix"] in r.result and r.data["error"] in r.result
    return r.data["error"]


def test_error_shapes(agent_box, monkeypatch):
    with stamped(operator=OTHER):                          # OTHER never enrolled
        assert _err(run_tool("wellness_summary", {"period": "today"}, operator=OTHER)) == \
            "not_enrolled"
    with stamped():
        assert _err(run_tool("wellness_summary", {"period": "today",
                                                  "categories": ["blood_pressure"]})) == \
            "category_off"
    with stamped(provider="openai", model="gpt-test"):
        assert _err(run_tool("wellness_summary", {"period": "today"})) == \
            "provider_not_allowed"
    with stamped(surface="portal", credential_ok=False):
        assert _err(run_tool("wellness_summary", {"period": "today"})) == \
            "surface_not_allowed"
    with stamped():
        assert _err(run_tool("wellness_summary", {"period": "custom", "start": "2026-01-01",
                                                  "end": "2026-01-03"})) == "no_data"
        agent_box["store"].meta_set("earliest_available", FX.day_of(-60))
        assert _err(run_tool("wellness_history", {"start": FX.day_of(-150),
                                                  "end": FX.day_of(-140)})) == \
            "not_enough_history"
        assert _err(run_tool("wellness_series", {"metric": "mood"})) == "invalid_argument"
        assert _err(run_tool("wellness_summary", {"period": "decade"})) == "invalid_argument"
        assert _err(run_tool("wellness_summary", {"period": "today",
                                                  "categories": ["stress"]})) == \
            "invalid_argument"
        assert _err(run_tool("wellness_sessions", {"kind": "nap"})) == "invalid_argument"
        assert _err(run_tool("wellness_history", {"start": FX.day_of(-100),
                                                  "end": FX.day_of(0)})) == "invalid_range"
    from Orchestrator.tests.wellness_agent_support import set_enabled
    set_enabled(False)
    with stamped():
        assert _err(run_tool("wellness_summary", {"period": "today"})) == "wellness_disabled"


def test_a_local_only_category_is_withheld_from_a_cloud_ai(agent_box):
    cats = dict(agent_box["record"]["categories"], blood_pressure="ask")
    edit_record(categories=cats)
    with stamped():                                        # Anthropic: cloud
        r = run_tool("wellness_summary", {"period": "last_7_days"})
        only_bp = run_tool("wellness_summary", {"period": "last_7_days",
                                                "categories": ["blood_pressure"]})
    assert r.success and "blood_pressure" not in r.data["categories"]
    assert r.data["withheld"] == [{"category": "blood_pressure", "error": "provider_not_allowed",
                                   "fix": gate.FIXES["provider_not_allowed"]}]
    assert "Not available here: blood_pressure (provider_not_allowed)" in r.result
    assert not only_bp.success and only_bp.data["error"] == "provider_not_allowed"
    custom_server()
    with stamped(provider="custom", model=LAN_MODEL):     # a server on this network
        local = run_tool("wellness_summary", {"period": "last_7_days"})
    assert "blood_pressure" in local.data["categories"] and "withheld" not in local.data
    assert LAN_ALIAS


def test_a_store_failure_is_a_clean_refusal_without_values(agent_box, monkeypatch, capsys):
    def boom(*a, **k):
        raise ValueError(f"cannot parse {FX.SENTINEL_SLEEPING_HR}")
    monkeypatch.setattr(api, "summary", boom)
    with stamped():
        r = run_tool("wellness_summary", {"period": "today"})
    assert not r.success and r.data["error"] == "wellness_error"
    out = capsys.readouterr()
    assert "ValueError" in out.out and not sentinel_hits(out.out + out.err + r.result)


# ------------------------------------------------------------------ logs

def test_running_every_tool_leaves_no_value_in_any_log(agent_box, capsys, caplog):
    """W32 / A11b: the catch-all's [TOOLVAULT-EXEC] line is the tool name and
    its time; nothing printed or logged carries a sentinel value."""
    import asyncio

    from Orchestrator.providers import tool_dispatch as td
    from Orchestrator.routes.chat.providers import _tool_context
    caplog.set_level(logging.DEBUG)
    calls = [("wellness_summary", {"period": "custom", "start": DAY_SLEEPING_HR,
                                   "end": DAY_STEPS}),
             ("wellness_series", {"metric": "steps", "range": "last_7_days"}),
             ("wellness_history", {"start": FX.day_of(-6), "end": FX.day_of(0)}),
             ("wellness_sessions", {"kind": "sleep"}), ("wellness_sources", {})]
    results = []
    with stamped():
        for loop in ("stream_anthropic", "call_anthropic"):
            for name, params in calls:
                outcome = asyncio.run(td.dispatch_tool(name, dict(params),
                                                       _tool_context(OPERATOR, loop)))
                results.append(outcome)
    assert any("12,345" in o.result for o in results)     # the values DID reach the model
    out = capsys.readouterr()
    printed = out.out + out.err + "\n".join(r.getMessage() for r in caplog.records)
    assert not sentinel_hits(printed), sentinel_hits(printed)
    exec_lines = [ln for ln in printed.splitlines() if "[TOOLVAULT-EXEC]" in ln]
    assert len(exec_lines) == 2 * len(calls)
    assert all(ln.rstrip("\x1b[0m").endswith(" ms") for ln in exec_lines), exec_lines
    for o in results:
        for event in o.events:
            assert not sentinel_hits(json.dumps(event))
