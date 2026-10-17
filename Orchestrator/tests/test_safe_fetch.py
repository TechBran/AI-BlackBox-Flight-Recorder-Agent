"""SSRF guard for model/user-supplied URLs (audit X03-security-backend-04,
O02-chat-routes-A-06, O20-mcp-server-01).

Every request through `tailscale serve`, and every fetch the box makes for a
model or an MCP caller, arrives at :9091 from 127.0.0.1, so a fetch that can
reach loopback / LAN / tailnet / link-local addresses is a proxy through the
perimeter. These tests pin the guard in Orchestrator/utils/safe_fetch.py:

- hostile literals and names are refused BEFORE any connection;
- DNS is resolved once, every address is vetted, and the connection is pinned
  to the vetted address (no second lookup a rebinding name could swap);
- redirects are never followed blindly: each hop is re-vetted, at most 3;
- response size and total time are capped.

Hermetic: DNS (`_resolve`), the box's interface list (`_local_addresses`) and
the transport (`_open_pinned`) are monkeypatched. The one real-socket test runs
a throwaway HTTP server on 127.0.0.1 and talks to it through the LOW-level
pinned connector only (vetting is exercised separately).
"""
from __future__ import annotations

import http.server
import threading

import pytest

from Orchestrator.utils import safe_fetch as sf


PUBLIC_IP = "93.184.215.14"        # a real public unicast address (never dialled)
PUBLIC_IP_2 = "93.184.215.15"


# --------------------------------------------------------------------- doubles

class FakeResponse:
    """The slice of urllib3's response the guard uses."""

    def __init__(self, status=200, headers=None, chunks=(b"<html>ok</html>",)):
        self.status = status
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.closed = False

    def stream(self, amt=None, decode_content=True):
        yield from self._chunks

    def release_conn(self):
        self.closed = True

    def close(self):
        self.closed = True


class FakeTransport:
    """Records every pinned open; replies from a per-call script."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, vetted, ip, headers, timeout):
        self.calls.append({"vetted": vetted, "ip": ip, "headers": dict(headers),
                           "timeout": timeout})
        nxt = self.responses.pop(0) if self.responses else FakeResponse()
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


@pytest.fixture
def dns(monkeypatch):
    """A fake resolver: dns.table[host] -> list of IPs (or a callable)."""
    class _DNS:
        table: dict = {}
        lookups: list = []

        def __call__(self, host, port):
            self.lookups.append(host)
            if host not in self.table:
                raise AssertionError(f"unexpected DNS lookup for {host!r}")
            v = self.table[host]
            return list(v() if callable(v) else v)

    d = _DNS()
    d.table = {}
    d.lookups = []
    monkeypatch.setattr(sf, "_resolve", d)
    return d


@pytest.fixture(autouse=True)
def no_local_addresses(monkeypatch):
    """The box's own interface addresses: none by default (tests opt in)."""
    monkeypatch.setattr(sf, "_local_addresses", lambda: frozenset())


@pytest.fixture
def transport(monkeypatch):
    def _install(*responses):
        t = FakeTransport(*responses)
        monkeypatch.setattr(sf, "_open_pinned", t)
        return t
    return _install


@pytest.fixture
def no_transport(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("a refused URL must never open a connection")
    monkeypatch.setattr(sf, "_open_pinned", _boom)


# ------------------------------------------------------------ refused literals

HOSTILE_LITERALS = [
    "http://127.0.0.1:9091/onboarding/config/ANTHROPIC_API_KEY?reveal=true",
    "http://127.1:9091/",
    "http://0x7f000001:9091/",
    "http://2130706433:9091/",
    "http://0177.0.0.1/",
    "http://0/",
    "http://0.0.0.0:9091/",
    "http://[::1]:9091/",
    "http://[::]/",
    "http://[::ffff:127.0.0.1]:9091/",
    "http://[::ffff:7f00:1]/",
    "http://[::127.0.0.1]/",                     # IPv4-compatible form
    "http://[64:ff9b::7f00:1]/",                 # NAT64 wrapping loopback
    "http://[2002:7f00:1::]/",                   # 6to4 wrapping loopback
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://[fe80::1]/",
    "http://10.0.0.5/",
    "http://172.16.0.1/",
    "http://192.168.1.153:8080/v1/models",       # LAN inference box
    "http://100.64.0.1/",                        # CGNAT / tailnet range
    "http://100.101.102.103:9091/",              # a tailnet peer
    "http://[fd7a:115c:a1e0::1]/",               # tailnet IPv6 (ULA)
    "http://[fc00::1]/",
    "http://224.0.0.1/",                         # multicast
    "http://[ff02::1]/",
    "http://255.255.255.255/",
    "http://240.0.0.1/",                         # reserved
    "http://localhost:9091/",
    "http://LOCALHOST/",
    "http://api.localhost/",
    "https://127.0.0.1/",
]


@pytest.mark.parametrize("url", HOSTILE_LITERALS)
def test_hostile_literals_are_refused_without_dns_or_connect(url, dns, no_transport):
    with pytest.raises(sf.UnsafeURLError):
        sf.safe_get(url)
    assert dns.lookups == [], "a literal/localhost host must not reach the resolver"


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com/",
    "javascript:alert(1)",
    "//example.com/",
    "example.com",
    "",
    "http://",
])
def test_non_http_schemes_and_malformed_urls_are_refused(url, dns, no_transport):
    with pytest.raises(sf.UnsafeURLError):
        sf.safe_get(url)


