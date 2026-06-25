# Inbound SMS Routing + Thread-Memory Hardening — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: execute via `superpowers:subagent-driven-development` (one implementer per task, then spec + quality review). Build directly on `main` (staging-as-prod). Every committed step must leave the app launchable. Tests: `Orchestrator/venv/bin/pytest`. Stage explicit paths only — never `git add -A`. Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` + `Claude-Session: https://claude.ai/code/session_012bwgtUdo4zCsHWF5EtAB4C`.

**Goal:** Make inbound SMS deterministic and context-aware — explicit per-contact `inbound_allowed` + `is_operator_self` flags, a 5-tier precedence rule that resolves the multi-book collision, an operator-notified peer path, and auto-injected SMS thread history — across backend + Portal + Android.

**Architecture:** See `docs/plans/2026-06-25-inbound-sms-routing-and-threading-design.md`. One contact book with two new booleans; the router chokepoint (`Orchestrator/sms/router.py:146-170`) becomes an explicit ordered resolver; thread history auto-injects from the local SQLite store (`Manifest/sms_messages.db`) in `_route_through_chat`.

**Tech stack:** FastAPI + APScheduler backend (Python, `Orchestrator/`), SQLite SMS store, Portal vanilla-JS modules, Android Jetpack-Compose Kotlin MVP. Asterisk AMI ↔ TG200 gateway.

**Known-ignore pre-existing failures:** `test_embeddings_providers.py::test_ollama_keep_alive_passthrough`; `test_retrieval_core::test_provenance_log_emitted_only_when_flag_enabled` (test-order pollution).

---

## M1 — Contact data model: two flags + write-time identity guard

**Files:**
- Modify: `Orchestrator/contacts.py` (contact dict shape; `upsert_contact` ~`:117-174`; new `get_operator_self_for_phone`, `is_inbound_allowed` helpers)
- Modify: `Orchestrator/routes/contacts_routes.py:9-16` (`ContactUpsertRequest`)
- Modify: `Orchestrator/routes/cron_routes.py:62` (the `/api/cron/contacts` projection must carry both flags)
- Test: `Orchestrator/sms/tests/test_contact_flags.py` (new)

**Task 1.1 — Flags persist through upsert.** TDD: failing test that `upsert_contact(..., inbound_allowed=True, is_operator_self=True)` stores both and `load_contacts` round-trips them; add the two params (default `False`) + storage; `ContactUpsertRequest` gains both fields; assert a POST `/contacts` persists them.

**Task 1.2 — Migration default.** Test: a contact loaded from a legacy record (no `inbound_allowed` key) reads back `inbound_allowed=True` (preserve current "in book ⇒ can text in" behavior); `is_operator_self` defaults `False`. Implement a read-time default in the loader (do not rewrite the file destructively).

**Task 1.3 — Write-time identity guard.** Test: setting `is_operator_self=True` for a number already self-flagged by *another* operator returns a `warning` in the upsert result (and the route surfaces it) but still saves; setting it when unique returns no warning. Implement the cross-book scan (last-10-digit normalize) in `upsert_contact`.

**Task 1.4 — Second endpoint parity.** Test: `GET /api/cron/contacts` includes both flags for each contact. Thread them through `cron_routes.py:62`’s projection.

**Commit:** `feat(sms): inbound_allowed + is_operator_self contact flags + write-time identity guard`

---

## M2 — Router resolution: deterministic 5-tier precedence

**Files:**
- Modify: `Orchestrator/sms/router.py` (`handle_incoming` :146-170; replace `_find_operator_by_phone` :50-100 and tighten `_find_in_operator_book` :118-144; fix `_is_system_seed` :40-48)
- Test: `Orchestrator/sms/tests/test_router_precedence.py` (new); extend `test_router_security.py`

**Task 2.1 — Tiered resolver (red→green per tier).** TDD tests, one per tier, using the `FakeClient` harness:
  - Line-owner: owned line + sender `inbound_allowed` in owner book → owner; owned line + sender NOT `inbound_allowed` → drop.
  - Identity: unowned line, sender `is_operator_self` in some book → that operator (even if also a plain contact elsewhere).
  - Single whitelist: sender `inbound_allowed` in exactly one book → that operator.
  - Multi-match: sender `inbound_allowed` in two books → most-recently-updated wins + a `WARNING` log naming both candidates.
  - No match → drop (`Ignoring SMS from unknown number`), nothing stored.
  Implement the ordered resolver returning `(operator, contact, classification)` where classification ∈ {`self`,`peer`}.

