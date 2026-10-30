"""Destination classes (spec §4.6): a custom server is local only when EVERY
address its host resolves to is non-public; a resolution failure is cloud;
cloud providers need their own tick (Google also the paid-key confirmation);
local-only categories never reach a cloud destination."""
from __future__ import annotations

import socket

import pytest

from Orchestrator.onboarding import custom_servers
from Orchestrator.wellness import destinations as dst


def _resolver(mapping):
    def resolve(host, port, type=None):
        if host not in mapping:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (ip, port)) for ip in mapping[host]]
    return resolve


@pytest.fixture
def servers(tmp_path, monkeypatch):
    monkeypatch.setattr(custom_servers, "REGISTRY_PATH", tmp_path / "custom_models.json")

    def add(alias, url):
        return custom_servers.add_server(alias, url)
    return add


@pytest.mark.parametrize("ip,local", [
    ("127.0.0.1", True), ("10.1.2.3", True), ("192.168.1.50", True), ("172.20.0.4", True),
    ("100.100.1.2", True), ("169.254.10.1", True), ("::1", True), ("fd7a:115c:a1e0::5", True),
    ("fe80::1", True), ("::ffff:192.168.1.5", True),
    ("8.8.8.8", False), ("1.1.1.1", False), ("2606:4700::1111", False),
    ("::ffff:8.8.8.8", False), ("2002:0808:0808::1", False), ("64:ff9b::808:808", False),
])
def test_address_classes(ip, local):
    assert dst.address_is_local(ip) is local


def test_lan_tailnet_and_loopback_servers_are_local(servers):
    servers("lan-box", "http://192.168.1.50:8080/v1")
    servers("tail-box", "http://100.100.1.2:9098/v1")
    servers("me", "http://127.0.0.1:9098/v1")
    for alias in ("lan-box", "tail-box", "me"):
        assert dst.classify(f"custom:{alias}") == dst.LOCAL


def test_public_server_is_cloud(servers):
    servers("router", "https://api.router.example/v1")
    r = _resolver({"api.router.example": ["93.184.216.34"]})
    d = dst.destination("custom:router", resolver=r)
    assert (d.id, d.cls, d.reason) == ("custom:router", dst.CLOUD, "public_address")


def test_any_public_address_makes_it_cloud(servers):
    servers("mixed", "http://mixed.example:8080/v1")
    r = _resolver({"mixed.example": ["192.168.1.9", "8.8.4.4"]})
    assert dst.classify("custom:mixed", resolver=r) == dst.CLOUD
    r = _resolver({"mixed.example": ["192.168.1.9", "10.0.0.3"]})
    assert dst.classify("custom:mixed", resolver=r) == dst.LOCAL


def test_resolution_failure_is_cloud(servers):
    servers("gone", "http://gone.example/v1")
    assert dst.classify("custom:gone", resolver=_resolver({})) == dst.CLOUD
    # The suite blocks real DNS: an unresolvable name in production reads the same.
    assert dst.classify("custom:gone") == dst.CLOUD


def test_registry_is_read_fresh(servers):
    assert dst.destination("custom:later").cls is None          # unknown: denied
    servers("later", "http://10.0.0.8/v1")
    assert dst.classify("custom:later") == dst.LOCAL


def test_custom_with_a_qualified_model(servers):
    servers("gemma-box", "http://10.0.0.9:9098/v1")
    d = dst.destination("custom", model="gemma-box::gemma-26b")
    assert (d.id, d.cls) == ("custom:gemma-box", dst.LOCAL)


def test_providers_and_aliases():
    assert dst.classify("anthropic") == dst.CLOUD
    assert dst.destination("gemini").id == "google"
    assert dst.destination("grok").id == "xai"
    assert dst.classify("local") == dst.ON_DEVICE
    assert dst.destination("someone-else").cls is None
    assert dst.destination(None).cls is None


ENR = {"local_only": {"sleep": False, "heart": False, "activity": False, "blood_pressure": True},
       "providers_allowed": [], "google_paid_key_confirmed": False}


def test_cloud_needs_a_tick(servers):
    assert dst.allowed("anthropic", "sleep", ENR) == (False, "not_ticked")
    ticked = dict(ENR, providers_allowed=["anthropic"])
    assert dst.allowed("anthropic", "sleep", ticked) == (True, None)
    assert dst.allowed("openai", "sleep", ticked) == (False, "not_ticked")


def test_local_and_on_device_are_allowed_by_default(servers):
    servers("lan", "http://192.168.1.20/v1")
    assert dst.allowed("custom:lan", "sleep", ENR) == (True, None)
    assert dst.allowed("local", "heart", ENR) == (True, None)


def test_google_needs_the_paid_key_confirmation():
    ticked = dict(ENR, providers_allowed=["google"])
    assert dst.allowed("google", "sleep", ticked) == (False, "google_unpaid_key")
    assert dst.allowed("gemini", "sleep", dict(ticked, google_paid_key_confirmed=True)) == \
        (True, None)


def test_local_only_categories_never_reach_the_cloud(servers):
    servers("lan", "http://192.168.1.20/v1")
    servers("router", "https://api.router.example/v1")
    everything = dict(ENR, providers_allowed=["anthropic", "custom:router"])
    assert dst.allowed("anthropic", "blood_pressure", everything) == (False, "local_only")
    assert dst.allowed("custom:router", "blood_pressure", everything,
                       resolver=_resolver({"api.router.example": ["8.8.8.8"]})) == \
        (False, "local_only")
    assert dst.allowed("custom:lan", "blood_pressure", everything) == (True, None)


def test_unknown_or_unenrolled_is_denied():
    assert dst.allowed("anthropic", "sleep", None) == (False, "not_enrolled")
    assert dst.allowed("who-knows", "sleep", ENR)[0] is False


def test_allow_list_validation_and_rows(servers):
    servers("lan", "http://192.168.1.20/v1")
    assert dst.ensure_known(["anthropic", "gemini", "custom:lan", "anthropic"]) == \
        ["anthropic", "google", "custom:lan"]
    with pytest.raises(ValueError):
        dst.ensure_known(["custom:nowhere"])
    rows = {r["id"]: r for r in dst.rows()}
    assert rows["custom:lan"]["class"] == dst.LOCAL and rows["google"]["terms"]
