"""
AMI connection manager — one AMISMSClient per NeoGate TG gateway.

Owns the lifecycle of one AMI connection for each enabled gateway. Each entry
keeps the live client alongside its (decrypted) gateway dict so callers can map
inbound/outbound SMS to the right gateway + GSM span.

Lazy-imports AMISMSClient and the gateway_manager helpers inside methods to
avoid import cycles (sms <-> asterisk <-> config), so importing this module is
side-effect free.
"""

import logging

log = logging.getLogger("sms.manager")


def _norm(num: str) -> str:
    """Normalize a phone number to its last 10 digits for comparison."""
    digits = "".join(ch for ch in str(num or "") if ch.isdigit())
    return digits[-10:]


class AMIConnectionManager:
    """Owns one AMI client per gateway, keyed by gateway id."""

    def __init__(self, client_factory=None):
        # client_factory(host, port, username, secret) -> client.
        # When None, defaults to AMISMSClient (lazy-imported in _make_client).
        self._factory = client_factory
        self._clients: dict = {}   # gateway_id -> client
        self._gateways: dict = {}  # gateway_id -> gw dict

    # ------------------------------------------------------------------
    # Client construction
    # ------------------------------------------------------------------
    def _make_client(self, gw: dict):
        factory = self._factory
        if factory is None:
            from .ami_client import AMISMSClient  # lazy to avoid cycles
            factory = AMISMSClient
        ami = gw.get("ami", {}) or {}
        return factory(
            host=gw["ip"],
            port=ami["port"],
            username=ami["user"],
            secret=ami["secret"],
        )

    # ------------------------------------------------------------------
    # Lifecycle (single gateway)
    # ------------------------------------------------------------------
    async def add_gateway(self, gw: dict) -> None:
        """Add (or replace) a gateway and connect its client.

        `gw` is a DECRYPTED gateway dict. Idempotent: re-adding an existing id
        disconnects the old client first. A connect() failure is logged but
        swallowed — the entry is kept with a disconnected client so one bad
        gateway never kills the others.
        """
        gateway_id = gw["id"]

        # Replace semantics: drop any existing client for this id first.
        if gateway_id in self._clients:
            old = self._clients.pop(gateway_id)
            self._gateways.pop(gateway_id, None)
            try:
                await old.disconnect()
            except Exception:
                log.exception("Error disconnecting old client for gateway %s", gateway_id)

        client = self._make_client(gw)
        self._clients[gateway_id] = client
        self._gateways[gateway_id] = gw

        try:
            await client.connect()
        except Exception:
            log.exception(
                "AMI connect failed for gateway %s (%s); keeping disconnected entry",
                gateway_id, gw.get("ip"),
            )

    async def remove_gateway(self, gateway_id: str) -> None:
        """Disconnect (best-effort) and drop the entry. No-op if absent."""
        client = self._clients.pop(gateway_id, None)
        self._gateways.pop(gateway_id, None)
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            log.exception("Error disconnecting gateway %s", gateway_id)

    async def reconnect(self, gateway_id: str) -> None:
        """Reload the gateway from disk and re-add it (or remove if gone)."""
        from Orchestrator.asterisk.gateway_manager import get_gateway_decrypted
        gw = get_gateway_decrypted(gateway_id)
        if gw is not None:
            await self.add_gateway(gw)
        else:
            await self.remove_gateway(gateway_id)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    def get(self, gateway_id: str):
        """Return the client for a gateway id, or None."""
        return self._clients.get(gateway_id)

    def clients(self) -> dict:
        """Return a copy of {gateway_id: client}."""
        return dict(self._clients)

    def gateways(self) -> dict:
        """Return a copy of {gateway_id: gw_dict}."""
        return dict(self._gateways)

    def default(self):
        """Return the client of the first ENABLED gateway (insertion order)."""
        for gateway_id, gw in self._gateways.items():
            if gw.get("enabled") is not False:
                return self._clients.get(gateway_id)
        return None

    def resolve_for_number(self, from_number: str):
        """Find the gateway+port whose phone_number matches `from_number`.

        Matches by last 10 digits. Returns (client, span) for the matching
        port, or None if no gateway owns that number.
        """
        target = _norm(from_number)
        if not target:
            return None
        for gateway_id, gw in self._gateways.items():
            for port in gw.get("ports", []) or []:
                if _norm(port.get("phone_number", "")) == target:
                    client = self._clients.get(gateway_id)
                    if client is not None:
                        return client, port.get("span")
        return None

    # ------------------------------------------------------------------
    # Lifecycle (all gateways)
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Load gateways and connect a client for every ENABLED gateway."""
        from Orchestrator.asterisk.gateway_manager import (
            load_gateways,
            get_gateway_decrypted,
        )
        for gw in load_gateways():
            if gw.get("enabled") is False:
                continue
            dec = get_gateway_decrypted(gw["id"])
            if dec is not None:
                await self.add_gateway(dec)

    async def stop(self) -> None:
        """Disconnect all clients and clear all state."""
        for gateway_id, client in list(self._clients.items()):
            try:
                await client.disconnect()
            except Exception:
                log.exception("Error disconnecting gateway %s during stop", gateway_id)
        self._clients.clear()
        self._gateways.clear()
