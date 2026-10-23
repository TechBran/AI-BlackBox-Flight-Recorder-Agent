"""A book's system contact comes from this box's telephony config, never a vendor seed.

X03-security-backend-18 / O12-07: every new operator book on every box was seeded
with the vendor's own phone line and email as the "self / caller identity"
contact (later a 555-01xx placeholder). Now:

  * a new book gets a system contact ONLY when the box has a line configured
    (cellular gateway ports, TG200_PHONE_NUMBER, ASTERISK_TRUNKS phones, the
    cellular modem's number, TWILIO_PHONE_NUMBER), and that contact can never
    whitelist an inbound SMS;
  * a read (GET /contacts, /contacts/search, /api/cron/contacts, a contact
    search tool call) never saves a book for a name that is not on the roster
    (O15-11, merged into O12-07), and the cron picker never offers the box's
    own line as a delivery target;
  * a startup migration strips the seeded records (exact match only, never a
    user-edited one) from existing books, with a backup and an atomic write;
  * the SMS router gates the box's own lines and the retired seed numbers.

The vendor's number never appears here: retired seeds are matched by SHA-256 and
the tests stand a fixture number in for it.
"""
import hashlib
import json
import os
import stat

import pytest

import Orchestrator.contacts as contacts

PLACEHOLDER_SEED = {
    "name": "AI BlackBox Flight Recorder",
    "phone": "+15555550100",
    "email": "",
    "relationship": "self",
    "notes": "Placeholder for this BlackBox's own phone number. Edit it to the box's real line to use it as the caller identity.",
    "tags": ["system", "self"],
    "created_by": "system",
}
LEGACY_NOTES = "This is your own phone number. The AI BlackBox system number. Use this as the caller identity."
# Stands in for the vendor line/email (the real ones are only known by digest).
FIXTURE_PHONE = "+15555550177"
FIXTURE_EMAIL = "seed-owner@example.invalid"
# SHA-256 of the last 10 digits of the line the earliest seed shipped.
LEGACY_SEED_PHONE_SHA256 = "6cbf6b02a2a8d8b079a48e45f81d6545cb54d489c8aada40c0a3090264c621a5"


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _seed_record(cid, base=PLACEHOLDER_SEED, ts="2026-03-01T00:00:00+00:00", materialized=True, **over):
    rec = {"id": cid, **base, "created_at": ts, "updated_at": ts}
    if materialized:  # a later save_contacts() writes the read-time flag defaults
        rec.update({"inbound_allowed": True, "is_operator_self": False})
    rec.update(over)
    return rec


@pytest.fixture
def no_lines(monkeypatch):
    monkeypatch.setattr(contacts, "system_numbers", lambda: [])


@pytest.fixture
def lines(monkeypatch):
    configured = [("+15555550140", "Alice"), ("+15555550141", None), ("+15555550142", "Carol")]
    monkeypatch.setattr(contacts, "system_numbers", lambda: list(configured))
    return configured


@pytest.fixture
def legacy_variant(monkeypatch):
    """Swap the vendor variant's digests for the fixture's."""
    variants = [v for v in contacts._RETIRED_SEED_VARIANTS
                if v[0] != LEGACY_SEED_PHONE_SHA256]
    variants.append((_sha(FIXTURE_PHONE[-10:]), _sha(FIXTURE_EMAIL), LEGACY_NOTES))
    monkeypatch.setattr(contacts, "_RETIRED_SEED_VARIANTS", tuple(variants))
    monkeypatch.setattr(contacts, "RETIRED_SEED_PHONE_SHA256",
                        frozenset(v[0] for v in variants))
    return {**PLACEHOLDER_SEED, "phone": FIXTURE_PHONE, "email": FIXTURE_EMAIL, "notes": LEGACY_NOTES}


# ── 1. the vendor seed is gone ───────────────────────────────────────────────

def test_seed_constants_are_deleted():
    import Orchestrator.sms.router as router
    assert not hasattr(contacts, "SEED_CONTACT")
    assert not hasattr(router, "SEED_PHONE")
    assert not hasattr(router, "_LEGACY_SEED_PHONE_SHA256")


