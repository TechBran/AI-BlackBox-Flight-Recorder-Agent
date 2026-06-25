# Inbound SMS Routing + Thread-Memory Hardening — Design

**Date:** 2026-06-25
**Status:** Design locked (decisions confirmed by Brandon). Feeds `writing-plans` → `subagent-driven-development`.
**Investigations:** two multi-agent workflows (routing/whitelist/operator-resolution + conversation-history). Findings reconciled below; all citations verified against real code + live data.

## Problem

The BlackBox sends/receives SMS via a TG200 gateway. Outbound delivery is fixed (commits `a31f5ed`, `8ba5f9d`). Inbound has three coupled weaknesses:

1. **No operator-identity binding.** An inbound number is matched to an operator by sniffing free-form `relationship`/`tags` strings ("self"/"owner") across all books, first-match by `USERS_LIST` order. There is no first-class "this phone is operator X" fact. (`router.py:50-100`)
2. **Whitelist == contact book.** Being in *any* operator's book is what authorizes texting in; there is no separate notion. Random numbers are dropped (`Ignoring SMS from unknown number`, `router.py:168`). (`router.py:164-170`)
3. **Amnesiac replies.** Inbound POSTs to `/chat`, so the model already gets persona + snapshot retrieval + tools — but the payload carries only the single current text; the per-thread SMS history (which we already persist) is never injected.

**Live evidence of the collision:** `+14108166914` is in three books — `Brandon` (owner), `Anna` (husband), `system` (owner). Today Brandon wins by config order, not intent. **The "whitelisted non-operator texts in" path has never run in production** (the `matched operator … (self/owner)` log fires from the *outbound* sender-lookup; `_find_operator_by_phone` has two callers — `router.py:167` inbound and `router.py:387` outbound). **Latent bug:** Anna's legitimate self-marker contact has `created_by="system"`, which `_is_system_seed` (`router.py:40-48`) currently excludes from identity matching.

## Decisions (confirmed)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Whitelist structure | **One book + per-contact `inbound_allowed` flag** (not a separate file). Book stays the outbound directory; the flag gates inbound. |
| 2 | Operator identity | **Per-contact `is_operator_self` flag** (not a global map). Uniqueness enforced in *logic* (precedence rule + write-time warn), not storage. |
| 3 | Whitelisted non-operator (Anna) | **Auto-reply in operator's voice + notify operator**, who can take over via manual send. |
| 4 | Thread memory | **Auto-inject the recent thread (~20 msgs)** from the local store; no model-facing tool. |

Defaults taken on the mechanical sub-decisions (no further input needed): per-`(operator, phone, line_number)` thread scoping; trim the just-stored current message caller-side; `ORDER BY timestamp, id` ordering tiebreak; `_is_system_seed` gates only the literal seed phone `+17164512527`; multi-match tie-break = most-recently-updated contact + a logged `WARNING`; existing contacts migrate to `inbound_allowed=true`.

## Source of truth (history)

The **local SQLite store** `Manifest/sms_messages.db` (`Orchestrator/sms/message_store.py`) is authoritative. It persists **both** directions (inbound `router.py:183`, every outbound segment `router.py:212`), is keyed `(operator, normalized-phone[, line])`, and exposes `get_conversation(operator, phone, limit, offset, line_number)` returning the thread oldest-first. The TG200 exposes **no** history-read over AMI (`gsm show sms` tested, unsupported — `docs/plans/2026-03-25-sms-inbound.md:87-90`); a SIM could only ever hold *received* messages anyway. No gateway round-trip; no new tool.

## Architecture

### Data model (one structure, two new flags)
`Orchestrator/contacts.py` contact dict + `ContactUpsertRequest` (`contacts_routes.py:9-16`) gain:
- `inbound_allowed: bool` — may this number text in? (migration default `true` for existing contacts)
- `is_operator_self: bool` — is this number the operator's own line/identity?

Both must also flow through the **second** contacts endpoint `/api/cron/contacts` (`cron_routes.py:62`) so neither is silently dropped.

