"""Enrollment: which phone is each operator's health source (spec §3.1).

Stores (all 0600, written through Orchestrator/utils/atomic_json.py, read
fresh on every request):

  data/wellness/enrollments.json   {operator: record}  (schema.EnrollmentRecord)
  data/wellness/revoked.json       {enrollment_id: {reason, at}}  tombstones, so
                                   a revoked phone's next push gets 410
  credentials/wellness_device_keys.json   {enrollment_id: {k, epoch, created_at}}

Rules enforced here:

  E1  at most one active source per operator (409 already_enrolled unless replace)
  E2  a device is the source of at most one operator (409 device_in_use)
  E3  the registry owner of the device equals the operator at enrollment AND
      on every signed request; a re-homed device moves the enrollment to
      ownership_changed and answers 409 owner_changed
  E4  the phone's chosen operator equals the operator (checked by the phone
      at bind time: the bind carries the operator)
  E5  only a live human operator enrolls (not blank, system or the Flight
      Recorder, and on the roster)
  E6  other operators of the same person are not linked (nothing to do)
  E7  one person per Health Connect profile, adult (attested on the phone)

The handshake: the phone POSTs /wellness/enroll with its 2-minute intent
nonce; the box resolves the device from the request's own address (never from
the body), checks the owner and that a pairing secret exists for that
address, mints a 256-bit key K and POSTs it to http://<that address>:8765/
wellness/bind with the pairing bearer. The real phone accepts only its own
live nonce, and WireGuard binds each tailnet address to its node key, so a
peer that planted a pairing secret for someone else's address can never
receive the bind. Only a 200 from the bind persists anything.

Replacing a source: the old phone is probed (30 s). If it answers, the new
enrollment waits as `pending` until the old phone approves with a signed POST
/wellness/replace-approve; if it does not answer, the replacement proceeds and
the old phone is told. Either way the old key is revoked and its next push
gets 410 source_moved. Stored data belongs to the operator and stays.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import ipaddress
import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional, Tuple

from Orchestrator.utils import atomic_json
from Orchestrator.wellness import catalog, coverage, origin_proof, paths, schema, store

logger = logging.getLogger(__name__)

PHONE_PORT = 8765
BIND_TIMEOUT_S = 10.0
REPLACE_PROBE_TIMEOUT_S = 30.0
PHONE_CALL_TIMEOUT_S = 5.0
REPLACE_PENDING_TTL_S = 24 * 3600
TOMBSTONE_KEEP_DAYS = 400
SYNC_INTERVAL_MIN = 60
HISTORY_DAYS_WITHOUT_GRANT = 30

STORED_LIVE = frozenset({"active", "paused"})
LIVE_STATES = frozenset({"active", "paused", "stale", "needs_attention"})
ATTENTION_TROUBLES = ("permission_revoked", "battery_restricted", "source_unreachable")
PROXY_PEERS = frozenset({"127.0.0.1", "::1"})   # where tailscale serve connects from

NOTICE_SELECT = ("Finish setting up Health on this phone", "Open BlackBox, Settings, Health")
NOTICE_REPLACE_ASK = ("Approve a new Health phone", "Open BlackBox, Settings, Health")
NOTICE_MOVED = ("Health moved to another phone", "Open BlackBox, Settings, Health")
NOT_PAIRED_FIX = "Open the BlackBox app once on this phone"
UNKNOWN_DEVICE_FIX = ("Open the BlackBox app once on this phone so it appears in Devices, "
                      "then try again")

_ENROLL_LOCK = threading.RLock()


class EnrollError(Exception):
    """A refusal with its HTTP status and the contract's error code."""

    def __init__(self, status: int, code: str, *, fix: Optional[str] = None,
                 detail: Optional[str] = None, retry_after: Optional[int] = None):
        self.status, self.code, self.fix, self.detail = status, code, fix, detail
        self.retry_after = retry_after
        super().__init__(code)

    def body(self) -> dict:
        out = {"error": self.code}
        if self.fix:
            out["fix"] = self.fix
        if self.detail:
            out["detail"] = self.detail
        return out


# ------------------------------------------------------------------ time

def _now_ms(now_ms: Optional[int]) -> int:
    return int(time.time() * 1000) if now_ms is None else int(now_ms)