def test_both_retired_seed_numbers_stay_gated_by_digest():
    assert LEGACY_SEED_PHONE_SHA256 in contacts.RETIRED_SEED_PHONE_SHA256
    assert _sha("5555550100") in contacts.RETIRED_SEED_PHONE_SHA256
    assert {v[0] for v in contacts._RETIRED_SEED_VARIANTS} == set(contacts.RETIRED_SEED_PHONE_SHA256)


def test_fresh_box_book_has_no_seed(no_lines):
    data = {}
    assert contacts.ensure_operator_book(data, "Alice") is True
    assert data == {"Alice": {}}
    assert contacts.ensure_operator_book(data, "Alice") is False


# ── 2. the system contact is built from the configured lines ─────────────────

def test_book_gets_the_line_its_operator_owns(lines):
    data = {}
    contacts.ensure_operator_book(data, "Alice")
    (seed,) = data["Alice"].values()
    assert seed["phone"] == "+15555550140"
    # "system", not "self": "text me" must reach the operator, not the box.
    assert seed["relationship"] == "system" and seed["tags"] == ["system"]
    assert seed["created_by"] == "system"
    assert seed["email"] == ""
    assert seed["inbound_allowed"] is False   # the box's own line never texts in
    assert seed["is_operator_self"] is False
    assert seed["id"] in data["Alice"]


def test_book_without_an_owned_line_gets_the_shared_line(lines):
    data = {}
    contacts.ensure_operator_book(data, "Bob")
    (seed,) = data["Bob"].values()
    assert seed["phone"] == "+15555550141"


def test_no_shared_line_means_no_seed(monkeypatch):
    monkeypatch.setattr(contacts, "system_numbers", lambda: [("+15555550142", "Carol")])
    data = {}
    contacts.ensure_operator_book(data, "Bob")
    assert data == {"Bob": {}}


def test_system_numbers_reads_every_configured_line_in_order(tmp_path, monkeypatch):
    import Orchestrator.asterisk.config as acfg
    import Orchestrator.config as cfg

    gw = tmp_path / "gateways.json"
    gw.write_text(json.dumps([
        {"id": "g1", "ports": [
            {"span": 2, "phone_number": "+1 (555) 555-0150", "operator": "Alice"},
            {"span": 3, "phone_number": "", "operator": ""},
            {"span": 4, "phone_number": "+15555550151", "operator": ""},
        ]},
        {"id": "legacy", "phone_numbers": ["+15555550152"]},   # pre-v2 record
        "junk",
    ]))
    monkeypatch.setattr(contacts, "_gateways_file", lambda: gw)
    monkeypatch.setattr(acfg, "TG200_PHONE_NUMBER", "+15555550153")
    monkeypatch.setattr(acfg, "ASTERISK_TRUNKS", [
        {"name": "tg200-1", "phone": "+15555550154"}, {"name": "no-line"}, "junk",
        {"name": "dup", "phone": "+15555550153"},
    ])
    monkeypatch.setattr(cfg, "CELLULAR_ENABLED", True)
    monkeypatch.setattr(cfg, "CELLULAR_PHONE_NUMBER", "+15555550155")
    monkeypatch.setattr(cfg, "TWILIO_PHONE_NUMBER", "5555550151")  # duplicate of a port
    assert contacts.system_numbers() == [
        ("+1 (555) 555-0150", "Alice"),
        ("+15555550151", None),
        ("+15555550152", None),
        ("+15555550153", None),
        ("+15555550154", None),
        ("+15555550155", None),
    ]
    # The modem's number counts only while the modem is enabled.
    monkeypatch.setattr(cfg, "CELLULAR_ENABLED", False)
    assert ("+15555550155", None) not in contacts.system_numbers()


