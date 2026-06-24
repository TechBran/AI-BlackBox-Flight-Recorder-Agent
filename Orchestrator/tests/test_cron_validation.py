"""
Tests for M2.1 — central field validation at the manager sink.

The single most dangerous cron bug: a job created with delivery='sms' (or
'voice_call') but a blank/missing delivery_target reports success, then
silently never delivers (the executor's _build_prompt falls through to
snapshot mode). Validation is centralised in CronJobManager._validate_job_fields
so all four surfaces (HTTP API, Portal, ToolVault tool, Android) inherit it.

Rules enforced:
  - delivery (when present) in {snapshot, sms, voice_call, notification}
  - status (when present) in {active, paused}
  - operator (present+non-blank always required on create)
  - delivery in {sms, voice_call} REQUIRES a delivery_target matching E.164
    ^\\+[1-9]\\d{6,14}$ (on the job or in the update for transitions)
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import CronJobManager


@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    db = tmp_path / "cron_jobs_test.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    return CronJobManager()


# ---------------------------------------------------------------------------
# create_job — happy path still works
# ---------------------------------------------------------------------------

def test_valid_job_still_creates(temp_manager):
    job = temp_manager.create_job(
        name="daily", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    assert job["id"]
    assert job["delivery"] == "snapshot"  # default preserved


def test_valid_sms_job_with_e164_target_creates(temp_manager):
    job = temp_manager.create_job(
        name="sms job",
        prompt="hi",
        schedule="0 15 * * *",
        operator="system",
        delivery="sms",
        delivery_target="+15551234567",
    )
    assert job["delivery"] == "sms"
    assert job["delivery_target"] == "+15551234567"


# ---------------------------------------------------------------------------
# create_job — bad cases each raise ValueError
# ---------------------------------------------------------------------------

def test_bad_delivery_enum_raises(temp_manager):
    with pytest.raises(ValueError):
        temp_manager.create_job(
            name="j", prompt="hi", schedule="0 15 * * *",
            operator="system", delivery="carrier-pigeon",
        )


def test_blank_operator_raises(temp_manager):
    with pytest.raises(ValueError):
        temp_manager.create_job(
            name="j", prompt="hi", schedule="0 15 * * *", operator="   ",
        )


def test_sms_without_target_raises(temp_manager):
    """THE bug: sms delivery + blank target must raise, not silently no-op."""
    with pytest.raises(ValueError):
        temp_manager.create_job(
            name="j", prompt="hi", schedule="0 15 * * *",
            operator="system", delivery="sms",
        )


def test_voice_call_blank_target_raises(temp_manager):
    with pytest.raises(ValueError):
        temp_manager.create_job(
            name="j", prompt="hi", schedule="0 15 * * *",
            operator="system", delivery="voice_call", delivery_target="",
        )


def test_sms_with_non_e164_target_raises(temp_manager):
    with pytest.raises(ValueError):
        temp_manager.create_job(
            name="j", prompt="hi", schedule="0 15 * * *",
            operator="system", delivery="sms", delivery_target="555-1234",
        )


# ---------------------------------------------------------------------------
# update_job — partial validation
# ---------------------------------------------------------------------------

def test_partial_update_prompt_only_does_not_require_operator(temp_manager):
    job = temp_manager.create_job(
        name="j", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    # Updating just the prompt must NOT demand operator be re-supplied.
    updated = temp_manager.update_job(job["id"], prompt="new prompt")
    assert updated["prompt"] == "new prompt"


def test_partial_update_bad_status_raises(temp_manager):
    job = temp_manager.create_job(
        name="j", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    with pytest.raises(ValueError):
        temp_manager.update_job(job["id"], status="zombie")


def test_partial_update_blank_operator_raises(temp_manager):
    job = temp_manager.create_job(
        name="j", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    with pytest.raises(ValueError):
        temp_manager.update_job(job["id"], operator="  ")


def test_update_transition_to_sms_without_target_raises(temp_manager):
    """Transitioning an existing snapshot job to sms with no target on the
    job and none in the update must raise — this is the silent-non-delivery
    bug surfacing through update, not create."""
    job = temp_manager.create_job(
        name="j", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    with pytest.raises(ValueError):
        temp_manager.update_job(job["id"], delivery="sms")


def test_update_transition_to_sms_with_target_in_update_ok(temp_manager):
    job = temp_manager.create_job(
        name="j", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    updated = temp_manager.update_job(
        job["id"], delivery="sms", delivery_target="+15551234567"
    )
    assert updated["delivery"] == "sms"
    assert updated["delivery_target"] == "+15551234567"


def test_update_delivery_only_when_target_already_on_job_ok(temp_manager):
    """If the job already carries a valid target, flipping delivery to sms
    alone (no target in the update) is fine — the effective target resolves
    from the existing row."""
    job = temp_manager.create_job(
        name="j", prompt="hi", schedule="0 15 * * *", operator="system",
        delivery="notification", delivery_target="+15551234567",
    )
    updated = temp_manager.update_job(job["id"], delivery="sms")
    assert updated["delivery"] == "sms"


def test_update_target_only_to_invalid_on_sms_job_raises(temp_manager):
    """Changing ONLY the target (delivery already sms) to a non-E.164 value
    must raise — the effective delivery is still sms."""
    job = temp_manager.create_job(
        name="j", prompt="hi", schedule="0 15 * * *", operator="system",
        delivery="sms", delivery_target="+15551234567",
    )
    with pytest.raises(ValueError):
        temp_manager.update_job(job["id"], delivery_target="not-a-number")


def test_update_blank_target_on_sms_job_raises(temp_manager):
    """Blanking the target on an existing sms job must raise — you cannot
    update a deliverable job into the silent-non-delivery state."""
    job = temp_manager.create_job(
        name="j", prompt="hi", schedule="0 15 * * *", operator="system",
        delivery="sms", delivery_target="+15551234567",
    )
    with pytest.raises(ValueError):
        temp_manager.update_job(job["id"], delivery_target="")


# ---------------------------------------------------------------------------
# PUT route surfaces ValueError as HTTP 400 (not 500)
# ---------------------------------------------------------------------------

def test_put_route_returns_400_on_bad_value(tmp_path, monkeypatch):
    # Importing Orchestrator.app registers every route (incl. cron) onto the
    # shared app instance used by the TestClient.
    import Orchestrator.app  # noqa: F401 — registers routes onto the shared app
    from Orchestrator.checkpoint import app

    db = tmp_path / "cron_jobs_route_test.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    # Reset the singleton so the route's get_scheduler_manager() picks up the
    # patched DB_PATH instead of any previously-built instance.
    monkeypatch.setattr(manager_mod, "_manager_instance", None, raising=False)

    from Orchestrator.scheduler import get_scheduler_manager
    mgr = get_scheduler_manager()
    job = mgr.create_job(
        name="j", prompt="hi", schedule="0 15 * * *", operator="system"
    )

    client = TestClient(app)
    # 'delivery' is a forwarded CronJobUpdate field; a bad enum value must
    # surface as a 400 from the central sink, never a 500.
    resp = client.put(
        f"/api/cron/jobs/{job['id']}", json={"delivery": "carrier-pigeon"}
    )
    assert resp.status_code == 400, resp.text
