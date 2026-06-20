#!/usr/bin/env python3
"""
web_tools.py - Web search and fetch utilities for all AI models

Provides two core capabilities:
1. perform_web_search() - Search the web using Perplexity Sonar API (with DuckDuckGo fallback)
2. perform_web_fetch() - Fetch and extract content from specific URLs

Both functions include:
- Caching (15-minute TTL for search, 30-minute for fetch)
- Rate limiting (to prevent abuse)
- Error handling with graceful fallbacks
- Clean, LLM-friendly output formatting
"""

import re
import sys
import time
import requests
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Any, Tuple
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

# Provider API configuration
# Handles both Orchestrator context (from Orchestrator.config) and MCP context (from config directly)
try:
    from Orchestrator.config import (PERPLEXITY_API_KEY, PERPLEXITY_URL,
        OPENAI_API_KEY, XAI_API_KEY, GEMINI_API_KEY)
except ImportError:
    try:
        from config import (PERPLEXITY_API_KEY, PERPLEXITY_URL,
            OPENAI_API_KEY, XAI_API_KEY, GEMINI_API_KEY)
    except ImportError:
        # Last-resort fallback if neither import path works (should never happen
        # in production; central config import is the authoritative source).
        PERPLEXITY_API_KEY = ""
        PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
        OPENAI_API_KEY = ""
        XAI_API_KEY = ""
        GEMINI_API_KEY = ""

# Provider endpoints / default search models. These are *choices*, not provider
# facts; the working shapes are proven by diagnostics/websearch_spike.py.
OPENAI_RESPONSES_URL_BASE = "https://api.openai.com"
XAI_RESPONSES_URL_BASE = "https://api.x.ai"
OPENAI_SEARCH_MODEL = "gpt-4.1"
XAI_SEARCH_MODEL = "grok-4.3"
GEMINI_SEARCH_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]

PERPLEXITY_AVAILABLE = bool(PERPLEXITY_API_KEY)
if PERPLEXITY_AVAILABLE:
    print(f"[WEB_TOOLS] Perplexity Sonar API configured (key: ...{PERPLEXITY_API_KEY[-4:]})", file=sys.stderr)
else:
    print("[WEB_TOOLS] Warning: PERPLEXITY_API_KEY not set, will fall back to DuckDuckGo", file=sys.stderr)

# =============================================================================
# Configuration
# =============================================================================

# Rate limiting
RATE_LIMIT_REQUESTS_PER_MINUTE = 30
RATE_LIMIT_WINDOW = 60  # seconds

# Caching
SEARCH_CACHE_TTL = 900  # 15 minutes
FETCH_CACHE_TTL = 1800  # 30 minutes
MAX_CACHE_SIZE = 1000

# Fetch settings
FETCH_TIMEOUT = 25  # seconds (allows time for larger pages)
MAX_FETCH_SIZE = 1000000  # 1MB raw HTML download limit
MAX_CONTENT_CHARS = 80000  # Return max 80K chars (models have 128K+ token context windows)

# =============================================================================
# Cache Implementation (Simple in-memory cache)
# =============================================================================

_cache: Dict[str, Tuple[Any, float]] = {}  # key -> (value, expiry_timestamp)
_request_timestamps: List[float] = []  # For rate limiting


def _get_cache(key: str) -> Optional[str]:
    """Get value from cache if not expired."""
    if key in _cache:
        value, expiry = _cache[key]
        if time.time() < expiry:
            return value
        else:
            del _cache[key]  # Expired, remove
    return None


