"""MN.2 — notify() bus tests.

The bus fans a notification out to the operator's SUBSCRIBERS that are currently
ONLINE on the tailnet (``reachable_subscribers`` — subscription-row reachability,
NOT the Gemma attestation registry), fire-and-forget (short per-device timeout,
never blocks the caller), and ALWAYS records the event as a snapshot — even when
zero devices are reachable.

These tests mock the two seams: ``reachable_subscribers`` (the
subscription-row → online-target resolver) and the per-device POST (so nothing
touches a real phone), plus ``mint_with_content`` (so nothing touches the real
volume). The per-device POST seam receives the resolved ``device`` dict so a test
can decide behaviour / assert the payload per device without parsing base_url.
"""

import asyncio

import pytest

import Orchestrator.notifications.bus as bus_mod
from Orchestrator.notifications.bus import notify, NotifyResult


def _device(device_id, operator="Brandon"):
    """A target item, shaped like reachable_subscribers() output.

    ``operator`` is retained for call-site compatibility but is not part of a
    reachable_subscribers row (it returns device_id + tailnet_name + node) — the
    bus only reads ``device_id`` and ``node``.
    """
    return {
        "device_id": device_id,
        "tailnet_name": device_id,
        "node": {
            "hostname": device_id,
            "dns_name": f"{device_id}.example.ts.net",
            "ip": "100.64.0.1",
            "online": True,
            "os": "android",
        },
    }


@pytest.fixture(autouse=True)
def _patch_record(monkeypatch):
    """Capture mint_with_content calls without touching the real volume."""
    calls = []

    def fake_mint(operator, content, reason="DIRECT", snap_type="normal"):
        calls.append(
            {"operator": operator, "content": content, "reason": reason, "snap_type": snap_type}
        )
        return {"snap_id": "SNAP-TEST-0001"}

    monkeypatch.setattr(bus_mod, "mint_with_content", fake_mint)
    return calls


@pytest.fixture()
def posts(monkeypatch):
    """Capture every per-device POST (device dict + payload); default = success."""
    sent = []

    async def fake_post(device, payload):
        sent.append({"device_id": device["device_id"], "payload": payload})
        return {"ok": True}

    monkeypatch.setattr(bus_mod, "_post_to_device", fake_post)
    return sent


def _patch_targets(monkeypatch, devices):
    """Mock the subscription-row → online-target resolver to a fixed target list.

    reachable_subscribers already applies the subscribed ∩ online join, so a test
    declares the FINAL online targets here (the model-free phone path is covered
    end-to-end in test_notify_reachability).
    """
    monkeypatch.setattr(bus_mod, "reachable_subscribers", lambda operator: list(devices))


@pytest.mark.asyncio
async def test_posts_to_resolved_targets(monkeypatch, posts):
    """Every device the resolver returns (subscribed + online) gets a POST.

    The resolver already applies the subscribed ∩ online join, so a device that is
    unsubscribed or offline simply never appears in its output — it is not a target
    and is excluded entirely (not even counted unreachable).
    """
    _patch_targets(monkeypatch, [_device("d-sub")])

    result = await notify("Brandon", "Hello", "Body text", category="test")

    assert isinstance(result, NotifyResult)
    assert result.delivered == ["d-sub"]
    assert result.recorded is True
    assert len(posts) == 1
    assert posts[0]["device_id"] == "d-sub"
    assert posts[0]["payload"]["title"] == "Hello"
    # A device the resolver excluded (unsubscribed or offline) appears nowhere.
    assert "d-unsub" not in result.delivered and "d-unsub" not in result.unreachable
    assert "d-offline" not in result.delivered


@pytest.mark.asyncio
async def test_zero_online_subscribers_still_records_no_raise(monkeypatch, posts, _patch_record):
    """Zero online subscribers (subscribed but offline) → recorded=True,
    delivered=[], and no exception. The resolver returns [] when no subscriber's
    tailnet_name is currently online."""
    _patch_targets(monkeypatch, [])

    result = await notify("Brandon", "T", "B")

    assert result.delivered == []
    assert result.recorded is True
    assert len(posts) == 0
    assert len(_patch_record) == 1
    assert _patch_record[0]["operator"] == "Brandon"


@pytest.mark.asyncio
async def test_no_targets_records(monkeypatch, posts, _patch_record):
    """No resolved targets (nobody subscribed, or none online) → no POST, but the
    event still records in the durable inbox."""
    _patch_targets(monkeypatch, [])

    result = await notify("Brandon", "T", "B")

    assert result.delivered == []
    assert result.recorded is True
    assert len(posts) == 0
    assert len(_patch_record) == 1


