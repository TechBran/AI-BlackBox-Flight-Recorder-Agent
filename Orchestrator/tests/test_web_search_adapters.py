import Orchestrator.web_tools as wt
from Orchestrator.web_tools import SearchResult, _format_search_result, perform_provider_search


def test_format_search_result_includes_answer_and_citations():
    r = SearchResult(answer="Hello world", citations=["https://a.com", "https://b.com"],
                     source_label="TestEngine")
    out = _format_search_result(r, "q")
    assert "Hello world" in out
    assert "https://a.com" in out and "https://b.com" in out
    assert "TestEngine" in out


def test_perform_provider_search_unknown_provider_returns_error_string():
    out = perform_provider_search("nope", "q", use_cache=False)
    assert "unknown" in out.lower() or "unsupported" in out.lower()


def test_perform_provider_search_dispatches_to_adapter(monkeypatch):
    called = {}
    def fake(query, recency):
        called["q"] = query
        return SearchResult(answer="A", citations=["u"], source_label="Perplexity Sonar")
    monkeypatch.setitem(wt.PROVIDER_SEARCHERS, "perplexity", fake)
    out = perform_provider_search("perplexity", "weather", use_cache=False)
    assert called["q"] == "weather"
    assert "A" in out and "Perplexity Sonar" in out


def test_responses_family_parses_output_and_citations(monkeypatch):
    payload = {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "Synth answer"}]}],
        "citations": ["https://x.com/1"]}
    class FakeResp:
        status_code = 200
        text = ""
        def json(self): return payload
        def raise_for_status(self): pass
    monkeypatch.setattr(wt.requests, "post", lambda *a, **k: FakeResp())
    r = wt._responses_search("https://api.x.ai", "KEY", "grok-4.3", "x_search", "q")
    assert r.answer == "Synth answer"
    assert r.citations == ["https://x.com/1"]


def test_gemini_adapter_parses_grounding(monkeypatch):
    payload = {"candidates": [{"content": {"parts": [{"text": "Gem answer"}]},
        "groundingMetadata": {"groundingChunks": [
            {"web": {"uri": "https://redirect/1", "title": "t"}}]}}]}
    class FakeResp:
        status_code = 200
        text = ""
        def json(self): return payload
    monkeypatch.setattr(wt.requests, "post", lambda *a, **k: FakeResp())
    monkeypatch.setattr(wt, "GEMINI_API_KEY", "test-gemini-key")  # never a real key
    r = wt._search_gemini("q", "month")
    assert "Gem answer" in r.answer
    assert any("redirect" in c for c in r.citations)


def test_general_provider_failure_falls_back_to_ddg(monkeypatch):
    monkeypatch.setitem(
        wt.PROVIDER_SEARCHERS,
        "perplexity",
        lambda query, recency: SearchResult(
            ok=False, source_label="Perplexity Sonar", error="HTTP 500"),
    )
    monkeypatch.setattr(
        wt,
        "_search_duckduckgo",
        lambda query, recency: SearchResult(
            ok=True, answer="ddg ans", citations=["u"], source_label="DuckDuckGo"),
    )
    out = perform_provider_search("perplexity", "q", use_cache=False)
    assert "ddg ans" in out
    assert "unavailable" in out


def test_grok_x_failure_does_not_fall_back(monkeypatch):
    monkeypatch.setitem(
        wt.PROVIDER_SEARCHERS,
        "grok_x",
        lambda query, recency: SearchResult(
            ok=False, source_label="Grok (X/Twitter)", error="HTTP 500"),
    )

    def _boom(query, recency):
        raise AssertionError("_search_duckduckgo must not be called for grok_x")

    monkeypatch.setattr(wt, "_search_duckduckgo", _boom)
    out = perform_provider_search("grok_x", "q", use_cache=False)
    assert "Search failed" in out
    assert "ddg" not in out.lower()


def test_duckduckgo_failure_no_recursion(monkeypatch):
    monkeypatch.setitem(
        wt.PROVIDER_SEARCHERS,
        "duckduckgo",
        lambda query, recency: SearchResult(
            ok=False, source_label="DuckDuckGo", error="no results"),
    )
    out = perform_provider_search("duckduckgo", "q", use_cache=False)
    assert "Search failed" in out