def _set_cache(key: str, value: str, ttl_seconds: int):
    """Set cache value with TTL."""
    global _cache

    # Evict oldest entries if cache too large (simple LRU)
    if len(_cache) >= MAX_CACHE_SIZE:
        # Remove 10% of oldest entries
        sorted_items = sorted(_cache.items(), key=lambda x: x[1][1])
        for i in range(MAX_CACHE_SIZE // 10):
            del _cache[sorted_items[i][0]]

    expiry = time.time() + ttl_seconds
    _cache[key] = (value, expiry)


def _check_rate_limit() -> bool:
    """Check if we're within rate limit. Returns True if OK to proceed."""
    global _request_timestamps

    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW

    # Remove timestamps outside window
    _request_timestamps = [ts for ts in _request_timestamps if ts > window_start]

    # Check if under limit
    if len(_request_timestamps) >= RATE_LIMIT_REQUESTS_PER_MINUTE:
        return False

    # Add current request
    _request_timestamps.append(now)
    return True


# =============================================================================
# Web Search Function (Perplexity Sonar API with DuckDuckGo fallback)
# =============================================================================

VALID_RECENCY_FILTERS = {"hour", "day", "week", "month", "year"}


# =============================================================================
# Provider-adapter layer
# =============================================================================
#
# Each web-search provider is an *adapter* taking (query, recency) and returning
# a normalized SearchResult. A single dispatcher, perform_provider_search(),
# handles caching / rate-limiting / output formatting so every provider shares
# the same human/LLM output-string contract.


@dataclass
class SearchResult:
    """Normalized result returned by every provider adapter."""
    answer: str = ""
    citations: list = field(default_factory=list)
    source_label: str = ""
    ok: bool = True
    error: str = ""


def _format_search_result(r: "SearchResult", query: str) -> str:
    """Render a SearchResult into the standard LLM-facing output string.

    Reproduces the existing perform_web_search() contract: a "Web Search
    Results:" block, an optional numbered "Sources:" list, and a trailing
    "Source: <label> | Query: ..." provenance line.
    """
    if not r.ok:
        label = r.source_label or "web search"
        return (
            f"Search failed via {label} for: \"{query}\" (error: {r.error})\n"
            f"Answer from your own knowledge instead."
        )

    formatted_result = f"Web Search Results:\n\n{r.answer}\n"
    if r.citations:
        formatted_result += "\nSources:\n"
        for i, cite in enumerate(r.citations, 1):
            formatted_result += f"  {i}. {cite}\n"
    formatted_result += f"\nSource: {r.source_label} | Query: \"{query}\""
    return formatted_result


def _responses_search(base_url: str, api_key: str, model: str, tool_type: str, query: str) -> "SearchResult":
    """OpenAI-Responses-shaped search (used by both OpenAI and xAI/Grok).

    POSTs to {base_url}/v1/responses with a single web-search/x-search tool.
    Shape proven by diagnostics/websearch_spike.py.
    """
    if "x_search" in tool_type:
        label = "Grok (X/Twitter)"
    elif "x.ai" in base_url:
        label = "Grok"
    else:
        label = "OpenAI"

    if not api_key:
        return SearchResult(ok=False, error="API key not configured", source_label=label)

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "input": [{"role": "user", "content": query}],
        "tools": [{"type": tool_type}],
    }
    try:
        resp = requests.post(f"{base_url}/v1/responses", headers=headers, json=payload, timeout=90)
    except Exception as e:
        return SearchResult(ok=False, error=f"{type(e).__name__}: {e}", source_label=label)

    if resp.status_code >= 400:
        return SearchResult(ok=False, error=f"HTTP {resp.status_code}", source_label=label)

    try:
        d = resp.json()
    except Exception as e:
        return SearchResult(ok=False, error=f"bad JSON: {e}", source_label=label)

    text = ""
    top = d.get("output_text")
    if isinstance(top, str):
        text += top
    for item in d.get("output", []) or []:
        if item.get("type") == "message":
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text"):
                    text += c.get("text", "")
    citations = list(d.get("citations") or [])
    return SearchResult(answer=text, citations=citations, source_label=label)


