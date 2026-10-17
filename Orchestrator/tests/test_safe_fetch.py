"""SSRF guard for model/user-supplied URLs (audit X03-security-backend-04,
O02-chat-routes-A-06, O20-mcp-server-01).

Every request through `tailscale serve`, and every fetch the box makes for a
model or an MCP caller, arrives at :9091 from 127.0.0.1, so a fetch that can
reach loopback / LAN / tailnet / link-local addresses is a proxy through the
perimeter. These tests pin the guard in Orchestrator/utils/safe_fetch.py:

- hostile literals and names are refused BEFORE any connection;
- DNS is resolved once, every address is vetted, and the connection is pinned
  to the vetted address (no second lookup a rebinding name could swap);
- redirects are never followed blindly: each hop is re-vetted, at most 3,
  carrying the cookies a hop set (like requests did) and repairing raw UTF-8
  Location headers;
- response size and total time are capped. The time cap is enforced at the
  SOCKET (a watchdog shuts the connection down at the deadline), so a server
  that trickles its TLS handshake, headers or body cannot hold a fetch open;
- stacked Content-Encodings (a decompression bomb) are refused.

Hermetic: DNS (`_resolve`), the box's interface list (`_local_addresses`) and
the transport (`_open_pinned`) are monkeypatched. The real-socket tests run
throwaway servers on 127.0.0.1: either through the LOW-level pinned connector,
or end to end with `is_public_address` patched so the loopback server can play
a public host (vetting itself is exercised separately).
"""
from __future__ import annotations

import http.client
import http.server
import io
import os
import socket
import threading
import time

import pytest

from Orchestrator.utils import safe_fetch as sf


PUBLIC_IP = "93.184.215.14"        # a real public unicast address (never dialled)
PUBLIC_IP_2 = "93.184.215.15"


# --------------------------------------------------------------------- doubles

class FakeResponse:
    """The slice of urllib3's response the guard uses."""

    def __init__(self, status=200, headers=None, chunks=(b"<html>ok</html>",),
                 set_cookie=None):
        self.status = status
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.closed = False
        if set_cookie:
            # urllib3 keeps the http.client response here; cookies are read
            # from its .msg, exactly as requests' cookie extraction does.
            raw = "".join(f"Set-Cookie: {c}\r\n" for c in set_cookie) + "\r\n"
            msg = http.client.parse_headers(io.BytesIO(raw.encode("latin-1")))
            self._original_response = type("Orig", (), {"msg": msg})()

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

    def __call__(self, vetted, ip, headers, timeout, watchdog=None):
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


# ------------------------------------------------- redirect cookies + Location

def test_a_cookie_set_on_a_redirect_is_sent_on_the_next_hop_to_that_host(dns, transport):
    """Sites that set a cookie and redirect back to themselves (consent and
    session probes) worked through requests' redirect handling; they must not
    loop into TooManyRedirects now."""
    dns.table["consent.example"] = [PUBLIC_IP]
    t = transport(
        FakeResponse(302, {"Location": "/page"}, set_cookie=["seen=1; Path=/"]),
        FakeResponse(200, {}, [b"ok"]),
    )
    res = sf.safe_get("https://consent.example/start")
    assert res.content == b"ok"
    assert "Cookie" not in t.calls[0]["headers"]
    assert t.calls[1]["headers"].get("Cookie") == "seen=1"


def test_a_redirect_cookie_never_follows_the_fetch_to_another_host(dns, transport):
    dns.table["a.example"] = [PUBLIC_IP]
    dns.table["b.example"] = [PUBLIC_IP_2]
    t = transport(
        FakeResponse(302, {"Location": "https://b.example/x"}, set_cookie=["sid=abc; Path=/"]),
        FakeResponse(200, {}, [b"ok"]),
    )
    sf.safe_get("https://a.example/")
    assert "Cookie" not in t.calls[1]["headers"]


def test_a_raw_utf8_location_is_repaired_like_requests_does(dns, transport):
    """http.client decodes header bytes as latin-1; a raw UTF-8 Location
    (`/z\\xc3\\xbcrich`) must land on /z%C3%BCrich, not the mojibake path."""
    dns.table["utf8.example"] = [PUBLIC_IP]
    mojibake = b"/z\xc3\xbcrich".decode("latin-1")
    t = transport(FakeResponse(302, {"Location": mojibake}), FakeResponse(200))
    res = sf.safe_get("http://utf8.example/start")
    assert t.calls[1]["vetted"].target == "/z%C3%BCrich"
    assert res.url == "http://utf8.example/z%C3%BCrich"


def test_a_latin1_location_that_is_not_utf8_is_kept_as_is(dns, transport):
    dns.table["latin.example"] = [PUBLIC_IP]
    t = transport(FakeResponse(302, {"Location": "/caf\xe9"}), FakeResponse(200))
    sf.safe_get("http://latin.example/")
    assert t.calls[1]["vetted"].target == "/caf%C3%A9"


