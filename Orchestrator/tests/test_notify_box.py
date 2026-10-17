"""Box-wide notifications: one push to every device that opted in to ANY operator.

W09 (2026-10-09 audit): a provider billing failure belongs to the BOX, not to one
operator. The API key is shared, so every operator's chat, cron and Computer Use
runs stop together. ``notify(operator, ...)`` only reaches one operator's
subscribers, and the 'system' operator can't be subscribed to at all, so a
billing alert sent that way reached nobody (the audit's ``delivered=0/0``).
``notify_box`` sends to the union of every subscribed device, once per device.

Pinned here:
  * The targets are the devices subscribed to SOMETHING ('all' or at least one
    operator) that are online now. A row subscribed to nothing is not a target
    (the subsystem stays opt-in).
  * Each target gets exactly one POST with the full body and operator 'system'.
    The text names a provider and a console, never an operator's content.
  * The sync bridge reports whether the push was actually scheduled, so a
    caller that dedupes can tell a sent alert from a dropped one.

Hermetic: tailscale status is a canned JSON string, the store is a tmp file and
the per-device POST is a fake.
"""
import asyncio
import json
import threading

import pytest

import Orchestrator.notifications.bridge as bridge
import Orchestrator.notifications.bus as bus_mod
from Orchestrator.notifications.reachability import reachable_box_subscribers
from Orchestrator.notifications.subscriptions import SubscriptionStore


STATUS = json.dumps({
    "Self": {"HostName": "box", "DNSName": "box.tailnet-x.ts.net.", "Online": True,
             "TailscaleIPs": ["100.64.0.1"], "OS": "linux"},
    "Peer": {
        "nodekey:a": {"HostName": "phone-a", "DNSName": "phone-a.tailnet-x.ts.net.",
                      "Online": True, "TailscaleIPs": ["100.64.0.2"], "OS": "android"},
        "nodekey:b": {"HostName": "phone-b", "DNSName": "phone-b.tailnet-x.ts.net.",
                      "Online": True, "TailscaleIPs": ["100.64.0.3"], "OS": "android"},
        "nodekey:c": {"HostName": "phone-c", "DNSName": "phone-c.tailnet-x.ts.net.",
                      "Online": False, "TailscaleIPs": ["100.64.0.4"], "OS": "android"},
        "nodekey:d": {"HostName": "phone-d", "DNSName": "phone-d.tailnet-x.ts.net.",
                      "Online": True, "TailscaleIPs": ["100.64.0.5"], "OS": "android"},
    },
})


@pytest.fixture
def store(tmp_path):
    s = SubscriptionStore(path=tmp_path / "subs.json")
    s.upsert("dev-all", all=True, operators=[], tailnet_name="phone-a")
    s.upsert("dev-op", all=False, operators=["OperatorOne"], tailnet_name="phone-b")
    s.upsert("dev-offline", all=True, operators=[], tailnet_name="phone-c")
    s.upsert("dev-optout", all=False, operators=[], tailnet_name="phone-d")
    return s


def test_box_targets_are_every_online_device_subscribed_to_anything(store):
    targets = reachable_box_subscribers(store=store, status_json=STATUS)
    assert sorted(t["device_id"] for t in targets) == ["dev-all", "dev-op"]


def test_box_targets_degrade_to_empty_on_a_fresh_box(tmp_path):
    empty = SubscriptionStore(path=tmp_path / "none.json")
    assert reachable_box_subscribers(store=empty, status_json=STATUS) == []


@pytest.fixture
def posts(monkeypatch):
    sent = []

    async def fake_post(device, payload):
        sent.append({"device_id": device["device_id"], "payload": payload})
        return {"ok": True}

    monkeypatch.setattr(bus_mod, "_post_to_device", fake_post)
    return sent


def _targets(*ids):
    return [{"device_id": i, "tailnet_name": i,
             "node": {"hostname": i, "dns_name": f"{i}.x.ts.net", "ip": "100.64.0.9",
                      "online": True, "os": "android"}} for i in ids]


def test_notify_box_posts_once_per_device_with_the_full_body(monkeypatch, posts):
    # A device listed twice by the resolver is still posted to once.
    monkeypatch.setattr(bus_mod, "reachable_box_subscribers",
                        lambda: _targets("dev-1", "dev-2", "dev-1"))

    result = asyncio.run(bus_mod.notify_box(
        "Anthropic is out of credits", "Anthropic account is out of credits.",
        category="alert", dedup_key="billing:anthropic:1"))

    assert sorted(result.delivered) == ["dev-1", "dev-2"]
    assert len(posts) == 2
    for p in posts:
        assert p["payload"]["body"] == "Anthropic account is out of credits."
        assert p["payload"]["operator"] == "system"
        assert p["payload"]["category"] == "alert"
        assert p["payload"]["notif_id"] == result.notif_id
    assert result.recorded is False


def test_notify_box_never_raises_when_the_resolver_fails(monkeypatch, posts):
    def boom():
        raise RuntimeError("tailscale down")

    monkeypatch.setattr(bus_mod, "reachable_box_subscribers", boom)
    result = asyncio.run(bus_mod.notify_box("T", "B"))
    assert result.delivered == [] and posts == []


def test_notify_box_counts_a_dead_device_unreachable(monkeypatch):
    monkeypatch.setattr(bus_mod, "reachable_box_subscribers", lambda: _targets("ok", "dead"))

    async def flaky(device, payload):
        if device["device_id"] == "dead":
            raise ConnectionError("no route")
        return {"ok": True}

    monkeypatch.setattr(bus_mod, "_post_to_device", flaky)
    result = asyncio.run(bus_mod.notify_box("T", "B"))
    assert result.delivered == ["ok"] and result.unreachable == ["dead"]


@pytest.fixture
def no_loop():
    saved = bridge._MAIN_LOOP
    bridge._MAIN_LOOP = None
    yield
    bridge._MAIN_LOOP = saved


def test_bridge_reports_a_dropped_box_push(no_loop, monkeypatch):
    called = []

    async def fake_notify_box(*a, **k):
        called.append(1)

    monkeypatch.setattr(bridge, "notify_box", fake_notify_box)
    assert bridge.notify_box_in_background("T", "B") is False
    assert called == []


def test_bridge_schedules_a_box_push_on_the_main_loop(no_loop, monkeypatch):
    received = []
    done = threading.Event()

    async def fake_notify_box(title, body, category="general", dedup_key=None):
        received.append((title, body, category, dedup_key))
        done.set()

    monkeypatch.setattr(bridge, "notify_box", fake_notify_box)
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        bridge.set_main_loop(loop)
        assert bridge.notify_box_in_background("T", "B", category="alert", dedup_key="k") is True
        assert done.wait(5)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(5)
        loop.close()
    assert received == [("T", "B", "alert", "k")]