def _search_perplexity(query: str, recency: str) -> "SearchResult":
    """Perplexity Sonar adapter (the historical default engine)."""
    label = "Perplexity Sonar"
    if not PERPLEXITY_API_KEY:
        return SearchResult(ok=False, error="API key not configured", source_label=label)

    headers = {"Authorization": f"Bearer {PERPLEXITY_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "sonar",
        "messages": [{"role": "user", "content": query}],
        "search_recency_filter": recency,
    }
    try:
        resp = requests.post(PERPLEXITY_URL, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        d = resp.json()
        answer = d["choices"][0]["message"]["content"]
        citations = list(d.get("citations", []) or [])
        return SearchResult(answer=answer, citations=citations, source_label=label)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        return SearchResult(ok=False, error=f"HTTP {status}", source_label=label)
    except Exception as e:
        return SearchResult(ok=False, error=f"{type(e).__name__}: {e}", source_label=label)


def _search_openai(query: str, recency: str) -> "SearchResult":
    return _responses_search(OPENAI_RESPONSES_URL_BASE, OPENAI_API_KEY, OPENAI_SEARCH_MODEL, "web_search", query)


def _search_grok_web(query: str, recency: str) -> "SearchResult":
    return _responses_search(XAI_RESPONSES_URL_BASE, XAI_API_KEY, XAI_SEARCH_MODEL, "web_search", query)


def _search_grok_x(query: str, recency: str) -> "SearchResult":
    return _responses_search(XAI_RESPONSES_URL_BASE, XAI_API_KEY, XAI_SEARCH_MODEL, "x_search", query)


def _search_gemini(query: str, recency: str) -> "SearchResult":
    """Google Gemini adapter using google_search grounding.

    Tries each model in GEMINI_SEARCH_MODELS until one returns < 400.
    Shape proven by diagnostics/websearch_spike.py.
    """
    label = "Google Gemini"
    if not GEMINI_API_KEY:
        return SearchResult(ok=False, error="API key not configured", source_label=label)

    body = {"contents": [{"parts": [{"text": query}]}], "tools": [{"google_search": {}}]}
    last_err = "all models failed"
    for model in GEMINI_SEARCH_MODELS:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={GEMINI_API_KEY}")
        try:
            resp = requests.post(url, headers={"Content-Type": "application/json"}, json=body, timeout=90)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
        if resp.status_code >= 400:
            last_err = f"HTTP {resp.status_code}"
            continue
        try:
            d = resp.json()
        except Exception as e:
            last_err = f"bad JSON: {e}"
            continue
        cand = (d.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts", []) or []
        answer = "".join(p.get("text", "") for p in parts)
        gm = cand.get("groundingMetadata") or {}
        chunks = gm.get("groundingChunks") or []
        citations = []
        for ch in chunks:
            uri = (ch.get("web") or {}).get("uri")
            if uri:
                citations.append(uri)
        return SearchResult(answer=answer, citations=citations, source_label=label)

    return SearchResult(ok=False, error=last_err, source_label=label)


def _search_duckduckgo(query: str, recency: str) -> "SearchResult":
    """DuckDuckGo adapter (keyless fallback engine)."""
    label = "DuckDuckGo"
    try:
        from ddgs import DDGS
    except ImportError:
        return SearchResult(ok=False, error="ddgs library not installed", source_label=label)
    try:
        results = DDGS().text(query, max_results=5)
    except Exception as e:
        return SearchResult(ok=False, error=f"{type(e).__name__}: {e}", source_label=label)

    if not results:
        return SearchResult(ok=False, error="no results", source_label=label)

    lines, citations = [], []
    for i, result in enumerate(results, 1):
        title = result.get("title", "No title")
        url = result.get("href", result.get("link", "No URL"))
        snippet = result.get("body", result.get("snippet", ""))
        lines.append(f"{i}. **{title}**\n   URL: {url}\n   {snippet}")
        if url and url != "No URL":
            citations.append(url)
    answer = "\n\n".join(lines)
    return SearchResult(answer=answer, citations=citations, source_label=label)


PROVIDER_SEARCHERS = {
    "perplexity": _search_perplexity,
    "openai": _search_openai,
    "gemini": _search_gemini,
    "grok": _search_grok_web,
    "grok_x": _search_grok_x,
    "duckduckgo": _search_duckduckgo,
}


def perform_provider_search(provider: str, query: str,
                            search_recency_filter: str = "month",
                            use_cache: bool = True) -> str:
    """Dispatch a web search to a named provider adapter and format the result.

    Shares caching, rate-limiting, and the output-string contract across every
    provider. Returns an LLM-ready string (never raises for provider errors).
    """
    if not query:
        return "Search query is required."
    if search_recency_filter not in VALID_RECENCY_FILTERS:
        search_recency_filter = "month"

    fn = PROVIDER_SEARCHERS.get(provider)
    if fn is None:
        return f"Unknown web-search provider: {provider!r}"

    cache_key = f"search:{provider}:{query}:{search_recency_filter}"
    if use_cache:
        hit = _get_cache(cache_key)
        if hit:
            print(f"[WEB_SEARCH] Cache hit for {provider}: {query}")
            return hit

    if not _check_rate_limit():
        return "Rate limit exceeded. Please try again in a moment."

    print(f"[WEB_SEARCH] Searching {provider} for: {query} (recency: {search_recency_filter})")
    r = fn(query, search_recency_filter)
    out = _format_search_result(r, query)
    if r.ok and use_cache:
        _set_cache(cache_key, out, SEARCH_CACHE_TTL)
    return out


def perform_web_search(query: str, max_results: int = 5, use_cache: bool = True, search_recency_filter: str = "month") -> str:
    """Legacy web-search entry point (Perplexity Sonar).

    Thin compatibility wrapper over perform_provider_search(). Preserved so the
    existing callers keep working; a later task migrates them to the per-provider
    tools and removes this alias. The ``max_results`` argument is advisory and
    retained only for signature compatibility.
    """
    return perform_provider_search(
        "perplexity",
        query,
        search_recency_filter=search_recency_filter,
        use_cache=use_cache,
    )


def _fallback_ddg_search(query: str, max_results: int = 5) -> str:
    """Fallback to DuckDuckGo if Perplexity is unavailable or erroring."""
    try:
        from ddgs import DDGS
    except ImportError:
        return (
            f"Web search unavailable: Perplexity API key not configured and DuckDuckGo library not installed.\n"
            f"Set PERPLEXITY_API_KEY in .env or install ddgs: pip install ddgs"
        )

    try:
        print(f"[WEB_SEARCH] Fallback: searching DuckDuckGo for: {query}")
        search_results = DDGS().text(query, max_results=max_results)

        if not search_results:
            return (
                f"No results found for: \"{query}\"\n"
                f"Try rephrasing with different keywords or simpler terms."
            )

        results = []
        for i, result in enumerate(search_results):
            title = result.get('title', 'No title')
            url = result.get('href', result.get('link', 'No URL'))
            snippet = result.get('body', result.get('snippet', ''))
            results.append(f"{i+1}. **{title}**\n   URL: {url}\n   {snippet}\n")

        formatted_result = "Web Search Results (fallback):\n\n" + "\n".join(results)
        formatted_result += f"\nSource: DuckDuckGo (fallback) | Query: \"{query}\" | Results: {len(results)}/{max_results}"
        return formatted_result

    except Exception as e:
        print(f"[WEB_SEARCH] DuckDuckGo fallback also failed: {e}")
        return (
            f"Search failed for: \"{query}\" (error: {e})\n"
            f"Answer from your own knowledge instead."
        )


# =============================================================================
# Web Fetch Function (URL content extraction)
# =============================================================================

def perform_web_fetch(url: str, max_chars: int = MAX_CONTENT_CHARS, use_cache: bool = True) -> str:
    """
    Fetch and extract clean content from a web URL.

    Args:
        url: URL to fetch
        max_chars: Maximum characters to return (default 80000)
        use_cache: Whether to use cached results (default True)

    Returns:
        Formatted content with title, URL, and cleaned text
    """
    # Validate URL
    if not url.startswith(('http://', 'https://')):
        return f"❌ Invalid URL: {url} (must start with http:// or https://)"

    # Check cache first
    cache_key = f"fetch:{url}:{max_chars}"
    if use_cache:
        cached = _get_cache(cache_key)
        if cached:
            print(f"[WEB_FETCH] Cache hit for URL: {url}")
            return cached

    # Check rate limit
    if not _check_rate_limit():
        return "⚠️ Rate limit exceeded. Please try again in a moment."

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
        }

        print(f"[WEB_FETCH] Fetching URL: {url}")
        response = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT, stream=True)

        if response.status_code != 200:
            return (
                f"⚠️ Failed to fetch URL (HTTP {response.status_code}): {url}\n\n"
                f"IMPORTANT: Do NOT retry the same URL. Instead:\n"
                f"1. If you have search results, try a different URL from the results\n"
                f"2. If the page requires authentication, inform the user\n"
                f"3. Try searching for the same information using web_search instead"
            )

        # Check content size
        content_length = response.headers.get('content-length')
        if content_length and int(content_length) > MAX_FETCH_SIZE:
            return (
                f"⚠️ Page too large to fetch ({int(content_length)} bytes): {url}\n\n"
                f"The page exceeds the download limit. Try:\n"
                f"1. Search for the specific information you need using web_search instead\n"
                f"2. Try a different, more focused page on the same topic"
            )

        # Read content with size limit
        content = b''
        for chunk in response.iter_content(chunk_size=8192):
            content += chunk
            if len(content) > MAX_FETCH_SIZE:
                return (
                    f"⚠️ Page too large to fetch (exceeded {MAX_FETCH_SIZE} bytes): {url}\n\n"
                    f"The page exceeds the download limit. Try:\n"
                    f"1. Search for the specific information you need using web_search instead\n"
                    f"2. Try a different, more focused page on the same topic"
                )

        html = content.decode('utf-8', errors='ignore')

        # Parse HTML with BeautifulSoup
        soup = BeautifulSoup(html, 'lxml')

        # Extract title
        title = soup.title.string if soup.title else "No title"
        title = title.strip()

        # Remove script, style, and other non-content elements
        for element in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe', 'noscript']):
            element.decompose()

        # Try to find main content area (common patterns)
        main_content = None
        for selector in ['main', 'article', '[role="main"]', '.main-content', '#main-content', '.article-content', '.post-content']:
            main_content = soup.select_one(selector)
            if main_content:
                break

        # If no main content found, use body
        if not main_content:
            main_content = soup.body if soup.body else soup

        # Extract text
        text = main_content.get_text(separator='\n', strip=True)

        # Clean up excessive whitespace
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        clean_text = '\n'.join(lines)

        # Truncate if too long
        if len(clean_text) > max_chars:
            clean_text = clean_text[:max_chars] + f"\n\n... (content truncated: showing {max_chars} of {len(clean_text)} total characters. To see more, call web_fetch again with a higher max_chars value.)"

        # Format result
        formatted_result = f"""📄 **Fetched Content**

**Title:** {title}
**URL:** {url}
**Length:** {len(clean_text)} characters

---

{clean_text}

---
✅ Successfully fetched and parsed content from {url}
"""

        # Cache the result
        _set_cache(cache_key, formatted_result, FETCH_CACHE_TTL)

        print(f"[WEB_FETCH] Successfully fetched {len(clean_text)} chars from {url}")
        return formatted_result

    except requests.Timeout:
        return (
            f"⚠️ Timeout fetching URL (>{FETCH_TIMEOUT}s): {url}\n\n"
            f"IMPORTANT: Do NOT retry the same URL. The page is too slow to respond. Instead:\n"
            f"1. Try a different URL from your search results\n"
            f"2. Use web_search to find the information from a different source"
        )
    except requests.RequestException as e:
        return (
            f"⚠️ Network error fetching URL: {url}\nError: {str(e)}\n\n"
            f"IMPORTANT: Do NOT retry the same URL. Instead:\n"
            f"1. Try a different URL from your search results\n"
            f"2. Use web_search to find alternative sources"
        )
    except Exception as e:
        print(f"[WEB_FETCH] Error: {str(e)}")
        return (
            f"⚠️ Error parsing content from {url}: {str(e)}\n\n"
            f"IMPORTANT: Do NOT retry the same URL. Instead:\n"
            f"1. Try a different URL from your search results\n"
            f"2. The page may have unusual formatting — try a different source"
        )


