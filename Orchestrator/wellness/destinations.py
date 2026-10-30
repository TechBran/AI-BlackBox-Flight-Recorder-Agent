"""Where a wellness answer would go: local, on_device or cloud (spec §4.6).

Classes are derived LIVE, never hard-coded:

  local      a `custom` server (credentials/custom_models.json, read fresh)
             whose base-URL host resolves ONLY to non-public addresses:
             loopback, RFC 1918, link-local, 100.64.0.0/10 (the tailnet),
             fc00::/7 (which holds the tailnet ULA fd7a:115c:a1e0::/48). Every
             resolved address must qualify; a resolution failure is cloud.
  on_device  the `local` provider (Gemma on the source phone; M7.5)
  cloud      anthropic, openai, google, xai, and any custom server on a
             public address (OpenRouter, a rented GPU), shown as its own
             `custom:<alias>` row

Allow-lists hold these provider ids: anthropic, openai, google, xai, local,
and custom:<alias>. Local and on-device destinations are allowed by default
(decision D2); every cloud destination needs its own tick, Google also needs
"my key is on a paid plan" (the Gemini terms say not to send sensitive data
to unpaid services), and a local-only category never goes to the cloud.
"""
from __future__ import annotations

import dataclasses
import ipaddress
import socket
from typing import Callable, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

LOCAL, ON_DEVICE, CLOUD = "local", "on_device", "cloud"
CLOUD_PROVIDERS = ("anthropic", "openai", "google", "xai")
PROVIDER_LABELS = {"anthropic": "Anthropic", "openai": "OpenAI", "google": "Google",
                   "xai": "xAI", "local": "this phone"}
TERMS_NOTES = {
    "anthropic": "Anthropic API: not used for training without your permission",
    "openai": "OpenAI API: not used for training by default; abuse logs kept up to 30 days",
    "google": "Google: only with a paid key",
    "xai": "xAI: see xAI's API terms",
}
_ALIASES = {"gemini": "google", "gemini_rest": "google", "claude": "anthropic",
            "openai_rest": "openai", "grok": "xai"}

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_ULA = ipaddress.ip_network("fc00::/7")

Resolver = Callable[..., list]


@dataclasses.dataclass(frozen=True)
class Destination:
    id: Optional[str]          # the allow-list id, None when unknown
    cls: Optional[str]         # local | on_device | cloud | None (unknown)
    label: str
    reason: Optional[str] = None


def normalize_provider(provider) -> Optional[str]:
    if not isinstance(provider, str) or not provider.strip():
        return None
    p = provider.strip().lower()
    if p.startswith("custom:"):
        return "custom:" + provider.strip()[len("custom:"):]
    return _ALIASES.get(p, p)


def address_is_local(ip) -> bool:
    """True for the private ranges the box treats as "your network"."""
    from Orchestrator.utils.safe_fetch import is_public_address
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip.split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if is_public_address(ip):
        return False
    if ip.version == 4:
        return ip.is_loopback or ip.is_private or ip.is_link_local or ip in _CGNAT
    return ip.is_loopback or ip.is_link_local or ip in _ULA


def host_is_local(host: str, port: Optional[int] = None,
                  resolver: Optional[Resolver] = None) -> bool:
    """Every address `host` resolves to is local. Failure or none: False."""
    if not host:
        return False
    try:
        return address_is_local(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        pass
    resolve = resolver or socket.getaddrinfo
    try:
        infos = resolve(host, port or 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        return False
    addrs = set()
    for info in infos or ():
        try:
            addrs.add(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]))
        except (IndexError, ValueError, TypeError):
            return False
    return bool(addrs) and all(address_is_local(a) for a in addrs)


def _servers() -> List[dict]:
    from Orchestrator.onboarding import custom_servers
    return custom_servers.list_servers(enabled_only=True)


