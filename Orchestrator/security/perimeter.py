"""Live wiring for the perimeter guard and the /app-proxy port allowlist.

origin_guard.py is pure; this module feeds it the box's facts. It is also the
ONE answer to "which loopback ports are user apps": the /app-proxy allowlist
(agent_routes.py), the guard's trusted same-host app origins and its
sandboxed-preview carve-out all key off the registered-apps registry
(state.APP_REGISTRY, read live — a registration takes effect with no
restart). Reserved infrastructure ports are excluded even when a registry
entry names one, because detect_app_startup registers whatever port it reads
in agent output.
"""
from __future__ import annotations

import functools
import logging
import os
import socket

from Orchestrator.config import (
    BLACKBOX_TAILNET_HOSTNAME,
    CFG,
    DEFAULT_ORIGIN,
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


def _url_port(url: str, default: int) -> int:
    netloc = (url or "").split("://", 1)[-1].split("/", 1)[0]
    parsed = split_host(netloc)
    return parsed[1] if parsed and parsed[1] else default


@functools.lru_cache(maxsize=1)
def reserved_ports() -> frozenset:
    """Loopback services that are never a "user app": proxying to the
    Orchestrator itself would bypass the Origin guard, MCP holds the remote
    tool surface, Chrome DevTools is full control of the CU browser."""
    ports = {
        ORCHESTRATOR_PORT,
        _url_port(os.getenv("BLACKBOX_MCP_INTERNAL_URL", ""), 9093),
    }
    try:
        from Orchestrator.browser.config import CDP_PORT
        ports.add(int(CDP_PORT))
    except Exception:  # noqa: BLE001 — CU stack absent: still reserve the default
        ports.add(9222)
    return frozenset(ports)


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