# =============================================================================
# Utility Functions
# =============================================================================

def clear_cache():
    """Clear all cached results."""
    global _cache
    _cache = {}
    print("[WEB_TOOLS] Cache cleared")


def get_cache_stats() -> dict:
    """Get cache statistics."""
    now = time.time()
    active_entries = sum(1 for _, (_, expiry) in _cache.items() if expiry > now)

    return {
        "total_entries": len(_cache),
        "active_entries": active_entries,
        "expired_entries": len(_cache) - active_entries,
        "max_size": MAX_CACHE_SIZE
    }


# =============================================================================
# Module Test (when run directly)
# =============================================================================

if __name__ == "__main__":
    print("=== Web Tools Module Test ===\n")
    print(f"Perplexity available: {PERPLEXITY_AVAILABLE}")
    print()

    # Test web search (Perplexity Sonar)
    print("Test 1: Web Search (Perplexity Sonar)")
    result = perform_web_search("latest AI news February 2026", max_results=3)
    print(result)
    print("\n" + "="*80 + "\n")

    # Test recency filter
    print("Test 2: Web Search with recency filter (day)")
    result = perform_web_search("breaking news today", search_recency_filter="day")
    print(result)
    print("\n" + "="*80 + "\n")

    # Test web fetch (unchanged)
    print("Test 3: Web Fetch")
    result = perform_web_fetch("https://example.com", max_chars=500)
    print(result)
    print("\n" + "="*80 + "\n")

    # Test cache
    print("Test 4: Cache (should hit cache on second call)")
    result1 = perform_web_search("test query")
    result2 = perform_web_search("test query")
    print(f"Cache stats: {get_cache_stats()}")