def iso(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ enrollments.json

def _valid_map(data) -> bool:
    return isinstance(data, dict)


def all_enrollments() -> Dict[str, dict]:
    data = atomic_json.read_json(paths.enrollments_path(), {}, valid=_valid_map)
    return {op: rec for op, rec in data.items() if isinstance(rec, dict)}


def get_enrollment(operator: Optional[str]) -> Optional[dict]:
    if not operator:
        return None
    return all_enrollments().get(operator)


@contextlib.contextmanager
def editing() -> Iterator[Dict[str, dict]]:
    paths.ensure_private_dir()
    with atomic_json.update_json(paths.enrollments_path(), {}, valid=_valid_map) as data:
        yield data


def _normalize(record: dict) -> dict:
    return schema.EnrollmentRecord.model_validate(record).model_dump()


def find_by_enrollment_id(enr: str) -> Tuple[Optional[str], Optional[dict], Optional[str]]:
    """(operator, record, "active" | "pending") for an enrollment id."""
    for op, rec in all_enrollments().items():
        if rec.get("enrollment_id") == enr:
            return op, rec, "active"
        pend = rec.get("pending") or {}
        if pend.get("enrollment_id") == enr:
            return op, rec, "pending"
    return None, None, None


# ------------------------------------------------------------------ keys and tombstones

def _keys() -> Dict[str, dict]:
    return atomic_json.read_json(paths.keys_path(), {}, valid=_valid_map)


def key_for(enr: str) -> Optional[bytes]:
    row = _keys().get(enr)
    if not isinstance(row, dict):
        return None
    try:
        return origin_proof.decode_key(row.get("k", ""))
    except ValueError:
        return None


def store_key(enr: str, key: bytes, epoch: int, now_ms: int) -> None:
    with atomic_json.update_json(paths.keys_path(), {}, valid=_valid_map) as keys:
        keys[enr] = {"k": origin_proof.encode_key(key), "epoch": int(epoch),
                     "created_at": iso(now_ms)}


def revoke_key(enr: Optional[str], reason: str, now_ms: int) -> None:
    """Forget a device key and remember why (the phone's next call gets 410)."""
    if not enr:
        return
    with atomic_json.update_json(paths.keys_path(), {}, valid=_valid_map) as keys:
        keys.pop(enr, None)
    tombstone(enr, reason, now_ms)
    origin_proof.NONCES.forget(enr)


def tombstone(enr: str, reason: str, now_ms: int) -> None:
    paths.ensure_private_dir()
    horizon = iso(now_ms - TOMBSTONE_KEEP_DAYS * 86_400_000)
    with atomic_json.update_json(paths.revoked_path(), {}, valid=_valid_map) as tomb:
        for old in [k for k, v in tomb.items() if not isinstance(v, dict) or
                    (v.get("at") or "") < horizon]:
            del tomb[old]
        tomb[enr] = {"reason": reason, "at": iso(now_ms)}


def tombstone_reason(enr: str) -> Optional[str]:
    row = atomic_json.read_json(paths.revoked_path(), {}, valid=_valid_map).get(enr)
    return row.get("reason") if isinstance(row, dict) else None


# ------------------------------------------------------------------ roster and devices

def live_humans() -> List[str]:
    from Orchestrator import config
    from Orchestrator.wellness.gate import live_humans as humans
    return humans(config.USERS_LIST, config.RESERVED_OPERATORS)


def require_live_human(operator: str) -> str:
    from Orchestrator.utils.operators import InvalidOperatorName, validate_operator
    try:
        validate_operator(operator)
    except InvalidOperatorName:
        raise EnrollError(400, "invalid_operator") from None
    if operator not in live_humans():
        raise EnrollError(403, "not_allowed")
    return operator


def _registry():
    from Orchestrator.device_registry import get_registry
    return get_registry()


def device_owner(device_id: Optional[str]) -> str:
    if not device_id:
        return ""
    dev = _registry().get_device(device_id)
    return (dev.owner or "").strip() if dev is not None else ""


def resolve_device_by_ip(ip: str):
    """The registry device at this address (tailscale_ip, or one of the
    addresses its metadata records). Deterministic on duplicates: Android
    rows first, then by id."""
    matches = []
    for d in _registry().get_all_devices():
        known = {d.tailscale_ip} | set((d.metadata or {}).get("tailnet_ips") or [])
        if ip in known:
            matches.append(d)
    matches.sort(key=lambda d: (d.device_type.value != "android", d.id))
    return matches[0] if matches else None


def _is_ip(text) -> bool:
    try:
        ipaddress.ip_address(str(text).split("%", 1)[0])
        return True
    except ValueError:
        return False


def client_ip(peer: Optional[str], forwarded_for: Optional[str]) -> Optional[str]:
    """The caller's address, by the box's proxy trust rule: X-Forwarded-For
    counts only when the socket peer is the tailscale-serve loopback, and then
    only its LAST entry (the one the proxy added)."""
    peer = (peer or "").strip()
    if peer in PROXY_PEERS and forwarded_for:
        last = forwarded_for.split(",")[-1].strip()
        if _is_ip(last):
            return str(ipaddress.ip_address(last))
    return str(ipaddress.ip_address(peer)) if _is_ip(peer) else None


def is_sim_address(ip: str) -> bool:
    """A loopback address the phone simulator may use (never the proxy's own)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback and str(addr) not in PROXY_PEERS


def _sim_enabled() -> bool:
    from Orchestrator import config
    return config.wellness_simulator()


def _listener_port() -> int:
    """The phones' listener port: [control_phone] port, the knob the
    notification bus and control_phone use too (default 8765)."""
    try:
        from Orchestrator.config import CFG
        return CFG.getint("control_phone", "port", fallback=PHONE_PORT)
    except Exception:  # noqa: BLE001
        return PHONE_PORT


def phone_address(ip: str) -> Tuple[str, int]:
    """(host, port) of the phone's listener at `ip` (the test seam)."""
    if is_sim_address(ip):
        from Orchestrator import config
        return ip, config.wellness_simulator_port()
    return ip, _listener_port()


def pairing_headers(ip: str) -> Optional[Dict[str, str]]:
    """The pairing bearer for the phone at `ip`, or None when it never paired."""
    if is_sim_address(ip):
        if not _sim_enabled():
            return None
        row = atomic_json.read_json(paths.sim_pairing_path(), {}, valid=_valid_map).get(ip)
        secret = row.get("secret") if isinstance(row, dict) else None
        return {"Authorization": f"Bearer {secret}"} if secret else None
    from Orchestrator.device_registry import pairing
    secret = pairing.secret_for_ip(ip)
    return {"Authorization": f"Bearer {secret}"} if secret else None


def call_phone(ip: str, method: str, path: str, body: Optional[dict], *,
               timeout: float) -> Optional[int]:
    """One call to the phone's :8765 listener with its pairing bearer. Returns
    the HTTP status, or None when it could not be reached. Never logs a body."""
    headers = pairing_headers(ip)
    if headers is None:
        return None
    host, port = phone_address(ip)
    netloc = f"[{host}]" if ":" in host else host
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://{netloc}:{port}{path}", data=data, method=method,
                                 headers={**headers, "Content-Type": "application/json"})
    # Straight to the phone: never through an environment HTTP proxy, never
    # following a redirect anywhere else.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # never follow a phone's redirect anywhere
        return None


