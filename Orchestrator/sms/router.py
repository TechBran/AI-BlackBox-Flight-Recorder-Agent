"""
SMS Router — matches incoming SMS to operators via contact book.

Flow:
1. AMI client fires on_sms callback with sender, body, span, recvtime
2. Normalize sender phone number
3. Search all operator contact books for matching phone
4. If found: store inbound, process through AI, send reply, store outbound
5. If not found: log and ignore (whitelist enforcement)
"""

import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger("sms.router")


def _normalize_phone(phone: str) -> str:
    """Strip to last 10 digits for comparison."""
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _phones_match(a: str, b: str) -> bool:
    """Compare two phone numbers by their last 10 digits."""
    return _normalize_phone(a) == _normalize_phone(b)


class SMSRouter:
    """Routes incoming SMS to the correct operator based on contact book."""

    def __init__(self, manager, message_store):
        self.manager = manager
        self.store = message_store
        # Register the inbound callback across ALL (current + future) gateways.
        self.manager.set_sms_callback(self.handle_incoming)
        log.info("SMSRouter initialized — listening for incoming SMS across all gateways")

    @staticmethod
    def _is_system_seed(contact: dict) -> bool:
        """The auto-injected system/self seed contact must NEVER satisfy the
        inbound whitelist (its number is fixed + spoofable). Real operator-added
        self contacts (created_by != 'system') are unaffected."""
        if (contact.get("created_by") or "") == "system":
            return True
        tags = [str(t).lower() for t in (contact.get("tags") or [])]
        return "system" in tags

    def _find_operator_by_phone(self, phone: str):
        """Route incoming SMS to the correct operator.

        Two-pass lookup:
        1. Check if sender IS an operator (matches a "self"/"owner" contact) → route to that operator
        2. Fall back: search all contact books for the number → route to book owner

        The lookup is READ-ONLY: it never fabricates a book (no
        ensure_operator_book) and skips the auto-injected system/self seed
        contact so the fixed, spoofable system number can't whitelist itself.

        Returns:
            (operator: str, contact: dict) if found, else (None, None)
        """
        try:
            from Orchestrator.contacts import load_contacts
            from Orchestrator.config import USERS_LIST
        except ImportError:
            from contacts import load_contacts
            from config import USERS_LIST

        data = load_contacts()

        # Pass 1: Is the sender an operator? (their own phone number)
        # Check contacts tagged as "self", "owner", or with relationship "self"/"owner"
        for operator in USERS_LIST:
            contacts = data.get(operator, {})
            for _cid, contact in contacts.items():
                if self._is_system_seed(contact):
                    continue
                contact_phone = contact.get("phone", "")
                if not contact_phone or not _phones_match(phone, contact_phone):
                    continue
                # Check if this contact represents the operator themselves
                relationship = (contact.get("relationship") or "").lower()
                tags = [t.lower() for t in (contact.get("tags") or [])]
                if relationship in ("self", "owner") or "self" in tags or "owner" in tags:
                    log.info("SMS sender %s matched operator %s (self/owner contact)", phone, operator)
                    return operator, contact

        # Pass 2: Fall back to contact book search (sender is a known contact of some operator)
        for operator in USERS_LIST:
            contacts = data.get(operator, {})
            for _cid, contact in contacts.items():
                if self._is_system_seed(contact):
                    continue
                contact_phone = contact.get("phone", "")
                if contact_phone and _phones_match(phone, contact_phone):
                    return operator, contact

        return None, None

    def _resolve_line(self, gateway_id, span):
        """Resolve the line (our number) and its owner from (gateway_id, span).

        Returns:
            (line_number: str, owner: str | None) — line_number is "" if the
            gateway/span can't be resolved; owner is None when the port has no
            dedicated operator.
        """
        gw = self.manager.gateways().get(gateway_id) if gateway_id else None
        if not gw:
            return "", None
        for p in gw.get("ports", []) or []:
            if str(p.get("span")) == str(span):
                return p.get("phone_number", "") or "", (p.get("operator") or None)
        return "", None

    def _find_in_operator_book(self, owner, sender):
        """Match `sender` ONLY against `owner`'s contact book.

        A dedicated line can only be reached by someone whitelisted FOR THAT
        OWNER — being in another operator's book does not count.

        READ-ONLY: never fabricates a book (no ensure_operator_book) and skips
        the auto-injected system/self seed contact, so a bookless owner has an
        empty whitelist and the fixed system number can't whitelist itself.

        Returns:
            (owner, contact) if found in owner's book, else (None, None).
        """
        try:
            from Orchestrator.contacts import load_contacts
        except ImportError:
            from contacts import load_contacts

        data = load_contacts()
        contacts = data.get(owner, {})
        for _cid, contact in contacts.items():
            if self._is_system_seed(contact):
                continue
            contact_phone = contact.get("phone", "")
            if contact_phone and _phones_match(sender, contact_phone):
                return owner, contact
        return None, None

    async def handle_incoming(self, sender: str, body: str, span: str, recvtime: str, gateway_id: str = None):
        """Process an incoming SMS from one of the gateway AMI clients.

        Args:
            sender: Sender phone number (E.164, e.g. +14108166914)
            body: Decoded message text
            span: GSM span that received the message
            recvtime: Timestamp from TG200 (e.g. "2026-03-25 17:20:48")
            gateway_id: Id of the gateway that received the message (reply goes back out the same one)
        """
        log.info("Incoming SMS from %s (gateway=%s span=%s): %s", sender, gateway_id, span, body[:80])

        # 1. Resolve the LINE (our number + owner) from (gateway_id, span).
        line_number, owner = self._resolve_line(gateway_id, span)

        # 2. Scope the whitelist by line ownership (only ever TIGHTENS).
        #    Owned line: match ONLY the owner's book. Unowned line: search all.
        #    Either way, no contact match -> DROP before any chat/task/send/store.
        if owner:
            operator, contact = self._find_in_operator_book(owner, sender)
        else:
            operator, contact = self._find_operator_by_phone(sender)
        if not operator:
            log.info("Ignoring SMS from unknown number: %s", sender)
            return

        contact_name = contact.get("name", sender) if contact else sender
        log.info("SMS routed to operator=%s contact=%s", operator, contact_name)

        # 2. Normalize timestamp to ISO 8601
        try:
            ts = datetime.strptime(recvtime, "%Y-%m-%d %H:%M:%S")
            timestamp = ts.replace(tzinfo=timezone.utc).isoformat()
        except (ValueError, TypeError):
            timestamp = datetime.now(timezone.utc).isoformat()

        # 3. Store inbound message (tagged with the resolved line + gateway)
        self.store.store_message(
            operator=operator,
            direction="inbound",
            phone_number=sender,
            contact_name=contact_name,
            body=body,
            timestamp=timestamp,
            line_number=line_number,
            gateway_id=gateway_id or "",
        )

        # 4. Process through main chat pipeline (replaces process_incoming_sms)
        reply = ""
        try:
            reply = await self._route_through_chat(sender, body, operator, contact_name)
        except Exception:
            log.exception("Chat pipeline failed for SMS from %s", sender)

        # 5. Send reply as SMS segments — out the SAME gateway that received it
        if reply:
            client = self.manager.get(gateway_id) or self.manager.default()
            if client is None:
                log.error("No gateway client available to reply to SMS from %s", sender)
                return
            segments = self._split_sms(reply)
            for i, segment in enumerate(segments):
                result = await client.send_sms(sender, segment, span=int(span))
                now = datetime.now(timezone.utc).isoformat()
                status = "delivered" if result.get("success") else "failed"
                self.store.store_message(
                    operator=operator,
                    direction="outbound",
                    phone_number=sender,
                    contact_name=contact_name,
                    body=segment,
                    timestamp=now,
                    status=status,
                    line_number=line_number,
                    gateway_id=gateway_id or "",
                )
                if not result.get("success"):
                    log.error("SMS segment %d/%d send failed: %s", i + 1, len(segments), result.get("error"))
                    break
                if i < len(segments) - 1:
                    await asyncio.sleep(0.5)  # Small delay between segments

            log.info("AI reply sent to %s (%d segments, %d chars total)", sender, len(segments), len(reply))
        else:
            log.warning("No AI reply generated for SMS from %s", sender)

    async def _route_through_chat(self, sender: str, body: str, operator: str, contact_name: str) -> str:
        """Route SMS through the main /chat pipeline for full context retrieval."""
        import time
        import aiohttp
        from Orchestrator.state import get_operator_preference

        sms_provider = get_operator_preference(operator, "sms_provider", "anthropic")
        sms_model = get_operator_preference(operator, "sms_model", "claude-sonnet-4-5")

        sms_context = f"[SMS from {contact_name} ({sender})]: {body}"

        payload = {
            "messages": [{"role": "user", "content": sms_context}],
            "operator": operator,
            "provider": sms_provider,
            "model": sms_model,
            "sms_mode": True,
            "sms_sender": sender,
            "sms_contact_name": contact_name,
        }

        try:
            async with aiohttp.ClientSession() as session:
                # Create the chat task
                async with session.post(
                    "http://localhost:9091/chat",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    result = await resp.json()
                    task_id = result.get("task_id")
                    if not task_id:
                        log.error("Chat pipeline returned no task_id: %s", result)
                        return ""

                # Poll for completion (max 45 seconds)
                deadline = time.time() + 45
                while time.time() < deadline:
                    await asyncio.sleep(1.5)
                    async with session.get(
                        f"http://localhost:9091/tasks/{task_id}",
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as status_resp:
                        status_data = await status_resp.json()
                        task_status = status_data.get("status", "")

                        if task_status == "completed":
                            result_data = status_data.get("result_data", {})
                            reply = result_data.get("ui_reply", "") or result_data.get("text", "")
                            # Strip any HTML/markdown that the chat pipeline might add
                            reply = self._strip_html(reply)
                            log.info("Chat pipeline reply for %s (%d chars)", sender, len(reply))
                            return reply
                        elif task_status == "failed":
                            error = status_data.get("error_message", "Unknown error")
                            log.error("Chat task failed for SMS from %s: %s", sender, error)
                            return ""

                log.error("Chat task timed out for SMS from %s (task_id=%s)", sender, task_id)
                return ""

        except Exception:
            log.exception("Failed to route SMS through chat pipeline")
            return ""

    @staticmethod
    def _strip_html(text: str) -> str:
        """Remove HTML tags from text (chat pipeline may include them)."""
        import re

        # Remove HTML tags
        text = re.sub(r"<[^>]+>", "", text)
        # Remove markdown formatting
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # bold
        text = re.sub(r"\*(.+?)\*", r"\1", text)  # italic
        text = re.sub(r"`(.+?)`", r"\1", text)  # inline code
        # Collapse whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _split_sms(text: str, max_len: int = 160) -> list:
        """Split long text into SMS-sized segments at word boundaries."""
        text = text.strip()
        if not text:
            return []
        if len(text) <= max_len:
            return [text]
        segments = []
        while text:
            if len(text) <= max_len:
                segments.append(text)
                break
            # Find last space within limit
            split_at = text.rfind(" ", 0, max_len)
            if split_at == -1:
                split_at = max_len
            segments.append(text[:split_at].rstrip())
            text = text[split_at:].lstrip()
        return segments[:10]  # Max 10 segments

    async def send_manual(
        self,
        operator: str,
        to: str,
        message: str,
        from_number: str = None,
        gateway_id: str = None,
        span: int = None,
    ) -> dict:
        """Send an SMS manually (from Portal UI).

        Picks the outbound gateway in priority order:
          1. explicit gateway_id
          2. from_number resolved to its owning gateway+span
          3. default (first enabled) gateway

        Args:
            operator: Operator sending the message
            to: Destination phone number
            message: Message text
            from_number: Originating line; resolves gateway + span when set
            gateway_id: Explicit gateway to send through
            span: GSM span override (defaults to 2 when unresolved)

        Returns:
            {"success": bool, "error": str | None, "message_id": int | None}
        """
        # Resolve which gateway client (and span) to use.
        client = None
        if gateway_id:
            client = self.manager.get(gateway_id)
        elif from_number:
            res = self.manager.resolve_for_number(from_number)
            if res:
                client, span = res
        else:
            client = self.manager.default()

        client = client or self.manager.default()
        if client is None:
            return {"success": False, "error": "No gateway available"}

        # Look up contact name
        _, contact = self._find_operator_by_phone(to)
        contact_name = contact.get("name", to) if contact else to

        # Send via the chosen gateway client (span resolution refined in Task 3.3)
        result = await client.send_sms(to, message, span=int(span) if span else 2)
        status = "delivered" if result.get("success") else "failed"

        # Store outbound message
        now = datetime.now(timezone.utc).isoformat()
        msg_id = self.store.store_message(
            operator=operator,
            direction="outbound",
            phone_number=to,
            contact_name=contact_name,
            body=message,
            timestamp=now,
            status=status,
        )

        return {
            "success": result.get("success", False),
            "error": result.get("error"),
            "message_id": msg_id,
        }