def test_userinfo_is_refused(dns, no_transport):
    """`http://public@127.0.0.1/` style confusion is refused outright."""
    with pytest.raises(sf.UnsafeURLError):
        sf.safe_get("http://user:pw@example.com/")


# --------------------------------------------------------------- refused names

def test_public_name_resolving_to_loopback_is_refused(dns, no_transport):
    dns.table["rebind.example"] = ["127.0.0.1"]
    with pytest.raises(sf.UnsafeURLError):
        sf.safe_get("http://rebind.example:9091/onboarding/config/X")


def test_name_with_any_private_record_is_refused(dns, no_transport):
    dns.table["mixed.example"] = [PUBLIC_IP, "10.0.0.7"]
    with pytest.raises(sf.UnsafeURLError):
        sf.safe_get("http://mixed.example/")


def test_name_resolving_to_tailnet_is_refused(dns, no_transport):
    dns.table["box.tailnet-name.ts.net"] = ["100.88.12.34"]
    with pytest.raises(sf.UnsafeURLError):
        sf.safe_get("https://box.tailnet-name.ts.net/onboarding/state")


def test_name_resolving_to_mapped_loopback_is_refused(dns, no_transport):
    dns.table["v6.example"] = ["::ffff:127.0.0.1"]
    with pytest.raises(sf.UnsafeURLError):
        sf.safe_get("http://v6.example/")


def test_the_box_own_public_interface_address_is_refused(dns, no_transport, monkeypatch):
    """A box with a public address on an interface must not fetch itself."""
    monkeypatch.setattr(sf, "_local_addresses",
                        lambda: frozenset({sf.ipaddress.ip_address(PUBLIC_IP)}))
    dns.table["me.example"] = [PUBLIC_IP]
    with pytest.raises(sf.UnsafeURLError):
        sf.safe_get("http://me.example:9091/")
    with pytest.raises(sf.UnsafeURLError):
        sf.safe_get(f"http://{PUBLIC_IP}:9091/")


def test_unresolvable_name_is_a_fetch_error(dns, no_transport, monkeypatch):
    def _nx(host, port):
        raise OSError("Name or service not known")
    monkeypatch.setattr(sf, "_resolve", _nx)
    with pytest.raises(sf.FetchError):
        sf.safe_get("http://does-not-exist.example/")


# ------------------------------------------------------------- allowed + pinned

def test_public_name_is_fetched_pinned_to_the_vetted_ip(dns, transport):
    dns.table["public.example"] = [PUBLIC_IP]
    t = transport(FakeResponse(200, {"Content-Type": "text/html"}, [b"<p>hi</p>"]))
    res = sf.safe_get("http://public.example/path?q=1", headers={"User-Agent": "UA"})
    assert res.status_code == 200
    assert res.content == b"<p>hi</p>"
    assert res.url == "http://public.example/path?q=1"
    assert len(t.calls) == 1
    call = t.calls[0]
    assert call["ip"] == PUBLIC_IP                      # connection pinned
    assert call["vetted"].host == "public.example"     # Host/SNI stay the name
    assert call["vetted"].target == "/path?q=1"
    assert call["headers"]["User-Agent"] == "UA"
    assert dns.lookups == ["public.example"]            # resolved exactly once


def test_public_ip_literal_is_allowed_without_dns(dns, transport):
    t = transport(FakeResponse(200))
    res = sf.safe_get(f"https://{PUBLIC_IP}/")
    assert res.status_code == 200
    assert t.calls[0]["ip"] == PUBLIC_IP
    assert dns.lookups == []