def test_a_box_with_only_the_cellular_modem_gets_a_system_contact(tmp_path, monkeypatch):
    import Orchestrator.asterisk.config as acfg
    import Orchestrator.config as cfg

    monkeypatch.setattr(contacts, "_gateways_file", lambda: tmp_path / "absent.json")
    monkeypatch.setattr(acfg, "TG200_PHONE_NUMBER", "")
    monkeypatch.setattr(acfg, "ASTERISK_TRUNKS", [])
    monkeypatch.setattr(cfg, "TWILIO_PHONE_NUMBER", "")
    monkeypatch.setattr(cfg, "CELLULAR_ENABLED", True)
    monkeypatch.setattr(cfg, "CELLULAR_PHONE_NUMBER", "+15555550156")
    data = {}
    contacts.ensure_operator_book(data, "Alice")
    (seed,) = data["Alice"].values()
    assert seed["phone"] == "+15555550156" and seed["inbound_allowed"] is False
    assert contacts.is_system_number("555-555-0156")


def test_system_numbers_on_a_fresh_box_is_empty(tmp_path, monkeypatch):
    import Orchestrator.asterisk.config as acfg
    import Orchestrator.config as cfg

    monkeypatch.setattr(contacts, "_gateways_file", lambda: tmp_path / "absent.json")
    monkeypatch.setattr(acfg, "TG200_PHONE_NUMBER", "")
    monkeypatch.setattr(acfg, "ASTERISK_TRUNKS", [])
    monkeypatch.setattr(cfg, "CELLULAR_ENABLED", False)
    monkeypatch.setattr(cfg, "CELLULAR_PHONE_NUMBER", "+15555550156")   # modem off
    monkeypatch.setattr(cfg, "TWILIO_PHONE_NUMBER", "")
    assert contacts.system_numbers() == []
    (tmp_path / "bad.json").write_text("{not json")
    monkeypatch.setattr(contacts, "_gateways_file", lambda: tmp_path / "bad.json")
    assert contacts.system_numbers() == []


def test_is_system_number(lines):
    assert contacts.is_system_number("+1 555 555 0141")
    assert contacts.is_system_number("+15555550100")       # retired placeholder seed
    assert not contacts.is_system_number("+15555550199")
    assert not contacts.is_system_number("")
    assert contacts.is_system_number("+15555550199", numbers=["5555550199"])


# ── 2b. reads never create a book for an arbitrary name ──────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    """An empty contacts store in tmp_path, on a box whose roster is Ann."""
    import Orchestrator.config as cfg
    cdir = tmp_path / "Contacts"
    cdir.mkdir()
    cfile = cdir / "contacts.json"
    cfile.write_text("{}")
    monkeypatch.setattr(contacts, "CONTACTS_DIR", cdir)
    monkeypatch.setattr(contacts, "CONTACTS_FILE", cfile)
    monkeypatch.setattr(cfg, "USERS_LIST", ["Ann"])
    return cfile


def _books(cfile):
    return set(json.loads(cfile.read_text()))


def test_reads_never_save_a_book_for_a_name_off_the_roster(store, lines):
    import asyncio
    from Orchestrator.routes.contacts_routes import list_contacts, search_contacts_endpoint
    from Orchestrator.routes.cron_routes import list_cron_contacts

    out = asyncio.run(list_contacts(operator="made-up"))
    assert out["operator"] == "made-up"
    asyncio.run(search_contacts_endpoint(operator="also-made-up", query="x"))
    asyncio.run(list_cron_contacts(operator="third-made-up"))
    contacts.search_contacts("x", "tool-call-made-up")   # the search_contacts tool
    assert _books(store) == set()


def test_a_read_still_seeds_a_roster_operators_book_once(store, lines):
    import asyncio
    from Orchestrator.routes.contacts_routes import list_contacts

    first = asyncio.run(list_contacts(operator="Ann"))
    assert _books(store) == {"Ann"}
    again = asyncio.run(list_contacts(operator="Ann"))
    assert [c["id"] for c in first["contacts"]] == [c["id"] for c in again["contacts"]]
    assert [c["phone"] for c in again["contacts"]] == ["+15555550141"]   # the shared line