def send_device_notice(operator: str, device_id: str, ip: str, title: str, body: str) -> None:
    """One neutral notification to one device through the bus. Best effort."""
    from Orchestrator.notifications import bridge, bus
    coro = bus.notify_device(operator, device_id, ip, title, body, category="wellness")
    loop = bridge.get_main_loop()
    try:
        if loop is not None:
            asyncio.run_coroutine_threadsafe(coro, loop)
            return
        asyncio.run(coro)
    except Exception as e:  # noqa: BLE001 - a notice never fails the request
        coro.close()
        logger.info("[WELLNESS] notice not delivered (%s)", type(e).__name__)


# ------------------------------------------------------------------ derived state

def effective_state(record: Optional[dict], now_ms: Optional[int] = None) -> str:
    """The state a reader sees: a stored active enrollment reads as
    needs_attention or stale when its troubles or its last push say so."""
    if not record:
        return "not_set_up"
    st = record.get("state") or "not_set_up"
    if st != "active":
        return st
    troubles = record.get("phone_troubles") or []
    if any(t.split(":", 1)[0] in ATTENTION_TROUBLES for t in troubles):
        return "needs_attention"
    last = record.get("last_push_at") or record.get("created_at")
    if coverage.is_stale(last, _now_ms(now_ms)):
        return "stale"
    return "active"


def source_label(report: Optional[dict]) -> Optional[str]:
    phone = (report or {}).get("phone") or {}
    name, model = phone.get("device_name"), phone.get("model")
    if name and model and model not in name:
        return f"{name} ({model})"
    return name or model


