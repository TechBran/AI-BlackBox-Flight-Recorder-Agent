"""perform_web_fetch goes through the SSRF guard (audit X03-security-backend-04,
O02-chat-routes-A-06, O20-mcp-server-01) and survives a missing lxml
(X01-deps-python-09 / O20-mcp-server-19).

perform_web_fetch is the one choke point behind every web_fetch surface: the
chat tool loops, the live-voice routes, driver_anthropic, the phone bridge, the
ToolVault executor and the MCP server. A prompt-injected model or a remote MCP
caller must not be able to read http://127.0.0.1:9091/... through it.

Hermetic: requests.get is booby-trapped (the old unguarded path must never be
used), DNS and the pinned transport are monkeypatched.
"""
from __future__ import annotations

import pytest

import Orchestrator.web_tools as wt
from Orchestrator.utils import safe_fetch as sf

PUBLIC_IP = "93.184.215.14"

PAGE = (b"<html><head><title>Example Domain</title></head>"
        b"<body><main><h1>Example</h1><p>Public page body.</p></main></body></html>")


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=(PAGE,)):
        self.status = status
        self.headers = dict(headers or {})
        self._chunks = list(chunks)

    def stream(self, amt=None, decode_content=True):
        yield from self._chunks

    def release_conn(self):
        pass


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    """No cache, no rate-limit carry-over, no real network on ANY path."""
    wt.clear_cache()
    monkeypatch.setattr(wt, "_request_timestamps", [])

    def _no_requests(*a, **k):
        raise AssertionError("perform_web_fetch must not use bare requests.get")
    monkeypatch.setattr(wt.requests, "get", _no_requests)
    monkeypatch.setattr(sf, "_local_addresses", lambda: frozenset())
    calls = []

    def _no_transport(vetted, ip, headers, timeout):
        calls.append((vetted.url, ip))
        raise AssertionError(f"unexpected connection to {ip} for {vetted.url}")
    monkeypatch.setattr(sf, "_open_pinned", _no_transport)

    def _no_dns(host, port):
        raise AssertionError(f"unexpected DNS lookup for {host!r}")
    monkeypatch.setattr(sf, "_resolve", _no_dns)
    yield calls


def _install(monkeypatch, dns: dict, *responses):
    script = list(responses)
    seen = []

    def _resolve(host, port):
        return list(dns[host])

    def _open(vetted, ip, headers, timeout):
        seen.append((vetted.url, ip, dict(headers)))
        return script.pop(0)

    monkeypatch.setattr(sf, "_resolve", _resolve)
    monkeypatch.setattr(sf, "_open_pinned", _open)
    return seen


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9091/onboarding/config/ANTHROPIC_API_KEY?reveal=true",
    "http://127.1:9091/onboarding/config/OPENAI_API_KEY?reveal=1",
    "http://0x7f000001:9091/fossil/hybrid?q=x&operator=system",
    "http://2130706433:9091/operators",
    "http://[::1]:9091/onboarding/custom-servers",
    "http://[::ffff:127.0.0.1]:9091/timeline",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/",
    "http://192.168.1.153:9098/v1/models",
    "http://100.64.0.1/",
    "http://100.101.102.103:9093/internal/reload",
    "http://localhost:9093/internal/reload",
])
def test_web_fetch_refuses_internal_targets(url):
    out = wt.perform_web_fetch(url, use_cache=False)
    assert "Refused" in out, out
    assert "Do NOT retry" in out


def test_web_fetch_refuses_public_name_resolving_to_loopback(monkeypatch):
    seen = _install(monkeypatch, {"rebind.example": ["127.0.0.1"]})
    out = wt.perform_web_fetch("http://rebind.example:9091/onboarding/state",
                               use_cache=False)
    assert "Refused" in out
    assert seen == []


def test_web_fetch_refuses_redirect_to_localhost(monkeypatch):
    seen = _install(
        monkeypatch, {"evil.example": [PUBLIC_IP]},
        FakeResponse(302, {"Location": "http://localhost:9091/onboarding/config/"
                                       "ANTHROPIC_API_KEY?reveal=true"}),
    )
    out = wt.perform_web_fetch("https://evil.example/innocent", use_cache=False)
    assert "Refused" in out
    assert [ip for _, ip, _ in seen] == [PUBLIC_IP]   # only the public hop opened


def test_web_fetch_refusal_is_not_cached(monkeypatch):
    wt.perform_web_fetch("http://127.0.0.1/", use_cache=True)
    assert not any(k.startswith("fetch:") for k in wt._cache)


def test_web_fetch_public_page_is_fetched_and_parsed(monkeypatch):
    seen = _install(monkeypatch, {"example.com": [PUBLIC_IP]},
                    FakeResponse(200, {"Content-Type": "text/html"}))
    out = wt.perform_web_fetch("https://example.com/", use_cache=False)
    assert "Example Domain" in out
    assert "Public page body." in out
    assert "Successfully fetched" in out
    url, ip, headers = seen[0]
    assert ip == PUBLIC_IP
    assert "Mozilla" in headers["User-Agent"]


def test_web_fetch_non_200_keeps_the_do_not_retry_guidance(monkeypatch):
    _install(monkeypatch, {"example.com": [PUBLIC_IP]}, FakeResponse(404, {}, [b"nope"]))
    out = wt.perform_web_fetch("https://example.com/missing", use_cache=False)
    assert "HTTP 404" in out and "Do NOT retry" in out


def test_web_fetch_oversize_page_reports_too_large(monkeypatch):
    _install(monkeypatch, {"example.com": [PUBLIC_IP]},
             FakeResponse(200, {"Content-Length": str(wt.MAX_FETCH_SIZE + 1)}))
    out = wt.perform_web_fetch("https://example.com/huge", use_cache=False)
    assert "too large" in out.lower()


def test_web_fetch_falls_back_to_html_parser_without_lxml(monkeypatch):
    """A fresh MCP venv has no lxml: bs4 raises FeatureNotFound for 'lxml'.
    web_fetch must fall back to the stdlib html.parser, not fail every call."""
    from bs4 import BeautifulSoup as RealSoup, FeatureNotFound
    features_seen = []

    def _soup(markup, features=None, *a, **k):
        features_seen.append(features)
        if features == "lxml":
            raise FeatureNotFound("Couldn't find a tree builder with the features you "
                                  "requested: lxml.")
        return RealSoup(markup, features, *a, **k)

    monkeypatch.setattr(wt, "BeautifulSoup", _soup)
    _install(monkeypatch, {"example.com": [PUBLIC_IP]}, FakeResponse(200))
    out = wt.perform_web_fetch("https://example.com/", use_cache=False)
    assert "Example Domain" in out and "Public page body." in out
    assert features_seen == ["lxml", "html.parser"]