**Task 2.2 — System-seed fix.** Test: a real operator contact with `created_by="system"` flagged `is_operator_self` now resolves (Anna's latent bug); the literal seed phone `+17164512527` still cannot whitelist itself. Narrow `_is_system_seed` to the seed phone only.

**Task 2.3 — Symmetric Anna/Brandon case.** Test: with Brandon `is_operator_self` on his number and both cross-listed in each other's books, an inbound from Brandon's phone → Brandon context; from Anna's phone → Anna context. Regression lock on the live `+14108166914` 3-book shape.

**Commit:** `feat(sms): deterministic 5-tier inbound operator resolution + collision logging`

---

## M3 — Peer path: classify, load operator voice, notify, allow takeover

**Files:**
- Modify: `Orchestrator/sms/router.py` (`handle_incoming` store + `_route_through_chat` :233 — pass `sms_peer`; fire notification on peer inbound)
- Modify: `Orchestrator/tasks.py:1458-1476` (SMS prefix: frame a peer reply as "on the operator's behalf" when `sms_peer`)
- Reuse: notification subsystem (`Orchestrator/notifications/bus.py::notify`)
- Test: `Orchestrator/sms/tests/test_sms_peer_path.py` (new)

**Task 3.1 — Peer classification carries through.** Test: a `peer`-classified inbound sets `sms_peer=True` on the stored row + the `/chat` payload; a `self` inbound does not. Implement using M2's classification.

**Task 3.2 — Operator notified on peer inbound.** Test (mock `notify`): a peer inbound calls `notify(operator, ...)` with sender name + message preview; a self inbound does not notify. Wire the hook in `handle_incoming` after resolution.

**Task 3.3 — Peer reply framing.** Test: when `sms_peer`, the SMS system prefix instructs the model it is replying to `<name>` on the operator's behalf (operator persona still loads). Extend `tasks.py:1463-1476`.

**Commit:** `feat(sms): whitelisted-peer path — operator voice + notification + takeover`

---

## M4 — Thread memory: auto-inject recent history

**Files:**
- Modify: `Orchestrator/sms/message_store.py:142-166` (`get_conversation` ordering tiebreak; optional `exclude_latest`)
- Modify: `Orchestrator/sms/router.py:233-252` (`_route_through_chat` — fetch, trim, map, prepend)
- Test: `Orchestrator/sms/tests/test_sms_thread_injection.py` (new); extend `test_message_store_lines.py`

**Task 4.1 — Ordering tiebreak.** TDD: a same-second inbound/outbound pair sorts inbound-before-outbound deterministically; change `ORDER BY timestamp ASC` → `ORDER BY timestamp, id`.

**Task 4.2 — Thread assembly.** Test: given a stored thread, `_route_through_chat` builds `payload["messages"]` = `[...mapped turns..., current]` with inbound→`user`/outbound→`assistant`, the just-stored current inbound trimmed exactly once (no duplicate), capped at the configured window (~20, newest-kept), scoped by `(operator, sender, line_number)`. Implement the fetch+trim+map+prepend.

**Task 4.3 — End-to-end shape.** Test (mock `/chat` POST): on a second inbound from the same number, the posted payload contains the prior exchange ahead of the new text; on a first-ever inbound it contains only the current text.

**Commit:** `feat(sms): auto-inject recent SMS thread into inbound replies`

---

## M5 — Three surfaces: Portal + Android contact editors

**Files:**
- Modify: `Portal/modules/contacts-manager.js` (edit form ~`:204-229`: two toggles + warning surface)
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/.../ui/contacts/ContactsScreen.kt` (`EditContactDialog` ~`:298-420`), `ContactsViewModel.kt` (~`:91-137`), `Contact` data class (~`:17-26`)
- Verify: WebView wrappers inherit Portal JS

**Task 5.1 — Portal toggles.** Add "This number IS operator X" (`is_operator_self`) and "Allow inbound texts" (`inbound_allowed`) controls; POST both; render the write-time identity warning if returned. Chrome-verify the add/edit flow.

**Task 5.2 — Android switches.** Two `Switch`es in `EditContactDialog`; thread through `Contact` data class + `ContactsViewModel.saveContact()` into the POST body; surface the warning. `compileDebugKotlin` green.

**Commit:** `feat(sms): contact-editor toggles for inbound_allowed + is_operator_self (Portal + Android)`

---

## M6 — Live verification + record

**Task 6.1 — Migration check.** On the real `Contacts/contacts.json`, confirm existing contacts read `inbound_allowed=True` and routing is unchanged for current senders.

**Task 6.2 — Live SMS smoke (hardware).** Exercise the never-run peer path: a whitelisted non-operator number texts in → operator notified + AI reply in operator voice; a second text confirms thread continuity; an unknown number is still dropped. Outbound via the MCP `send_sms` path. Restart (`sudo systemctl restart blackbox.service`, pre-authorized) to load all changes first.

**Task 6.3 — Whole-branch review + snapshot.** Final `superpowers:code-reviewer` pass over the diff; update `project_sms_pipeline_hardening` memory; mint a dev snapshot.

**Commit (if needed):** review fixes only.

---

## Out of scope (deferred, documented)
Global unique identity map; separate whitelist file; model-facing cross-thread `get_sms_thread` tool; SMS-store retention/rotation; full-E.164 rekey to remove the last-10-digit collision risk.
