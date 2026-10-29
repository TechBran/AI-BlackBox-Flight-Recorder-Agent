"""A14-ui-misc-06: an edit names its contact by id, so a rename updates it in place.

POST /contacts upserted by name only, so renaming a contact in the Android or Portal
editor created a SECOND contact under the new name and left the old record (its phone,
inbound_allowed and is_operator_self flags included) in the book, while the app said
"Contact updated". The request now carries an optional ``id`` that, when it names a
contact in the operator's book, wins over the name match.

Hermetic: the contacts store is pointed at a tmp file, and the router runs in a bare
FastAPI app (no production lifespan).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import Orchestrator.contacts as contacts_mod
from Orchestrator.routes.contacts_routes import router as contacts_router

OP = "op-a"


@pytest.fixture
def isolated_contacts(tmp_path, monkeypatch):
    cdir = tmp_path / "Contacts"
    cdir.mkdir()
    monkeypatch.setattr(contacts_mod, "CONTACTS_DIR", cdir)
    monkeypatch.setattr(contacts_mod, "CONTACTS_FILE", cdir / "contacts.json")
    return cdir / "contacts.json"


@pytest.fixture
def client(isolated_contacts):
    app = FastAPI()
    app.include_router(contacts_router)
    return TestClient(app)


def _create(client, name, **fields):
    resp = client.post("/contacts", json={"operator": OP, "name": name, **fields})
    assert resp.status_code == 200, resp.text
    return resp.json()["contact"]


def _book():
    return contacts_mod.load_contacts().get(OP, {})


def test_rename_by_id_updates_the_record_in_place(client):
    original = _create(client, "Sam Carter", phone="+15555550101", inbound_allowed=True, is_operator_self=True)

    resp = client.post("/contacts", json={
        "operator": OP, "id": original["id"], "name": "Samantha Carter", "phone": "+15555550101",
    })
    assert resp.status_code == 200, resp.text
    renamed = resp.json()["contact"]

    assert renamed["id"] == original["id"], "the edit must update the contact it names"
    book = _book()
    assert len(book) == 1, f"a rename must not leave a duplicate: {[c['name'] for c in book.values()]}"
    kept = book[original["id"]]
    assert kept["name"] == "Samantha Carter"
    assert kept["is_operator_self"] is True, "the record's flags travel with the rename"
    assert kept["inbound_allowed"] is True


def test_rename_onto_another_contacts_name_is_refused(client):
    sam = _create(client, "Sam")
    _create(client, "Alex")

    resp = client.post("/contacts", json={"operator": OP, "id": sam["id"], "name": "alex"})

    assert resp.status_code == 409, resp.text
    names = sorted(c["name"] for c in _book().values())
    assert names == ["Alex", "Sam"], "nothing is changed by a refused rename"


def test_an_unknown_id_falls_back_to_the_name_match(client):
    resp = client.post("/contacts", json={"operator": OP, "id": "no-such-id", "name": "Jordan"})
    assert resp.status_code == 200, resp.text
    created = resp.json()["contact"]
    assert created["id"] != "no-such-id", "a client never chooses a new contact's id"
    assert [c["name"] for c in _book().values()] == ["Jordan"]


def test_an_id_from_another_operators_book_is_not_used(client):
    other = client.post("/contacts", json={"operator": "op-b", "name": "Pat"}).json()["contact"]

    resp = client.post("/contacts", json={"operator": OP, "id": other["id"], "name": "Pat Renamed"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["contact"]["id"] != other["id"]
    assert contacts_mod.load_contacts()["op-b"][other["id"]]["name"] == "Pat", "another book is never edited"


def test_without_an_id_the_name_match_still_updates(client):
    first = _create(client, "Riley", notes="first")
    again = _create(client, "riley", notes="second")
    assert again["id"] == first["id"]
    assert len(_book()) == 1


def test_upsert_contact_takes_the_id_as_a_keyword(isolated_contacts):
    # The model/voice save_contact callers pass no id and keep the name match.
    a = contacts_mod.upsert_contact(name="Casey", notes=None, tags=None, operator=OP, created_by=OP)
    b = contacts_mod.upsert_contact(name="Casey Jones", notes=None, tags=None, operator=OP, created_by=OP,
                                    match_id=a["id"])
    assert b["id"] == a["id"]
    assert [c["name"] for c in contacts_mod.load_contacts()[OP].values()] == ["Casey Jones"]
