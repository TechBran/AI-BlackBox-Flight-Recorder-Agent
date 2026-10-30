"""Health tracking routes (/wellness/*, design M7; the box side of M7.2).

Phone -> box (JSON; every error is {"error": <code>, ...}):

  POST /wellness/enroll          unsigned (there is no key yet): the handshake
  POST /wellness/ingest          signed; gzip or plain bbx.wellness.ingest/1
  POST /wellness/capabilities    signed; the capability report (counts only)
  GET  /wellness/status          ?operator=; unsigned = {state, source_label,
                                 last_sync, enrolled} only; signed by that
                                 operator's ACTIVE phone = the full settings
                                 view (a replacement awaiting approval gets the
                                 four fields); with the kill switch off, the
                                 four fields with state "disabled"
  POST /wellness/select          the Portal's "Use for health" (unsigned)
  POST /wellness/pause           narrowing: any surface for that operator
  POST /wellness/resume          widening: signed
  POST /wellness/disconnect      keep_history=true: any surface; false: signed
  POST /wellness/erase           signed
  PUT  /wellness/settings        widening changes signed (W21); narrowing and
                                 the instinct prompt from any surface
  POST /wellness/replace-approve signed by the OLD phone
  POST /wellness/sync-now        ask the phone to push now (any surface)
  GET  /wellness/privacy-policy  text/markdown, X-Policy-Version
  GET  /wellness/ref/{ref}       signed: one delivered payload ("Show numbers")
  GET  /wellness/disclosures     signed: who saw the data, newest first

"Signed" = the X-BlackBox-Origin proof keyed with the phone's device key
(Orchestrator/wellness/origin_proof.py). GET /fossil/snapshot/{id}
(routes/task_routes.py) takes the same optional proof as GET /wellness/status
(optional_proof): the enrolled phone then reads its own wellness turns as
minted. So do POST /tts, /tts/batch and /tts/queue (routes/tts_routes.py):
the speech guard then also holds the text to that operator's own recent
results. The kill switch ([wellness] enabled,
read fresh) stops collection and every answer that carries health
information; pause, disconnect, erase and narrowing settings still work.
The operator is always the one the proof or the body names for THIS
operator's own data; no route takes another operator as a target, and the
unsigned status says nothing beyond its four fields (rule W17).

Every handler is a plain `def`, so FastAPI runs it in the threadpool (the
store writes, the bind call to the phone and the recompute never block the
event loop). Bodies are read by an async dependency that stops at the route's
cap (2 MB for a batch; a gzip batch is then inflated to at most 8 MB, so a
bomb never exhausts memory). The signature is checked on the exact bytes
received. Logs carry counts and codes, never a body.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from Orchestrator.wellness import (api, catalog, enroll, erase, fde, ingest, origin_proof,
                                   schema)
from Orchestrator.wellness import store as store_mod

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wellness", tags=["wellness"])

POLICY_PATH = Path(__file__).resolve().parents[1] / "wellness" / "policy" / "health_privacy.md"
SMALL_BODY = 64 * 1024
REPORT_BODY = 256 * 1024
RATE_BATCHES = 60
RATE_WINDOW_S = 600
RETRY_PAUSED_S = 3600
RETRY_PENDING_S = 300
RETRY_DISABLED_S = 3600


# ------------------------------------------------------------------ plumbing

@dataclasses.dataclass
class RawBody:
    data: Optional[bytes]
    too_large: bool = False


def _capped(limit: int):
    async def read(request: Request) -> RawBody:
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > limit:
            return RawBody(None, True)
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > limit:
                return RawBody(None, True)
        return RawBody(bytes(buf))
    return read


def _json(status: int, body: dict, headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=body, headers=headers or {})


def _refuse(e: enroll.EnrollError) -> JSONResponse:
    headers = {"Retry-After": str(e.retry_after)} if e.retry_after else None
    return _json(e.status, e.body(), headers)


def _too_large() -> JSONResponse:
    return _json(413, {"error": "too_large"})


def _invalid(detail: str) -> JSONResponse:
    return _json(422, {"error": "invalid", "detail": detail})


def _parse(model, raw: RawBody):
    """(model instance, None) or (None, the 413/422 response)."""
    if raw.too_large:
        return None, _too_large()
    try:
        obj = ingest.parse_json(raw.data or b"{}")
        return model.model_validate(obj), None
    except ingest.IngestError as e:
        return None, _invalid(e.detail or e.code)
    except ValidationError as e:
        return None, _invalid(ingest.validation_detail(e))


def _signed(request: Request, raw: RawBody, required: bool = True):
    """(Signed or None, refusal response or None)."""
    header = request.headers.get(origin_proof.HEADER)
    if header is None and not required:
        return None, None
    try:
        return enroll.verify_signed_request(header, request.method, request.url.path,
                                            raw.data or b""), None
    except enroll.EnrollError as e:
        return None, _refuse(e)


# The body a route with an optional proof reads (the signature covers it).
small_body = _capped(SMALL_BODY)


async def proof_body(request: Request) -> RawBody:
    """The exact body bytes for the optional proof of a route that takes a
    JSON `Body(...)` (the TTS routes' speech guard). Read only when the
    request carries X-BlackBox-Origin; FastAPI has already read the whole
    body to parse it, so this is that cached copy, not a second read."""
    if request.headers.get(origin_proof.HEADER) is None:
        return RawBody(b"")
    return RawBody(await request.body())


def optional_proof(request: Request, raw: RawBody):
    """(Signed or None, refusal response or None) where the proof is OPTIONAL:
    GET /wellness/status and the signed ledger read (GET /fossil/snapshot/{id},
    routes/task_routes.py). No header is no proof. A present one is checked
    the way every signed route checks it (_signed: enroll.verify_signed_request)
    and a bad one is refused (401 bad_signature, 409 owner_changed, 410 revoked
    or source_moved), never downgraded to an unsigned answer. With the kill
    switch off nothing is verified (the status's four fields, a hidden turn)."""
    if not _enabled():
        return None, None
    return _signed(request, raw, required=False)


def _disabled() -> Optional[JSONResponse]:
    if _enabled():
        return None
    return _json(503, {"error": "wellness_disabled"}, {"Retry-After": str(RETRY_DISABLED_S)})


class _RateLimit:
    """A token bucket per enrollment: 60 batches per 10 minutes."""

    def __init__(self, capacity: int = RATE_BATCHES, window_s: int = RATE_WINDOW_S):
        self.capacity, self.rate = capacity, capacity / float(window_s)
        self._lock = threading.Lock()
        self._buckets: Dict[str, list] = {}

    def take(self, key: str, now: Optional[float] = None) -> Optional[int]:
        """None when allowed, else the seconds to wait."""
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, at = self._buckets.get(key, [float(self.capacity), now])
            tokens = min(self.capacity, tokens + (now - at) * self.rate)
            if tokens < 1.0:
                self._buckets[key] = [tokens, now]
                return max(1, int((1.0 - tokens) / self.rate + 0.999))
            self._buckets[key] = [tokens - 1.0, now]
            return None


RATE = _RateLimit()


def _now_ms() -> int:
    return int(time.time() * 1000)


# ------------------------------------------------------------------ status

def _enabled() -> bool:
    from Orchestrator import config
    return config.wellness_enabled()


def _minimal_status(operator: str) -> dict:
    rec = enroll.get_enrollment(operator)
    if not rec:
        return {"state": "not_set_up" if _enabled() else "disabled", "source_label": None,
                "last_sync": None, "enrolled": False}
    label = rec.get("source_label")
    if not label and rec.get("device_id"):
        try:
            dev = enroll._registry().get_device(rec["device_id"])
            label = dev.name if dev is not None else None
        except Exception:  # noqa: BLE001
            label = None
    return {"state": enroll.effective_state(rec) if _enabled() else "disabled",
            "source_label": label, "last_sync": rec.get("last_push_at"),
            "enrolled": rec.get("state") in enroll.STORED_LIVE}


def full_status(operator: str, *, now_ms: Optional[int] = None) -> dict:
    """The settings view. `troubles` is the list of trouble codes (spec §3.5's
    vocabulary, e.g. "stale_sync", "permission_revoked:sleep");
    `trouble_details` carries the same troubles with their wording; `fde`
    is whether the box's disk is encrypted (wellness/fde.py)."""
    now_ms = _now_ms() if now_ms is None else now_ms
    rec = enroll.get_enrollment(operator) or {}
    out = _minimal_status(operator)
    st = enroll.store_for(rec, create=False) if rec else None
    wear = {"hr_mode": None, "nights_worn_7": 0}
    sync = {"last_push_at": rec.get("last_push_at"), "outbox_hint":
            {"next_min_interval_s": ingest.NEXT_MIN_INTERVAL_S, "batches_received": 0}}
    disclosures = 0
    try:
        _store_view(st, now_ms, wear, sync)
        disclosures = st.disclosures_count() if st is not None else 0
    except store_mod.StoreGone:
        pass
    details = enroll.troubles_for(rec, now_ms) if rec else []
    out.update({
        "troubles": [t["code"] for t in details],
        "trouble_details": details,
        "categories": rec.get("categories") or dict(catalog.DEFAULT_CONSENT),
        "local_only": rec.get("local_only") or dict(catalog.DEFAULT_LOCAL_ONLY),
        "providers_allowed": rec.get("providers_allowed") or [],
        "google_paid_key_confirmed": bool(rec.get("google_paid_key_confirmed")),
        "surfaces_mode": rec.get("surfaces_mode") or "auto",
        "wear": wear, "sync": sync,
        "instinct_prompt": rec.get("instinct_prompt"),
        "instinct_prompt_default": catalog.DEFAULT_INSTINCT_PROMPT,
        "replace_pending": bool(rec.get("pending")),
        "policy_version": catalog.POLICY_VERSION,
        "disclosures_count": disclosures,
        "fde": fde.status(),
    })
    return out


def _store_view(st, now_ms: int, wear: dict, sync: dict) -> None:
    """Fill the wear profile and the batch count from the store (may raise
    StoreGone when an erase got there first)."""
    if st is None:
        return
    add_days = store_mod.add_days
    today = st.offsets().day_of(now_ms)        # the local day, not the UTC date
    days = st.coverage_days(add_days(today, -7), today)
    if days:
        wear["hr_mode"] = days[max(days)].get("hr_mode")
        wear["nights_worn_7"] = sum(1 for k in range(7)
                                    if (days.get(add_days(today, -k)) or {}).get(
                                        "night_worn") == 1)
    sync["outbox_hint"]["batches_received"] = st.ingest_stats()["batches"]


@router.get("/status")
def wellness_status(request: Request, operator: str = "",
                    raw: RawBody = Depends(small_body)):
    try:
        schema.OperatorBody(operator=operator)
    except ValidationError:
        return _json(400, {"error": "invalid_operator"})
    if not _enabled():
        return _minimal_status(operator)
    signed, refusal = optional_proof(request, raw)
    if refusal is not None:
        return refusal
    # Only the operator's ACTIVE phone reads the settings view: a replacement
    # still waiting for the old phone's tap gets what anyone gets (W17).
    if signed is not None and signed.operator == operator and signed.which == "active":
        return full_status(operator)
    return _minimal_status(operator)


# ------------------------------------------------------------------ enroll and select

@router.post("/enroll")
def wellness_enroll(request: Request, raw: RawBody = Depends(_capped(SMALL_BODY))):
    body, refusal = _parse(schema.EnrollBody, raw)
    if refusal is not None:
        return refusal
    ip = enroll.client_ip(request.client.host if request.client else None,
                          request.headers.get("x-forwarded-for"))
    try:
        return enroll.enroll(body, ip)
    except enroll.EnrollError as e:
        logger.info("[WELLNESS] enroll refused: %s", e.code)
        return _refuse(e)


@router.post("/select")
def wellness_select(raw: RawBody = Depends(_capped(SMALL_BODY))):
    refusal = _disabled()
    if refusal is not None:
        return refusal
    body, refusal = _parse(schema.SelectBody, raw)
    if refusal is not None:
        return refusal
    try:
        return enroll.select(body.operator, body.device_id)
    except enroll.EnrollError as e:
        return _refuse(e)


@router.post("/replace-approve")
def wellness_replace_approve(request: Request, raw: RawBody = Depends(_capped(SMALL_BODY))):
    signed, refusal = _signed(request, raw)
    if refusal is not None:
        return refusal
    body, refusal = _parse(schema.ReplaceApproveBody, raw)
    if refusal is not None:
        return refusal
    if signed.operator != body.operator or signed.which != "active":
        return _json(403, {"error": "not_allowed"})
    try:
        return enroll.approve_replace(body.operator, body.approve)
    except enroll.EnrollError as e:
        return _refuse(e)


# ------------------------------------------------------------------ ingest

@router.post("/ingest")
def wellness_ingest(request: Request,
                    raw: RawBody = Depends(_capped(ingest.MAX_COMPRESSED_BYTES))):
    if raw.too_large:
        return _too_large()
    refusal = _disabled()
    if refusal is not None:
        return refusal
    signed, refusal = _signed(request, raw)
    if refusal is not None:
        return refusal
    if signed.which == "pending":
        return _json(503, {"error": "replace_pending"}, {"Retry-After": str(RETRY_PENDING_S)})
    rec = signed.record
    if rec.get("state") == "paused":
        return _json(503, {"error": "paused"}, {"Retry-After": str(RETRY_PAUSED_S)})
    wait = RATE.take(signed.enrollment_id)
    if wait is not None:
        return _json(429, {"error": "rate_limited"}, {"Retry-After": str(wait)})
    now_ms = _now_ms()
    try:
        data = ingest.decompress(raw.data or b"", request.headers.get("content-encoding"))
        batch = ingest.parse_batch(ingest.parse_json(data))
        # Never create the store here: it exists from the bind on, and one
        # that is gone was erased (a batch must not bring it back).
        st = enroll.store_for(rec, create=False)
        if st is None:
            raise ingest.IngestError(410, "revoked")
        result = ingest.process_batch(st, batch, enrollment_id=signed.enrollment_id,
                                      epoch=int(rec.get("epoch") or 0), now_ms=now_ms,
                                      device_id=rec.get("device_id"),
                                      earliest_available=enroll.earliest_available(rec),
                                      still_enrolled=lambda: enroll.still_active(signed),
                                      consent=lambda: (enroll.get_enrollment(signed.operator)
                                                       or {}).get("categories"))
    except ingest.IngestError as e:
        logger.info("[WELLNESS] ingest refused: %s", e.code)
        if e.status == 413:
            return _too_large()
        if e.status == 410:
            return _json(410, {"error": "revoked"})
        return _invalid(e.detail or e.code)
    enroll.note_push(signed.operator, now_ms=now_ms)
    return result.response()


@router.post("/capabilities")
def wellness_capabilities(request: Request, raw: RawBody = Depends(_capped(REPORT_BODY))):
    if raw.too_large:
        return _too_large()
    refusal = _disabled()
    if refusal is not None:
        return refusal
    signed, refusal = _signed(request, raw)
    if refusal is not None:
        return refusal
    report, refusal = _parse(schema.CapabilityReport, raw)
    if refusal is not None:
        return refusal
    if signed.which == "active":
        enroll.record_capabilities(signed.operator, report.model_dump())
    return {"ok": True}


# ------------------------------------------------------------------ pause / resume / disconnect / erase

def _signed_operator(request: Request, raw: RawBody, model):
    """(body, signed, refusal) for a route that needs the operator's own phone."""
    signed, refusal = _signed(request, raw)
    if refusal is not None:
        return None, None, refusal
    body, refusal = _parse(model, raw)
    if refusal is not None:
        return None, None, refusal
    if signed.operator != body.operator or signed.which != "active":
        return None, None, _json(403, {"error": "not_allowed"})
    return body, signed, None


@router.post("/pause")
def wellness_pause(raw: RawBody = Depends(_capped(SMALL_BODY))):
    body, refusal = _parse(schema.OperatorBody, raw)
    if refusal is not None:
        return refusal
    try:
        return enroll.pause(body.operator)
    except enroll.EnrollError as e:
        return _refuse(e)


@router.post("/resume")
def wellness_resume(request: Request, raw: RawBody = Depends(_capped(SMALL_BODY))):
    refusal = _disabled()
    if refusal is not None:
        return refusal
    body, signed, refusal = _signed_operator(request, raw, schema.OperatorBody)
    if refusal is not None:
        return refusal
    try:
        return enroll.resume(body.operator)
    except enroll.EnrollError as e:
        return _refuse(e)


@router.post("/disconnect")
def wellness_disconnect(request: Request, raw: RawBody = Depends(_capped(SMALL_BODY))):
    body, refusal = _parse(schema.DisconnectBody, raw)
    if refusal is not None:
        return refusal
    if body.keep_history:
        try:
            return enroll.disconnect_keep_history(body.operator)
        except enroll.EnrollError as e:
            return _refuse(e)
    _body, _signed_ctx, refusal = _signed_operator(request, raw, schema.DisconnectBody)
    if refusal is not None:
        return refusal
    erase.erase_operator(body.operator, reason="disconnect")
    return {"state": "not_set_up"}


@router.post("/erase")
def wellness_erase(request: Request, raw: RawBody = Depends(_capped(SMALL_BODY))):
    body, _signed_ctx, refusal = _signed_operator(request, raw, schema.OperatorBody)
    if refusal is not None:
        return refusal
    erase.erase_operator(body.operator, reason="erase")
    return {"state": "not_set_up"}


@router.post("/sync-now")
def wellness_sync_now(raw: RawBody = Depends(_capped(SMALL_BODY))):
    refusal = _disabled()
    if refusal is not None:
        return refusal
    body, refusal = _parse(schema.OperatorBody, raw)
    if refusal is not None:
        return refusal
    return {"nudged": enroll.nudge(body.operator)}


# ------------------------------------------------------------------ settings

@router.put("/settings")
def wellness_settings(request: Request, raw: RawBody = Depends(_capped(SMALL_BODY))):
    body, refusal = _parse(schema.SettingsBody, raw)
    if refusal is not None:
        return refusal
    signed, refusal = _signed(request, raw, required=False)
    if refusal is not None:
        return refusal
    credentialed = bool(signed and signed.operator == body.operator and signed.which == "active")
    if credentialed:
        refusal = _disabled()
        if refusal is not None:
            return refusal
    try:
        enroll.update_settings(body, credentialed=credentialed,
                               surface="android" if credentialed else "any")
    except enroll.EnrollError as e:
        return _refuse(e)
    # Only the enrolled phone gets the settings view back: a narrowing change
    # is accepted from any surface, and its answer must not read more than
    # the unsigned status does (rule W17).
    return full_status(body.operator) if credentialed else _minimal_status(body.operator)


# ------------------------------------------------------------------ policy, refs, disclosures

@router.get("/privacy-policy")
def wellness_privacy_policy():
    return Response(content=POLICY_PATH.read_text(encoding="utf-8"),
                    media_type="text/markdown; charset=utf-8",
                    headers={"X-Policy-Version": catalog.POLICY_VERSION})


@router.get("/ref/{ref}")
def wellness_ref(ref: str, request: Request, raw: RawBody = Depends(_capped(SMALL_BODY))):
    refusal = _disabled()
    if refusal is not None:
        return refusal
    signed, refusal = _signed(request, raw)
    if refusal is not None:
        return refusal
    if signed.which != "active":
        return _json(403, {"error": "not_allowed"})
    st = enroll.store_for(signed.record, create=False)
    try:
        row = st.get_delivery(ref) if st is not None else None
    except store_mod.StoreGone:
        row = None
    if row is None:
        return _json(404, {"error": "unknown_ref"})
    return {"ref": row["ref"], "ts": row["ts"], "tool": row["tool"],
            "categories": row["categories"], "payload": row["payload"]}


@router.get("/disclosures")
def wellness_disclosures(request: Request, raw: RawBody = Depends(_capped(SMALL_BODY))):
    refusal = _disabled()
    if refusal is not None:
        return refusal
    signed, refusal = _signed(request, raw)
    if refusal is not None:
        return refusal
    if signed.which != "active":
        return _json(403, {"error": "not_allowed"})
    st = enroll.store_for(signed.record, create=False)
    try:
        rows = disclosure_log(st) if st is not None else []
    except store_mod.StoreGone:
        rows = []
    return {"disclosures": rows}


# "Who saw my health data": where each delivery was asked from, in the words
# the Portal's Health section uses (wellness-section.js SURFACE_LABELS), so
# the native app and the in-app Portal show the same row. The Portal inside
# the app (portal_webview, a turn its bridge signed and labelled) reads
# apart from the native chat.
SURFACE_LABELS = {"android": "BlackBox app", "portal_webview": "Portal in the app",
                  "portal": "Portal", "cron": "scheduled", "voice": "voice",
                  "on_device": "on-device AI"}
OTHER_SURFACE_LABEL = "another surface"


def disclosure_log(st) -> list:
    """The newest 200 deliveries, each time shown with the offset of its own
    day (may raise StoreGone). `surface` is the stored id, `surface_label`
    its words (SURFACE_LABELS)."""
    offsets = st.offsets()
    return [{"time": api.local_iso(r["ts"], offsets.offset_at(r["ts"])),
             "surface": r["surface"],
             "surface_label": SURFACE_LABELS.get(r["surface"], OTHER_SURFACE_LABEL),
             "provider": r["provider"], "model": r["model"],
             "tool": r["tool"], "categories": r["categories"]}
            for r in st.disclosures(limit=200)]