def test_cron_picker_never_offers_the_boxs_own_line(store, lines):
    import asyncio
    from Orchestrator.routes.cron_routes import list_cron_contacts

    contacts.upsert_contact(name="Pat", notes=None, tags=None, operator="Ann",
                            created_by="Ann", phone="+15555550160")
    # A contact the operator saved by hand for a box line is no target either.
    contacts.upsert_contact(name="Our line", notes=None, tags=None, operator="Ann",
                            created_by="Ann", phone="+1 555 555 0140")
    out = asyncio.run(list_cron_contacts(operator="Ann"))
    assert [c["name"] for c in out["contacts"]] == ["Pat"]


# ── 3. the SMS router gates the box's own lines ──────────────────────────────

class _Mgr:
    def __init__(self, owner="Alice"):
        self._gw = {"g1": {"id": "g1", "ports": [
            {"span": 2, "phone_number": "+15555550140", "operator": owner},
            {"span": 3, "phone_number": "+15555550141", "operator": ""},
        ]}}

    def set_sms_callback(self, cb):
        pass

    def gateways(self):
        return dict(self._gw)


def _router(monkeypatch, books):
    import copy
    import Orchestrator.config as cfg
    from Orchestrator.sms.router import SMSRouter
    monkeypatch.setattr(contacts, "load_contacts",
                        lambda: contacts._apply_inbound_defaults(copy.deepcopy(books)))
    monkeypatch.setattr(cfg, "USERS_LIST", list(books))
    return SMSRouter(_Mgr(), None)


@pytest.mark.parametrize("owner", ["Alice", None])
def test_a_contact_holding_a_box_line_never_whitelists(monkeypatch, lines, owner):
    # Even a record the operator flagged inbound_allowed AND operator-self.
    books = {"Alice": {"c1": {"name": "Our line", "phone": "+15555550141",
                              "inbound_allowed": True, "is_operator_self": True,
                              "created_by": "Alice", "relationship": "self"}}}
    r = _router(monkeypatch, books)
    assert r.resolve_inbound("+15555550141", owner=owner).operator is None
    assert r._find_operator_by_phone("+15555550141") == (None, None)


def test_a_real_contact_still_routes(monkeypatch, lines):
    books = {"Alice": {"c1": {"name": "Pat", "phone": "+15555550160",
                              "inbound_allowed": True, "created_by": "Alice"}}}
    r = _router(monkeypatch, books)
    res = r.resolve_inbound("+15555550160", owner=None)
    assert (res.operator, res.classification) == ("Alice", "peer")


def test_router_reads_the_lines_once_per_resolution(monkeypatch):
    calls = []
    monkeypatch.setattr(contacts, "system_numbers", lambda: calls.append(1) or [])
    books = {"Alice": {f"c{i}": {"name": f"P{i}", "phone": f"+1555555016{i}",
                                 "inbound_allowed": True} for i in range(5)}}
    r = _router(monkeypatch, books)
    r.resolve_inbound("+15555550199", owner=None)
    assert len(calls) == 1


# ── 4. the migration strips exactly the seeded records ───────────────────────

def _write(path, data, mode=0o600):
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, mode)


def test_migration_strips_placeholder_and_legacy_seeds(tmp_path, no_lines, legacy_variant):
    path = tmp_path / "contacts.json"
    books = {
        "Alice": {"s1": _seed_record("s1"),
                  "c1": {"id": "c1", "name": "Pat", "phone": "+15555550160"}},
        "Bob": {"s2": _seed_record("s2", base=legacy_variant)},
        "Carol": {"s3": _seed_record("s3", materialized=False)},   # never re-saved
        "Dave": {},
    }
    _write(path, books)
    original = path.read_bytes()

    assert contacts.strip_retired_seed_contacts(path) == 3
    after = json.loads(path.read_text())
    assert after == {"Alice": {"c1": books["Alice"]["c1"]}, "Bob": {}, "Carol": {}, "Dave": {}}
    # Backup of the exact original bytes, next to the file, same mode.
    (backup,) = tmp_path.glob("contacts.json.bak-seed-*")
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["contacts.json", backup.name])