# ---------------------------------------------------------- stacked encodings

class _Unreadable(FakeResponse):
    def stream(self, amt=None, decode_content=True):
        raise AssertionError("a refused body must not be decoded")


@pytest.mark.parametrize("encoding", ["gzip, gzip", "gzip,deflate", "deflate, gzip, gzip"])
def test_stacked_content_encodings_are_refused_before_decoding(dns, transport, encoding):
    """The urllib3 2.5 in the Orchestrator lock decodes stacked codings with no
    bound: a few hundred bytes become hundreds of MB before the size cap fires."""
    dns.table["bomb.example"] = [PUBLIC_IP]
    transport(_Unreadable(200, {"Content-Encoding": encoding}))
    with pytest.raises(sf.FetchError):
        sf.safe_get("http://bomb.example/")


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "identity", "gzip, identity", ""])
def test_a_single_content_encoding_is_fine(dns, transport, encoding):
    dns.table["ok.example"] = [PUBLIC_IP]
    transport(FakeResponse(200, {"Content-Encoding": encoding}, [b"fine"]))
    assert sf.safe_get("http://ok.example/").content == b"fine"


# ------------------------------------------- the total deadline, real sockets

BUDGET = 1.5          # seconds handed to safe_get
SLACK = 1.0           # scheduling slack on a loaded CI box
DRIP = 0.2            # seconds between trickled bytes (well under BUDGET, so
                      # no single recv ever hits the per-read timeout)


@pytest.fixture
def raw_server():
    """A raw TCP listener on 127.0.0.1 running `handler(conn)` per connection."""
    listeners = []

    def _start(handler):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        listeners.append(srv)

        def _serve_one(conn):
            try:
                conn.settimeout(15)
                handler(conn)
            except OSError:
                pass            # the client hung up (the point of most tests)
            finally:
                conn.close()

        def _accept():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=_serve_one, args=(conn,), daemon=True).start()

        threading.Thread(target=_accept, daemon=True).start()
        return srv.getsockname()[1]

    yield _start
    for srv in listeners:
        srv.close()


@pytest.fixture
def loopback_plays_public(monkeypatch, dns):
    """End to end through safe_get, with the policy told that 127.0.0.1 is a
    public address so a local server can stand in for a hostile public host."""
    monkeypatch.setattr(sf, "is_public_address", lambda ip: True)
    dns.table["slow.example"] = ["127.0.0.1"]
    return dns


def _read_request(conn):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _drip(conn, data: bytes):
    for i in range(len(data)):
        time.sleep(DRIP)
        conn.sendall(data[i:i + 1])


def _assert_cut_at_the_deadline(url):
    t0 = time.monotonic()
    with pytest.raises(sf.FetchTimeout):
        sf.safe_get(url, timeout=BUDGET)
    elapsed = time.monotonic() - t0
    assert elapsed < BUDGET + SLACK, f"fetch ran {elapsed:.1f}s on a {BUDGET}s budget"


def test_a_trickled_body_cannot_outlast_the_total_budget(raw_server, loopback_plays_public):
    """One byte every 0.2 s never trips a per-read timeout; only a deadline
    enforced at the socket stops it (the old per-chunk check waited for 8 KB)."""
    def handler(conn):
        _read_request(conn)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\nConnection: close\r\n\r\n")
        _drip(conn, b"x" * 40)
    port = raw_server(handler)
    _assert_cut_at_the_deadline(f"http://slow.example:{port}/")


def test_trickled_headers_cannot_outlast_the_total_budget(raw_server, loopback_plays_public):
    def handler(conn):
        _read_request(conn)
        _drip(conn, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    port = raw_server(handler)
    _assert_cut_at_the_deadline(f"http://slow.example:{port}/")


def test_a_trickled_tls_handshake_cannot_outlast_the_total_budget(raw_server, loopback_plays_public):
    """A TLS record header announcing 16 KB, then one byte at a time. Python
    already bounds a handshake by the socket timeout as a TOTAL (3.5+); this
    keeps that true now that the watchdog's connection class wraps connect."""
    def handler(conn):
        conn.recv(4096)                                  # the ClientHello
        conn.sendall(b"\x16\x03\x03\x40\x00")            # handshake record, 16384 bytes
        _drip(conn, b"\x00" * 40)
    port = raw_server(handler)
    _assert_cut_at_the_deadline(f"https://slow.example:{port}/")


@pytest.fixture
def tls_identity(tmp_path, monkeypatch):
    """A throwaway self-signed certificate for slow.example, trusted by the
    guard for this test only (so the HTTPS path is exercised offline)."""
    import shutil
    import subprocess
    if not shutil.which("openssl"):
        pytest.skip("needs the openssl CLI")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    proc = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-keyout", str(key), "-out", str(cert), "-subj", "/CN=slow.example",
         "-addext", "subjectAltName=DNS:slow.example"],
        capture_output=True, timeout=60)
    if proc.returncode != 0:
        pytest.skip(f"openssl could not make a test certificate: {proc.stderr[:200]!r}")
    monkeypatch.setattr(sf, "_ca_bundle", lambda: str(cert))
    return key, cert


