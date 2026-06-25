"""M1 — Contact data model: two flags + write-time identity guard.

Covers:
- 1.1 `inbound_allowed` / `is_operator_self` persist through upsert + round-trip,
  and a POST /contacts persists them.
- 1.2 Migration default: a legacy record (no `inbound_allowed` key) reads back
  `inbound_allowed=True` (preserve "in book ⇒ can text in"); `is_operator_self`
  defaults False. Read-time default — the on-disk file is NOT rewritten.
- 1.3 Write-time identity guard: setting `is_operator_self=True` for a number
  already self-flagged by ANOTHER operator returns a `warning` (and still saves);
  unique → no warning.
- 1.4 Second-endpoint parity: GET /api/cron/contacts carries both flags.

Hermetic: every test monkeypatches contacts.CONTACTS_DIR / CONTACTS_FILE to a
tmp path so the real Contacts/contacts.json is never touched.
"""
import json

import pytest

import Orchestrator.contacts as contacts_mod


@pytest.fixture
def isolated_contacts(tmp_path, monkeypatch):
    """Point the contacts store at a throwaway tmp file."""
    cdir = tmp_path / "Contacts"
    cdir.mkdir()
    cfile = cdir / "contacts.json"
    monkeypatch.setattr(contacts_mod, "CONTACTS_DIR", cdir)
    monkeypatch.setattr(contacts_mod, "CONTACTS_FILE", cfile)
    return cfile


# ---------------------------------------------------------------------------
# 1.1 — flags persist through upsert + load_contacts round-trip
# ---------------------------------------------------------------------------
def test_upsert_stores_both_flags(isolated_contacts):
    contact = contacts_mod.upsert_contact(
        name="Anna",
        notes="",
        tags=[],
        operator="Brandon",
        created_by="Brandon",
        phone="+14108166914",
        inbound_allowed=True,
        is_operator_self=True,
    )
    assert contact["inbound_allowed"] is True
    assert contact["is_operator_self"] is True


def test_load_contacts_round_trips_flags(isolated_contacts):
    contacts_mod.upsert_contact(
        name="Anna",
        notes="",
        tags=[],
        operator="Brandon",
        created_by="Brandon",
        phone="+14108166914",
        inbound_allowed=True,
        is_operator_self=True,
    )
    data = contacts_mod.load_contacts()
    saved = next(c for c in data["Brandon"].values() if c["name"] == "Anna")
    assert saved["inbound_allowed"] is True
    assert saved["is_operator_self"] is True


def test_upsert_flags_default_false(isolated_contacts):
    contact = contacts_mod.upsert_contact(
        name="Bob",
        notes="",
        tags=[],
        operator="Brandon",
        created_by="Brandon",
        phone="+15551112222",
    )
    assert contact["inbound_allowed"] is False
    assert contact["is_operator_self"] is False


def test_post_contacts_persists_flags(isolated_contacts):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from Orchestrator.routes.contacts_routes import router as contacts_router

    app = FastAPI()
    app.include_router(contacts_router)
    client = TestClient(app)

    resp = client.post(
        "/contacts",
        json={
            "operator": "Brandon",
            "name": "Anna",
            "phone": "+14108166914",
            "inbound_allowed": True,
            "is_operator_self": True,
        },
    )
    assert resp.status_code == 200
    contact = resp.json()["contact"]
    assert contact["inbound_allowed"] is True
    assert contact["is_operator_self"] is True

    # Persisted on disk + readable.
    data = contacts_mod.load_contacts()
    saved = next(c for c in data["Brandon"].values() if c["name"] == "Anna")
    assert saved["inbound_allowed"] is True
    assert saved["is_operator_self"] is True


# ---------------------------------------------------------------------------
# 1.2 — migration default (read-time, non-destructive)
# ---------------------------------------------------------------------------
def test_legacy_record_reads_inbound_allowed_true(isolated_contacts):
    """A legacy contact with no inbound_allowed key reads back True."""
    legacy = {
        "Brandon": {
            "c1": {
                "id": "c1",
                "name": "Legacy Bob",
                "phone": "+15551112222",
                "created_by": "Brandon",
            }
        }
    }
    isolated_contacts.write_text(json.dumps(legacy))

    data = contacts_mod.load_contacts()
    bob = data["Brandon"]["c1"]
    assert bob["inbound_allowed"] is True
    assert bob["is_operator_self"] is False


def test_legacy_default_does_not_rewrite_file(isolated_contacts):
    """The read-time default must NOT mutate the on-disk legacy record."""
    legacy = {
        "Brandon": {
            "c1": {
                "id": "c1",
                "name": "Legacy Bob",
                "phone": "+15551112222",
                "created_by": "Brandon",
            }
        }
    }
    isolated_contacts.write_text(json.dumps(legacy))

    contacts_mod.load_contacts()  # apply the read-time default

    on_disk = json.loads(isolated_contacts.read_text())
    assert "inbound_allowed" not in on_disk["Brandon"]["c1"]
    assert "is_operator_self" not in on_disk["Brandon"]["c1"]


