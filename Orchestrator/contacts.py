"""
contacts.py - Contact Book for AI BlackBox Flight Recorder

Per-operator contact storage with fuzzy search.
Storage: Contacts/contacts.json
"""

import hashlib
import json
import logging
import os
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

log = logging.getLogger("contacts")

# Path to contacts file
CONTACTS_DIR = Path(__file__).resolve().parent.parent / "Contacts"
CONTACTS_FILE = CONTACTS_DIR / "contacts.json"

# A new book's system contact names THIS box's own phone line, taken from its
# telephony configuration (system_numbers); nothing is seeded when no line is
# configured. It can never whitelist an inbound SMS: it is stored with
# inbound_allowed=False, and sms/router.py gates every number this box owns.
# It is labelled "system", not "self": the operator's own phone is a contact
# with is_operator_self, and "text me" must never reach the box's line.
SYSTEM_CONTACT_NAME = "AI BlackBox Flight Recorder"
SYSTEM_CONTACT_NOTES = (
    "This BlackBox's own phone line, from its telephony settings: calls and "
    "texts the AI places go out from this number. It is not the operator's phone."
)

# Earlier releases seeded every book with a fixed "self" contact instead: a real
# line published in this public repo, with its owner's email, and from 2026-10 a
# 555-01xx placeholder. The SMS router keeps gating both numbers, and the startup
# migration (strip_retired_seed_contacts) removes the seeded records. One entry
# per shipped variant: SHA-256 of the phone's last 10 digits, SHA-256 of the
# email, and the notes text. Digests, so the real line and email stay out of
# the source.
_RETIRED_SEED_VARIANTS = (
    ("6cbf6b02a2a8d8b079a48e45f81d6545cb54d489c8aada40c0a3090264c621a5",
     "05a151a7cd22ed7a5c4ab11261158009370c579481ea4dd4a6bde56fdfbf4c3e",
     "This is your own phone number. The AI BlackBox system number. Use this as the caller identity."),
    ("6d5dc3b8b5671aa65a03f3bbbd6c670ca4f713012a0840d50f791bc9d1ad0834",
     "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
     "Placeholder for this BlackBox's own phone number. Edit it to the box's real line to use it as the caller identity."),
)
RETIRED_SEED_PHONE_SHA256 = frozenset(variant[0] for variant in _RETIRED_SEED_VARIANTS)
# Every field a release ever wrote on a seeded record (the two inbound-SMS flags
# appear once a later save_contacts() materializes the read-time defaults).
_SEED_FIELDS = frozenset({
    "id", "name", "phone", "email", "relationship", "notes", "tags", "created_by",
    "created_at", "updated_at", "inbound_allowed", "is_operator_self",
})