def test_a_trickled_https_body_cannot_outlast_the_total_budget(
        raw_server, loopback_plays_public, tls_identity):
    """The same drip over TLS: the watchdog is handed the socket before the
    TLS wrap, so it covers HTTPS bodies too (most of the web)."""
    import ssl
    key, cert = tls_identity
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))

    def handler(conn):
        with ctx.wrap_socket(conn, server_side=True) as tls:
            _read_request(tls)
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\nConnection: close\r\n\r\n")
            _drip(tls, b"x" * 40)
    port = raw_server(handler)
    _assert_cut_at_the_deadline(f"https://slow.example:{port}/")


def test_a_fast_https_page_is_fetched_with_the_certificate_checked(
        raw_server, loopback_plays_public, tls_identity):
    import ssl
    key, cert = tls_identity
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))

    def handler(conn):
        with ctx.wrap_socket(conn, server_side=True) as tls:
            _read_request(tls)
            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\ntls")
    port = raw_server(handler)
    res = sf.safe_get(f"https://slow.example:{port}/", timeout=5)
    assert (res.status_code, res.content) == (200, b"tls")


def test_an_unterminated_body_cut_at_the_deadline_is_a_timeout_not_a_short_page(
        raw_server, loopback_plays_public):
    """No Content-Length + Connection: close means EOF ends the body. The
    watchdog's shutdown looks like EOF to urllib3, so the guard must report a
    timeout instead of handing back a silently truncated page."""
    def handler(conn):
        _read_request(conn)
        conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nstart")
        _drip(conn, b"x" * 40)
    port = raw_server(handler)
    _assert_cut_at_the_deadline(f"http://slow.example:{port}/")


def test_a_fast_page_is_unaffected_by_the_watchdog(raw_server, loopback_plays_public):
    def handler(conn):
        _read_request(conn)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nfast")
    port = raw_server(handler)
    res = sf.safe_get(f"http://slow.example:{port}/", timeout=BUDGET)
    assert (res.status_code, res.content) == (200, b"fast")


def test_a_real_cookie_redirect_loop_back_to_itself_resolves(raw_server, loopback_plays_public):
    def handler(conn):
        req = _read_request(conn)
        if b"Cookie: seen=1" in req:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        else:
            conn.sendall(b"HTTP/1.1 302 Found\r\nSet-Cookie: seen=1; Path=/\r\n"
                         b"Location: /cookie\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    port = raw_server(handler)
    res = sf.safe_get(f"http://slow.example:{port}/cookie", timeout=5)
    assert (res.status_code, res.content) == (200, b"ok")


# --------------------------------------------------------- the watchdog itself

def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_watchdog_wakes_a_blocked_recv_at_the_deadline():
    a, b = socket.socketpair()
    try:
        wd = sf._Watchdog(time.monotonic() + 0.3)
        wd.watch(a)
        a.settimeout(10)
        t0 = time.monotonic()
        assert a.recv(16) == b""            # shut down, not a 10 s timeout
        assert time.monotonic() - t0 < 2.0
        assert wd.fired
        wd.stop()
    finally:
        a.close()
        b.close()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_watchdog_closes_every_handle_it_took():
    """The watchdog holds a dup of each socket (so it can never act on a
    reused descriptor); release()/stop() must close those dups, or each fetch
    would leak a descriptor and keep its TCP connection half open."""
    a, b = socket.socketpair()
    try:
        before = _open_fds()
        wd = sf._Watchdog(time.monotonic() + 60)
        wd.watch(a)
        assert _open_fds() == before + 1
        wd.release()
        assert _open_fds() == before
        wd.watch(a)
        wd.stop()
        assert _open_fds() == before
        assert not wd.fired
        wd.watch(a)                          # after stop: never tracked
        assert _open_fds() == before
    finally:
        a.close()
        b.close()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_fetches_leave_no_descriptors_behind(raw_server, loopback_plays_public):
    def handler(conn):
        _read_request(conn)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
    port = raw_server(handler)
    sf.safe_get(f"http://slow.example:{port}/", timeout=5)      # warm imports
    time.sleep(0.2)
    before = _open_fds()
    for _ in range(5):
        sf.safe_get(f"http://slow.example:{port}/", timeout=5)
    time.sleep(0.2)                         # let the server threads close theirs
    assert _open_fds() <= before
