#!/usr/bin/env python3
"""
gateway_manager.py - TG200 Gateway Discovery, Configuration, and Management

Handles:
- Gateway persistence (gateways.json)
- Auto-discovery of TG200 units on the local network
- Gateway status checking (SIP registration, SIM info)
- PJSIP config generation for discovered gateways
- SMS via TG200 HTTP API
"""

import asyncio
import copy
import json
import os
import socket
import time
import uuid
from typing import Optional, Dict, List, Any

import aiohttp

from Orchestrator.asterisk.config import (
    GATEWAYS_FILE,
    TG200_DEFAULT_IP,
    TG200_SIP_PORT,
    TG200_HTTP_PORT,
)
from Orchestrator.asterisk import secrets
from Orchestrator import config as _root_config


# ---------------------------------------------------------------------------
# Model / span helpers
# ---------------------------------------------------------------------------
# NeoGate TG spans start at 2 (span = 2 + slot). One GSM port per slot.
MODEL_PORTS = {"TG100": 1, "TG200": 2, "TG400": 4, "TG800": 8}

_DEFAULT_MODEL = "TG200"
_DEFAULT_PORT_COUNT = 2


def port_count(model: str) -> int:
    """Number of GSM ports for a model. Defaults to 2 for unknown models."""
    return MODEL_PORTS.get(model, _DEFAULT_PORT_COUNT)


def slot_to_span(slot: int) -> int:
    """Map a 0-based slot index to its NeoGate TG span (span = 2 + slot)."""
    return 2 + slot


def spans_for_model(model: str) -> list:
    """List of GSM spans for a model, e.g. TG800 -> [2,3,4,5,6,7,8,9]."""
    return [slot_to_span(slot) for slot in range(port_count(model))]


def _model_from_capacity(capacity: int) -> str:
    """Derive a model name from a port capacity (default TG200)."""
    for model, count in MODEL_PORTS.items():
        if count == capacity:
            return model
    return _DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Discovery fingerprinting (pure helpers — unit-testable without sockets)
# ---------------------------------------------------------------------------
# Body tokens that identify a NeoGate when paired with a generic Boa server.
_NEOGATE_BODY_TOKENS = ("neogate", "astman", "mypbx", "webcgi", "yeastar")

# Standalone body keywords that identify a Yeastar/NeoGate regardless of server.
_NEOGATE_BODY_KEYWORDS = (
    "yeastar", "neogate",
    "tg100", "tg200", "tg400", "tg800",
    "gsm gateway", "voip gateway",
)

# Model numbers to scan for in the page body, longest-first so "TG1600" wins
# over a substring match against shorter ids.
_MODEL_SCAN_ORDER = ("TG1600", "TG800", "TG400", "TG200", "TG100")


def _looks_like_neogate(server_header: str, body: str) -> bool:
    """Fingerprint a Yeastar/NeoGate TG gateway from an HTTP response.

    Matches if ANY of:
      - the ``Server`` header contains ``boa`` AND the body contains a NeoGate
        token (Boa alone is a generic embedded server and must NOT match), OR
      - the body contains any known Yeastar/NeoGate keyword, OR
      - the ``Server`` header contains ``yeastar``.
    """
    server = (server_header or "").lower()
    body_l = (body or "").lower()

    if "yeastar" in server:
        return True
    if any(kw in body_l for kw in _NEOGATE_BODY_KEYWORDS):
        return True
    if "boa" in server and any(tok in body_l for tok in _NEOGATE_BODY_TOKENS):
        return True
    return False


def _detect_model(body: str) -> str:
    """Detect the NeoGate TG model from a page body (default ``TG200``)."""
    body_u = (body or "").upper()
    for model in _MODEL_SCAN_ORDER:
        if model in body_u:
            return model
    return _DEFAULT_MODEL


def _ami_block() -> dict:
    """Build the AMI creds block from config. Never hardcodes a secret literal."""
    return {
        "port": _root_config.ASTERISK_AMI_PORT,
        "user": _root_config.ASTERISK_AMI_USER or "blackbox",
        "secret": _root_config.ASTERISK_AMI_SECRET or "",
    }


def _build_ports(model: str, phone_numbers: list) -> list:
    """Build the ports[] array for a model, distributing phone numbers into
    the first ports."""
    phone_numbers = phone_numbers or []
    if len(phone_numbers) > port_count(model):
        print(
            f"[GatewayManager] WARNING: {len(phone_numbers)} phone_numbers exceed "
            f"{port_count(model)} ports for {model}; extra numbers dropped"
        )
    ports = []
    for slot in range(port_count(model)):
        ports.append({
            "span": slot_to_span(slot),
            "slot": slot,
            "phone_number": phone_numbers[slot] if slot < len(phone_numbers) else "",
            "carrier": "",
            "enabled": True,
            "operator": "",
        })
    return ports