def troubles_for(record: dict, now_ms: Optional[int] = None) -> List[dict]:
    """The enrollment's trouble states with their wording (no values)."""
    now_ms = _now_ms(now_ms)
    codes = list(record.get("phone_troubles") or [])
    if record.get("state") == "ownership_changed":
        codes.append("ownership_changed")
    elif record.get("state") == "active" and coverage.is_stale(
            record.get("last_push_at") or record.get("created_at"), now_ms):
        codes.append("stale_sync")
    st = store_for(record, create=False)
    if st is not None:
        today = iso(now_ms)[:10]
        days = st.coverage_days(store.add_days(today, -14), today)
        if days:
            latest = days[max(days)]
            if latest.get("hr_mode") == coverage.MODE_MANUAL:
                codes.append("hr_mode_manual")
            nights = [days.get(store.add_days(today, -k), {}).get("night_worn")
                      for k in range(coverage.NOT_WORN_LOOKBACK_NIGHTS)]
            worn_before = any(d.get("night_worn") == 1 for d in days.values())
            if coverage.not_worn_likely(nights, worn_before):
                codes.append("not_worn_likely")
    last = record.get("last_push_at")
    out = []
    for code in dict.fromkeys(codes):
        out.append(coverage.trouble(code, last_sync=last[11:16] if last else "a while"))
    return out


def store_for(record: Optional[dict], create: bool = True) -> Optional[store.Store]:
    key = (record or {}).get("op_key")
    if not paths.valid_op_key(key):
        return None
    if not create and not paths.db_path(key).exists():
        return None
    return store.open_store(key)


# ------------------------------------------------------------------ select (Portal)

def select(operator: str, device_id: str, *, now_ms: Optional[int] = None) -> dict:
    """"Use for health" on the Portal: mark the operator's own Android device
    as awaiting_phone and send it ONE neutral notice."""
    now_ms = _now_ms(now_ms)
    require_live_human(operator)
    dev = _registry().get_device(device_id)
    if dev is None:
        raise EnrollError(404, "unknown_device")
    if (dev.owner or "").strip() != operator:
        raise EnrollError(403, "not_owner")
    if dev.device_type.value != "android":
        raise EnrollError(422, "not_android",
                          fix="Health tracking needs the BlackBox Android app with Health Connect")
    with _ENROLL_LOCK:
        for op, rec in all_enrollments().items():
            if op != operator and rec.get("device_id") == device_id and \
                    rec.get("state") in STORED_LIVE:
                raise EnrollError(409, "device_in_use")
        with editing() as data:
            rec = data.get(operator)
            if rec and rec.get("state") in STORED_LIVE | {"replace_pending"}:
                raise EnrollError(409, "already_enrolled")
            base = rec or {"operator": operator, "op_key": store.new_op_key(),
                           "created_at": iso(now_ms)}
            base.update({"operator": operator, "state": "awaiting_phone", "device_id": device_id,
                         "tailnet_ip": dev.tailscale_ip, "updated_at": iso(now_ms)})
            data[operator] = _normalize(base)
    send_device_notice(operator, device_id, dev.tailscale_ip, *NOTICE_SELECT)
    logger.info("[WELLNESS] select: awaiting_phone")
    return {"state": "awaiting_phone"}


# ------------------------------------------------------------------ enroll (phone)

def _expire_pending(rec: dict, now_ms: int) -> dict:
    """Drop a replacement the old phone never answered (24 h): its key is
    revoked and the pending entry cleared on disk."""
    pend = rec.get("pending")
    if pend and pend.get("expires_at_ms", 0) < now_ms:
        revoke_key(pend.get("enrollment_id"), "revoked", now_ms)
        with editing() as data:
            cur = data.get(rec.get("operator"))
            if cur and (cur.get("pending") or {}).get("enrollment_id") == \
                    pend.get("enrollment_id"):
                cur["pending"] = None
        rec = dict(rec, pending=None)
    return rec


def _probe(ip: Optional[str]) -> bool:
    if not ip:
        return False
    status = call_phone(ip, "GET", "/healthz", None, timeout=REPLACE_PROBE_TIMEOUT_S)
    return status is not None and 200 <= status < 300


