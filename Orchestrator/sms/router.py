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
from collections import namedtuple
from datetime import datetime, timezone

# Notification bus (MN.2). Imported at module scope so the M3 peer-inbound hook
# can ``await notify(...)`` directly (handle_incoming is async, running on the
# AMI client's event loop — no sync->async bridge needed; mirrors
# scheduler.manager's async _notify_run_outcome). notify() never raises and
# always records a searchable snapshot. Tests monkeypatch ``router.notify``.
try:
    from Orchestrator.notifications.bus import notify
except ImportError:  # pragma: no cover - import-path fallback (mirrors load_contacts)
    from notifications.bus import notify

log = logging.getLogger("sms.router")

# Notification body preview cap for a peer inbound — keep the alert short; the
# full inbound is already persisted to the SMS store + the chat thread.
PEER_NOTIFY_PREVIEW_CHARS = 200

# M4 — recent SMS thread auto-injection window. The N most-recent stored rows
# (both directions) are prepended to the /chat payload as conversation turns.
# Bounded by COUNT (newest-kept) to protect the per-turn prefill budget and the
# 45s SMS reply deadline — do NOT inject unbounded history.
SMS_THREAD_WINDOW = 20

# The fixed, spoofable system/self seed number. A contact whose phone is this
# number can NEVER satisfy the inbound whitelist, regardless of created_by /
# is_operator_self / tags. Gate by the NUMBER (last-10-digit), not by metadata,
# so a real operator contact that merely has created_by="system" is unaffected.
SEED_PHONE = "+17164512527"


def _normalize_phone(phone: str) -> str:
    """Strip to last 10 digits for comparison.

    Last-10-digit normalization — kept byte-for-byte in agreement with
    ``contacts._normalize_phone`` (None-safe). The two are intentionally
    identical; if one changes, change both.
    """
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _phones_match(a: str, b: str) -> bool:
    """Compare two phone numbers by their last 10 digits."""
    return _normalize_phone(a) == _normalize_phone(b)


# Result of inbound operator resolution.
#   operator      — resolved operator, or None when the SMS must be dropped.
#   contact       — the matched contact dict, or None.
#   classification — 'self'  (matched contact has is_operator_self=True),
#                    'peer'  (inbound_allowed=True, is_operator_self=False),
#                    or None (no match / drop).
# M2 COMPUTES + returns classification; M3 consumes it (peer notification +
# tagging the chat payload). handle_incoming does not act on it yet.
Resolution = namedtuple("Resolution", ["operator", "contact", "classification"])


