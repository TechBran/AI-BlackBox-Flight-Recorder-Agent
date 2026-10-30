"""What the model is told (spec §4.3, §4.6, §5, §10): the WELLNESS RULES and
the operator's health instinct prompt ride in the tool instructions only when a
wellness tool is offered, the operator's own prompt replaces the default, the
"not shared with this AI" line appears for an enrolled operator on a provider
their data is not shared with, and nothing at all otherwise. The core system
prompt itself is untouched: these are tool instructions."""
from __future__ import annotations

import pytest

from Orchestrator.tests.wellness_agent_support import (  # noqa: F401 - agent_box is a fixture
    LAN_ALIAS, LAN_MODEL, OTHER, agent_box, custom_server, edit_record, stamped, stub_retrieval)
from Orchestrator.tests.wellness_support import ENR, OPERATOR
from Orchestrator.wellness import access, catalog

PROMPT = "should I run today or rest"
SIGNED = access._Granted(OPERATOR, ENR)


@pytest.fixture
def box(agent_box, monkeypatch):
    import Orchestrator.routes.chat_routes as cr
    monkeypatch.setattr(cr, "TOOLVAULT_ENABLED", True)
    stub_retrieval(monkeypatch)
    return agent_box


def _turn(provider="anthropic", model=None, **kw):
    return access.request_turn(operator=kw.pop("operator", OPERATOR), provider=provider,
                               model=model, **kw)


def _system(provider="anthropic", operator=OPERATOR) -> str:
    import Orchestrator.routes.chat_routes as cr
    msgs, _p, _t = cr.build_streaming_context([{"role": "user", "content": PROMPT}], operator,
                                              provider)
    return msgs[0]["content"]


def test_rules_then_the_default_instinct_prompt_with_the_tools(box):
    with stamped(_turn(signed=SIGNED)):
        system = _system()
    assert "Tool: wellness_summary" in system
    rules = system.index(catalog.WELLNESS_RULES)
    assert system.index(catalog.DEFAULT_INSTINCT_PROMPT) > rules
    # Once: the per-tool notes that carry the same rules are not repeated.
    assert system.count(catalog.WELLNESS_RULES) == 1
    assert access.NOT_SHARED.split("{")[0] not in system


def test_the_operators_own_instinct_prompt_replaces_the_default(box):
    own = "When I ask about training, look at my last two weeks of sleep first."
    edit_record(instinct_prompt=own)
    with stamped(_turn(signed=SIGNED)):
        system = _system()
    assert own in system and catalog.DEFAULT_INSTINCT_PROMPT not in system
    assert system.index(own) > system.index(catalog.WELLNESS_RULES)


def test_the_not_shared_line_names_the_allowed_ais(box):
    custom_server()                              # a model server on this network
    with stamped(_turn("openai", signed=SIGNED)):
        system = _system("openai")
    assert catalog.WELLNESS_RULES not in system and "Tool: wellness_" not in system
    line = access.NOT_SHARED.format(allowed=f"Anthropic or {LAN_ALIAS}")
    assert line in system and system.count("not shared with this AI") == 1


def test_the_not_shared_line_without_any_allowed_ai(box):
    edit_record(providers_allowed=[])
    with stamped(_turn("openai", signed=SIGNED)):
        system = _system("openai")
    assert access.NOT_SHARED_NONE in system


@pytest.mark.parametrize("turn_kw", [
    {"operator": OTHER},                                  # not enrolled
    {},                                                   # unsigned Portal, credentialed box
    {"declared": "sms"},
    {"declared": "cron"},
])
def test_nothing_at_all_for_the_unenrolled_or_a_denied_surface(box, turn_kw):
    operator = turn_kw.get("operator", OPERATOR)
    with stamped(_turn(**turn_kw)):
        system = _system(operator=operator)
    assert catalog.WELLNESS_RULES not in system
    assert catalog.DEFAULT_INSTINCT_PROMPT not in system
    assert "not shared with this AI" not in system
    # ... and not even on a provider that is not allowed.
    with stamped(_turn("openai", **turn_kw)):
        assert "not shared with this AI" not in _system("openai", operator=operator)


def test_nothing_with_no_stamped_turn(box):
    system = _system()
    assert catalog.WELLNESS_RULES not in system and "not shared with this AI" not in system


def test_a_local_model_gets_the_rules_by_default(box):
    custom_server()
    with stamped(_turn("custom", LAN_MODEL, signed=SIGNED)):
        system = _system("custom")
    assert catalog.WELLNESS_RULES in system and "Tool: wellness_summary" in system


def test_the_core_system_prompt_is_untouched(box):
    from Orchestrator.tasks import build_core_system_prompt
    with stamped(_turn(signed=SIGNED)):
        from Orchestrator.toolvault.injector import inject_for_prompt
        _, instructions = inject_for_prompt(PROMPT, "anthropic",
                                            ctx=access.tool_context("anthropic"))
        system = _system()
    assert catalog.WELLNESS_RULES in instructions
    assert system == build_core_system_prompt(instructions, operator=OPERATOR)
    assert catalog.WELLNESS_RULES not in build_core_system_prompt("", operator=OPERATOR)


def test_tools_never_go_out_without_their_rules(box, monkeypatch):
    """If the rules cannot be built, the wellness tools are withheld."""
    from Orchestrator.routes.chat import providers
    from Orchestrator.tests.wellness_agent_support import tool_names
    from Orchestrator.toolvault import injector

    def broken(*a, **k):
        raise RuntimeError("no rules")
    monkeypatch.setattr(access, "instructions", broken)
    with stamped(_turn(signed=SIGNED)):
        tools, text = injector.inject_for_prompt(PROMPT, "anthropic",
                                                 ctx=access.tool_context("anthropic"))
        assert not {n for n in tool_names(tools) if n.startswith("wellness_")}
        assert not {n for n in tool_names(providers._get_tools("anthropic", PROMPT))
                    if n.startswith("wellness_")}
    assert "wellness_" not in text
