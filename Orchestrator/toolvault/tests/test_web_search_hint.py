from Orchestrator.toolvault import availability as av
from Orchestrator.toolvault import injector


def test_no_web_tool_no_hint():
    assert av.default_web_search_hint(["roll_dice", "web_fetch"]) == ""


def test_prefers_default_when_injected(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {"WEB_SEARCH_DEFAULT": "perplexity"})
    hint = av.default_web_search_hint(["perplexity_web_search", "grok_web_search"])
    assert "perplexity_web_search" in hint
    assert "prefer" in hint.lower()


def test_cross_check_when_multiple_general(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {"WEB_SEARCH_DEFAULT": "perplexity"})
    hint = av.default_web_search_hint(["perplexity_web_search", "grok_web_search"])
    assert "cross-check" in hint.lower()


def test_grok_x_mentioned_when_present(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {"WEB_SEARCH_DEFAULT": ""})
    hint = av.default_web_search_hint(["perplexity_web_search", "grok_x_search"])
    assert "grok_x_search" in hint
    assert "x (twitter)" in hint.lower() or "twitter" in hint.lower()


def test_default_unset_is_graceful(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {})
    hint = av.default_web_search_hint(["perplexity_web_search"])
    assert hint  # non-empty, no crash
    assert "prefer" not in hint.lower()  # no default -> no "prefer X"


def test_build_tool_instructions_appends_hint(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {"WEB_SEARCH_DEFAULT": "perplexity"})
    # use a real injected web tool name; build_tool_instructions reads the registry entry
    out = injector.build_tool_instructions(["perplexity_web_search"])
    assert "WEB SEARCH GUIDANCE" in out
    assert "perplexity_web_search" in out


def test_build_tool_instructions_no_hint_without_web_tool():
    out = injector.build_tool_instructions(["roll_dice"])
    assert "WEB SEARCH GUIDANCE" not in out
