import json
import pytest

import Orchestrator.local_provider.registry as r
import Orchestrator.local_provider.mesh as m


# A representative `tailscale status --json` sample (sanitized): a linux Self, an
# ONLINE android phone (brandon-fold6), and an OFFLINE laptop. Mirrors the real
# shape (Peer keyed by nodekey; DNSName as a trailing-dot FQDN; v4+v6 IPs).
SAMPLE_STATUS = json.dumps({
    "Self": {
        "HostName": "ai-black-box-fc",
        "DNSName": "ai-black-box-fc.tailnet-abc.ts.net.",
        "Online": True,
        "TailscaleIPs": ["100.74.17.54", "fd7a:115c:a1e0::2536:1136"],
        "OS": "linux",
    },
    "Peer": {
        "nodekey:aaa": {
            "HostName": "brandon-fold6",
            "DNSName": "brandon-fold6.tailnet-abc.ts.net.",
            "Online": True,
            "TailscaleIPs": ["100.88.0.7", "fd7a:115c:a1e0::1"],
            "OS": "android",
        },
        "nodekey:bbb": {
            "HostName": "brandon-laptop",
            "DNSName": "brandon-laptop.tailnet-abc.ts.net.",
            "Online": False,
            "TailscaleIPs": ["100.88.0.9"],
            "OS": "windows",
        },
    },
})


@pytest.fixture
def fresh_registry(tmp_path, monkeypatch):
    """A file-backed registry singleton mesh will pick up via get_local_registry()."""
    monkeypatch.setattr(r, "STORE_FILE", tmp_path / "local_devices.json")
    monkeypatch.setattr(r, "_registry", None)  # reset singleton so mesh sees ours
    return r.get_local_registry()


def _attest(reg, operator="Brandon", device_id="fold6", tailnet_name="brandon-fold6",
            **over):
    kw = dict(model_slug="gemma-4-e4b", version="1", sha256="x", delegate="gpu",
              autonomy_mode="yolo")
    kw.update(over)
    return reg.attest(operator=operator, device_id=device_id,
                      tailnet_name=tailnet_name, **kw)


# ── parse_tailscale_status (pure) ──

def test_parse_includes_self_and_peers():
    nodes = m.parse_tailscale_status(SAMPLE_STATUS)
    assert {n.hostname for n in nodes} == {"ai-black-box-fc", "brandon-fold6", "brandon-laptop"}


def test_parse_extracts_fields_and_strips_trailing_dot():
    fold = next(n for n in m.parse_tailscale_status(SAMPLE_STATUS) if n.hostname == "brandon-fold6")
    assert fold.dns_name == "brandon-fold6.tailnet-abc.ts.net"  # trailing dot stripped
    assert fold.ip == "100.88.0.7"  # first IPv4, not the IPv6
    assert fold.online is True
    assert fold.os == "android"


def test_parse_malformed_returns_empty():
    assert m.parse_tailscale_status("not json") == []
    assert m.parse_tailscale_status("") == []
    assert m.parse_tailscale_status("[]") == []     # valid json but not a dict
    assert m.parse_tailscale_status("null") == []


# ── reachable_devices / resolve_origin (the join) ──

def test_reachable_joins_online_attested_device(fresh_registry):
    _attest(fresh_registry)
    devs = m.reachable_devices(operator="Brandon", status_json=SAMPLE_STATUS)
    assert len(devs) == 1
    d = devs[0]
    assert d["operator"] == "Brandon"
    assert d["model_slug"] == "gemma-4-e4b"
    assert d["node"]["dns_name"] == "brandon-fold6.tailnet-abc.ts.net"
    assert d["node"]["ip"] == "100.88.0.7"


def test_reachable_excludes_offline_device(fresh_registry):
    _attest(fresh_registry, device_id="laptop", tailnet_name="brandon-laptop")
    assert m.reachable_devices(operator="Brandon", status_json=SAMPLE_STATUS) == []


def test_reachable_skips_records_without_tailnet_name(fresh_registry):
    # Attest without a tailnet_name -> stored as None -> unjoinable.
    fresh_registry.attest(operator="Brandon", device_id="fold6", model_slug="gemma-4-e4b",
                          version="1", sha256="x", delegate="gpu", autonomy_mode="yolo")
    # And a truly key-absent legacy row (predates the tailnet_name field).
    fresh_registry._store["Brandon"]["old"] = {"device_id": "old", "model_slug": "g"}
    assert m.reachable_devices(operator="Brandon", status_json=SAMPLE_STATUS) == []


def test_reachable_operator_filter(fresh_registry):
    _attest(fresh_registry, operator="Brandon", device_id="fold6")
    _attest(fresh_registry, operator="Other", device_id="fold6b")
    all_devs = m.reachable_devices(status_json=SAMPLE_STATUS)  # operator=None -> all
    assert {d["operator"] for d in all_devs} == {"Brandon", "Other"}
    only = m.reachable_devices(operator="Brandon", status_json=SAMPLE_STATUS)
    assert [d["operator"] for d in only] == ["Brandon"]


def test_resolve_origin_returns_node(fresh_registry):
    _attest(fresh_registry)
    node = m.resolve_origin("Brandon", status_json=SAMPLE_STATUS)
    assert node is not None
    assert node.dns_name == "brandon-fold6.tailnet-abc.ts.net"
    assert node.ip == "100.88.0.7"
    assert node.online is True


def test_resolve_origin_none_when_unmatched(fresh_registry):
    assert m.resolve_origin("Nobody", status_json=SAMPLE_STATUS) is None


def test_name_match_accepts_fqdn_attestation(fresh_registry):
    # A device that attested its full MagicDNS FQDN still matches the node.
    _attest(fresh_registry, tailnet_name="brandon-fold6.tailnet-abc.ts.net")
    node = m.resolve_origin("Brandon", status_json=SAMPLE_STATUS)
    assert node is not None and node.hostname == "brandon-fold6"
