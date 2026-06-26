"""
contacts.py - Contact Book for AI BlackBox Flight Recorder

Per-operator contact storage with fuzzy search.
Storage: Contacts/contacts.json
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

# Path to contacts file
CONTACTS_DIR = Path(__file__).resolve().parent.parent / "Contacts"
CONTACTS_FILE = CONTACTS_DIR / "contacts.json"

# Seed contact added to every new operator's book
SEED_CONTACT = {
    "name": "AI BlackBox Flight Recorder",
    "phone": "+17164512527",
    "email": "brandon@aiblackboxfc.com",
    "relationship": "self",
    "notes": "This is your own phone number. The AI BlackBox system number. Use this as the caller identity.",
    "tags": ["system", "self"],
    "created_by": "system"
}


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


def _make_seed_contact() -> Dict[str, Any]:
    """Create a seed contact entry with generated ID and timestamps."""
    now = datetime.now(timezone.utc).isoformat()
    contact_id = str(uuid.uuid4())
    return {
        "id": contact_id,
        **SEED_CONTACT,
        "created_at": now,
        "updated_at": now
    }


def ensure_operator_book(data: Dict[str, Any], operator: str) -> bool:
    """
    Ensure operator has a phone book. Creates one with seed contact if missing.
    Returns True if a new book was created.
    """
    if operator not in data:
        seed = _make_seed_contact()
        data[operator] = {seed["id"]: seed}
        return True
    return False


def search_contacts(query: str, operator: str) -> List[Dict[str, Any]]:
    """
    Case-insensitive fuzzy match across all contact fields.
    Returns up to 10 matches, exact name matches ranked first.
    """
    data = load_contacts()
    if ensure_operator_book(data, operator):
        save_contacts(data)

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
