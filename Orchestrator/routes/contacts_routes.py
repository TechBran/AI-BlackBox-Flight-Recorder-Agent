"""REST API routes for Contact Book management."""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List

router = APIRouter(prefix="/contacts", tags=["contacts"])


class ContactUpsertRequest(BaseModel):
    operator: str
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    relationship: Optional[str] = None
    notes: Optional[str] = None   # None = leave unchanged on update
    tags: Optional[List[str]] = None  # None = leave unchanged on update
    inbound_allowed: Optional[bool] = None   # may this number text in? (None = leave unchanged)
    is_operator_self: Optional[bool] = None  # is this the operator's own line? (None = leave unchanged)
    id: Optional[str] = None  # the contact an editor is changing: a rename updates it in place


@router.get("")
async def list_contacts(operator: str = Query(...)):
    """List all contacts for an operator, sorted by name."""
    from Orchestrator.contacts import load_contacts, ensure_book_for_read
    data = load_contacts()
    ensure_book_for_read(data, operator)
    contacts = list(data.get(operator, {}).values())
    contacts.sort(key=lambda c: c.get("name", "").lower())
    return {"contacts": contacts, "operator": operator}


@router.get("/search")
async def search_contacts_endpoint(operator: str = Query(...), query: str = Query(...)):
    """Search contacts by name, phone, email, or tags."""
    from Orchestrator.contacts import search_contacts
    results = search_contacts(query, operator)
    return {"results": results, "operator": operator, "query": query}


@router.post("")
async def upsert_contact(req: ContactUpsertRequest):
    """Create or update a contact: the one `id` names in the operator's book, else
    the one with the same name (case-insensitive). 409 when a rename would give the
    contact a name another contact already has."""
    from Orchestrator.contacts import ContactNameTaken, upsert_contact
    try:
        contact = upsert_contact(
            name=req.name,
            notes=req.notes,    # None = leave unchanged (additive)
            tags=req.tags,      # None = leave unchanged (additive)
            operator=req.operator,
            created_by=req.operator,
            phone=req.phone,
            email=req.email,
            relationship=req.relationship,
            inbound_allowed=req.inbound_allowed,    # None = leave unchanged (additive)
            is_operator_self=req.is_operator_self,  # None = leave unchanged (additive)
            match_id=req.id,
        )
    except ContactNameTaken as e:
        raise HTTPException(status_code=409, detail=str(e))
    resp = {"success": True, "contact": contact}
    if contact.get("warning"):
        resp["warning"] = contact["warning"]
    return resp


@router.delete("/{contact_id}")
async def delete_contact(contact_id: str, operator: str = Query(...)):
    """Delete a contact by ID."""
    from Orchestrator.contacts import editing_contacts
    with editing_contacts() as data:   # locked; the 404 saves nothing
        book = data.get(operator, {})
        if contact_id not in book:
            raise HTTPException(status_code=404, detail="Contact not found")
        del book[contact_id]
    return {"success": True, "deleted": contact_id}