# ---------------------------------------------------------------------------
# Gateway data model
# ---------------------------------------------------------------------------
def _new_gateway(
    name: str,
    ip: str,
    sip_port: int = 5060,
    http_port: int = 80,
    http_user: str = "admin",
    http_password: str = "password",
    phone_numbers: list = None,
    capacity: int = 2,
    codec: str = "g722",
    model: str = None,
) -> dict:
    """Create a new v2 gateway config dict.

    Backward-compatible signature: still accepts the legacy kwargs the route
    passes. `model` is optional; if omitted it is derived from `capacity`.
    """
    model = model or _model_from_capacity(capacity)
    return {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "model": model,
        "ip": ip,
        "enabled": True,
        "sip_port": sip_port,
        "http_port": http_port,
        "codec": codec,
        "trunk_name": f"tg-{name.lower().replace(' ', '-')}",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "http": {"user": http_user, "password": http_password},
        "ami": _ami_block(),
        "ports": _build_ports(model, phone_numbers),
    }


# ---------------------------------------------------------------------------
# Migration (legacy flat dict -> v2)
# ---------------------------------------------------------------------------
def migrate_gateway(gw: dict) -> dict:
    """Upgrade a legacy flat gateway record to the v2 shape.

    Idempotent: a record already in v2 shape (dict `http` + dict `ami` +
    list `ports`) is returned unchanged.
    """
    if (
        isinstance(gw.get("http"), dict)
        and isinstance(gw.get("ami"), dict)
        and isinstance(gw.get("ports"), list)
    ):
        # Already v2. Ensure http_port exists (added after the initial v2
        # cutover). Return a COPY when adding it so we never mutate the caller's
        # dict — and so load_gateways' `migrated != data` compare sees the
        # change and re-saves. A record that already has http_port is unchanged.
        if "http_port" not in gw:
            return {**gw, "http_port": 80}
        return gw

    capacity = gw.get("capacity", _DEFAULT_PORT_COUNT)
    model = gw.get("model") or _model_from_capacity(capacity)
    name = gw.get("name", "")
    return {
        "id": gw.get("id", str(uuid.uuid4())[:8]),
        "name": name,
        "model": model,
        "ip": gw.get("ip", ""),
        "enabled": gw.get("enabled", True),
        "sip_port": gw.get("sip_port", 5060),
        "http_port": gw.get("http_port", 80),
        "codec": gw.get("codec", "g722"),
        "trunk_name": gw.get("trunk_name") or f"tg-{name.lower().replace(' ', '-')}",
        "created_at": gw.get("created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        "http": {
            "user": gw.get("http_user", "admin"),
            "password": gw.get("http_password", ""),
        },
        "ami": _ami_block(),
        "ports": _build_ports(model, gw.get("phone_numbers", [])),
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def load_gateways() -> List[dict]:
    """Load gateway configs from disk, migrating any legacy records to v2.

    If migration changed any record, the upgraded list is re-saved
    (idempotent: a subsequent load makes no further changes).
    """
    try:
        if os.path.exists(GATEWAYS_FILE):
            with open(GATEWAYS_FILE, "r") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
            migrated = [migrate_gateway(gw) for gw in data]
            if migrated != data:
                save_gateways(migrated)
            return migrated
    except (json.JSONDecodeError, OSError) as e:
        print(f"[GatewayManager] Error loading gateways: {e}")
    return []


def save_gateways(gateways: List[dict]):
    """Save gateway configs to disk with credentials encrypted at rest.

    Produces a deep copy of the list and encrypts each record's
    ``http.password`` and ``ami.secret`` via ``secrets.encrypt`` (idempotent —
    values already prefixed ``enc:`` are left unchanged). The caller's list /
    dicts are NEVER mutated, so callers may keep holding plaintext in memory.
    """
    try:
        encrypted = copy.deepcopy(gateways)
        for gw in encrypted:
            if isinstance(gw.get("http"), dict):
                gw["http"]["password"] = secrets.encrypt(gw["http"].get("password", ""))
            if isinstance(gw.get("ami"), dict):
                gw["ami"]["secret"] = secrets.encrypt(gw["ami"].get("secret", ""))
        with open(GATEWAYS_FILE, "w") as f:
            json.dump(encrypted, f, indent=2)
    except OSError as e:
        print(f"[GatewayManager] Error saving gateways: {e}")


def get_gateway_decrypted(gateway_id: str) -> Optional[dict]:
    """Get a single gateway by ID with credentials decrypted.

    Returns a deep copy where ``http.password`` and ``ami.secret`` are run
    through ``secrets.decrypt`` (passthrough-safe for legacy plaintext). Runtime
    credential consumers MUST use this rather than the raw on-disk record.
    Returns None if not found.
    """
    for gw in load_gateways():
        if gw["id"] == gateway_id:
            dec = copy.deepcopy(gw)
            if isinstance(dec.get("http"), dict):
                dec["http"]["password"] = secrets.decrypt(dec["http"].get("password", ""))
            if isinstance(dec.get("ami"), dict):
                dec["ami"]["secret"] = secrets.decrypt(dec["ami"].get("secret", ""))
            return dec
    return None


def redact_gateway(gw: dict) -> dict:
    """Return a deep copy safe to expose over the API.

    Drops ``http.password`` / ``ami.secret`` and replaces them with boolean
    ``http.has_password`` / ``ami.has_secret`` (via ``secrets.mask``). Secrets
    are never sent to clients.
    """
    red = copy.deepcopy(gw)
    if isinstance(red.get("http"), dict):
        pw = red["http"].pop("password", None)
        red["http"]["has_password"] = secrets.mask(pw)
    if isinstance(red.get("ami"), dict):
        sec = red["ami"].pop("secret", None)
        red["ami"]["has_secret"] = secrets.mask(sec)
    return red


def add_gateway(gateway: dict) -> dict:
    """Add a new gateway and save."""
    gateways = load_gateways()
    gateways.append(gateway)
    save_gateways(gateways)
    return gateway


def update_gateway(gateway_id: str, updates: dict) -> Optional[dict]:
    """Update an existing gateway."""
    gateways = load_gateways()
    for i, gw in enumerate(gateways):
        if gw["id"] == gateway_id:
            # Don't allow overwriting id or created_at
            updates.pop("id", None)
            updates.pop("created_at", None)
            gateways[i].update(updates)
            save_gateways(gateways)
            return gateways[i]
    return None


def remove_gateway(gateway_id: str) -> bool:
    """Remove a gateway."""
    gateways = load_gateways()
    original_len = len(gateways)
    gateways = [gw for gw in gateways if gw["id"] != gateway_id]
    if len(gateways) < original_len:
        save_gateways(gateways)
        return True
    return False


def get_gateway(gateway_id: str) -> Optional[dict]:
    """Get a single gateway by ID."""
    for gw in load_gateways():
        if gw["id"] == gateway_id:
            return gw
    return None


# ---------------------------------------------------------------------------
# Gateway status
# ---------------------------------------------------------------------------
async def check_gateway_status(gateway: dict) -> dict:
    """
    Check a gateway's live status.

    Returns dict with:
        - reachable: bool (HTTP ping)
        - sip_registered: bool (from Asterisk)
        - sim_slots: list of {slot, status, carrier, signal, phone_number}
    """
    status = {
        "id": gateway["id"],
        "name": gateway["name"],
        "ip": gateway["ip"],
        "reachable": False,
        "sip_registered": False,
        "sim_slots": [],
        "active_calls": 0,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Check HTTP reachability
    try:
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            url = f"http://{gateway['ip']}:{gateway.get('http_port', 80)}"
            async with session.get(url) as resp:
                status["reachable"] = resp.status in (200, 301, 302, 401)
    except Exception:
        pass

    # Check SIP registration via Asterisk ARI
    try:
        from Orchestrator.asterisk.client import get_ari_client
        client = get_ari_client()
        if client and client.is_connected:
            # Check endpoint state
            detail = await client.get_endpoint_detail("PJSIP", gateway.get("trunk_name", "tg200"))
            if detail:
                state = detail.get("state", "unknown")
                status["sip_registered"] = state in ("online", "reachable")
    except Exception:
        pass

    # Get SIM/GSM info over AMI (the NeoGate TG has no REST API — Boa server).
    # `gsm show spans` + `gsm show span N` give real carrier/signal/registration.
    # The SIM's own MSISDN is NOT exposed by the gateway; phone_number stays
    # operator-configured in the gateway's ports[].
    try:
        from Orchestrator.sms import get_ami_client
        ami = get_ami_client(gateway["id"])
        if ami and ami.connected:
            # Map configured phone numbers by span for quick lookup.
            phone_by_span = {
                p.get("span"): p.get("phone_number", "")
                for p in gateway.get("ports", [])
            }
            spans = await ami.get_all_spans()
            for span in spans:
                span_num = span.get("span")
                status["sim_slots"].append({
                    "slot": span_num - 2 if isinstance(span_num, int) else None,
                    "span": span_num,
                    "status": "up" if span.get("up") else "down",
                    "carrier": span.get("carrier", ""),
                    "signal": span.get("signal"),
                    "registered": span.get("registered", False),
                    "phone_number": phone_by_span.get(span_num, ""),
                })
    except Exception:
        # AMI not connected / not configured — leave sim_slots empty.
        pass

    return status


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------
async def discover_gateways(subnet: str = None, timeout: float = 3.0) -> List[dict]:
    """
    Discover Yeastar TG200 gateways on the local network.

    Scans for HTTP on port 80 and checks for Yeastar-identifiable responses.
    Also checks Asterisk's registered endpoints.

    Args:
        subnet: Network prefix (e.g. "192.168.1"). If None, auto-detect.
        timeout: Connection timeout per host in seconds.

    Returns:
        List of discovered gateway dicts (not yet saved).
    """
    discovered = []

    # Auto-detect subnet from local interfaces
    if not subnet:
        subnet = _get_local_subnet()
        if not subnet:
            print("[GatewayManager] Could not detect local subnet")
            return []

    print(f"[GatewayManager] Scanning subnet {subnet}.0/24 for TG200 gateways...")

    # Scan common IPs (TG200 defaults to 192.168.5.150)
    # Also scan the local subnet
    scan_targets = set()
    for i in range(1, 255):
        scan_targets.add(f"{subnet}.{i}")
    # Always check the default TG200 IP
    scan_targets.add(TG200_DEFAULT_IP)

    # Parallel HTTP probe
    async def probe_host(ip: str):
        try:
            conn_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=conn_timeout) as session:
                url = f"http://{ip}:{TG200_HTTP_PORT}"
                async with session.get(url) as resp:
                    if resp.status in (200, 301, 302, 401):
                        # Fingerprint the real NeoGate TG (Boa server +
                        # astman/mypbx/WebCGI/NeoGate body, or known keywords).
                        text = await resp.text()
                        server = resp.headers.get("Server", "")
                        if _looks_like_neogate(server, text):
                            # Determine model from the page body (default TG200).
                            model = _detect_model(text)
                            return _new_gateway(
                                name=f"{model} ({ip})",
                                ip=ip,
                                capacity=port_count(model),
                                model=model,
                            )
        except Exception:
            pass
        return None

    # Run probes in batches of 50 to avoid overwhelming the network
    batch_size = 50
    targets = list(scan_targets)
    for batch_start in range(0, len(targets), batch_size):
        batch = targets[batch_start:batch_start + batch_size]
        tasks = [probe_host(ip) for ip in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, dict):
                # Don't discover already-configured gateways
                existing = load_gateways()
                existing_ips = {gw["ip"] for gw in existing}
                if result["ip"] not in existing_ips:
                    discovered.append(result)

    print(f"[GatewayManager] Discovered {len(discovered)} new gateway(s)")
    return discovered


def _get_local_subnet() -> Optional[str]:
    """Detect the local subnet prefix (e.g., '192.168.1')."""
    try:
        # Create a socket to determine local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        parts = local_ip.split(".")
        if len(parts) == 4:
            return ".".join(parts[:3])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# SMS via TG200 AMI
# ---------------------------------------------------------------------------
async def send_sms_via_gateway(
    gateway: dict,
    to: str,
    message: str,
    port: int = 1,
) -> dict:
    """
    Send an SMS through a TG200 gateway via AMI.

    Args:
        gateway: Gateway config dict (used for span selection)
        to: Destination phone number (E.164)
        message: SMS text
        port: SIM slot (maps to GSM span — TG200 span 2 = slot 1, span 3 = slot 2)

    Returns:
        {"success": bool, "error": str or None}
    """
    try:
        from Orchestrator.sms import get_ami_client
        ami = get_ami_client(gateway["id"])
        if ami is None or not ami.connected:
            return {"success": False, "error": "AMI client not connected"}

        # Map 1-based port/slot to GSM span (slot 0 -> span 2, slot 1 -> span 3, ...)
        span = slot_to_span(port - 1)
        result = await ami.send_sms(to, message, span=span)
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# PJSIP config generation
# ---------------------------------------------------------------------------
def generate_pjsip_trunk_config(gateway: dict) -> str:
    """Generate PJSIP config block for a gateway (for dynamic trunk addition)."""
    trunk = gateway.get("trunk_name", f"tg-{gateway['id']}")
    ip = gateway["ip"]
    sip_port = gateway.get("sip_port", 5060)
    codec = gateway.get("codec", "g722")

    config = f"""
; === {gateway['name']} (Auto-configured) ===
[{trunk}]
type=endpoint
context=from-tg200
disallow=all
allow={codec}
allow=ulaw
allow=alaw
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
aors={trunk}
identify_by=ip

[{trunk}]
type=aor
contact=sip:{ip}:{sip_port}
qualify_frequency=30
qualify_timeout=5

[{trunk}-identify]
type=identify
endpoint={trunk}
match={ip}/32
"""
    return config.strip()
