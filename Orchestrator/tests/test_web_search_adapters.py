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
