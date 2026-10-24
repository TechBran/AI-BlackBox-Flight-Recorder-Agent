#!/usr/bin/env python3
"""
hotplug.py - USB Hot-Plug Monitor for SIM7600G-H Cellular Modem

Polls /dev/ttyUSB* every few seconds. When 4+ ports appear in a cluster
(SIM7600G-H signature — allows gaps like 0,1,2,4,5), it probes the AT port,
initializes the modem, and re-registers callbacks. When ports disappear,
it cleans up gracefully.

Zero new dependencies — just glob and existing serial.
"""

import asyncio
import glob
import re
import time
import traceback
from typing import Optional, Dict, Callable, Awaitable, Any, List

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False


POLL_INTERVAL = 3.0        # seconds between polls
SETTLE_DELAY = 2.0         # seconds to wait after ports appear before probing
MIN_CLUSTER_PORTS = 4      # Minimum ports in a cluster (SIM7600G-H has 5, but gaps happen)
CLUSTER_RANGE = 6          # Max range base..base+5 to look for a cluster
AT_PORT_OFFSET = 2         # AT command port = base + 2
AUDIO_PORT_OFFSET = 4      # Audio PCM port = base + 4


class CellularHotplugMonitor:
    """
    Background monitor that detects SIM7600G-H USB plug/unplug events.

    On arrival: discovers ports, probes AT, initializes modem, re-registers callbacks.
    On departure: stops audio, disconnects modem, clears singletons.
    """

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._state = "disconnected"  # "disconnected", "settling", "connected"
        self._known_ports: set = set()
        self._at_port: Optional[str] = None
        self._audio_port: Optional[str] = None
        self._base_port: Optional[int] = None
        self._reconnect_count = 0
        self._last_event = ""
        self._last_event_time = 0.0

        # Persistent callbacks — survive reconnects, re-applied to each fresh modem
        self._callbacks: Dict[str, Callable] = {}

    @property
    def state(self) -> str:
        return self._state

    def get_status(self) -> Dict[str, Any]:
        """Return monitor status for the /cellular/hotplug endpoint."""
        return {
            "state": self._state,
            "running": self._running,
            "at_port": self._at_port,
            "audio_port": self._audio_port,
            "base_port": self._base_port,
            "known_ports": sorted(self._known_ports),
            "reconnect_count": self._reconnect_count,
            "last_event": self._last_event,
            "last_event_time": self._last_event_time,
            "registered_callbacks": list(self._callbacks.keys()),
        }

    def register_callback(self, name: str, callback: Callable):
        """Register a persistent callback that survives reconnects.

        Names map to modem callback registration methods:
            "on_ring", "on_sms", "on_call_end", "on_call_begin",
            "on_audio_ready", "on_dtmf"
        """
        self._callbacks[name] = callback

    async def start(self):
        """Start the polling loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        print("[HOTPLUG] Monitor started (polling every {:.0f}s)".format(POLL_INTERVAL))

    async def stop(self):
        """Stop the polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        print("[HOTPLUG] Monitor stopped")

    async def _poll_loop(self):
        """Main polling loop — runs every POLL_INTERVAL seconds."""
        while self._running:
            try:
                current_ports = set(glob.glob("/dev/ttyUSB*"))

                if self._state == "disconnected":
                    # Check if new ports appeared
                    if current_ports:
                        result = self.discover_ports(current_ports)
                        if result:
                            self._log_event("ports_detected",
                                            f"Found {len(current_ports)} ports, "
                                            f"base=ttyUSB{result['base']}")
                            await self._on_modem_arrived(result)
                    # else: still no ports, stay disconnected

                elif self._state == "connected":
                    # Check if our known ports disappeared
                    if self._at_port and self._at_port not in current_ports:
                        self._log_event("ports_gone",
                                        f"AT port {self._at_port} disappeared")
                        await self._on_modem_departed()
                    elif self._audio_port and self._audio_port not in current_ports:
                        self._log_event("ports_gone",
                                        f"Audio port {self._audio_port} disappeared")
                        await self._on_modem_departed()

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[HOTPLUG] Poll error: {e}")
                traceback.print_exc()

            try:
                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                break

    def discover_ports(self, ports: set = None) -> Optional[Dict[str, Any]]:
        """Find SIM7600G-H port cluster (allows gaps like 0,1,2,4,5).

        Looks for clusters of 4+ ports within a range of 6 (base..base+5).
        The AT port must exist at base+2 and audio port at base+4.

        Returns dict with 'base', 'at_port', 'audio_port' or None.
        """
        if ports is None:
            ports = set(glob.glob("/dev/ttyUSB*"))

        if not ports:
            return None

        # Extract port numbers
        numbers = set()
        for p in ports:
            m = re.search(r"ttyUSB(\d+)$", p)
            if m:
                numbers.add(int(m.group(1)))

        if len(numbers) < MIN_CLUSTER_PORTS:
            return None

        sorted_nums = sorted(numbers)

        # Sliding window: find clusters of 4+ ports within a span of CLUSTER_RANGE
        best_cluster = None
        for base_candidate in sorted_nums:
            cluster = [n for n in sorted_nums
                       if base_candidate <= n < base_candidate + CLUSTER_RANGE]
            if len(cluster) < MIN_CLUSTER_PORTS:
                continue

            base = cluster[0]
            at_num = base + AT_PORT_OFFSET
            audio_num = base + AUDIO_PORT_OFFSET

            # AT and audio ports must actually exist
            if at_num not in numbers or audio_num not in numbers:
                continue

            # Prefer the cluster with the most ports
            if best_cluster is None or len(cluster) > len(best_cluster["nums"]):
                best_cluster = {
                    "base": base,
                    "nums": cluster,
                    "at_port": f"/dev/ttyUSB{at_num}",
                    "audio_port": f"/dev/ttyUSB{audio_num}",
                }

        if best_cluster:
            return {
                "base": best_cluster["base"],
                "at_port": best_cluster["at_port"],
                "audio_port": best_cluster["audio_port"],
                "ports": [f"/dev/ttyUSB{n}" for n in best_cluster["nums"]],
            }

        return None

    async def _on_modem_arrived(self, discovery: Dict[str, Any]):
        """Handle modem USB plug-in."""
        self._state = "settling"
        at_port = discovery["at_port"]
        audio_port = discovery["audio_port"]

        print(f"[HOTPLUG] *** MODEM DETECTED *** AT={at_port} Audio={audio_port}")
        print(f"[HOTPLUG] Waiting {SETTLE_DELAY}s for USB to settle...")

        await asyncio.sleep(SETTLE_DELAY)

        # Verify ports still exist after settle
        if not glob.glob(at_port):
            print(f"[HOTPLUG] AT port {at_port} gone after settle — aborting")
            self._state = "disconnected"
            return

        # Probe AT port
        if not await self._probe_at_port(at_port):
            print(f"[HOTPLUG] AT probe failed on {at_port} — staying disconnected")
            self._state = "disconnected"
            return

        # Update state (modem AT command init removed — telephony runs on Asterisk now)
        self._at_port = at_port
        self._audio_port = audio_port
        self._base_port = discovery["base"]
        self._known_ports = set(discovery["ports"])
        self._reconnect_count += 1
        self._state = "connected"

        print(f"[HOTPLUG] *** MODEM DETECTED *** (reconnect #{self._reconnect_count})")
        self._log_event("connected", f"AT={at_port} Audio={audio_port}")

    async def _on_modem_departed(self):
        """Handle modem USB unplug."""
        print("[HOTPLUG] *** MODEM DISCONNECTED ***")

        # Update state (modem AT command cleanup removed — telephony runs on Asterisk now)
        self._at_port = None
        self._audio_port = None
        self._base_port = None
        self._known_ports.clear()
        self._state = "disconnected"

        self._log_event("disconnected", "USB removed")
        print("[HOTPLUG] Cleanup complete — waiting for reconnect...")

    async def _probe_at_port(self, port: str) -> bool:
        """Send AT to the port and check for OK response."""
        if not SERIAL_AVAILABLE:
            return False
        try:
            s = serial.Serial(
                port=port,
                baudrate=115200,
                timeout=1.0,
                write_timeout=2.0,
            )
            # Flush and send AT
            s.reset_input_buffer()
            s.write(b"AT\r\n")
            time.sleep(0.5)  # noqa: ASYNC251

            # Read response
            response = ""
            if s.in_waiting > 0:
                response = s.read(s.in_waiting).decode("utf-8", errors="replace")

            s.close()

            if "OK" in response:
                print(f"[HOTPLUG] AT probe OK on {port}")
                return True
            else:
                print(f"[HOTPLUG] AT probe failed on {port}: {response.strip()!r}")
                return False

        except Exception as e:
            print(f"[HOTPLUG] AT probe error on {port}: {e}")
            return False


    def _log_event(self, event: str, detail: str = ""):
        """Record the last event for status reporting."""
        self._last_event = f"{event}: {detail}" if detail else event
        self._last_event_time = time.time()


# =============================================================================
# Module-level singleton
# =============================================================================

_monitor: Optional[CellularHotplugMonitor] = None


def get_hotplug_monitor() -> Optional[CellularHotplugMonitor]:
    """Get the global hotplug monitor instance (None if not started)."""
    return _monitor


async def start_hotplug_monitor() -> CellularHotplugMonitor:
    """Create and start the global hotplug monitor."""
    global _monitor
    if _monitor is None:
        _monitor = CellularHotplugMonitor()
    if not _monitor._running:
        await _monitor.start()
    return _monitor


async def stop_hotplug_monitor():
    """Stop the global hotplug monitor."""
    global _monitor
    if _monitor:
        await _monitor.stop()
        _monitor = None