def test_public_ipv6_literal_is_allowed(dns, transport):
    t = transport(FakeResponse(200))
    sf.safe_get("http://[2606:4700:4700::1111]/")
    assert t.calls[0]["ip"] == "2606:4700:4700::1111"


def test_idn_host_is_idna_encoded_before_resolution(dns, transport):
    dns.table["xn--bcher-kva.example"] = [PUBLIC_IP]
    t = transport(FakeResponse(200))
    sf.safe_get("http://bücher.example/")
    assert t.calls[0]["vetted"].host == "xn--bcher-kva.example"


def test_connect_failure_falls_through_to_the_next_vetted_address(dns, transport):
    """IPv6-first answers on an IPv4-only box must not break fetching: the
    guard tries each VETTED address in order, like socket.create_connection."""
    dns.table["dual.example"] = ["2606:4700:4700::1111", PUBLIC_IP]
    t = transport(sf._ConnectFailed("no route to host"), FakeResponse(200))
    res = sf.safe_get("http://dual.example/")
    assert res.status_code == 200
    assert [c["ip"] for c in t.calls] == ["2606:4700:4700::1111", PUBLIC_IP]


def test_all_addresses_failing_to_connect_is_a_fetch_error(dns, transport):
    dns.table["down.example"] = [PUBLIC_IP]
    transport(sf._ConnectFailed("connection refused"))
    with pytest.raises(sf.FetchError):
        sf.safe_get("http://down.example/")


# ------------------------------------------------------------------- redirects

def test_redirect_from_public_host_to_localhost_is_refused(dns, transport):
    dns.table["evil.example"] = [PUBLIC_IP]
    t = transport(FakeResponse(302, {
        "Location": "http://localhost:9091/onboarding/config/ANTHROPIC_API_KEY?reveal=1"}))
    with pytest.raises(sf.UnsafeURLError):
        sf.safe_get("http://evil.example/start")
    assert len(t.calls) == 1, "the loopback hop must never be opened"


@pytest.mark.parametrize("location", [
    "http://127.0.0.1:9091/fossil/hybrid?q=x",
    "http://[::1]:9091/",
    "http://169.254.169.254/latest/meta-data/iam/",
    "http://0x7f000001/",
    "file:///etc/passwd",
])
def test_redirect_to_any_hostile_target_is_refused(dns, transport, location):
    dns.table["evil.example"] = [PUBLIC_IP]
    for status in (301, 302, 303, 307, 308):
        t = transport(FakeResponse(status, {"Location": location}))
        with pytest.raises(sf.UnsafeURLError):
            sf.safe_get("https://evil.example/")
        assert len(t.calls) == 1


def test_redirect_hop_is_re_resolved_and_re_vetted(dns, transport):
    """A name that was public on hop 1 and rebinds to loopback by hop 2 is
    refused — every hop is vetted afresh."""
    answers = iter([[PUBLIC_IP], ["127.0.0.1"]])
    dns.table["flip.example"] = lambda: next(answers)
    t = transport(FakeResponse(302, {"Location": "/again"}))
    with pytest.raises(sf.UnsafeURLError):
        sf.safe_get("http://flip.example/")
    assert len(t.calls) == 1
    assert dns.lookups == ["flip.example", "flip.example"]


def test_safe_relative_redirect_is_followed(dns, transport):
    dns.table["public.example"] = [PUBLIC_IP]
    dns.table["cdn.example"] = [PUBLIC_IP_2]
    t = transport(
        FakeResponse(301, {"Location": "/moved"}),
        FakeResponse(302, {"Location": "https://cdn.example/final"}),
        FakeResponse(200, {}, [b"done"]),
    )
    res = sf.safe_get("http://public.example/start")
    assert res.content == b"done"
    assert res.url == "https://cdn.example/final"
    assert [c["vetted"].target for c in t.calls] == ["/start", "/moved", "/final"]
    assert [c["ip"] for c in t.calls] == [PUBLIC_IP, PUBLIC_IP, PUBLIC_IP_2]


def test_at_most_three_redirects(dns, transport):
    dns.table["loop.example"] = [PUBLIC_IP]
    t = transport(*[FakeResponse(302, {"Location": "/loop"}) for _ in range(10)])
    with pytest.raises(sf.TooManyRedirects):
        sf.safe_get("http://loop.example/")
    assert len(t.calls) == 4          # the original request + 3 followed hops