@pytest.mark.parametrize("edit", [
    {"notes": "my edit"},
    {"updated_at": "2026-04-01T00:00:00+00:00"},          # re-saved through upsert
    {"is_operator_self": True},
    {"inbound_allowed": False},
    {"tags": ["system", "self", "mine"]},
    {"relationship": "system"},
    {"name": "AI BlackBox"},
    {"phone": "+15555550161"},
    {"email": "me@example.invalid"},
    {"created_by": "Alice"},
    {"birthday": "1990-01-01"},                            # a field we never seeded
    {"id": "other"},                                       # id no longer its key
])
def test_migration_keeps_user_edited_records(tmp_path, no_lines, legacy_variant, edit):
    path = tmp_path / "contacts.json"
    _write(path, {"Alice": {"s1": _seed_record("s1", **edit)},
                  "Bob": {"s2": _seed_record("s2", base=legacy_variant, **edit)}})
    before = path.read_bytes()
    assert contacts.strip_retired_seed_contacts(path) == 0
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.bak-seed-*"))


def test_legacy_number_with_other_metadata_is_a_user_contact(tmp_path, no_lines, legacy_variant):
    # A contact the user created for the old system line (different notes and
    # relationship) is theirs, even though the number matches.
    path = tmp_path / "contacts.json"
    _write(path, {"Alice": {"c9": {**_seed_record("c9", base=legacy_variant),
                                   "relationship": "system", "tags": ["system"],
                                   "notes": "The system phone number."}}})
    assert contacts.strip_retired_seed_contacts(path) == 0


def test_migration_is_idempotent(tmp_path, no_lines):
    path = tmp_path / "contacts.json"
    _write(path, {"Alice": {"s1": _seed_record("s1")}})
    assert contacts.strip_retired_seed_contacts(path) == 1
    once = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    assert contacts.strip_retired_seed_contacts(path) == 0
    assert path.read_bytes() == once and path.stat().st_mtime_ns == mtime
    assert len(list(tmp_path.glob("*.bak-seed-*"))) == 1


def test_migration_replaces_a_stripped_seed_with_the_configured_line(tmp_path, lines):
    path = tmp_path / "contacts.json"
    _write(path, {"Alice": {"s1": _seed_record("s1")},
                  "Bob": {"s2": _seed_record("s2"),
                          "c1": {"id": "c1", "name": "Shared", "phone": "+15555550141"}},
                  "Dave": {"c2": {"id": "c2", "name": "Pat", "phone": "+15555550160"}}})
    assert contacts.strip_retired_seed_contacts(path) == 2
    after = json.loads(path.read_text())
    (alice,) = after["Alice"].values()
    assert alice["phone"] == "+15555550140" and alice["inbound_allowed"] is False
    # Bob already holds the shared line: no duplicate added.
    assert list(after["Bob"]) == ["c1"]
    # A book that never had a seed is left alone.
    assert list(after["Dave"]) == ["c2"]
    # The replacement is not itself a retired seed: a second run is a no-op.
    assert contacts.strip_retired_seed_contacts(path) == 0


@pytest.mark.parametrize("content", ["{not json", "[]", ""])
def test_migration_never_writes_over_an_unreadable_store(tmp_path, no_lines, content):
    path = tmp_path / "contacts.json"
    path.write_text(content)
    assert contacts.strip_retired_seed_contacts(path) == 0
    assert path.read_text() == content
    assert sorted(p.name for p in tmp_path.iterdir()) == ["contacts.json"]


def test_migration_on_a_fresh_box_creates_nothing(tmp_path, no_lines):
    assert contacts.strip_retired_seed_contacts(tmp_path / "contacts.json") == 0
    assert list(tmp_path.iterdir()) == []


def test_startup_runs_the_migration(monkeypatch):
    import Orchestrator.startup as startup
    seen = []
    monkeypatch.setattr(contacts, "strip_retired_seed_contacts",
                        lambda path=None: seen.append(path) or 2)
    startup.startup_strip_retired_seed_contacts()
    assert seen == [contacts.CONTACTS_FILE]

    def boom(path=None):
        raise OSError("disk gone")

    monkeypatch.setattr(contacts, "strip_retired_seed_contacts", boom)
    startup.startup_strip_retired_seed_contacts()   # never blocks boot