def _normalize_phone(phone: str) -> str:
    """Strip to last 10 digits for cross-book comparison."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _apply_inbound_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    """Read-time migration defaults. The read itself does not rewrite the file
    (a later save_contacts() would materialize the defaults onto disk).

    Legacy records predate the inbound-SMS flags. Preserve the current
    "in book => can text in" behavior so nobody is locked out:
      - missing ``inbound_allowed``  => True
      - missing ``is_operator_self`` => False
    An explicit value (including ``False``) is always preserved.
    """
    for book in data.values():
        if not isinstance(book, dict):
            continue
        for contact in book.values():
            if not isinstance(contact, dict):
                continue
            if "inbound_allowed" not in contact:
                contact["inbound_allowed"] = True
            if "is_operator_self" not in contact:
                contact["is_operator_self"] = False
    return data


def load_contacts() -> Dict[str, Any]:
    """Read contacts.json. Creates file with {} if missing.

    Applies read-time inbound-SMS defaults (see ``_apply_inbound_defaults``);
    the read itself does not rewrite the file.
    """
    CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
    if not CONTACTS_FILE.exists():
        CONTACTS_FILE.write_text("{}")
        return {}
    try:
        return _apply_inbound_defaults(json.loads(CONTACTS_FILE.read_text()))
    except (json.JSONDecodeError, IOError):
        return {}


def save_contacts(data: Dict[str, Any]) -> None:
    """Write full contacts dict to disk."""
    CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
    CONTACTS_FILE.write_text(json.dumps(data, indent=2))


def _gateways_file() -> Path:
    """The cellular gateway store (one record per TG gateway, with its ports)."""
    from Orchestrator.asterisk.config import GATEWAYS_FILE
    return Path(GATEWAYS_FILE)


def _gateway_lines() -> List[Tuple[str, Optional[str]]]:
    """(number, owner) for every gateway port that has a number. Read-only and
    tolerant: a missing or unparseable store, or a malformed record, yields
    nothing rather than an error."""
    try:
        gateways = json.loads(_gateways_file().read_text())
    except (OSError, ValueError, ImportError):
        return []
    lines: List[Tuple[str, Optional[str]]] = []
    for gw in gateways if isinstance(gateways, list) else []:
        if not isinstance(gw, dict):
            continue
        ports = gw.get("ports")
        if isinstance(ports, list):
            for port in ports:
                if isinstance(port, dict):
                    number = str(port.get("phone_number") or "").strip()
                    if number:
                        lines.append((number, str(port.get("operator") or "").strip() or None))
        else:  # pre-v2 record: a bare phone_numbers list, no owners
            for number in gw.get("phone_numbers") or []:
                if isinstance(number, str) and number.strip():
                    lines.append((number.strip(), None))
    return lines


def system_numbers() -> List[Tuple[str, Optional[str]]]:
    """This box's own phone lines as (number, owner) pairs. ``owner`` is the
    operator a gateway port is dedicated to, None for a shared line.

    Sources, in order: the cellular gateway ports, TG200_PHONE_NUMBER, the
    "phone" of each ASTERISK_TRUNKS entry, CELLULAR_PHONE_NUMBER (when
    CELLULAR_ENABLED), then TWILIO_PHONE_NUMBER; deduplicated by the last 10
    digits (first one wins). Read on every call: lines are edited in the
    Portal at runtime. Empty on a box with no telephony configured.
    """
    lines = _gateway_lines()
    try:
        from Orchestrator.asterisk import config as asterisk_config
        lines.append((str(asterisk_config.TG200_PHONE_NUMBER or "").strip(), None))
        trunks = asterisk_config.ASTERISK_TRUNKS
        for trunk in trunks if isinstance(trunks, list) else []:
            if isinstance(trunk, dict):
                lines.append((str(trunk.get("phone") or "").strip(), None))
    except Exception:
        pass
    try:
        from Orchestrator import config
        if config.CELLULAR_ENABLED:
            lines.append((str(config.CELLULAR_PHONE_NUMBER or "").strip(), None))
        lines.append((str(config.TWILIO_PHONE_NUMBER or "").strip(), None))
    except Exception:
        pass
    seen = set()
    numbers: List[Tuple[str, Optional[str]]] = []
    for number, owner in lines:
        key = _normalize_phone(number)
        if key and key not in seen:
            seen.add(key)
            numbers.append((number, owner))
    return numbers


def is_system_number(phone: str, numbers: Optional[List[str]] = None) -> bool:
    """True for one of this box's own lines (``numbers``, default
    system_numbers()) or a retired seed number.

    A contact holding such a number must never satisfy the inbound SMS
    whitelist: the box's own line, or a fixed number published in this repo,
    is trivially spoofed as a sender. Pass ``numbers`` to check many contacts
    against one read of the configuration.
    """
    target = _normalize_phone(phone)
    if not target:
        return False
    if numbers is None:
        numbers = [number for number, _owner in system_numbers()]
    if any(_normalize_phone(number) == target for number in numbers):
        return True
    return hashlib.sha256(target.encode()).hexdigest() in RETIRED_SEED_PHONE_SHA256


def system_self_contact(operator: str) -> Optional[Dict[str, Any]]:
    """A new system contact for ``operator``'s book: the line that operator
    owns, else the first shared line. None when neither is configured."""
    lines = system_numbers()
    number = (next((n for n, owner in lines if owner == operator), None)
              or next((n for n, owner in lines if not owner), None))
    if not number:
        return None
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": str(uuid.uuid4()),
        "name": SYSTEM_CONTACT_NAME,
        "phone": number,
        "email": "",
        "relationship": "system",
        "notes": SYSTEM_CONTACT_NOTES,
        "tags": ["system"],
        # The box's own line is neither a texting peer nor an operator identity.
        "inbound_allowed": False,
        "is_operator_self": False,
        "created_by": "system",
        "created_at": now,
        "updated_at": now,
    }


def ensure_operator_book(data: Dict[str, Any], operator: str) -> bool:
    """
    Ensure operator has a phone book. A new book holds this box's own line as
    its system contact when one is configured (system_self_contact), else
    nothing. Returns True if a new book was created.
    """
    if operator not in data:
        book: Dict[str, Any] = {}
        seed = system_self_contact(operator)
        if seed:
            book[seed["id"]] = seed
        data[operator] = book
        return True
    return False


def ensure_book_for_read(data: Dict[str, Any], operator: str) -> None:
    """Give a READ path (a GET route, a search) the operator's book.

    The new book and its editable seed contact are saved only for an operator
    on the live roster: a GET's query string or a tool argument can carry any
    name, and a read must not create a book for each one. Any other name sees
    the seed book in memory only.
    """
    if ensure_operator_book(data, operator):
        from Orchestrator import config as _cfg  # lazy: keep this module import-light
        if operator in _cfg.USERS_LIST:
            save_contacts(data)


def _is_retired_seed(contact_id: str, contact: Any) -> bool:
    """True only for a seeded 'self' record exactly as a release wrote it:
    every field as seeded (the inbound-SMS flags at most materialized at their
    defaults) and never re-saved since (updated_at == created_at). Any edit,
    or any field the seed never had, makes the record the user's."""
    if not isinstance(contact, dict) or not _SEED_FIELDS.issuperset(contact):
        return False
    if contact.get("id") != contact_id or contact.get("created_at") != contact.get("updated_at"):
        return False
    if (contact.get("name") != SYSTEM_CONTACT_NAME or contact.get("relationship") != "self"
            or contact.get("tags") != ["system", "self"] or contact.get("created_by") != "system"):
        return False
    if contact.get("inbound_allowed", True) is not True or contact.get("is_operator_self", False) is not False:
        return False
    phone, email = contact.get("phone"), contact.get("email")
    if not isinstance(phone, str) or not isinstance(email, str):
        return False
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) != 11 or phone != "+" + digits:  # every seed was E.164 +1
        return False
    variant = (hashlib.sha256(digits[-10:].encode()).hexdigest(),
               hashlib.sha256(email.encode()).hexdigest(),
               contact.get("notes"))
    return variant in _RETIRED_SEED_VARIANTS