def enroll(body: schema.EnrollBody, ip: Optional[str], *,
           now_ms: Optional[int] = None) -> dict:
    """The enroll handshake. Raises EnrollError with the contract's codes."""
    now_ms = _now_ms(now_ms)
    from Orchestrator import config
    if not config.wellness_enabled():
        raise EnrollError(503, "wellness_disabled", retry_after=3600)
    operator = require_live_human(body.operator)
    if not ip:
        raise EnrollError(400, "no_client_address")
    if is_sim_address(ip) and not _sim_enabled():
        raise EnrollError(404, "unknown_device", fix=UNKNOWN_DEVICE_FIX)
    dev = resolve_device_by_ip(ip)
    if dev is None:
        raise EnrollError(404, "unknown_device", fix=UNKNOWN_DEVICE_FIX)
    owner = (dev.owner or "").strip()
    if owner and owner != operator:
        raise EnrollError(409, "owner_mismatch",
                          detail="This phone is assigned to someone else in Devices")
    if not owner and not body.assign_to_me:
        raise EnrollError(409, "owner_mismatch",
                          detail="This phone is not assigned to anyone yet; tick "
                                 "\"Assign this phone to me\"")
    report = body.capability_report.model_dump() if body.capability_report else None
    # Probe a replaced phone (up to 30 s) before taking the lock, so a replace
    # never holds up anyone else's enrollment, pause or settings change.
    before = get_enrollment(operator) or {}
    old_online = None
    if body.replace and before.get("state") in STORED_LIVE and before.get("device_id") != dev.id:
        old_online = _probe(before.get("tailnet_ip"))
    with _ENROLL_LOCK:
        existing = get_enrollment(operator)
        if existing:
            existing = _expire_pending(existing, now_ms)
        live = existing and existing.get("state") in STORED_LIVE
        if live and not body.replace:
            raise EnrollError(409, "already_enrolled")
        for op, rec in all_enrollments().items():
            if op != operator and rec.get("device_id") == dev.id and \
                    rec.get("state") in STORED_LIVE:
                raise EnrollError(409, "device_in_use")
        if pairing_headers(ip) is None:
            raise EnrollError(424, "not_paired", fix=NOT_PAIRED_FIX)
        same_device = bool(live and existing.get("device_id") == dev.id)
        if live and not same_device and old_online is None:
            old_online = _probe(existing.get("tailnet_ip"))   # the source changed meanwhile
        pending = bool(live and not same_device and old_online)
        epoch = int((existing or {}).get("epoch") or 0) + 1
        if pending and (existing.get("pending") or {}).get("epoch"):
            epoch = max(epoch, int(existing["pending"]["epoch"]) + 1)
        enr = "en_" + secrets.token_hex(4)
        key = origin_proof.new_key()
        status = call_phone(ip, "POST", "/wellness/bind",
                            {"operator": operator, "enrollment_id": enr, "epoch": epoch,
                             "nonce": body.nonce, "k": origin_proof.encode_key(key)},
                            timeout=BIND_TIMEOUT_S)
        if status != 200:
            logger.info("[WELLNESS] enroll: bind failed (status=%s)", status)
            raise EnrollError(502, "bind_failed")
        # ---- the phone holds K: persist
        if not owner:
            _registry().update_device(dev.id, owner=operator, is_primary=False)
        store_key(enr, key, epoch, now_ms)
        label = source_label(report) or dev.name
        categories = dict(catalog.DEFAULT_CONSENT)
        categories.update((existing or {}).get("categories") or {})
        if body.categories:
            categories.update(body.categories)
        with editing() as data:
            rec = dict(data.get(operator) or {"operator": operator,
                                              "op_key": store.new_op_key(),
                                              "created_at": iso(now_ms)})
            if pending:
                rec["pending"] = {"enrollment_id": enr, "epoch": epoch, "device_id": dev.id,
                                  "tailnet_ip": ip, "install_id": body.install_id,
                                  "source_label": label, "categories": categories,
                                  "created_at": iso(now_ms),
                                  "expires_at_ms": now_ms + REPLACE_PENDING_TTL_S * 1000}
            else:
                rec.update({"enrollment_id": enr, "epoch": epoch, "state": "active",
                            "device_id": dev.id, "tailnet_ip": ip,
                            "install_id": body.install_id, "categories": categories,
                            "source_label": label, "pending": None, "last_push_at": None,
                            "created_at": rec.get("created_at") or iso(now_ms),
                            "policy_version": catalog.POLICY_VERSION})
            rec["updated_at"] = iso(now_ms)
            if not paths.valid_op_key(rec.get("op_key")):
                rec["op_key"] = store.new_op_key()
            data[operator] = _normalize(rec)
            final = data[operator]
        if not pending:
            if existing and existing.get("enrollment_id") and live:
                reason = "revoked" if same_device else "source_moved"
                revoke_key(existing["enrollment_id"], reason, now_ms)
                if not same_device:
                    send_device_notice(operator, existing.get("device_id") or "",
                                       existing.get("tailnet_ip") or "", *NOTICE_MOVED)
        else:
            send_device_notice(operator, existing.get("device_id") or "",
                               existing.get("tailnet_ip") or "", *NOTICE_REPLACE_ASK)
    st = store_for(final)
    with st.write() as conn:
        st.record_epoch(conn, epoch, enr, dev.id, (report or {}).get("phone", {}).get("model"),
                        now_ms)
    st.consent_event(at=now_ms, change={"enroll": {"categories": categories,
                                                   "replace": bool(body.replace)}},
                     surface="android", policy_version=catalog.POLICY_VERSION)
    if report:
        record_capabilities(operator, report, now_ms=now_ms, record=final)
    logger.info("[WELLNESS] enroll: %s epoch=%d", "replace_pending" if pending else "active",
                epoch)
    return {"enrollment_id": enr, "epoch": epoch,
            "state": "replace_pending" if pending else "active", "op_key_hint": None,
            "sync_interval_min": SYNC_INTERVAL_MIN, "policy_version": catalog.POLICY_VERSION}


