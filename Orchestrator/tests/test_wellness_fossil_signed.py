"""The signed ledger read (M7 wave 3b, spec §6.1 "Chat": Show numbers on a
turn reloaded from the ledger).

GET /fossil/snapshot/{id} takes the enrolled phone's OPTIONAL X-BlackBox-Origin
proof. Signed by the operator's active phone (the native app, or the Portal
inside it through the app's bridge) the operator's own wellness turns read as
minted: masked with [·], the WELLNESS gauge and its refs intact, so the client
can offer Show numbers. Another operator's turn, a turn the gate hides
(consent off, kill switch) and an unsigned read stay hidden. A present but bad
proof is refused exactly as GET /wellness/status refuses it, never read
unsigned. An `operator` query must be the proof's. The read records no
disclosure and logs nothing of the snapshot.

Hermetic: the real app in-process (wellness_support.make_box, a FakePhone
enrolled through the real handshake) over a real ledger minted through
perform_mint (wellness_ledger_support).
"""
from __future__ import annotations

import logging
import time

import pytest

from Orchestrator.tests.wellness_ledger_support import ledger_box, mint_turn
from Orchestrator.tests.wellness_support import OPERATOR, close_box, make_box
from Orchestrator.wellness import enroll, origin_proof, redact

OTHER = "other-human"
GAUGE = {"categories": ["heart", "sleep"], "labels": ["night of 2026-10-22"],
         "refs": ["w-20261028-0a0a", "w-20261028-0b0b"]}
USER_WORDS = "how did I sleep? I felt awful and dizzy"
REPLY_WORDS = "You slept [·], a bit less than usual, and your heart was calmer."
PRIVATE = ("awful", "dizzy", "a bit less than usual", "calmer", "night of 2026-10-22")


@pytest.fixture
def box(isolated_stores, toolvault_embed_sync, monkeypatch):
    lb = ledger_box(isolated_stores.root, monkeypatch)
    b = make_box(isolated_stores, monkeypatch)
    assert b.enroll().status_code == 200
    b.tagged = mint_turn(OPERATOR, USER_WORDS, REPLY_WORDS, wellness=GAUGE)
    b.plain = mint_turn(OPERATOR, "draft the email to the team", "Here is the email draft.")
    b.other = mint_turn(OTHER, "how did I sleep? terrible", "You slept [·], awful night.",
                        wellness=GAUGE)
    b.ledger = lb
    yield b
    close_box(b)


def _read(box, snap_id, *, signed=False, query="", headers=None):
    return box.get(f"/fossil/snapshot/{snap_id}{query}", signed=signed, headers=headers)


def _private(text: str):
    return [w for w in PRIVATE if w in (text or "")]


def _store():
    return enroll.store_for(enroll.get_enrollment(OPERATOR), create=False)


# ------------------------------------------------------------------ the owner's phone

def test_the_owner_s_signed_read_is_the_turn_as_minted(box):
    r = _read(box, box.tagged, signed=True)
    assert r.status_code == 200, r.text
    content = r.json()["content"]
    tag = redact.parse_tag(content)
    assert tag is not None and tag.refs == tuple(GAUGE["refs"])
    assert tag.line.startswith("WELLNESS: heart,sleep · night of 2026-10-22 · ref ")
    assert tag.line.endswith(" · values not recorded")
    assert "[·]" in content and redact.PLACEHOLDER not in content
    assert USER_WORDS in content and "a bit less than usual" in content
    assert r.json()["snap_id"] == box.tagged


def test_the_bridge_s_operator_query_must_be_the_proof_s(box):
    """The in-app Portal names the operator on every GET (the bridge's rule);
    the query is not signed, so the box checks it against the proof."""
    ok = _read(box, box.tagged, signed=True, query=f"?operator={OPERATOR}")
    assert ok.status_code == 200 and redact.parse_tag(ok.json()["content"]) is not None
    for query in (f"?operator={OTHER}", f"?operator={OPERATOR}&operator={OTHER}"):
        bad = _read(box, box.tagged, signed=True, query=query)
        assert bad.status_code == 400 and bad.json() == {"error": "operator_mismatch"}
    # Without a proof there is nothing to compare: the read is the unsigned one.
    unsigned = _read(box, box.tagged, query=f"?operator={OTHER}")
    assert unsigned.status_code == 200 and not _private(unsigned.json()["content"])


# ------------------------------------------------------------------ what stays hidden

def test_another_operator_s_turn_stays_hidden_to_the_owner_s_phone(box):
    content = _read(box, box.other, signed=True).json()["content"]
    assert redact.PLACEHOLDER in content and redact.parse_tag(content) is None
    assert "terrible" not in content and not _private(content)


def test_an_unsigned_read_is_what_it_was(box):
    content = _read(box, box.tagged).json()["content"]
    assert redact.PLACEHOLDER in content and redact.parse_tag(content) is None
    assert not _private(content) and "w-20261028-0a0a" not in content
    # An untagged snapshot is byte-identical either way.
    plain = _read(box, box.plain).json()
    assert _read(box, box.plain, signed=True).json() == plain
    assert "Here is the email draft." in plain["content"]


