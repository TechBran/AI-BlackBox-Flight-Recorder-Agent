"""
AMI (Asterisk Manager Interface) client for SMS via Yeastar TG200 gateway.

Protocol: TCP line-based, \r\n terminated, blank line separates messages.
SMS uses the proprietary SMSCommand action and ReceivedSMS event.
"""

import asyncio
import logging
import urllib.parse
import uuid
from datetime import datetime
from typing import Callable, Optional

log = logging.getLogger("sms.ami")


class AMISMSClient:
    """Async AMI client specialised for TG200 SMS operations."""

    def __init__(
        self,
        host: str = "",
        port: int = 5038,
        username: str = "",
        secret: str = "",
    ):
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret

        # Connection state
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._authenticated = False
        self._shutting_down = False

        # Pending action responses keyed by ActionID
        self._pending: dict[str, asyncio.Future] = {}

        # Background tasks
        self._read_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._multipart_flush_tasks: dict[str, asyncio.Task] = {}

        # Incoming SMS callback
        self._sms_callbacks: list[Callable] = []

        # Multi-part SMS buffer: (sender, recvtime) -> {index: content, ...}
        self._multipart_buf: dict[tuple, dict] = {}
        self._multipart_total: dict[tuple, int] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self):
        """Open TCP socket, authenticate, and start the read loop."""
        self._shutting_down = False
        if not self.host or not self.username or not self.secret:
            log.warning(
                "AMI credentials not configured "
                "(ASTERISK_AMI_HOST/USER/SECRET); SMS disabled"
            )
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=10
            )
        except (OSError, asyncio.TimeoutError) as exc:
            log.error("AMI connect failed (%s:%s): %s", self.host, self.port, exc)
            self._schedule_reconnect()
            return

        # Read the banner line
        try:
            banner = await asyncio.wait_for(self._reader.readline(), timeout=5)
            log.info("AMI banner: %s", banner.decode(errors="replace").strip())
        except (asyncio.TimeoutError, ConnectionError) as exc:
            log.error("Failed to read AMI banner: %s", exc)
            await self._close_transport()
            self._schedule_reconnect()
            return

        self._connected = True

        # Authenticate
        if not await self._authenticate():
            log.error("AMI authentication failed")
            await self._close_transport()
            self._schedule_reconnect()
            return

        self._authenticated = True
        log.info("AMI authenticated to %s:%s", self.host, self.port)

        # Start background loops
        self._read_task = asyncio.create_task(self._read_loop(), name="ami-read")
        self._keepalive_task = asyncio.create_task(self._keepalive_loop(), name="ami-keepalive")

    async def disconnect(self):
        """Gracefully disconnect from AMI."""
        self._shutting_down = True

        # Cancel multipart flush timers
        for task in self._multipart_flush_tasks.values():
            task.cancel()
        self._multipart_flush_tasks.clear()

        # Send Logoff if still connected
        if self._connected and self._writer and not self._writer.is_closing():
            try:
                await self._send_raw("Action: Logoff\r\n\r\n")
            except Exception:
                pass

        # Cancel background tasks
        for task in (self._read_task, self._keepalive_task, self._reconnect_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        await self._close_transport()
        self._authenticated = False
        self._connected = False
        log.info("AMI disconnected")

    async def _close_transport(self):
        """Close the underlying TCP transport."""
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        self._reader = None
        self._connected = False
        self._authenticated = False

        # Fail all pending futures
        for action_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(ConnectionError("AMI connection lost"))
        self._pending.clear()

    async def _authenticate(self) -> bool:
        """Send Login action and verify success."""
        payload = (
            "Action: Login\r\n"
            f"Username: {self.username}\r\n"
            f"Secret: {self.secret}\r\n"
            "Events: on\r\n"
            "\r\n"
        )
        await self._send_raw(payload)

        # Read the response (before read loop is running)
        try:
            response = await self._read_message(timeout=5)
        except (asyncio.TimeoutError, ConnectionError) as exc:
            log.error("Auth response read failed: %s", exc)
            return False

        return response.get("Response", "").lower() == "success"

    def _schedule_reconnect(self):
        """Schedule an auto-reconnect with exponential backoff."""
        if self._shutting_down:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return  # Already scheduled
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        """Retry connection with exponential backoff up to 30s."""
        delay = 1.0
        while not self._shutting_down:
            log.info("AMI reconnect in %.0fs ...", delay)
            await asyncio.sleep(delay)
            if self._shutting_down:
                return
            await self.connect()
            if self._authenticated:
                log.info("AMI reconnected successfully")
                return
            delay = min(delay * 2, 30.0)

    # ------------------------------------------------------------------
    # Raw I/O
    # ------------------------------------------------------------------

    async def _send_raw(self, data: str):
        """Write raw bytes to the AMI socket."""
        if not self._writer or self._writer.is_closing():
            raise ConnectionError("Not connected to AMI")
        self._writer.write(data.encode("utf-8"))
        await self._writer.drain()

    async def _send_action(self, action: str, **params) -> str:
        """Send an AMI action with a unique ActionID and return the raw response."""
        action_id = uuid.uuid4().hex[:12]
        lines = [f"Action: {action}", f"ActionID: {action_id}"]
        for key, value in params.items():
            lines.append(f"{key}: {value}")
        payload = "\r\n".join(lines) + "\r\n\r\n"

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[action_id] = fut

        try:
            await self._send_raw(payload)
        except Exception as exc:
            self._pending.pop(action_id, None)
            raise exc

        try:
            result = await asyncio.wait_for(fut, timeout=15)
        except asyncio.TimeoutError:
            self._pending.pop(action_id, None)
            raise TimeoutError(f"AMI action {action} timed out (ActionID={action_id})")

        return result

    # ------------------------------------------------------------------
    # Message reading / parsing
    # ------------------------------------------------------------------

    async def _read_message(self, timeout: float = 10) -> dict:
        """Read a single AMI message block and return it as a dict.

        Handles both standard blank-line-delimited messages and
        proprietary terminators (--END COMMAND--, --END SMS EVENT--).

        For "Response: Follows" messages, we continue parsing Key: Value
        headers (like ActionID, Privilege) and only treat non-KV lines
        as body text. The message ends at --END COMMAND-- or --END SMS EVENT--.
        """
        if not self._reader:
            raise ConnectionError("Not connected to AMI")

        fields: dict[str, str] = {}
        body_lines: list[str] = []
        has_terminator = False  # True when we expect --END ... -- instead of blank line

        deadline = asyncio.get_event_loop().time() + timeout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError("Timed out reading AMI message")

            raw = await asyncio.wait_for(self._reader.readline(), timeout=remaining)
            if not raw:
                raise ConnectionError("AMI connection closed (EOF)")

            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")

            # Proprietary terminators — end of message
            # Check both startswith AND contains, because TG200 sometimes
            # embeds the terminator at the end of a body line (e.g. after AT echo)
            if "--END COMMAND--" in line or "--END SMS EVENT--" in line:
                # Strip terminator from the line if it has leading content
                prefix = line.split("--END")[0].strip()
                if prefix:
                    body_lines.append(prefix)
                if body_lines:
                    fields["_body"] = "\n".join(body_lines)
                return fields

            # Blank line = end of standard message (only if no terminator expected)
            if line == "":
                if fields and not has_terminator:
                    return fields
                continue  # Skip blank lines in terminated blocks or leading blanks

            # Try to parse as Key: Value
            if ": " in line:
                key, _, value = line.partition(": ")
                # Validate key looks like a header (alphanumeric, no spaces in key)
                if key and " " not in key:
                    fields[key] = value
                    if key == "Response" and value == "Follows":
                        has_terminator = True
                    continue

            # Non-header line = body text
            body_lines.append(line)

        return fields

    async def _read_loop(self):
        """Continuously read AMI messages, dispatching events and responses."""
        try:
            while self._connected and not self._shutting_down:
                try:
                    msg = await self._read_message(timeout=60)
                except asyncio.TimeoutError:
                    continue  # Keep-alive will detect true dead connections
                except (ConnectionError, RuntimeError) as exc:
                    if "already waiting" in str(exc):
                        # Concurrent read — back off and retry
                        log.debug("AMI read contention, backing off: %s", exc)
                        await asyncio.sleep(0.5)
                        continue
                    log.warning("AMI read loop: connection lost: %s", exc)
                    break

                # Dispatch based on message type
                action_id = msg.get("ActionID")
                event = msg.get("Event")

                if action_id and action_id in self._pending:
                    fut = self._pending.pop(action_id)
                    if not fut.done():
                        fut.set_result(msg)
                elif event == "ReceivedSMS":
                    await self._handle_received_sms(msg)
                elif event:
                    log.debug("AMI event: %s", event)
                elif msg.get("Response"):
                    log.debug("AMI unsolicited response: %s", msg.get("Response"))

        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("AMI read loop unexpected error")

        # Connection lost — attempt reconnect
        if not self._shutting_down:
            log.warning("AMI read loop exited, scheduling reconnect")
            await self._close_transport()
            self._schedule_reconnect()

    async def _keepalive_loop(self):
        """Send Ping every 30s to detect dead connections."""
        try:
            while self._connected and not self._shutting_down:
                await asyncio.sleep(30)
                if not self._connected or self._shutting_down:
                    return
                try:
                    resp = await self._send_action("Ping")
                    if resp.get("Response", "").lower() != "success":
                        log.warning("AMI Ping got unexpected response: %s", resp)
                except (ConnectionError, TimeoutError) as exc:
                    log.warning("AMI keepalive failed: %s", exc)
                    await self._close_transport()
                    self._schedule_reconnect()
                    return
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Incoming SMS handling
    # ------------------------------------------------------------------

    def on_sms(self, callback: Callable):
        """Register an async callback: callback(sender, body, span, recvtime)."""
        self._sms_callbacks.append(callback)

    async def _handle_received_sms(self, msg: dict):
        """Process a ReceivedSMS event, buffering multi-part if needed."""
        sender = msg.get("Sender", "").strip()
        raw_content = msg.get("Content", "")
        span = msg.get("GsmSpan", "2")
        recvtime = msg.get("Recvtime", "")

        # URL-decode the content (+ = space, %XX = char)
        body = urllib.parse.unquote_plus(raw_content)

        total = int(msg.get("Total", "1"))
        index = int(msg.get("Index", "0"))

        log.info(
            "SMS received: sender=%s span=%s part=%d/%d len=%d",
            sender, span, index + 1, total, len(body),
        )

        if total <= 1:
            # Single-part SMS — deliver immediately
            await self._dispatch_sms(sender, body, span, recvtime)
            return

        # Multi-part: buffer until all parts arrive
        buf_key = (sender, recvtime)
        if buf_key not in self._multipart_buf:
            self._multipart_buf[buf_key] = {}
            self._multipart_total[buf_key] = total
            # Start a 30s flush timer
            task = asyncio.create_task(self._multipart_timeout(buf_key, sender, span, recvtime))
            self._multipart_flush_tasks[buf_key] = task

        self._multipart_buf[buf_key][index] = body

        # Check if all parts have arrived
        if len(self._multipart_buf[buf_key]) >= total:
            # Cancel the timeout timer
            timer = self._multipart_flush_tasks.pop(buf_key, None)
            if timer and not timer.done():
                timer.cancel()
            await self._flush_multipart(buf_key, sender, span, recvtime)

    async def _multipart_timeout(self, buf_key: tuple, sender: str, span: str, recvtime: str):
        """After 30s, flush whatever parts we have for a multi-part SMS."""
        try:
            await asyncio.sleep(30)
            self._multipart_flush_tasks.pop(buf_key, None)
            if buf_key in self._multipart_buf:
                log.warning(
                    "Multi-part SMS timeout for %s — flushing %d/%d parts",
                    sender,
                    len(self._multipart_buf[buf_key]),
                    self._multipart_total.get(buf_key, "?"),
                )
                await self._flush_multipart(buf_key, sender, span, recvtime)
        except asyncio.CancelledError:
            return

    async def _flush_multipart(self, buf_key: tuple, sender: str, span: str, recvtime: str):
        """Assemble buffered parts in order and deliver."""
        parts = self._multipart_buf.pop(buf_key, {})
        self._multipart_total.pop(buf_key, None)

        # Assemble in index order
        assembled = ""
        for i in sorted(parts.keys()):
            assembled += parts[i]

        await self._dispatch_sms(sender, assembled, span, recvtime)

    async def _dispatch_sms(self, sender: str, body: str, span: str, recvtime: str):
        """Invoke all registered SMS callbacks."""
        log.info("SMS dispatch: sender=%s body=%r", sender, body[:100])
        for cb in self._sms_callbacks:
            try:
                await cb(sender, body, span, recvtime)
            except Exception:
                log.exception("SMS callback error")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_sms(self, destination: str, message: str, span: int = 2) -> dict:
        """Send an SMS via TG200 (fire-and-forget).

        Uses fire-and-forget because the SMSCommand response from TG200's AMI
        has no useful data (just "Response: Follows" with empty body), and the
        interleaved UpdateSMSSend events cause ActionID matching timeouts.
        We validated via raw nc that the send always works if we're connected.

        Args:
            destination: Phone number (e.g. "+14105551234")
            message: Text body (will be quote-escaped)
            span: GSM span on the TG200 (default 2)

        Returns:
            {"success": bool, "error": str | None}
        """
        if not self._authenticated:
            return {"success": False, "error": "Not connected to AMI"}

        # Escape double quotes in message body
        safe_msg = message.replace('"', '\\"')

        command = f'gsm send sms {span} {destination} "{safe_msg}"'
        log.info("Sending SMS to %s via span %d (%d chars)", destination, span, len(message))

        try:
            # Fire-and-forget: send action without waiting for response
            # The read loop will consume the response/events normally
            await self._send_raw(
                f"Action: SMSCommand\r\n"
                f"Command: {command}\r\n"
                f"\r\n"
            )
            log.info("SMS dispatched to %s", destination)
            return {"success": True, "error": None}
        except (ConnectionError, OSError) as exc:
            log.error("send_sms failed: %s", exc)
            return {"success": False, "error": str(exc)}

    async def get_span_status(self, span: int = 2) -> dict:
        """Query GSM span status (network, signal, registration state).

        Returns:
            {"network": str, "signal": str, "state": str, "raw": str}
        """
        if not self._authenticated:
            return {"network": "", "signal": "", "state": "disconnected", "raw": ""}

        try:
            resp = await self._send_action("SMSCommand", Command=f"gsm show span {span}")
        except (ConnectionError, TimeoutError) as exc:
            log.error("get_span_status failed: %s", exc)
            return {"network": "", "signal": "", "state": "error", "raw": str(exc)}

        body = resp.get("_body", "")
        result = {"network": "", "signal": "", "state": "", "raw": body}

        for line in body.splitlines():
            line_lower = line.lower().strip()
            if "network" in line_lower and ":" in line:
                result["network"] = line.split(":", 1)[1].strip()
            elif "signal" in line_lower and ":" in line:
                result["signal"] = line.split(":", 1)[1].strip()
            elif "state" in line_lower and ":" in line:
                result["state"] = line.split(":", 1)[1].strip()

        return result

    @property
    def connected(self) -> bool:
        """Whether the client is currently connected and authenticated."""
        return self._connected and self._authenticated
