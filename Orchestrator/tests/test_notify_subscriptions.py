"""MN.1 — SubscriptionStore tests (per-device operator-subscription store).

The store is the durable, atomic, corrupt-tolerant record of which devices want
notifications for which operators. Locked design:
  * Fresh box is OPT-IN: an absent file / a device with no row is subscribed to
    NOTHING.
  * Membership = sub["all"] OR operator in sub["operators"].
  * The write is atomic (tmp file → os.replace), the read tolerates a missing or
    corrupt file by returning empty {} (never raises).
"""

import json

import pytest

from Orchestrator.notifications.subscriptions import SubscriptionStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A SubscriptionStore backed by a throwaway JSON file under tmp_path."""
    path = tmp_path / "device_notification_subs.json"
    s = SubscriptionStore(path=path)
    return s


def test_absent_file_no_subscribers_no_raise(store):
    """Fresh box: absent file → no subscribers for anyone, and no exception."""
    assert store.subscribers_for("Brandon") == []
    assert store.is_subscribed("dev-1", "Brandon") is False
    assert store.all_subscriptions() == {}
    assert store.get("dev-1") is None


def test_device_with_no_row_is_subscribed_to_nothing(store):
    """Opt-in: a device that exists for one operator is NOT subscribed to others."""
    store.upsert("dev-1", all=False, operators=["Brandon"])
    assert store.is_subscribed("dev-1", "Brandon") is True
    assert store.is_subscribed("dev-1", "Casey") is False
    # An entirely unknown device is subscribed to nothing.
    assert store.is_subscribed("ghost", "Brandon") is False


def test_subscribers_for_operator_or_all(store):
    """subscribers_for returns devices subscribed to the operator OR 'all'."""
    store.upsert("dev-op", all=False, operators=["Brandon"])
    store.upsert("dev-all", all=True, operators=[])
    store.upsert("dev-other", all=False, operators=["Casey"])

    subs = set(store.subscribers_for("Brandon"))
    assert subs == {"dev-op", "dev-all"}  # dev-other is not subscribed to Brandon

    # The 'all' device shows up for every operator.
    assert "dev-all" in store.subscribers_for("Casey")
    assert "dev-op" not in store.subscribers_for("Casey")


def test_upsert_get_round_trip(store):
    """upsert → get round-trips the full row including metadata + updated_at."""
    store.upsert(
        "dev-1",
        all=False,
        operators=["Brandon", "Casey"],
        tailnet_name="brandon-fold6",
        device_kind="android",
        display_name="Brandon's Fold",
    )
    row = store.get("dev-1")
    assert row is not None
    assert row["all"] is False
    assert row["operators"] == ["Brandon", "Casey"]
    assert row["tailnet_name"] == "brandon-fold6"
    assert row["device_kind"] == "android"
    assert row["display_name"] == "Brandon's Fold"
    assert isinstance(row["updated_at"], str) and row["updated_at"]


def test_upsert_overwrites(store):
    """A second upsert for the same device replaces the prior row."""
    store.upsert("dev-1", all=False, operators=["Brandon"])
    store.upsert("dev-1", all=True, operators=[])
    row = store.get("dev-1")
    assert row["all"] is True
    assert row["operators"] == []


def test_delete_removes_device(store):
    """delete removes the row; subsequent get is None; delete of absent is a no-op."""
    store.upsert("dev-1", all=True, operators=[])
    assert store.get("dev-1") is not None
    store.delete("dev-1")
    assert store.get("dev-1") is None
    # Deleting again must not raise.
    store.delete("dev-1")
    assert store.subscribers_for("Brandon") == []


def test_corrupt_file_treated_as_empty(tmp_path):
    """A corrupt JSON file → empty store, no raise."""
    path = tmp_path / "device_notification_subs.json"
    path.write_text("{ this is not valid json ")
    s = SubscriptionStore(path=path)
    assert s.all_subscriptions() == {}
    assert s.subscribers_for("Brandon") == []
    assert s.get("dev-1") is None


def test_non_dict_json_treated_as_empty(tmp_path):
    """A JSON array (wrong top-level type) → empty store, no raise."""
    path = tmp_path / "device_notification_subs.json"
    path.write_text(json.dumps(["not", "a", "dict"]))
    s = SubscriptionStore(path=path)
    assert s.all_subscriptions() == {}


def test_write_is_atomic_tmp_then_replace(store, monkeypatch):
    """The write goes through a tmp sibling then os.replace (no torn writes)."""
    import Orchestrator.notifications.subscriptions as subs_mod

    seen = {"tmp_written": False, "replaced": False}
    real_replace = subs_mod.os.replace

    def spy_replace(src, dst):
        # The source must be the tmp sibling and must already exist on disk.
        assert str(src).endswith(".tmp"), f"replace source not a tmp file: {src}"
        seen["replaced"] = True
        return real_replace(src, dst)

    monkeypatch.setattr(subs_mod.os, "replace", spy_replace)
    store.upsert("dev-1", all=True, operators=[])
    assert seen["replaced"] is True
    # The tmp sibling must not linger after a successful replace.
    assert not (store.path.with_suffix(".json.tmp")).exists()


def test_persists_across_instances(tmp_path):
    """A second store instance over the same file sees the first's writes."""
    path = tmp_path / "device_notification_subs.json"
    SubscriptionStore(path=path).upsert("dev-1", all=False, operators=["Brandon"])
    reopened = SubscriptionStore(path=path)
    assert reopened.is_subscribed("dev-1", "Brandon") is True
