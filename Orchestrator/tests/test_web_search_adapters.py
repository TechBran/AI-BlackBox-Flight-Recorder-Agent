import types
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