def approve_replace(operator: str, approve: bool, *, now_ms: Optional[int] = None) -> dict:
    """The OLD phone's answer (the route checked its signature)."""
    now_ms = _now_ms(now_ms)
    with _ENROLL_LOCK:
        rec = get_enrollment(operator)
        rec = _expire_pending(rec, now_ms) if rec else rec
        pend = (rec or {}).get("pending")
        if not pend:
            raise EnrollError(409, "no_pending_replace")
        old_enr, old_epoch = rec.get("enrollment_id"), rec.get("epoch")
        with editing() as data:
            cur = dict(data.get(operator) or rec)
            if approve:
                cur.update({"enrollment_id": pend["enrollment_id"], "epoch": pend["epoch"],
                            "state": "active", "device_id": pend["device_id"],
                            "tailnet_ip": pend["tailnet_ip"], "install_id": pend["install_id"],
                            "source_label": pend.get("source_label"),
                            "categories": pend.get("categories") or cur.get("categories"),
                            "last_push_at": None})
            cur["pending"] = None
            cur["updated_at"] = iso(now_ms)
            data[operator] = _normalize(cur)
        if approve:
            revoke_key(old_enr, "source_moved", now_ms)
            st = store_for(rec)
            with st.write() as conn:
                st.end_epoch(conn, int(old_epoch or 0), now_ms)
        else:
            revoke_key(pend["enrollment_id"], "revoked", now_ms)
    return {"state": "active"}


# ------------------------------------------------------------------ signed requests

@dataclasses.dataclass
class Signed:
    operator: str
    record: dict
    which: str            # "active" | "pending"
    enrollment_id: str


def verify_signed_request(header, method: str, path: str, body: bytes, *,
                          now_s: Optional[int] = None) -> Signed:
    """Check a phone request's origin proof, then the enrollment it names:
    410 for a revoked key, 409 when the device was re-homed (E3, on EVERY
    request), 401 for everything else that fails."""
    fields = origin_proof.parse_header(header)
    if fields is None:
        raise EnrollError(401, "bad_signature")
    enr = fields["enr"]
    reason = tombstone_reason(enr)
    if reason:
        raise EnrollError(410, reason if reason in ("source_moved", "revoked") else "revoked")
    operator, rec, which = find_by_enrollment_id(enr)
    if rec is None:
        raise EnrollError(401, "bad_signature")
    try:
        origin_proof.verify_request(header, method, path, body, lookup_key=key_for, now=now_s)
    except origin_proof.OriginProofError:
        raise EnrollError(401, "bad_signature") from None
    now_ms = _now_ms(None if now_s is None else now_s * 1000)
    state = rec.get("state")
    if which == "active":
        if state == "ownership_changed":
            raise EnrollError(409, "owner_changed")
        if state not in STORED_LIVE:
            raise EnrollError(410, "revoked")
        device_id = rec.get("device_id")
    else:
        if (rec.get("pending") or {}).get("expires_at_ms", 0) < now_ms:
            raise EnrollError(410, "revoked")
        device_id = rec["pending"].get("device_id")
    if device_owner(device_id) != operator:
        if which == "active":
            with editing() as data:
                cur = dict(data.get(operator) or rec)
                cur["state"] = "ownership_changed"
                cur["updated_at"] = iso(now_ms)
                data[operator] = _normalize(cur)
            logger.info("[WELLNESS] enrollment moved to ownership_changed (E3)")
        raise EnrollError(409, "owner_changed")
    return Signed(operator=operator, record=rec, which=which, enrollment_id=enr)


# ------------------------------------------------------------------ pushes and reports

def note_push(operator: str, *, now_ms: Optional[int] = None) -> None:
    now_ms = _now_ms(now_ms)
    with editing() as data:
        rec = data.get(operator)
        if rec:
            rec["last_push_at"] = iso(now_ms)
            rec.pop("source_unreachable", None)