# ── DuckDuckGo adapter against the ddgs API (X01-deps-python-06) ────────────
# ddgs replaced duckduckgo_search. These pin the call shape the adapter relies
# on (DDGS().text(query, max_results=...) -> [{title, href, body}]) with a fake
# module, so the test never touches the network.

class _FakeDDGS:
    calls = []

    def text(self, query, **kwargs):
        _FakeDDGS.calls.append((query, kwargs))
        return [
            {"title": "First", "href": "https://example.com/a", "body": "alpha"},
            {"title": "Second", "href": "https://example.com/b", "body": "beta"},
        ]


def _fake_ddgs_module():
    import importlib.machinery
    import types
    mod = types.ModuleType("ddgs")
    mod.__spec__ = importlib.machinery.ModuleSpec("ddgs", None)
    mod.DDGS = _FakeDDGS
    return mod


def test_duckduckgo_adapter_uses_the_ddgs_api(monkeypatch):
    import sys
    _FakeDDGS.calls = []
    monkeypatch.setitem(sys.modules, "ddgs", _fake_ddgs_module())
    r = wt._search_duckduckgo("solar flares", "month")
    assert r.ok
    assert _FakeDDGS.calls == [("solar flares", {"max_results": 5})]
    assert r.citations == ["https://example.com/a", "https://example.com/b"]
    assert "**First**" in r.answer and "alpha" in r.answer
    assert r.source_label == "DuckDuckGo"


def test_duckduckgo_adapter_reports_missing_ddgs(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "ddgs", None)  # `import ddgs` -> ImportError
    r = wt._search_duckduckgo("q", "month")
    assert not r.ok
    assert "ddgs" in r.error


def test_ddgs_available_tracks_importability(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "ddgs", None)
    assert wt.ddgs_available() is False
    monkeypatch.setitem(sys.modules, "ddgs", _fake_ddgs_module())
    assert wt.ddgs_available() is True


class _BrokenLazyLoad(type):
    def __getattr__(cls, name):
        raise ModuleNotFoundError("import of primp halted; None in sys.modules")


class _BrokenLazyDDGS(metaclass=_BrokenLazyLoad):
    """ddgs 9.x exports a proxy: `from ddgs import DDGS` succeeds, and the real
    class, with primp (its HTTP client), is imported on first use."""


def test_ddgs_probe_loads_the_real_class(monkeypatch):
    """O06-frontier-agent-17 (bypass review): find_spec() said yes to an
    installed ddgs whose lazily imported HTTP client could not load, so boot
    stayed quiet while every DuckDuckGo search failed."""
    import sys
    mod = _fake_ddgs_module()
    mod.DDGS = _BrokenLazyDDGS
    monkeypatch.setitem(sys.modules, "ddgs", mod)
    assert wt.ddgs_available() is False
    assert "primp" in wt.ddgs_import_error()

    monkeypatch.setitem(sys.modules, "ddgs", _fake_ddgs_module())
    assert wt.ddgs_import_error() is None


def test_startup_says_so_when_the_keyless_floor_is_missing(monkeypatch, caplog):
    """O06-frontier-agent-17: the import is lazy, so a venv without ddgs only
    surfaced as a failed search. Boot must say it, say why, and say how to fix it."""
    import asyncio
    import logging
    from Orchestrator import startup
    monkeypatch.setattr(wt, "ddgs_import_error",
                        lambda: "ModuleNotFoundError: import of primp halted")
    with caplog.at_level(logging.WARNING, logger="blackbox.startup"):
        asyncio.run(startup.startup_web_search_floor_check())
    assert "ddgs" in caplog.text
    assert "import of primp halted" in caplog.text
    assert "requirements.lock.txt" in caplog.text

    caplog.clear()
    monkeypatch.setattr(wt, "ddgs_import_error", lambda: None)
    with caplog.at_level(logging.WARNING, logger="blackbox.startup"):
        asyncio.run(startup.startup_web_search_floor_check())
    assert caplog.text == ""