**Write-time identity guard** (`upsert_contact`): when `is_operator_self=true`, scan other operators' books for the same normalized number already flagged self; if found, surface a warning (route returns it; UI shows it). Soft enforcement — chosen storage has no global unique key, so the rule + warning carry the guarantee.

### Router (the single chokepoint, `router.py:146-170`)
Replace the Pass1/Pass2 string-sniffing with an explicit, **deterministic, fully-logged** resolution order:

1. **Line ownership.** If `_resolve_line` returns an `owner`, the text was sent to that operator's dedicated line → that operator, *provided the sender is `inbound_allowed` in that owner's book*.
2. **Operator-identity.** Else, if the sender matches a contact with `is_operator_self=true` → that operator (their own line). Multiple self-flags (shouldn't happen) → most-recently-updated + `WARNING`.
3. **Single inbound_allowed match.** Sender is an `inbound_allowed` contact in exactly one book → that operator (the "Anna texts in" peer case).
4. **Multi-match collision.** In several books → most-recently-updated `inbound_allowed` contact wins, **log a `WARNING` naming all candidates** (today it is silent).
5. **No match → drop** (`Ignoring SMS from unknown number`, unchanged).

Fix `_is_system_seed` to gate only the literal seed phone, not all `created_by="system"`.

**Symmetric Anna/Brandon case, resolved:** an inbound from Brandon's phone matches *Brandon's* `is_operator_self` (tier 2) → always Brandon's context, regardless of being in Anna's book. The directory↔identity separation makes cross-listing irrelevant to inbound identity.

### Peer path (decision 3)
When resolution lands a sender that is `inbound_allowed` but **not** `is_operator_self`, tag the stored inbound + chat payload `sms_peer=true` (sender name available). The owning operator's persona/memory loads (so the reply is in their voice), AND a notification fires via the existing notification subsystem so the operator is alerted and can reply via `send_manual` (`router.py:334`). Auto-reply is preserved; the human-takeover path is added.

### Thread injection (decision 4)
In `_route_through_chat` (`router.py:233`), before building the payload:
```
history = self.store.get_conversation(operator, sender, limit=~20, line_number=line_number)
# drop the trailing row that equals the just-stored current inbound (router.py:183)
# map inbound→{role:user}, outbound→{role:assistant}
payload["messages"] = [*history_turns, {role:user, content: current_sms_context}]
```
Composes automatically — `tasks.py:1505 msg_list.extend(inp.messages)` appends the whole thread; persona + snapshot block + tools layer on top as today. Harden `get_conversation` ordering to `ORDER BY timestamp, id` (`message_store.py:162`). Cap by message count (newest-kept) to protect the per-turn prefill budget (Anthropic 75K / Google 200K) and the 45s SMS reply deadline (`router.py:269`).

### Three surfaces (per the frontend-three-surfaces rule)
- **Portal:** `Portal/modules/contacts-manager.js` edit form (~`:204-229`) — two toggles ("This number IS operator X" → `is_operator_self`; "Allow inbound texts" → `inbound_allowed`); surface the write-time identity warning.
- **Android:** `EditContactDialog` in `ContactsScreen.kt` (~`:298-420`) — two `Switch`es threaded through `ContactsViewModel.saveContact()` (~`:91-137`) and the `Contact` data class (~`:17-26`).
- **WebView wrappers** inherit the Portal JS (verify).

## Testing
- Backend: pytest over the new resolution order (each tier, the collision tie-break, the system-seed fix), the two flags through both contacts endpoints, the write-time identity guard, the thread-injection assembly + current-message trim + ordering tiebreak. Reuse the `FakeClient`/`test_router_security.py` harness.
- Live: the "Anna texts in" path has never run — exercise it on hardware (a non-operator whitelisted number texting in → operator notified + AI reply + thread continuity on a second text). Outbound test via the MCP `send_sms` path.

## Out of scope (deferred)
Global unique identity map; separate whitelist file; a model-facing cross-thread `get_sms_thread` tool; retention/rotation on the SMS store; full-E.164 rekey to remove the last-10-digit collision risk. All documented as future work.