def _server_for(provider: str, model: Optional[str]) -> Optional[dict]:
    servers = _servers()
    if provider.startswith("custom:"):
        alias = provider[len("custom:"):].lower()
        return next((s for s in servers if (s.get("alias") or "").lower() == alias), None)
    if model:
        from Orchestrator.onboarding import custom_servers
        try:
            srv, _bare = custom_servers.resolve_model(model)
        except Exception:  # noqa: BLE001 - a broken registry reads as "unknown"
            return None
        return srv
    return servers[0] if servers else None


def destination(provider, model: Optional[str] = None,
                resolver: Optional[Resolver] = None) -> Destination:
    p = normalize_provider(provider)
    if p is None:
        return Destination(None, None, "an unknown AI", "unknown_provider")
    if p == "local":
        return Destination("local", ON_DEVICE, PROVIDER_LABELS["local"])
    if p in CLOUD_PROVIDERS:
        return Destination(p, CLOUD, PROVIDER_LABELS[p])
    if p == "custom" or p.startswith("custom:"):
        srv = _server_for(p, model)
        if srv is None:
            return Destination(None, None, "an unknown model server", "unknown_custom_server")
        alias = srv.get("alias") or ""
        parts = urlsplit(srv.get("base_url") or "")
        try:
            port = parts.port
        except ValueError:
            port = None
        local = host_is_local(parts.hostname or "", port, resolver)
        return Destination(f"custom:{alias}", LOCAL if local else CLOUD, alias,
                           None if local else "public_address")
    return Destination(None, None, "an unknown AI", "unknown_provider")


def classify(provider, model: Optional[str] = None,
             resolver: Optional[Resolver] = None) -> Optional[str]:
    return destination(provider, model, resolver).cls


def allowed(provider, category: str, enrollment: Optional[dict], model: Optional[str] = None,
            resolver: Optional[Resolver] = None) -> Tuple[bool, Optional[str]]:
    """(may this destination receive `category`, why not)."""
    if not enrollment:
        return False, "not_enrolled"
    dest = destination(provider, model, resolver)
    if dest.cls is None:
        return False, dest.reason or "unknown_provider"
    if dest.cls in (LOCAL, ON_DEVICE):
        return True, None
    if (enrollment.get("local_only") or {}).get(category):
        return False, "local_only"
    if dest.id not in (enrollment.get("providers_allowed") or []):
        return False, "not_ticked"
    if dest.id == "google" and not enrollment.get("google_paid_key_confirmed"):
        return False, "google_unpaid_key"
    return True, None


def known_ids() -> List[str]:
    """Every id an allow-list may hold on this box right now."""
    ids = list(CLOUD_PROVIDERS) + ["local"]
    try:
        ids += [f"custom:{s.get('alias')}" for s in _servers() if s.get("alias")]
    except Exception:  # noqa: BLE001
        pass
    return ids


def rows(resolver: Optional[Resolver] = None) -> List[dict]:
    """The provider screen: each destination with its class and terms line."""
    out = [{"id": p, "class": CLOUD, "label": PROVIDER_LABELS[p], "terms": TERMS_NOTES[p]}
           for p in CLOUD_PROVIDERS]
    out.append({"id": "local", "class": ON_DEVICE, "label": PROVIDER_LABELS["local"],
                "terms": None})
    try:
        servers = _servers()
    except Exception:  # noqa: BLE001
        servers = []
    for srv in servers:
        d = destination(f"custom:{srv.get('alias')}", None, resolver)
        out.append({"id": d.id, "class": d.cls, "label": d.label,
                    "terms": None if d.cls == LOCAL else "a model server on a public address"})
    return out


def ensure_known(ids: Iterable[str]) -> List[str]:
    """Normalized allow-list ids, refusing any this box does not know."""
    known = set(known_ids())
    out = []
    for raw in ids:
        p = normalize_provider(raw)
        if p not in known:
            raise ValueError(f"unknown provider {raw!r}")
        if p not in out:
            out.append(p)
    return out
