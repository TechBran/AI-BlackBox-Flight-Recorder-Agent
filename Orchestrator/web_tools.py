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
from typing import Dict, Optional, List, Any, Tuple
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

# Perplexity Sonar API configuration
# Handles both Orchestrator context (from Orchestrator.config) and MCP context (from config directly)
try:
    from Orchestrator.config import PERPLEXITY_API_KEY, PERPLEXITY_URL
except ImportError:
    try:
        from config import PERPLEXITY_API_KEY, PERPLEXITY_URL
    except ImportError:
        # Last-resort fallback if neither import path works (should never happen
        # in production; central config import is the authoritative source).
        PERPLEXITY_API_KEY = ""
        PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"

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


def perform_web_search(query: str, max_results: int = 5, use_cache: bool = True, search_recency_filter: str = "month") -> str:
    """
    Perform web search using Perplexity Sonar API. Falls back to DuckDuckGo if unavailable.

    Args:
        query: Search query string
        max_results: Maximum number of results (advisory — Perplexity returns a synthesized answer)
        use_cache: Whether to use cached results (default True)
        search_recency_filter: Filter by recency: 'day', 'week', 'month', 'year' (default 'month')

    Returns:
        Synthesized answer with citations, ready for LLM consumption
    """
    # Validate recency filter
    if search_recency_filter not in VALID_RECENCY_FILTERS:
        search_recency_filter = "month"

    # Check cache first
    cache_key = f"search:{query}:{search_recency_filter}"
    if use_cache:
        cached = _get_cache(cache_key)
        if cached:
            print(f"[WEB_SEARCH] Cache hit for query: {query}")
            return cached

    # Check rate limit
    if not _check_rate_limit():
        return "Rate limit exceeded. Please try again in a moment."

    # If Perplexity not available, go straight to fallback
    if not PERPLEXITY_AVAILABLE:
        return _fallback_ddg_search(query, max_results)

    try:
        print(f"[WEB_SEARCH] Searching Perplexity Sonar for: {query} (recency: {search_recency_filter})")

        headers = {
            "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "sonar",
            "messages": [{"role": "user", "content": query}],
            "search_recency_filter": search_recency_filter,
        }

        response = requests.post(
            PERPLEXITY_URL,
            headers=headers,
            json=payload,
            timeout=30
        )
        response.raise_for_status()

        data = response.json()
        answer = data["choices"][0]["message"]["content"]
        citations = data.get("citations", [])

        # Format output for LLM consumption
        formatted_result = f"Web Search Results:\n\n{answer}\n"
        if citations:
            formatted_result += "\nSources:\n"
            for i, cite in enumerate(citations, 1):
                formatted_result += f"  {i}. {cite}\n"
        formatted_result += f"\nSource: Perplexity Sonar | Query: \"{query}\" | Recency: {search_recency_filter}"

        # Cache the result
        _set_cache(cache_key, formatted_result, SEARCH_CACHE_TTL)

        print(f"[WEB_SEARCH] Perplexity returned {len(answer)} chars, {len(citations)} citations")
        return formatted_result

    except requests.HTTPError as e:
        status = e.response.status_code if e.response else "unknown"
        print(f"[WEB_SEARCH] Perplexity HTTP error {status}: {e}")
        if status == 429:
            return (
                "Search rate limited by Perplexity. The search service is temporarily throttling requests.\n"
                "Answer from your own knowledge if possible, or tell the user to try again in a minute."
            )
        # Fall back to DuckDuckGo on other HTTP errors
        print("[WEB_SEARCH] Falling back to DuckDuckGo due to Perplexity error")
        return _fallback_ddg_search(query, max_results)

    except requests.Timeout:
        print("[WEB_SEARCH] Perplexity request timed out, falling back to DuckDuckGo")
        return _fallback_ddg_search(query, max_results)

    except Exception as e:
        print(f"[WEB_SEARCH] Perplexity error ({type(e).__name__}): {e}")
        return _fallback_ddg_search(query, max_results)


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
