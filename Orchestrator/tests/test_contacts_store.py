"""Contacts/contacts.json: one truncated write or two concurrent saves must
never wipe the contact books (audit O12-messaging-08)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from Orchestrator import contacts


@pytest.fixture
def book(isolated_stores, monkeypatch):
    monkeypatch.setattr(contacts, "system_numbers", lambda: [])
    return Path(contacts.CONTACTS_FILE)


def _names(path: Path, operator: str):
    return sorted(c["name"] for c in json.loads(path.read_text()).get(operator, {}).values())


def test_corrupt_book_is_quarantined_not_overwritten_by_the_next_save(book):
    contacts.upsert_contact("Dana", "", [], "Ann", "test", phone="+15550100")
    contacts.upsert_contact("Eli", "", [], "Ann", "test", phone="+15550101")
    torn = book.read_text()[:60]
    book.write_text(torn)

    contacts.upsert_contact("Fay", "", [], "Ann", "test", phone="+15550102")

    (moved,) = sorted(book.parent.glob("contacts.json.corrupt-*"))
    assert moved.read_text() == torn
    assert _names(book, "Ann") == ["Fay"]


def test_concurrent_upserts_keep_every_contact(book):
    def worker(n):
        for i in range(10):
            contacts.upsert_contact(f"c{n}-{i}", "", [], "Ann", "test")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(_names(book, "Ann")) == 50


def test_book_is_owner_only(book):
    contacts.upsert_contact("Dana", "", [], "Ann", "test")
    assert book.stat().st_mode & 0o077 == 0


def test_delete_route_keeps_concurrent_upserts(book, app_client):
    saved = contacts.upsert_contact("Dana", "", [], "Ann", "test")
    contacts.upsert_contact("Eli", "", [], "Ann", "test")
    r = app_client.delete(f"/contacts/{saved['id']}", params={"operator": "Ann"})
    assert r.status_code == 200
    assert _names(book, "Ann") == ["Eli"]
    assert app_client.delete("/contacts/nope", params={"operator": "Ann"}).status_code == 404
    assert _names(book, "Ann") == ["Eli"]


def test_readers_survive_a_corrupt_book(book):
    book.write_text("{nope")
    assert contacts.load_contacts() == {}
    assert contacts.search_contacts("x", "Nobody") == []