def record_capabilities(operator: str, report: dict, *, now_ms: Optional[int] = None,
                        record: Optional[dict] = None) -> None:
    """Keep the latest capability report (counts and flags only) in the
    operator's store, and the troubles and label it implies in the record."""
    now_ms = _now_ms(now_ms)
    rec = record or get_enrollment(operator)
    if not rec:
        return
    st = store_for(rec)
    granted = {g.split(".")[-1] for g in ((report.get("health_connect") or {}).get("granted")
                                          or []) if isinstance(g, str)}
    earliest = None
    if (report.get("health_connect") or {}).get("granted") is not None and \
            catalog.PERMISSION_HISTORY not in granted:
        created = (rec.get("created_at") or iso(now_ms))[:10]
        earliest = (date.fromisoformat(created) -
                    timedelta(days=HISTORY_DAYS_WITHOUT_GRANT)).isoformat()
    with st.write() as conn:
        st.meta_set("capabilities", report, conn=conn)
        st.meta_set("capabilities_at", now_ms, conn=conn)
        st.meta_set("earliest_available", earliest, conn=conn)
    troubles = coverage.phone_troubles(report, rec.get("categories") or {})
    label = source_label(report)
    with editing() as data:
        cur = data.get(operator)
        if cur:
            cur["phone_troubles"] = troubles
            if label:
                cur["source_label"] = label
            cur["updated_at"] = iso(now_ms)


def earliest_available(record: dict) -> Optional[str]:
    st = store_for(record, create=False)
    return st.meta_get("earliest_available") if st is not None else None


# ------------------------------------------------------------------ pause / resume / disconnect

def _set_state(operator: str, allowed_from, new_state: str, now_ms: int) -> dict:
    with _ENROLL_LOCK, editing() as data:
        rec = data.get(operator)
        if not rec or rec.get("state") not in allowed_from:
            raise EnrollError(409, "not_enrolled")
        rec["state"] = new_state
        rec["updated_at"] = iso(now_ms)
        return dict(rec)


def pause(operator: str, *, surface: str = "any", now_ms: Optional[int] = None) -> dict:
    now_ms = _now_ms(now_ms)
    rec = _set_state(operator, {"active", "paused"}, "paused", now_ms)
    _consent(rec, {"pause": True}, surface, now_ms)
    return {"state": "paused"}


def resume(operator: str, *, surface: str = "android", now_ms: Optional[int] = None) -> dict:
    now_ms = _now_ms(now_ms)
    rec = _set_state(operator, {"paused", "active"}, "active", now_ms)
    _consent(rec, {"resume": True}, surface, now_ms)
    return {"state": effective_state(rec, now_ms)}


def unbind_phone(record: dict) -> Optional[int]:
    ip = record.get("tailnet_ip")
    if not ip:
        return None
    return call_phone(ip, "POST", "/wellness/unbind",
                      {"operator": record.get("operator"),
                       "enrollment_id": record.get("enrollment_id")},
                      timeout=PHONE_CALL_TIMEOUT_S)


def disconnect_keep_history(operator: str, *, surface: str = "any",
                            now_ms: Optional[int] = None) -> dict:
    """Stop collecting and keep what was stored: the key is revoked, the phone
    told to forget, the record kept (op_key, settings) for a later re-enroll."""
    now_ms = _now_ms(now_ms)
    with _ENROLL_LOCK:
        rec = get_enrollment(operator)
        if not rec or rec.get("state") in ("revoked",):
            raise EnrollError(409, "not_enrolled")
        unbind_phone(rec)
        revoke_key(rec.get("enrollment_id"), "revoked", now_ms)
        if rec.get("pending"):
            revoke_key(rec["pending"].get("enrollment_id"), "revoked", now_ms)
        with editing() as data:
            cur = dict(data.get(operator) or rec)
            cur.update({"state": "revoked", "pending": None, "updated_at": iso(now_ms)})
            data[operator] = _normalize(cur)
    _consent(rec, {"disconnect": {"keep_history": True}}, surface, now_ms)
    return {"state": "revoked"}


def mark_unreachable(operator: str, now_ms: Optional[int] = None) -> None:
    with editing() as data:
        rec = data.get(operator)
        if rec:
            troubles = [t for t in rec.get("phone_troubles") or [] if t != "source_unreachable"]
            rec["phone_troubles"] = troubles + ["source_unreachable"]


def nudge(operator: str) -> bool:
    """Ask the source phone to sync now (POST :8765/wellness/sync)."""
    rec = get_enrollment(operator)
    if not rec or rec.get("state") not in STORED_LIVE:
        return False
    status = call_phone(rec.get("tailnet_ip") or "", "POST", "/wellness/sync",
                        {"operator": operator, "enrollment_id": rec.get("enrollment_id")},
                        timeout=PHONE_CALL_TIMEOUT_S)
    ok = status is not None and 200 <= status < 300
    if not ok:
        mark_unreachable(operator)
    return ok