def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
    """Write ``payload`` to ``path`` via a synced temp file in the same dir."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def strip_retired_seed_contacts(path: Optional[Path] = None) -> int:
    """Startup migration: remove the seeded 'self' records earlier releases put
    in every book (exact matches only, see _is_retired_seed) and give each such
    book this box's own line instead when one is configured. Idempotent: once
    the seeds are gone nothing matches, so later boots change nothing.

    Writes only when it removed something: first a backup of the original bytes
    (<file>.bak-seed-<UTC>), then the store, each atomically and with the
    store's mode. A missing store, or one that does not parse, is never
    written. Returns how many records it removed.
    """
    path = Path(path or CONTACTS_FILE)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return 0
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("contacts: %s does not parse; retired-seed cleanup skipped", path)
        return 0
    if not isinstance(data, dict):
        return 0
    removed = 0
    for operator, book in data.items():
        if not isinstance(book, dict):
            continue
        seeds = [cid for cid, contact in book.items() if _is_retired_seed(cid, contact)]
        if not seeds:
            continue
        for cid in seeds:
            del book[cid]
        removed += len(seeds)
        replacement = system_self_contact(operator)
        if replacement and not any(
            isinstance(c, dict) and _normalize_phone(c.get("phone")) == _normalize_phone(replacement["phone"])
            for c in book.values()
        ):
            book[replacement["id"]] = replacement
    if not removed:
        return 0
    mode = stat.S_IMODE(path.stat().st_mode)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.bak-seed-{stamp}")
    _atomic_write(backup, raw, mode)
    _atomic_write(path, json.dumps(data, indent=2).encode(), mode)
    log.info("contacts: removed %d retired seed contact(s); backup at %s", removed, backup)
    return removed


def search_contacts(query: str, operator: str) -> List[Dict[str, Any]]:
    """
    Case-insensitive fuzzy match across all contact fields.
    Returns up to 10 matches, exact name matches ranked first.
    """
    data = load_contacts()
    ensure_book_for_read(data, operator)

    book = data.get(operator, {})
    query_lower = query.lower()
    results = []

    for contact in book.values():
        score = 0
        # Exact name match (highest priority)
        if contact.get("name", "").lower() == query_lower:
            score = 100
        # Partial name match
        elif query_lower in contact.get("name", "").lower():
            score = 80
        # Phone match
        elif query_lower in contact.get("phone", "").replace("+", "").replace("-", "").replace(" ", ""):
            score = 70
        # Email match
        elif query_lower in contact.get("email", "").lower():
            score = 60
        # Relationship match
        elif query_lower in contact.get("relationship", "").lower():
            score = 50
        # Tag match
        elif any(query_lower in tag.lower() for tag in contact.get("tags", [])):
            score = 40
        # Notes match
        elif query_lower in contact.get("notes", "").lower():
            score = 30

        if score > 0:
            results.append((score, contact))

    # Sort by score descending, return top 10
    results.sort(key=lambda x: x[0], reverse=True)
    return [contact for _, contact in results[:10]]


def _scan_self_flag_collision(
    data: Dict[str, Any], phone: Optional[str], operator: str
) -> Optional[str]:
    """Cross-book scan for an identity collision (write-time guard).

    Returns a warning string if the same number (last-10-digit match) is already
    flagged ``is_operator_self`` in a DIFFERENT operator's book, else None. Soft
    enforcement: the caller still saves — the storage has no global unique key,
    so the rule + warning carry the uniqueness guarantee.
    """
    target = _normalize_phone(phone)
    if not target:
        return None
    for other_op, book in data.items():
        if other_op == operator or not isinstance(book, dict):
            continue
        for contact in book.values():
            if not isinstance(contact, dict):
                continue
            if not contact.get("is_operator_self"):
                continue
            if _normalize_phone(contact.get("phone", "")) == target:
                return (
                    f"This number is already flagged as operator-self in "
                    f"{other_op}'s book (contact \"{contact.get('name', '')}\"). "
                    f"Saved anyway, but a number should identify one operator."
                )
    return None


def upsert_contact(
    name: str,
    notes: Optional[str],
    tags: Optional[List[str]],
    operator: str,
    created_by: str,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    relationship: Optional[str] = None,
    inbound_allowed: Optional[bool] = None,
    is_operator_self: Optional[bool] = None
) -> Dict[str, Any]:
    """
    Create or update a contact. Matches existing by name (case-insensitive).
    Returns the saved contact. When ``is_operator_self`` collides with another
    operator's self-flag for the same number, a ``warning`` key is included on
    the returned contact (it still saves).
    """
    data = load_contacts()
    ensure_operator_book(data, operator)
    book = data[operator]
    now = datetime.now(timezone.utc).isoformat()

    # Write-time identity guard: scan OTHER books before saving.
    warning = None
    if is_operator_self:
        warning = _scan_self_flag_collision(data, phone, operator)

    # Check for existing contact with same name
    existing_id = None
    for cid, contact in book.items():
        if contact.get("name", "").lower() == name.lower():
            existing_id = cid
            break

    if existing_id:
        # Update existing
        contact = book[existing_id]
        contact["name"] = name
        # Additive contract: only overwrite when the caller explicitly provided
        # the field (an explicit "" / [] still clears — real caller intent;
        # omission [None] preserves). A model-facing partial save_contact must
        # not wipe existing notes/tags.
        if notes is not None:
            contact["notes"] = notes
        if tags is not None:
            contact["tags"] = tags
        if phone is not None:
            contact["phone"] = phone
        if email is not None:
            contact["email"] = email
        if relationship is not None:
            contact["relationship"] = relationship
        # Additive contract: only overwrite a flag when the caller explicitly
        # provided it. Flag-less callers (model/voice save_contact) must NOT
        # wipe a previously-set flag back to False.
        if inbound_allowed is not None:
            contact["inbound_allowed"] = inbound_allowed
        if is_operator_self is not None:
            contact["is_operator_self"] = is_operator_self
        contact["updated_at"] = now
    else:
        # Create new
        contact_id = str(uuid.uuid4())
        contact = {
            "id": contact_id,
            "name": name,
            "phone": phone or "",
            "email": email or "",
            "relationship": relationship or "",
            "notes": notes if notes is not None else "",
            "tags": tags if tags is not None else [],
            # New contacts default OPEN to inbound: backward-compatible with the
            # legacy migration default ("in book => can text in") so a flag-less
            # create (model/voice save_contact) stays textable; opt-out by
            # passing inbound_allowed=False for outbound-only contacts.
            "inbound_allowed": bool(inbound_allowed) if inbound_allowed is not None else True,
            "is_operator_self": bool(is_operator_self),  # None -> False
            "created_by": created_by,
            "created_at": now,
            "updated_at": now
        }
        book[contact_id] = contact

    save_contacts(data)
    if warning:
        return {**contact, "warning": warning}
    return contact
