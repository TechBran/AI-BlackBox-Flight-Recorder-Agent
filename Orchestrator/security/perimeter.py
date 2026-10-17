"""Live wiring for the perimeter guard and the /app-proxy port allowlist.

origin_guard.py is pure; this module feeds it the box's facts. It is also the
ONE answer to "which loopback ports are user apps": the /app-proxy allowlist
(agent_routes.py), the guard's trusted same-host app origins and its
sandboxed-preview carve-out all key off the registered-apps registry
(state.APP_REGISTRY, read live — a registration takes effect with no
restart). Reserved infrastructure ports are excluded even when a registry
entry names one: the registry is a list of ports, not proof of what listens
on them, and POST /agent/apps/register accepts any port (it refuses reserved
ones up front, but a persisted entry from before that check is still here).
"""
from __future__ import annotations

import functools
import ipaddress
import logging
import os
import socket

from Orchestrator.config import (
    ASTERISK_AMI_PORT,
    ASTERISK_ARI_URL,
    ASTERISK_AUDIOSOCKET_PORT,
    BLACKBOX_TAILNET_HOSTNAME,
    CFG,
    DEFAULT_ORIGIN,
    DRACHTIO_PORT,
    FREESWITCH_ESL_PORT,
    LOCAL_MODELS_BASE_URL,
    OLLAMA_BASE_URL,
    ORCHESTRATOR_PORT,
    SECURITY_EXTRA_ALLOWED_HOSTS,
    SECURITY_EXTRA_ALLOWED_ORIGINS,
    TWILIO_WEBHOOK_BASE_URL,
)
from Orchestrator.security.origin_guard import OriginPolicy, split_host
from Orchestrator.state import APP_REGISTRY

logger = logging.getLogger(__name__)

# Locked zellij-web port: mirrors zellij_client._DEFAULT_ZELLIJ_WEB_PORT, the
# Portal's PROXY_BASE ('/app-proxy/9097/') and Android's APP_PROXY_PREFIX.
ZELLIJ_WEB_PORT = 9097

# The CU desktop's loopback bridges (browser/display.py, native_stream.py):
# RFB 5900+N — per-session x11vnc 5901.., the native :0 bridge 5999 — and the
# websockify WS bridges in front of them — per-session 6101.., native 6099,
# noVNC's own 6080 default. Whole blocks, so a slot-count change stays covered.
_CU_DESKTOP_PORTS = frozenset(range(5900, 6000)) | frozenset(range(6080, 6200))

# Default ports of the URL-configured services below, reserved even when the
# config points elsewhere (an older local instance may still be listening):
# MCP, Asterisk ARI, Ollama, llama-swap, the legacy vLLM reranker.
_DEFAULT_SERVICE_PORTS = frozenset({9093, 8088, 11434, 9098, 8091})


def _loopback_url_port(url: str):
    """The port of a configured service URL when it points at THIS box's
    loopback, else None — a remote server's port says nothing about what
    listens here (and may be a user app's port on this box)."""
    netloc = (url or "").split("://", 1)[-1].split("/", 1)[0]
    parsed = split_host(netloc)
    if not parsed or not parsed[1]:
        return None
    host = parsed[0]
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    return parsed[1] if loopback else None


@functools.lru_cache(maxsize=1)
def reserved_ports() -> frozenset:
    """Loopback services that are never a "user app". Proxied under the
    Portal's origin, each would be readable by any trusted page: the
    Orchestrator itself (bypasses the Origin guard), MCP (the remote tool
    surface), Chrome DevTools (full control of the CU browser), the CU
    desktop bridges (the real screen), the telephony control planes and the
    local model servers."""
    ports = set(_CU_DESKTOP_PORTS) | set(_DEFAULT_SERVICE_PORTS)
    ports |= {ORCHESTRATOR_PORT, ASTERISK_AUDIOSOCKET_PORT, ASTERISK_AMI_PORT,
              DRACHTIO_PORT, FREESWITCH_ESL_PORT}
    rerank_url = CFG.get("rerank", "base_url", fallback="") or ""
    for url in (os.getenv("BLACKBOX_MCP_INTERNAL_URL", ""), ASTERISK_ARI_URL,
                OLLAMA_BASE_URL, LOCAL_MODELS_BASE_URL, rerank_url):
        port = _loopback_url_port(url)
        if port:
            ports.add(port)
    try:
        from Orchestrator.browser.config import CDP_PORT
        ports.add(int(CDP_PORT))
    except Exception:  # noqa: BLE001 — CU stack absent: still reserve the default
        ports.add(9222)
    return frozenset(ports - {ZELLIJ_WEB_PORT})


def reserved_port_reason(port) -> str:
    """The refusal text for a reserved port, or "" when `port` is not one."""
    try:
        reserved = int(port) in reserved_ports()
    except (TypeError, ValueError):
        return ""
    if not reserved:
        return ""
    return (f"Port {int(port)} is reserved for a BlackBox service, not a user app; "
            "pick a free port in 8060-8099")


def registered_app_ports() -> frozenset:
    """Ports of the registered user apps, minus zellij and reserved ports."""
    ports = set()
    for registered in list(APP_REGISTRY.values()):
        try:
            port = int(registered.port)
        except (TypeError, ValueError):
            continue
        if 1024 <= port <= 65535:
            ports.add(port)
    return frozenset(ports - reserved_ports() - {ZELLIJ_WEB_PORT})


def proxy_port_allowed(port: int) -> bool:
    """/app-proxy/{port} targets: zellij-web plus the registered user apps."""
    return port == ZELLIJ_WEB_PORT or port in registered_app_ports()


def _host_of(url: str):
    netloc = (url or "").split("://", 1)[-1].split("/", 1)[0]
    parsed = split_host(netloc)
    return parsed[0] if parsed else None


def build_origin_policy() -> OriginPolicy:
    """The live policy: config extras plus every public name this box is
    configured to answer to (pairing origin, Twilio webhook base, tailnet)."""
    pairing_origin = (CFG.get("pairing", "default_origin", fallback="") or "").strip()
    hosts = list(SECURITY_EXTRA_ALLOWED_HOSTS)
    for url in (DEFAULT_ORIGIN, pairing_origin, TWILIO_WEBHOOK_BASE_URL, BLACKBOX_TAILNET_HOSTNAME):
        host = _host_of(url)
        if host:
            hosts.append(host)
    origins = list(SECURITY_EXTRA_ALLOWED_ORIGINS)
    origins += [o for o in (DEFAULT_ORIGIN, pairing_origin) if o]
    box_names = []
    for fn in (socket.gethostname, socket.getfqdn):
        try:
            box_names.append(fn())
        except OSError:
            pass
    policy = OriginPolicy(
        orchestrator_port=ORCHESTRATOR_PORT,
        zellij_port=ZELLIJ_WEB_PORT,
        extra_hosts=hosts,
        extra_origins=origins,
        registered_app_ports=registered_app_ports,
        box_names=box_names,
    )
    logger.info("[ORIGIN-GUARD] active: extra hosts=%s extra origins=%s box names=%s",
                sorted(policy.extra_hosts), sorted(policy.extra_origins), sorted(policy.box_names))
    return policy
