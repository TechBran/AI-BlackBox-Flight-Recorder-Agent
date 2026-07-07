"""
ADB Connection Manager — manages ADB connections to Android devices over Tailscale.
"""
import asyncio
import logging
import re
import time
from typing import Optional, Dict, List, Tuple

log = logging.getLogger(__name__)


class ADBManager:
    """Manages ADB connections to Android devices."""

    def __init__(self):
        self._connected: Dict[str, float] = {}

    async def _run_adb(self, *args, timeout: int = 10) -> Tuple[int, str, str]:
        """Run an ADB command and return (returncode, stdout, stderr)."""
        import shutil
        if not shutil.which("adb"):
            return -1, "", "ADB is not installed. Run: sudo apt-get install -y adb"
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb", *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError:
            return -1, "", "ADB binary not found. Run: sudo apt-get install -y adb"
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, stdout.decode().strip(), stderr.decode().strip()
        except (asyncio.TimeoutError, asyncio.CancelledError):
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return -1, "", f"ADB command timed out ({' '.join(args[:2])})"
        except Exception as e:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return -1, "", f"ADB command error: {str(e)}"

    async def connect(self, device_id: str) -> Dict:
        """Connect to an Android device via ADB over Tailscale."""
        from Orchestrator.device_registry import get_registry, DeviceProtocol, DeviceStatus
        registry = get_registry()
        device = registry.get_device(device_id)
        if not device:
            return {"success": False, "error": f"Device not found: {device_id}"}
        if device.protocol != DeviceProtocol.ADB:
            return {"success": False, "error": f"Device {device_id} is not an ADB device"}

        conn_str = device.connection_string()
        rc, stdout, stderr = await self._run_adb("connect", conn_str, timeout=15)

        if rc == 0 and ("connected" in stdout.lower() or "already connected" in stdout.lower()):
            self._connected[device_id] = time.time()
            device.status = DeviceStatus.ONLINE
            device.last_seen = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            log.info(f"[ADB] Connected to {device_id} at {conn_str}")
            # Enrich device metadata from ADB properties
            try:
                await self._enrich_metadata(device_id)
            except Exception as e:
                log.warning(f"[ADB] Metadata enrichment failed for {device_id}: {e}")
            return {"success": True, "device_id": device_id, "message": stdout}

        return {"success": False, "error": stderr or stdout or "Connection failed"}

    async def _enrich_metadata(self, device_id: str):
        """Pull device metadata via ADB and store in device registry."""
        from Orchestrator.device_registry import get_registry
        device = get_registry().get_device(device_id)
        if not device:
            return

        conn_str = device.connection_string()

        # Pull device properties
        props = {}
        for prop_name, prop_key in [
            ("model", "ro.product.model"),
            ("manufacturer", "ro.product.manufacturer"),
            ("android_version", "ro.build.version.release"),
            ("sdk_version", "ro.build.version.sdk"),
            ("device_name", "ro.product.name"),
            ("brand", "ro.product.brand"),
        ]:
            rc, stdout, stderr = await self._run_adb(
                "-s", conn_str, "shell", f"getprop {prop_key}", timeout=5
            )
            if rc == 0 and stdout.strip():
                props[prop_name] = stdout.strip()

        # Get screen size
        rc, stdout, stderr = await self._run_adb(
            "-s", conn_str, "shell", "wm size", timeout=5
        )
        if rc == 0 and "x" in stdout:
            size_str = stdout.split(":")[-1].strip()
            props["screen_size"] = size_str

        # Update device metadata and name
        if props:
            device.metadata.update(props)
            # Generate a better name if we have model info
            if "manufacturer" in props and "model" in props:
                device.name = f"{props['manufacturer'].title()} {props['model']}"
            if "android_version" in props:
                device.description = (
                    f"Android {props['android_version']}"
                    f"{', ' + props.get('screen_size', '') if 'screen_size' in props else ''}"
                )
            get_registry()._save_to_file()
            log.info(f"[ADB] Enriched metadata for {device_id}: {props}")

    async def pair(self, device_id: str, pairing_port: int, pairing_code: str) -> Dict:
        """Pair with an Android device using wireless debugging pairing code."""
        from Orchestrator.device_registry import get_registry
        registry = get_registry()
        device = registry.get_device(device_id)
        if not device:
            return {"success": False, "error": f"Device not found: {device_id}"}

        pair_str = f"{device.tailscale_ip}:{pairing_port}"
        rc, stdout, stderr = await self._run_adb("pair", pair_str, pairing_code, timeout=15)

        if rc == 0 and "successfully" in stdout.lower():
            log.info(f"[ADB] Paired with {device_id} at {pair_str}")
            return {"success": True, "device_id": device_id, "message": stdout}

        return {"success": False, "error": stderr or stdout or "Pairing failed"}

    # =========================================================================
    # Smart Pair Orchestration
    # =========================================================================

    async def check_reachability(self, ip: str, timeout: int = 3) -> bool:
        """Check if a Tailscale IP is reachable via TCP probe.

        Uses TCP connection attempt instead of ICMP ping because the systemd
        service runs with NoNewPrivileges=true, which strips cap_net_raw
        from the ping binary.
        """
        # Try common ports: ADB wireless debugging typically listens on high ports,
        # but Tailscale itself ensures IP reachability — any open port confirms it.
        # We try a quick TCP connect to port 5555 (ADB default) and if that fails,
        # fall back to checking if the host responds to a connection attempt at all
        # (even a "connection refused" means the host is reachable).
        for port in (5555, 7):  # 5555=ADB default, 7=echo (usually closed but reachable)
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, port),
                    timeout=timeout
                )
                writer.close()
                await writer.wait_closed()
                print(f"[ADB] Reachability OK for {ip}:{port} (connected)")
                return True
            except ConnectionRefusedError:
                # Connection refused = host is reachable, port just isn't open
                print(f"[ADB] Reachability OK for {ip}:{port} (refused but reachable)")
                return True
            except (asyncio.TimeoutError, OSError):
                continue

        print(f"[ADB] Reachability FAILED for {ip} (all probes timed out)")
        return False

    async def discover_connection_port(self, ip: str, stored_port: int = 5555) -> Optional[int]:
        """After pairing, discover the wireless debugging connection port.

        Strategy 1: Parse `adb mdns services` for _adb-tls-connect entries
        Strategy 2: Check `adb devices` for existing connections to this IP
        Strategy 3: Fall back to the stored port if it's non-default
        """
        # Strategy 1: mDNS service discovery
        try:
            rc, stdout, stderr = await self._run_adb("mdns", "services", timeout=5)
            if rc == 0 and stdout:
                # Lines like: adb-XXXXX	_adb-tls-connect._tcp.	100.x.x.x:39871
                for line in stdout.split("\n"):
                    if "_adb-tls-connect" in line and ip in line:
                        match = re.search(rf"{re.escape(ip)}:(\d+)", line)
                        if match:
                            port = int(match.group(1))
                            if 1 <= port <= 65535:
                                log.info(f"[ADB] mDNS discovered connection port {port} for {ip}")
                                return port
        except Exception as e:
            log.debug(f"[ADB] mDNS discovery failed: {e}")

        # Strategy 2: Check already-connected devices
        try:
            rc, stdout, stderr = await self._run_adb("devices", timeout=5)
            if rc == 0:
                for line in stdout.split("\n"):
                    if ip in line and "device" in line:
                        match = re.search(rf"{re.escape(ip)}:(\d+)", line)
                        if match:
                            port = int(match.group(1))
                            log.info(f"[ADB] Found existing connection on port {port} for {ip}")
                            return port
        except Exception as e:
            log.debug(f"[ADB] Device list check failed: {e}")

        # Strategy 3: Stored port if non-default
        if stored_port and stored_port != 5555:
            log.info(f"[ADB] Using stored port {stored_port} for {ip}")
            return stored_port

        log.info(f"[ADB] Could not auto-discover connection port for {ip}")
        return None

    async def smart_pair(self, device_id: str, pairing_port: int, pairing_code: str) -> Dict:
        """Full smart pairing orchestration with step-by-step progress.

        Returns: {
            success: bool,
            steps: [{step: str, status: "ok"|"fail"|"skip", message: str}],
            partial: bool,          # True if paired but not connected
            needs_connection_port: bool  # True if port discovery failed
        }
        """
        from Orchestrator.device_registry import get_registry, DeviceProtocol, DeviceStatus
        registry = get_registry()
        device = registry.get_device(device_id)
        if not device:
            return {
                "success": False,
                "steps": [{"step": "lookup", "status": "fail", "message": f"Device not found: {device_id}"}],
                "partial": False, "needs_connection_port": False
            }
        if device.protocol != DeviceProtocol.ADB:
            return {
                "success": False,
                "steps": [{"step": "lookup", "status": "fail", "message": f"Device {device_id} is not an ADB device"}],
                "partial": False, "needs_connection_port": False
            }

        ip = device.tailscale_ip
        steps: List[Dict] = []

        # Step 1: Reachability
        log.info(f"[ADB] Smart pair step 1: reachability check for {ip}")
        reachable = await self.check_reachability(ip)
        if not reachable:
            steps.append({
                "step": "reachability", "status": "fail",
                "message": f"Device {ip} is not reachable. Is Tailscale connected on both devices?"
            })
            return {"success": False, "steps": steps, "partial": False, "needs_connection_port": False}
        steps.append({"step": "reachability", "status": "ok", "message": f"Device {ip} is reachable"})

        # Step 2: ADB Pair
        log.info(f"[ADB] Smart pair step 2: pairing {ip}:{pairing_port}")
        pair_str = f"{ip}:{pairing_port}"
        rc, stdout, stderr = await self._run_adb("pair", pair_str, pairing_code, timeout=15)
        combined = f"{stdout} {stderr}".lower()

        if rc != 0 or "successfully" not in combined:
            # Contextual error messages
            if "connection refused" in combined:
                msg = "Connection refused. Is Wireless Debugging enabled and the pairing dialog open?"
            elif "timeout" in combined or "timed out" in combined:
                msg = "Connection timed out. The pairing code may have expired — try again with a fresh code."
            elif "wrong" in combined or "incorrect" in combined or "failed to authenticate" in combined:
                msg = "Wrong pairing code. Open a new pairing dialog and enter the fresh code."
            elif "protocol fault" in combined or "couldn't read" in combined:
                msg = "Connection error — wrong pairing port? Check the port shown in the pairing dialog."
            else:
                msg = stderr or stdout or "Pairing failed for unknown reason"
            steps.append({"step": "pairing", "status": "fail", "message": msg})
            return {"success": False, "steps": steps, "partial": False, "needs_connection_port": False}

        steps.append({"step": "pairing", "status": "ok", "message": "Paired successfully"})
        log.info(f"[ADB] Smart pair step 2 OK: paired with {device_id}")

        # Store pairing timestamp in device metadata
        device.metadata["adb_paired_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        device.metadata["adb_pairing_status"] = "paired"
        registry._save_to_file()

        # Step 3: Discover connection port
        log.info(f"[ADB] Smart pair step 3: discovering connection port for {ip}")
        discovered_port = await self.discover_connection_port(ip, device.adb_port)

        if not discovered_port:
            steps.append({
                "step": "port_discovery", "status": "skip",
                "message": "Could not auto-detect connection port. Please enter it manually."
            })
            return {
                "success": False, "steps": steps,
                "partial": True, "needs_connection_port": True
            }
        steps.append({
            "step": "port_discovery", "status": "ok",
            "message": f"Connection port discovered: {discovered_port}"
        })

        # Update device port in registry
        registry.update_device(device_id, adb_port=discovered_port)

        # Step 4: Connect
        log.info(f"[ADB] Smart pair step 4: connecting to {ip}:{discovered_port}")
        conn_str = f"{ip}:{discovered_port}"
        rc, stdout, stderr = await self._run_adb("connect", conn_str, timeout=15)

        if rc == 0 and ("connected" in stdout.lower() or "already connected" in stdout.lower()):
            self._connected[device_id] = time.time()
            device = registry.get_device(device_id)
            if device:
                device.status = DeviceStatus.ONLINE
                device.last_seen = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                registry._save_to_file()
            steps.append({"step": "connect", "status": "ok", "message": f"Connected to {conn_str}"})

            # Enrich metadata in background
            try:
                await self._enrich_metadata(device_id)
            except Exception as e:
                log.warning(f"[ADB] Metadata enrichment failed: {e}")

            log.info(f"[ADB] Smart pair complete for {device_id}")
            return {"success": True, "steps": steps, "partial": False, "needs_connection_port": False}

        steps.append({
            "step": "connect", "status": "fail",
            "message": stderr or stdout or f"Failed to connect on port {discovered_port}"
        })
        return {"success": False, "steps": steps, "partial": True, "needs_connection_port": True}

    async def set_connection_port(self, device_id: str, connection_port: int) -> Dict:
        """Fallback: manually set connection port and connect after smart_pair discovered pairing."""
        from Orchestrator.device_registry import get_registry, DeviceStatus
        registry = get_registry()
        device = registry.get_device(device_id)
        if not device:
            return {"success": False, "error": f"Device not found: {device_id}"}

        # Update port
        registry.update_device(device_id, adb_port=connection_port)
        device = registry.get_device(device_id)
        ip = device.tailscale_ip

        # Connect
        conn_str = f"{ip}:{connection_port}"
        rc, stdout, stderr = await self._run_adb("connect", conn_str, timeout=15)

        if rc == 0 and ("connected" in stdout.lower() or "already connected" in stdout.lower()):
            self._connected[device_id] = time.time()
            device.status = DeviceStatus.ONLINE
            device.last_seen = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            registry._save_to_file()
            log.info(f"[ADB] Manual port connect: {device_id} on {conn_str}")
            try:
                await self._enrich_metadata(device_id)
            except Exception:
                pass
            return {"success": True, "device_id": device_id, "message": f"Connected on port {connection_port}"}

        return {"success": False, "error": stderr or stdout or f"Failed to connect on port {connection_port}"}

    async def pair_from_ip(self, tailscale_ip: str, pairing_port: int, pairing_code: str) -> Dict:
        """Android app relay: find device by Tailscale IP in registry, then smart_pair."""
        from Orchestrator.device_registry import get_registry, DeviceType, DeviceProtocol
        registry = get_registry()

        # Find device by IP
        device = None
        for d in registry.get_all_devices():
            if d.tailscale_ip == tailscale_ip:
                device = d
                break

        if not device:
            # Create placeholder device
            device_id = f"android-{tailscale_ip.replace('.', '-')}"
            from Orchestrator.device_registry import Device
            device = Device(
                id=device_id,
                name=f"Android ({tailscale_ip})",
                tailscale_ip=tailscale_ip,
                device_type=DeviceType.ANDROID,
                protocol=DeviceProtocol.ADB,
                owner="",  # I2: discovery is UNCLAIMED — ownership set only via POST /{id}/operator
            )
            registry.add_device(device)
            log.info(f"[ADB] Created placeholder device {device_id} for IP {tailscale_ip}")

        return await self.smart_pair(device.id, pairing_port, pairing_code)

    async def disconnect(self, device_id: str) -> Dict:
        from Orchestrator.device_registry import get_registry
        device = get_registry().get_device(device_id)
        if not device:
            return {"success": False, "error": f"Device not found: {device_id}"}
        conn_str = device.connection_string()
        rc, stdout, stderr = await self._run_adb("disconnect", conn_str)
        self._connected.pop(device_id, None)
        return {"success": True, "message": stdout or "Disconnected"}

    async def is_connected(self, device_id: str) -> bool:
        from Orchestrator.device_registry import get_registry
        device = get_registry().get_device(device_id)
        if not device:
            return False
        conn_str = device.connection_string()
        rc, stdout, stderr = await self._run_adb("devices")
        return conn_str in stdout and "device" in stdout

    async def ensure_connected(self, device_id: str) -> Dict:
        if await self.is_connected(device_id):
            return {"success": True, "message": "Already connected"}

        # Try direct connect first
        result = await self.connect(device_id)
        if result["success"]:
            return result

        # If failed, try port rediscovery
        from Orchestrator.device_registry import get_registry
        device = get_registry().get_device(device_id)
        if device:
            discovered_port = await self.discover_connection_port(device.tailscale_ip, device.adb_port)
            if discovered_port and discovered_port != device.adb_port:
                get_registry().update_device(device_id, adb_port=discovered_port)
                result = await self.connect(device_id)

        return result

    async def list_connected(self) -> list:
        rc, stdout, stderr = await self._run_adb("devices")
        lines = stdout.strip().split("\n")[1:]
        devices = []
        for line in lines:
            if "\t" in line:
                serial, state = line.split("\t", 1)
                devices.append({"serial": serial.strip(), "state": state.strip()})
        return devices


_adb_manager: Optional[ADBManager] = None

def get_adb_manager() -> ADBManager:
    global _adb_manager
    if _adb_manager is None:
        _adb_manager = ADBManager()
    return _adb_manager