# --------------------------------------------------------------- size and time

def test_declared_oversize_body_is_refused_before_reading(dns, transport):
    dns.table["big.example"] = [PUBLIC_IP]
    transport(FakeResponse(200, {"Content-Length": "5000"}, [b"x" * 10]))
    with pytest.raises(sf.ResponseTooLarge):
        sf.safe_get("http://big.example/", max_bytes=1000)


def test_streamed_oversize_body_is_cut_off(dns, transport):
    dns.table["big.example"] = [PUBLIC_IP]
    transport(FakeResponse(200, {}, [b"x" * 600, b"x" * 600, b"x" * 600]))
    with pytest.raises(sf.ResponseTooLarge):
        sf.safe_get("http://big.example/", max_bytes=1000)


def test_total_time_is_capped_across_a_slow_body(dns, transport, monkeypatch):
    """A trickling body cannot hold the fetch open past the total deadline."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(sf, "_monotonic", lambda: clock["t"])

    class Slow(FakeResponse):
        def stream(self, amt=None, decode_content=True):
            for _ in range(100):
                clock["t"] += 2.0          # each chunk "takes" 2 seconds
                yield b"x"

    dns.table["slow.example"] = [PUBLIC_IP]
    transport(Slow(200))
    with pytest.raises(sf.FetchTimeout):
        sf.safe_get("http://slow.example/", timeout=5)


# ----------------------------------------------------- address classification

@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True),
    (PUBLIC_IP, True),
    ("2606:4700:4700::1111", True),
    ("127.0.0.1", False),
    ("10.1.2.3", False),
    ("100.64.0.1", False),
    ("100.127.255.254", False),
    ("169.254.169.254", False),
    ("192.0.2.1", False),
    ("198.18.0.1", False),
    ("::1", False),
    ("::ffff:8.8.8.8", False),         # mapped forms are refused outright
    ("::8.8.8.8", False),              # IPv4-compatible (deprecated)
    ("64:ff9b::808:808", True),        # NAT64 of a public IPv4
    ("64:ff9b::a00:1", False),         # NAT64 of 10.0.0.1
    ("fec0::1", False),                # deprecated site-local
    ("ff0e::1", False),                # global-scope multicast
    ("2001:db8::1", False),            # documentation
])
def test_is_public_address(ip, public):
    assert sf.is_public_address(sf.ipaddress.ip_address(ip)) is public


# ------------------------------------------- the real pinned connector (local)

class _Recorder(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):  # noqa: N802 — http.server API
        _Recorder.seen.append({"path": self.path, "host": self.headers.get("Host")})
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"<title>pinned</title>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def local_server():
    _Recorder.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def test_pinned_connector_dials_the_ip_and_keeps_the_host_header(local_server):
    """The low-level connector connects to the given IP but sends the original
    Host — the property that makes pinning safe for virtual hosts and TLS SNI."""
    port = local_server
    vetted = sf.VettedURL(url=f"http://public.example:{port}/x?y=1", scheme="http",
                          host="public.example", port=port, target="/x?y=1",
                          addresses=("127.0.0.1",))
    resp = sf._open_pinned(vetted, "127.0.0.1", {"User-Agent": "t"}, 5.0)
    try:
        assert resp.status == 200
        assert b"".join(resp.stream(1024)) == b"<title>pinned</title>"
    finally:
        resp.release_conn()
    assert _Recorder.seen == [{"path": "/x?y=1", "host": f"public.example:{port}"}]


def test_pinned_connector_never_follows_redirects_itself(local_server):
    port = local_server
    vetted = sf.VettedURL(url=f"http://public.example:{port}/redirect", scheme="http",
                          host="public.example", port=port, target="/redirect",
                          addresses=("127.0.0.1",))
    resp = sf._open_pinned(vetted, "127.0.0.1", {}, 5.0)
    try:
        assert resp.status == 302
    finally:
        resp.release_conn()
    assert [s["path"] for s in _Recorder.seen] == ["/redirect"]


def test_a_rebinding_name_cannot_reach_a_live_loopback_server(local_server, dns):
    """End to end with a REAL listener on 127.0.0.1: the guard refuses before
    the transport is ever asked to connect."""
    port = local_server
    dns.table["rebind.example"] = ["127.0.0.1"]
    with pytest.raises(sf.UnsafeURLError):
        sf.safe_get(f"http://rebind.example:{port}/x")
    assert _Recorder.seen == []