def test_explicit_inbound_false_is_preserved(isolated_contacts):
    """An explicit inbound_allowed=False must NOT be coerced to True."""
    rec = {
        "Brandon": {
            "c1": {
                "id": "c1",
                "name": "Blocked Bob",
                "phone": "+15551112222",
                "inbound_allowed": False,
            }
        }
    }
    isolated_contacts.write_text(json.dumps(rec))

    data = contacts_mod.load_contacts()
    assert data["Brandon"]["c1"]["inbound_allowed"] is False


# ---------------------------------------------------------------------------
# 1.3 — write-time identity guard
# ---------------------------------------------------------------------------
def test_self_flag_collision_returns_warning(isolated_contacts):
    """Same number self-flagged by a DIFFERENT operator → warning, still saves."""
    contacts_mod.upsert_contact(
        name="Anna",
        notes="",
        tags=[],
        operator="Anna",
        created_by="Anna",
        phone="+14108166914",
        is_operator_self=True,
    )

    result = contacts_mod.upsert_contact(
        name="Anna (mine)",
        notes="",
        tags=[],
        operator="Brandon",
        created_by="Brandon",
        phone="+1 410 816 6914",  # same last-10 digits
        is_operator_self=True,
    )

    assert result.get("warning")
    assert "Anna" in result["warning"]
    # Still saved.
    data = contacts_mod.load_contacts()
    saved = next(c for c in data["Brandon"].values() if c["name"] == "Anna (mine)")
    assert saved["is_operator_self"] is True


def test_self_flag_unique_no_warning(isolated_contacts):
    """A unique self-flag returns no warning."""
    result = contacts_mod.upsert_contact(
        name="Brandon",
        notes="",
        tags=[],
        operator="Brandon",
        created_by="Brandon",
        phone="+17165551234",
        is_operator_self=True,
    )
    assert not result.get("warning")


def test_self_flag_same_operator_no_warning(isolated_contacts):
    """Re-flagging the same number in the SAME operator's book → no warning."""
    contacts_mod.upsert_contact(
        name="Brandon",
        notes="",
        tags=[],
        operator="Brandon",
        created_by="Brandon",
        phone="+17165551234",
        is_operator_self=True,
    )
    result = contacts_mod.upsert_contact(
        name="Brandon",
        notes="updated",
        tags=[],
        operator="Brandon",
        created_by="Brandon",
        phone="+17165551234",
        is_operator_self=True,
    )
    assert not result.get("warning")


def test_no_warning_when_not_self_flagged(isolated_contacts):
    """A plain inbound_allowed contact (not self) never triggers the guard."""
    contacts_mod.upsert_contact(
        name="Anna",
        notes="",
        tags=[],
        operator="Anna",
        created_by="Anna",
        phone="+14108166914",
        is_operator_self=True,
    )
    result = contacts_mod.upsert_contact(
        name="Anna",
        notes="",
        tags=[],
        operator="Brandon",
        created_by="Brandon",
        phone="+14108166914",
        inbound_allowed=True,
        is_operator_self=False,
    )
    assert not result.get("warning")


# ---------------------------------------------------------------------------
# 1.4 — second-endpoint parity: GET /api/cron/contacts
# ---------------------------------------------------------------------------
def test_cron_contacts_includes_both_flags(isolated_contacts):
    contacts_mod.upsert_contact(
        name="Anna",
        notes="",
        tags=[],
        operator="Brandon",
        created_by="Brandon",
        phone="+14108166914",
        inbound_allowed=True,
        is_operator_self=True,
    )

    from Orchestrator.routes.cron_routes import list_cron_contacts
    import asyncio

    result = asyncio.run(list_cron_contacts(operator="Brandon"))
    anna = next(c for c in result["contacts"] if c["name"] == "Anna")
    assert anna["inbound_allowed"] is True
    assert anna["is_operator_self"] is True


def test_cron_contacts_legacy_defaults(isolated_contacts):
    """Cron projection reflects the migration default for legacy records."""
    legacy = {
        "Brandon": {
            "c1": {
                "id": "c1",
                "name": "Legacy Bob",
                "phone": "+15551112222",
            }
        }
    }
    isolated_contacts.write_text(json.dumps(legacy))

    from Orchestrator.routes.cron_routes import list_cron_contacts
    import asyncio

    result = asyncio.run(list_cron_contacts(operator="Brandon"))
    bob = next(c for c in result["contacts"] if c["name"] == "Legacy Bob")
    assert bob["inbound_allowed"] is True
    assert bob["is_operator_self"] is False