def _recency_key(contact: dict) -> str:
    """Sort key for the most-recently-updated tiebreak.

    Contacts carry ISO-8601 ``updated_at``/``created_at`` strings (see
    contacts._make_seed_contact / upsert_contact); ISO-8601 sorts correctly as
    text. Missing both -> "" so timestamped contacts always outrank undated ones,
    and undated contacts preserve their USERS_LIST encounter order via a stable
    sort.
    """
    return contact.get("updated_at") or contact.get("created_at") or ""


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
        inbound whitelist (its number is fixed + spoofable).

        Gate ONLY the literal seed phone +17164512527 — NOT all
        created_by="system" contacts. A real operator contact can legitimately
        carry created_by="system" (e.g. Anna's self-marker for Brandon was
        created during system seeding); the old metadata-based check wrongly
        excluded it from identity matching. The number is the spoofable fact, so
        the number is what we gate.
        """
        return _phones_match(contact.get("phone", ""), SEED_PHONE)

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

    def resolve_inbound(self, sender: str, owner) -> Resolution:
        """Deterministic 5-tier inbound operator resolution (first match wins).

        Every tier crossed is logged. The seed phone is gated everywhere via
        ``_is_system_seed``. ``load_contacts`` guarantees every contact carries
        ``inbound_allowed`` (default True for legacy) and ``is_operator_self``
        (default False).

        Args:
            sender: the inbound sender's phone number (E.164).
            owner:  the operator who owns the receiving line, or None for an
                    unowned line. Pass the value from ``_resolve_line``.

        Resolution order:
          1. Owned line — sender must be inbound_allowed in OWNER's book ->
             owner; else DROP (owned lines are strict — no fall-through).
          2. Identity   — unowned line: sender is is_operator_self in some book.
             Multiple self-flags -> most-recently-updated + WARNING.
          3. Single whitelist — sender inbound_allowed in exactly one book.
          4. Multi-match      — inbound_allowed in several books -> most-recently-
             updated + WARNING naming all candidate operators.
          5. No match -> DROP.

        Returns:
            Resolution(operator, contact, classification). On a drop:
            Resolution(None, None, None).
        """
        try:
            from Orchestrator.contacts import load_contacts
            from Orchestrator.config import USERS_LIST
        except ImportError:
            from contacts import load_contacts
            from config import USERS_LIST

        data = load_contacts()

        # ---- Tier 1: Line ownership (STRICT — owned lines never fall through).
        if owner:
            for _cid, contact in (data.get(owner, {}) or {}).items():
                if self._is_system_seed(contact):
                    continue
                if not _phones_match(sender, contact.get("phone", "")):
                    continue
                # Operator identity bypasses the inbound whitelist gate (mirrors
                # tier2): your own line always reaches you, even with
                # inbound_allowed=False. M5 exposes both flags as independent
                # toggles, so this must NOT lock an operator out of their own line.
                if contact.get("is_operator_self"):
                    log.info(
                        "SMS %s resolved via tier1 (owned line, operator identity) -> operator=%s class=self",
                        sender, owner,
                    )
                    return Resolution(owner, contact, "self")
                if not contact.get("inbound_allowed"):
                    log.info(
                        "SMS %s on %s's owned line: contact found but inbound_allowed=False -> DROP",
                        sender, owner,
                    )
                    return Resolution(None, None, None)
                # Non-self at this point -> always "peer".
                log.info(
                    "SMS %s resolved via tier1 (owned line) -> operator=%s class=peer",
                    sender, owner,
                )
                return Resolution(owner, contact, "peer")
            log.info(
                "SMS %s on %s's owned line: not inbound_allowed in owner's book -> DROP",
                sender, owner,
            )
            return Resolution(None, None, None)

        # ---- Gather every (non-seed) contact in every book that matches sender.
        #      Preserve USERS_LIST encounter order for the no-timestamp tiebreak.
        ordered_ops = list(USERS_LIST)
        # Defensive: include any book not in USERS_LIST (deterministic order).
        for op in data:
            if op not in ordered_ops:
                ordered_ops.append(op)

        self_matches = []   # (operator, contact) where is_operator_self
        peer_matches = []   # (operator, contact) where inbound_allowed, not self
        for operator in ordered_ops:
            for _cid, contact in (data.get(operator, {}) or {}).items():
                if self._is_system_seed(contact):
                    continue
                if not _phones_match(sender, contact.get("phone", "")):
                    continue
                if contact.get("is_operator_self"):
                    self_matches.append((operator, contact))
                elif contact.get("inbound_allowed"):
                    peer_matches.append((operator, contact))

        # ---- Tier 2: Operator-identity (unowned line).
        if self_matches:
            if len(self_matches) > 1:
                # Defensive — M1 warns at write time; surface it again here.
                candidates = ", ".join(op for op, _ in self_matches)
                log.warning(
                    "SMS %s: is_operator_self collision across operators [%s] "
                    "-> routing to most-recently-updated",
                    sender, candidates,
                )
            operator, contact = self._pick_most_recent(self_matches)
            log.info(
                "SMS %s resolved via tier2 (operator-identity) -> operator=%s class=self",
                sender, operator,
            )
            return Resolution(operator, contact, "self")

        # ---- Tier 3 / Tier 4: inbound_allowed whitelist match(es).
        if peer_matches:
            if len(peer_matches) > 1:
                candidates = ", ".join(op for op, _ in peer_matches)
                log.warning(
                    "SMS %s: inbound_allowed multi-match collision across operators "
                    "[%s] -> routing to most-recently-updated",
                    sender, candidates,
                )
                operator, contact = self._pick_most_recent(peer_matches)
                log.info(
                    "SMS %s resolved via tier4 (multi-match) -> operator=%s class=peer",
                    sender, operator,
                )
            else:
                operator, contact = peer_matches[0]
                log.info(
                    "SMS %s resolved via tier3 (single whitelist) -> operator=%s class=peer",
                    sender, operator,
                )
            return Resolution(operator, contact, "peer")

        # ---- Tier 5: No match.
        return Resolution(None, None, None)

    @staticmethod
    def _pick_most_recent(matches):
        """Most-recently-updated tiebreak with a stable USERS_LIST fallback.

        ``matches`` is a list of (operator, contact) already in USERS_LIST
        encounter order. Sort DESCENDING by the contact's updated_at/created_at
        (ISO-8601, text-comparable). Python's sort is stable, so contacts with
        equal (or absent) timestamps keep their input order — i.e. the first
        operator listed in USERS_LIST wins when nothing else distinguishes them.
        Always returns one (operator, contact).
        """
        # Stable sort: highest recency key first; ties keep input (USERS_LIST) order.
        ranked = sorted(matches, key=lambda m: _recency_key(m[1]), reverse=True)
        return ranked[0]

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

        # 2. Resolve the operator via the deterministic 5-tier precedence rule.
        #    Owned lines are strict; unowned lines walk identity -> single
        #    whitelist -> multi-match (most-recently-updated + WARNING). No match
        #    -> DROP before any chat/task/send/store. The classification ('self'
        #    /'peer') is COMPUTED here for M3 (peer notification + payload tag);
        #    M2 only logs it and does not yet act on it.
        operator, contact, classification = self.resolve_inbound(sender, owner)
        if not operator:
            log.info("Ignoring SMS from unknown number: %s", sender)
            return

        contact_name = contact.get("name", sender) if contact else sender
        # M3: consume the classification. A 'peer' inbound is a whitelisted
        # NON-operator (e.g. Anna) texting the operator's AI — the reply is
        # framed as on the operator's behalf AND the operator is notified so they
        # can take over via send_manual. A 'self' inbound is the operator
        # themselves — no peer framing, no notification.
        is_peer = classification == "peer"
        log.info(
            "SMS routed to operator=%s contact=%s class=%s",
            operator, contact_name, classification,
        )

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

        # 3b. M3 — notify the operator when a whitelisted PEER (not themselves)
        #     texts in, so they know e.g. Anna messaged and can take over via the
        #     existing send_manual path. notify() never raises and always records
        #     a searchable snapshot; we await it directly (handle_incoming is
        #     async). No notification on a 'self' inbound (the operator's own line).
        if is_peer:
            try:
                preview = body[:PEER_NOTIFY_PREVIEW_CHARS]
                if len(body) > PEER_NOTIFY_PREVIEW_CHARS:
                    preview += "…"
                await notify(
                    operator,
                    f"SMS from {contact_name}",
                    preview,
                    category="sms",
                    dedup_key=f"sms-inbound:{operator}:{sender}:{timestamp}",
                )
            except Exception:
                # notify() is engineered never to raise; guard anyway so an alert
                # failure never blocks the auto-reply.
                log.exception("Peer-inbound notification failed for SMS from %s", sender)

        # 4. Process through main chat pipeline (replaces process_incoming_sms)
        reply = ""
        try:
            reply = await self._route_through_chat(
                sender, body, operator, contact_name,
                is_peer=is_peer, line_number=line_number,
            )
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

    def _build_thread_messages(
        self,
        operator: str,
        sender: str,
        current_body: str,
        line_number: str = "",
    ) -> list:
        """Assemble the recent SMS thread as chat turns (M4).

        Fetches the most recent ``SMS_THREAD_WINDOW`` stored rows for this
        ``(operator, sender, line_number)`` thread (oldest-first), then:
          - DROPS the just-stored current inbound (``handle_incoming`` persisted
            it before this runs, so it is the trailing inbound row matching
            ``current_body``) — exactly once — so it isn't duplicated; it is
            re-appended as the live final user turn.
          - MERGES consecutive same-direction rows into ONE turn (a single AI
            reply is stored as one outbound row per 160-char segment).
          - MAPS inbound -> ``user``, outbound -> ``assistant``.
          - DROPS a leading ``assistant`` turn so the first non-system message is
            a ``user`` (Anthropic requires the first turn be ``user``).

        Returns the history turns (WITHOUT the current text). On any failure or an
        empty thread, returns ``[]`` so the caller falls back to today's single
        current user turn.
        """
        try:
            rows = self.store.get_conversation(
                operator, sender, limit=SMS_THREAD_WINDOW,
                line_number=line_number or "", recent=True,
            )
        except Exception:
            log.exception("SMS thread fetch failed for %s; falling back to single turn", sender)
            return []

        # Drop the trailing current-inbound row exactly once (the one
        # handle_incoming just stored: last inbound row whose body == current).
        for i in range(len(rows) - 1, -1, -1):
            r = rows[i]
            if r.get("direction") == "inbound" and r.get("body") == current_body:
                del rows[i]
                break

        # Merge consecutive same-direction rows into one turn, mapping direction
        # -> role. inbound -> user, outbound -> assistant.
        turns: list = []
        for r in rows:
            role = "assistant" if r.get("direction") == "outbound" else "user"
            body = r.get("body") or ""
            if turns and turns[-1]["role"] == role:
                turns[-1]["content"] += body
            else:
                turns.append({"role": role, "content": body})

        # First non-system message must be a user turn (Anthropic). If the merged
        # history would lead with an assistant turn, drop it.
        if turns and turns[0]["role"] == "assistant":
            turns = turns[1:]

        return turns

    async def _route_through_chat(
        self,
        sender: str,
        body: str,
        operator: str,
        contact_name: str,
        is_peer: bool = False,
        line_number: str = "",
    ) -> str:
        """Route SMS through the main /chat pipeline for full context retrieval.

        ``is_peer`` (M3): the inbound is from a whitelisted NON-operator (e.g.
        Anna). It is surfaced to the model as ``sms_peer=True`` in the payload so
        tasks.py can frame the reply as on the operator's behalf; the operator's
        own persona/memory still loads (their voice). Omitted/False for a 'self'
        inbound.

        ``line_number`` (M4): the resolved receiving line, used to scope the
        recent-thread fetch so a peer texting two different owned lines doesn't
        bleed across threads.
        """
        import time
        import aiohttp
        from Orchestrator.state import get_operator_preference

        sms_provider = get_operator_preference(operator, "sms_provider", "anthropic")
        sms_model = get_operator_preference(operator, "sms_model", "claude-sonnet-4-5")

        sms_context = f"[SMS from {contact_name} ({sender})]: {body}"

        # M4: prepend the recent SMS thread (oldest-first) as conversation turns,
        # then append the live current user turn. A first-ever inbound (no prior
        # thread) yields exactly the single current user turn (today's behavior).
        history_turns = self._build_thread_messages(
            operator, sender, body, line_number=line_number or "",
        )
        messages = [*history_turns, {"role": "user", "content": sms_context}]

        payload = {
            "messages": messages,
            "operator": operator,
            "provider": sms_provider,
            "model": sms_model,
            "sms_mode": True,
            "sms_sender": sender,
            "sms_contact_name": contact_name,
            "sms_peer": is_peer,
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
        #   line_number tracks the originating line we end up using, so the
        #   outbound is stored on the correct thread.
        client = None
        line_number = ""
        if gateway_id:
            client = self.manager.get(gateway_id)
        elif from_number:
            res = self.manager.resolve_for_number(from_number)
            if res:
                client, span = res
                line_number = from_number
        else:
            client = self.manager.default()

        client = client or self.manager.default()
        if client is None:
            return {"success": False, "error": "No gateway available"}

        # The chosen gateway's id (the client carries it; see manager._make_client).
        resolved_gateway_id = getattr(client, "gateway_id", "") or (gateway_id or "")
        # Fall back to whatever from-number we were given if we couldn't resolve a line.
        if not line_number:
            line_number = from_number or ""

        # Look up contact name
        _, contact = self._find_operator_by_phone(to)
        contact_name = contact.get("name", to) if contact else to

        # Send via the chosen gateway client.
        result = await client.send_sms(to, message, span=int(span) if span else 2)
        status = "delivered" if result.get("success") else "failed"

        # Store outbound message (tagged with the resolved line + chosen gateway)
        now = datetime.now(timezone.utc).isoformat()
        msg_id = self.store.store_message(
            operator=operator,
            direction="outbound",
            phone_number=to,
            contact_name=contact_name,
            body=message,
            timestamp=now,
            status=status,
            line_number=line_number,
            gateway_id=resolved_gateway_id,
        )

        return {
            "success": result.get("success", False),
            "error": result.get("error"),
            "message_id": msg_id,
        }