@pytest.mark.asyncio
async def test_one_failing_post_others_still_deliver_and_record(monkeypatch, _patch_record):
    """A device POST that raises is counted unreachable; the rest still deliver."""
    _patch_targets(monkeypatch, [_device("d-ok"), _device("d-bad")])

    async def flaky_post(device, payload):
        if device["device_id"] == "d-bad":
            raise RuntimeError("connection refused")
        return {"ok": True}

    monkeypatch.setattr(bus_mod, "_post_to_device", flaky_post)

    result = await notify("Brandon", "T", "B")

    assert result.delivered == ["d-ok"]
    assert result.unreachable == ["d-bad"]
    assert result.recorded is True
    assert len(_patch_record) == 1  # still recorded exactly once


@pytest.mark.asyncio
async def test_timeout_counts_unreachable_does_not_raise(monkeypatch, _patch_record):
    """A per-device POST that times out is unreachable; notify() never raises."""
    _patch_targets(monkeypatch, [_device("d-slow")])

    async def slow_post(device, payload):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(bus_mod, "_post_to_device", slow_post)

    result = await notify("Brandon", "T", "B")
    assert result.delivered == []
    assert result.unreachable == ["d-slow"]
    assert result.recorded is True


@pytest.mark.asyncio
async def test_records_in_every_case(monkeypatch, posts, _patch_record):
    """mint_with_content is called with [NOTIFY:category] content + the operator."""
    _patch_targets(monkeypatch, [_device("d-1")])

    await notify("Casey", "Deploy done", "All green", category="ops")

    assert len(_patch_record) == 1
    rec = _patch_record[0]
    assert rec["operator"] == "Casey"
    assert "[NOTIFY:ops]" in rec["content"]
    assert "Deploy done" in rec["content"]
    assert "All green" in rec["content"]


@pytest.mark.asyncio
async def test_dedup_key_drives_notif_id(monkeypatch, posts, _patch_record):
    """A provided dedup_key yields a stable, derived notif_id (not random)."""
    _patch_targets(monkeypatch, [_device("d-1")])

    r1 = await notify("Brandon", "T", "B", dedup_key="job-42")
    r2 = await notify("Brandon", "T", "B", dedup_key="job-42")
    assert r1.notif_id == r2.notif_id
    assert posts[0]["payload"]["notif_id"] == r1.notif_id


@pytest.mark.asyncio
async def test_metadata_only_across_operators(monkeypatch, posts, _patch_record):
    """A device subscribed via 'all' to a NON-owner operator gets metadata only.

    The device whose subscription explicitly names the event operator gets the
    full body; a cross-operator 'all' recipient gets title/category/notif_id but
    NO full body.
    """
    _patch_targets(monkeypatch, [_device("d-owner"), _device("d-all")])

    def fake_get(self, device_id):
        rows = {
            "d-owner": {"all": False, "operators": ["Brandon"]},  # explicit Brandon
            "d-all": {"all": True, "operators": []},               # via 'all'
        }
        return rows.get(device_id)

    monkeypatch.setattr(bus_mod.SubscriptionStore, "get", fake_get)

    await notify("Brandon", "Secret title", "Sensitive body", category="alert")

    payloads = {p["device_id"]: p["payload"] for p in posts}
    assert set(payloads) == {"d-owner", "d-all"}

    # Owner (explicit Brandon subscription) gets the full body.
    assert payloads["d-owner"]["body"] == "Sensitive body"
    # Cross-operator 'all' recipient gets metadata only — NO full body.
    assert not payloads["d-all"].get("body")
    # But still gets the metadata: title + category + notif_id.
    assert payloads["d-all"]["title"] == "Secret title"
    assert payloads["d-all"]["category"] == "alert"
    assert payloads["d-all"]["notif_id"]


@pytest.mark.asyncio
async def test_resolver_raises_does_not_raise_still_records(monkeypatch, posts, _patch_record):
    """If reachable_subscribers raises, notify() swallows it (no targets) and still
    records — the durable inbox is never collateral damage to a Tailscale glitch."""
    def boom(operator):
        raise RuntimeError("tailscale exploded")

    monkeypatch.setattr(bus_mod, "reachable_subscribers", boom)

    result = await notify("Brandon", "T", "B")

    assert result.delivered == []
    assert result.recorded is True
    assert len(posts) == 0
    assert len(_patch_record) == 1


@pytest.mark.asyncio
async def test_mint_raises_does_not_raise_recorded_false(monkeypatch, posts):
    """If mint_with_content raises, notify() never raises — it degrades to
    recorded=False rather than breaking the caller."""
    _patch_targets(monkeypatch, [])

    def boom(*a, **k):
        raise RuntimeError("volume offline")

    monkeypatch.setattr(bus_mod, "mint_with_content", boom)

    result = await notify("Brandon", "T", "B")

    assert result.recorded is False
    assert result.delivered == []