def test_a_category_turned_off_or_the_kill_switch_hides_the_turn(box, monkeypatch):
    with enroll.editing() as data:
        data[OPERATOR]["categories"]["sleep"] = "off"
    hidden = _read(box, box.tagged, signed=True).json()["content"]
    assert redact.PLACEHOLDER in hidden and not _private(hidden)
    with enroll.editing() as data:
        data[OPERATOR]["categories"]["sleep"] = "ask"
    assert redact.parse_tag(_read(box, box.tagged, signed=True).json()["content"])
    from Orchestrator import config
    monkeypatch.setattr(config, "wellness_enabled", lambda: False)
    off = _read(box, box.tagged, signed=True)
    assert off.status_code == 200 and not _private(off.json()["content"])


def test_a_paused_enrollment_reads_from_stored_data_like_the_tools(box):
    """Pause stops collection; the gate still answers from stored data
    (spec §4.1 check 3), so the person's own masked turns stay readable on
    their phone, as Show numbers (GET /wellness/ref) does."""
    assert box.post("/wellness/pause", {"operator": OPERATOR}).status_code == 200
    assert enroll.get_enrollment(OPERATOR)["state"] == "paused"
    content = _read(box, box.tagged, signed=True).json()["content"]
    assert redact.parse_tag(content) is not None and "awful" in content


# ------------------------------------------------------------------ a bad proof

def test_a_bad_or_stale_proof_is_refused_as_the_status_refuses_it(box):
    path = f"/fossil/snapshot/{box.tagged}"
    forged = origin_proof.signed_headers(bytes(32), box.phone.enrollment_id, "GET", path)
    key = origin_proof.decode_key(box.phone.key)
    stale = origin_proof.signed_headers(key, box.phone.enrollment_id, "GET", path,
                                        now=int(time.time()) - 300)
    garbage = {origin_proof.HEADER: "v1;enr=en_00000000;ts=1;nonce=" + "a" * 32 + ";sig=" +
               "0" * 64}
    other_path = box.phone.headers("GET", f"/fossil/snapshot/{box.plain}")
    for headers in (forged, stale, garbage, other_path):
        r = _read(box, box.tagged, headers=headers)
        status = box.get(f"/wellness/status?operator={OPERATOR}", headers=headers)
        assert r.status_code == status.status_code == 401
        assert r.json() == status.json() == {"error": "bad_signature"}
    good = box.phone.headers("GET", path)
    assert _read(box, box.tagged, headers=good).status_code == 200
    replay = _read(box, box.tagged, headers=good)
    assert replay.status_code == 401 and replay.json() == {"error": "bad_signature"}


def test_a_revoked_key_is_410_and_a_rehomed_phone_409(box):
    from Orchestrator.device_registry import get_registry
    get_registry().update_device("fixture-phone", owner=OTHER)
    r = _read(box, box.tagged, signed=True)
    assert r.status_code == 409 and r.json() == {"error": "owner_changed"}
    get_registry().update_device("fixture-phone", owner=OPERATOR)
    key, enr = box.phone.key, box.phone.enrollment_id
    assert box.post("/wellness/disconnect", {"operator": OPERATOR,
                                             "keep_history": True}).status_code == 200
    box.phone.key, box.phone.enrollment_id = key, enr       # a phone that missed the unbind
    r = _read(box, box.tagged, signed=True)
    assert r.status_code == 410 and r.json() == {"error": "revoked"}


def test_a_replacement_awaiting_approval_reads_hidden(box):
    from Orchestrator.device_registry import pairing
    from Orchestrator.tests.wellness_support import FakePhone, add_device
    new = FakePhone()
    add_device("fixture-phone-2", "100.100.7.9", OPERATOR)
    pairing.register("fixture-phone-2", "100.100.7.9", new.secret, "Phone two", "phone")
    box.ports["100.100.7.9"] = new.port
    old, box.phone = box.phone, new
    try:
        assert box.enroll(ip="100.100.7.9", replace=True).json()["state"] == "replace_pending"
        r = _read(box, box.tagged, signed=True)
        assert r.status_code == 200 and not _private(r.json()["content"])
    finally:
        box.phone = old
        new.close()


# ------------------------------------------------------------------ hygiene

def test_a_signed_read_records_nothing_and_logs_nothing_of_the_turn(box, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    before = _store().disclosures_count()
    for query in ("", f"?operator={OPERATOR}"):
        assert _read(box, box.tagged, signed=True, query=query).status_code == 200
    assert _read(box, box.other, signed=True).status_code == 200
    assert _store().disclosures_count() == before
    out = capsys.readouterr()
    printed = out.out + out.err + "\n".join(r.getMessage() for r in caplog.records)
    assert not _private(printed) and "terrible" not in printed
    assert "w-20261028-0a0a" not in printed


def test_the_route_still_reads_in_process_without_a_request(box):
    """A direct call (no request: an in-process reader) reads as nobody."""
    from Orchestrator.routes import task_routes
    content = task_routes.fossil_get_snapshot(box.tagged)["content"]
    assert redact.PLACEHOLDER in content and not _private(content)