# ------------------------------------------------------------------ settings (W21)

_RANK = {"off": 0, "ask": 1, "proactive": 2}


def _consent(rec: dict, change: dict, surface: str, now_ms: int) -> None:
    st = store_for(rec, create=False)
    if st is not None:
        st.consent_event(at=now_ms, change=change, surface=surface,
                         policy_version=catalog.POLICY_VERSION)


def widening(rec: dict, body: schema.SettingsBody, providers: Optional[List[str]]) -> List[str]:
    """What in `body` widens access (needs the enrolled phone's signature)."""
    out = []
    for cat, val in (body.categories or {}).items():
        if _RANK[val] > _RANK.get((rec.get("categories") or {}).get(cat, "off"), 0):
            out.append(f"categories.{cat}")
    for cat, val in (body.local_only or {}).items():
        if (rec.get("local_only") or {}).get(cat, True) and val is False:
            out.append(f"local_only.{cat}")
    if providers is not None:
        added = set(providers) - set(rec.get("providers_allowed") or [])
        out += [f"providers_allowed.{p}" for p in sorted(added)]
    if body.google_paid_key_confirmed and not rec.get("google_paid_key_confirmed"):
        out.append("google_paid_key_confirmed")
    if body.surfaces_mode is not None and body.surfaces_mode != rec.get("surfaces_mode"):
        if body.surfaces_mode == "household" or (body.surfaces_mode == "auto" and
                                                 rec.get("surfaces_mode") == "credentialed"):
            out.append("surfaces_mode")
    return out


def update_settings(body: schema.SettingsBody, *, credentialed: bool, surface: str,
                    now_ms: Optional[int] = None) -> dict:
    """Apply a settings change. Widening needs the signature (W21); narrowing
    and the instinct prompt are accepted from any surface for that operator."""
    from Orchestrator.wellness import destinations
    now_ms = _now_ms(now_ms)
    operator = body.operator
    with _ENROLL_LOCK:
        rec = get_enrollment(operator)
        if not rec:
            raise EnrollError(409, "not_enrolled")
        providers = None
        if body.providers_allowed is not None:
            try:
                providers = destinations.ensure_known(body.providers_allowed)
            except ValueError as e:
                raise EnrollError(422, "invalid", detail=str(e)) from None
        wide = widening(rec, body, providers)
        if wide and not credentialed:
            raise EnrollError(403, "signature_required",
                              fix="Change this in Settings, Health, on your phone",
                              detail=", ".join(wide))
        change = {}
        with editing() as data:
            cur = dict(data.get(operator) or rec)
            if body.categories:
                cur["categories"] = {**(cur.get("categories") or {}), **body.categories}
                change["categories"] = body.categories
            if body.local_only:
                cur["local_only"] = {**(cur.get("local_only") or {}), **body.local_only}
                change["local_only"] = body.local_only
            if providers is not None:
                cur["providers_allowed"] = providers
                change["providers_allowed"] = providers
            if body.google_paid_key_confirmed is not None:
                cur["google_paid_key_confirmed"] = body.google_paid_key_confirmed
                change["google_paid_key_confirmed"] = body.google_paid_key_confirmed
            if body.surfaces_mode is not None:
                cur["surfaces_mode"] = body.surfaces_mode
                change["surfaces_mode"] = body.surfaces_mode
            if "instinct_prompt" in body.model_fields_set:
                cur["instinct_prompt"] = body.instinct_prompt or None
                change["instinct_prompt"] = "custom" if body.instinct_prompt else "default"
            cur["updated_at"] = iso(now_ms)
            data[operator] = _normalize(cur)
            rec = data[operator]
    if change:
        _consent(rec, change, surface, now_ms)
    return rec


def instinct_prompt(operator: str) -> str:
    """The operator's health instinct prompt (wave 2 injects it with the tools)."""
    rec = get_enrollment(operator) or {}
    return rec.get("instinct_prompt") or catalog.DEFAULT_INSTINCT_PROMPT


def mesh_health(operator: Optional[str]) -> Optional[Tuple[str, str]]:
    """(device_id, state) of `operator`'s own health source, for the Devices
    view's owner-only badge, or None (rule W17: never about anyone else)."""
    if not operator:
        return None
    try:
        rec = get_enrollment(operator)
    except Exception:  # noqa: BLE001 - the Devices view never fails over health
        return None
    if not rec or not rec.get("device_id"):
        return None
    state = effective_state(rec)
    if state in ("revoked", "not_set_up"):
        return None
    return rec["device_id"], state
