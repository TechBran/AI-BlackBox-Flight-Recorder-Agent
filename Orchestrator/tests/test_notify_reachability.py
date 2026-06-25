"""MN.2 — notification-reachability resolver tests.

``reachable_subscribers(operator)`` decouples notification delivery from the Gemma
attestation registry: a device is a deliverable target if its SUBSCRIPTION ROW's
``tailnet_name`` is CURRENTLY ONLINE on the tailnet — checked the same way
``mesh.reachable_devices`` checks online-ness (parse ``tailscale status`` → online
nodes → ``_name_matches``), NOT by requiring an attestation row.

These tests mock the tailscale online-set via the ``status_json`` seam so they are
offline and deterministic. They use a real (tmp-backed) SubscriptionStore.
"""

import json

import pytest

from Orchestrator.notifications.reachability import reachable_subscribers
from Orchestrator.notifications.subscriptions import SubscriptionStore


# A tailscale status sample: an ONLINE android phone (model-free, never attested)
# and an OFFLINE laptop. Mirrors the real shape (Peer keyed by nodekey; DNSName a
# trailing-dot FQDN; v4+v6 IPs).
SAMPLE_STATUS = json.dumps({
    "Self": {
        "HostName": "ai-black-box-fc",
        "DNSName": "ai-black-box-fc.tailnet-abc.ts.net.",
        "Online": True,
        "TailscaleIPs": ["100.74.17.54"],
        "OS": "linux",
    },
    "Peer": {
        "nodekey:aaa": {
            "HostName": "modelfree-phone",
            "DNSName": "modelfree-phone.tailnet-abc.ts.net.",
            "Online": True,
            "TailscaleIPs": ["100.88.0.7"],
            "OS": "android",
        },
        "nodekey:bbb": {
            "HostName": "offline-phone",
            "DNSName": "offline-phone.tailnet-abc.ts.net.",
            "Online": False,
            "TailscaleIPs": ["100.88.0.9"],
            "OS": "android",
        },
    },
})


@pytest.fixture
def store(tmp_path):
    return SubscriptionStore(path=tmp_path / "subs.json")


def test_online_subscriber_without_attestation_is_target(store):
    """The core regression: a model-free phone (NO Gemma row) that is subscribed +
    ONLINE on the tailnet IS a delivery target, resolved purely from its
    subscription row's tailnet_name."""
    store.upsert("d-phone", all=False, operators=["Brandon"],
                 tailnet_name="modelfree-phone")

    targets = reachable_subscribers("Brandon", store=store, status_json=SAMPLE_STATUS)

    assert len(targets) == 1
    t = targets[0]
    assert t["device_id"] == "d-phone"
    # The POST address resolves from the matched online node.
    assert t["node"]["dns_name"] == "modelfree-phone.tailnet-abc.ts.net"
    assert t["node"]["online"] is True


def test_offline_subscriber_is_not_a_target(store):
    """A subscriber whose tailnet_name is OFFLINE is excluded."""
    store.upsert("d-off", all=False, operators=["Brandon"],
                 tailnet_name="offline-phone")

    targets = reachable_subscribers("Brandon", store=store, status_json=SAMPLE_STATUS)

    assert targets == []


def test_subscriber_without_tailnet_name_is_skipped(store):
    """A subscriber with no tailnet_name cannot be located → not a target."""
    store.upsert("d-notail", all=False, operators=["Brandon"], tailnet_name=None)

    targets = reachable_subscribers("Brandon", store=store, status_json=SAMPLE_STATUS)

    assert targets == []


def test_matches_by_self_reported_ip(store):
    """A phone that self-reports its tailnet IPv4 as the join key still resolves."""
    store.upsert("d-byip", all=True, operators=[], tailnet_name="100.88.0.7")

    targets = reachable_subscribers("Brandon", store=store, status_json=SAMPLE_STATUS)

    assert [t["device_id"] for t in targets] == ["d-byip"]


def test_all_sentinel_subscriber_resolves(store):
    """A device subscribed via the 'all' sentinel is included for any operator."""
    store.upsert("d-all", all=True, operators=[], tailnet_name="modelfree-phone")

    targets = reachable_subscribers("Casey", store=store, status_json=SAMPLE_STATUS)

    assert [t["device_id"] for t in targets] == ["d-all"]


def test_unsubscribed_device_excluded(store):
    """A device not subscribed to the operator (and not 'all') is excluded."""
    store.upsert("d-other", all=False, operators=["Dana"],
                 tailnet_name="modelfree-phone")

    targets = reachable_subscribers("Brandon", store=store, status_json=SAMPLE_STATUS)

    assert targets == []


def test_no_online_nodes_returns_empty(store):
    """Tailscale down / empty status → no targets, no raise."""
    store.upsert("d-phone", all=False, operators=["Brandon"],
                 tailnet_name="modelfree-phone")

    targets = reachable_subscribers("Brandon", store=store, status_json="")

    assert targets == []
