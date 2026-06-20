from Orchestrator.toolvault import registry

NEW = ["perplexity_web_search","openai_web_search","gemini_web_search",
       "grok_web_search","grok_x_search","duckduckgo_web_search"]

def test_six_web_search_tools_load():
    names = {t["name"] for t in registry.load_canonical()}
    for n in NEW:
        assert n in names, f"missing {n}"
    assert "web_search" not in names  # old generic tool removed

def test_grok_x_search_has_no_recency_param():
    entry = next(t for t in registry.load_canonical() if t["name"] == "grok_x_search")
    props = entry["parameters"]["properties"]
    assert "search_recency_filter" not in props
    assert "query" in props

def test_web_search_tools_carry_availability_gate():
    by_name = {t["name"]: t for t in registry.load_canonical()}
    assert by_name["grok_web_search"]["x-availability"]["requires_env"] == ["XAI_API_KEY"]
    assert by_name["duckduckgo_web_search"]["x-availability"]["requires_env"] == []
