"""REST API routes for SMS messaging."""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from Orchestrator.sms import get_ami_client, get_message_store, get_router
from Orchestrator.asterisk.config import TG200_PHONE_NUMBER

router = APIRouter(prefix="/sms", tags=["sms"])


class SMSSendRequest(BaseModel):
    operator: str
    to: str
    message: str
    from_number: Optional[str] = None


class SMSMarkReadRequest(BaseModel):
    operator: Optional[str] = None
    phone: Optional[str] = None
    message_id: Optional[int] = None


class SMSPreferencesRequest(BaseModel):
    operator: str
    # Empty = follow the box's configured default (DEFAULT_PROVIDER, its
    # *_MODEL_DEFAULT); see config.resolve_default_model.
    sms_provider: str = ""
    sms_model: str = ""


def _require_sms():
    """Raise 503 if the SMS system isn't started yet."""
    if get_ami_client() is None:
        raise HTTPException(status_code=503, detail="SMS system not started")


@router.get("/threads")
async def get_threads(operator: str = Query(...)):
    """Return list of conversation threads for the operator."""
    _require_sms()
    store = get_message_store()
    threads = store.get_recent_threads(operator)
    return {"threads": threads, "operator": operator}


@router.get("/messages")
async def get_messages(
    operator: str = Query(...),
    phone: str = Query(...),
    limit: int = Query(50),
    offset: int = Query(0),
):
    """Return messages in a conversation thread (oldest first for chat).

    Read-only: clients POST /sms/mark-read once they show the thread (a GET
    that marked threads read could be fired by any cross-site <img>)."""
    _require_sms()
    store = get_message_store()
    messages = store.get_conversation(operator, phone, limit, offset)
    return {"messages": messages, "operator": operator, "phone": phone}


@router.post("/send")
async def send_sms(req: SMSSendRequest):
    """Send an SMS message."""
    _require_sms()
    sms_router = get_router()
    if sms_router is None:
        raise HTTPException(status_code=503, detail="SMS router not available")
    result = await sms_router.send_manual(
        req.operator, req.to, req.message, from_number=req.from_number
    )
    return {
        "success": result.get("success", False),
        "error": result.get("error"),
        "message_id": result.get("message_id"),
    }


@router.post("/mark-read")
async def mark_read(req: SMSMarkReadRequest):
    """Mark messages as read by phone number or message ID."""
    _require_sms()
    store = get_message_store()
    if req.phone and req.operator:
        store.mark_all_read(req.operator, req.phone)
    elif req.message_id is not None:
        store.mark_read(req.message_id)
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide either (operator + phone) or message_id",
        )
    return {"success": True}


@router.get("/unread")
async def get_unread(operator: str = Query(...)):
    """Return unread message count for the operator."""
    _require_sms()
    store = get_message_store()
    unread = store.get_unread_count(operator)
    return {"unread": unread, "operator": operator}


@router.get("/preferences")
async def get_sms_preferences(operator: str = Query(...)):
    """Get per-operator SMS model/provider preferences.

    Unset preferences report the box's defaults: ``sms_provider`` is
    DEFAULT_PROVIDER and ``sms_model`` is "" (Auto, which follows that
    provider's configured default). ``sms_model_resolved`` is the model the
    next SMS reply actually runs on (Auto resolved, a retired Claude id mapped
    to its successor) — the same resolution the SMS router applies.
    """
    from Orchestrator import config
    from Orchestrator.state import get_operator_preference
    provider = get_operator_preference(operator, "sms_provider", "") or config.DEFAULT_PROVIDER
    model = get_operator_preference(operator, "sms_model", "") or ""
    return {
        "operator": operator,
        "sms_provider": provider,
        "sms_model": model,
        "sms_model_resolved": config.resolve_default_model(provider, model),
    }


@router.post("/preferences")
async def set_sms_preferences(req: SMSPreferencesRequest):
    """Set per-operator SMS model/provider preferences."""
    from Orchestrator.state import set_operator_preference
    set_operator_preference(req.operator, "sms_provider", req.sms_provider)
    set_operator_preference(req.operator, "sms_model", req.sms_model)
    return {"success": True, "operator": req.operator}


@router.get("/status")
async def get_status():
    """Get AMI connection status and GSM span status."""
    ami = get_ami_client()
    if ami is None:
        return {
            "connected": False,
            "span": None,
            "phone_number": TG200_PHONE_NUMBER,
        }
    span = await ami.get_span_status()
    return {
        "connected": ami.connected,
        "span": span,
        "phone_number": TG200_PHONE_NUMBER,
    }
