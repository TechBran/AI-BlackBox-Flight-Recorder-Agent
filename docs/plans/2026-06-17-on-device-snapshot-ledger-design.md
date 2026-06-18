# On-device Gemma — Snapshot Ledger & Context Pipeline (design)

> Validated design from the 2026-06-17 brainstorm with Brandon. Follow-on to the
> shipped on-device Gemma `local` provider ([[project-on-device-gemma]], main @ 16050c9).
> Branch: `feat/on-device-snapshot-ledger` (off `main`).

## Goal

Make the on-device Gemma model a **first-class consumer of the BlackBox's
server-side context-assembly + minting pipeline** — the same flow every other
BlackBox model already uses. This is what gives the BlackBox its **"teleporting"**
capability: talk about any topic at any time and the relevant memory is already in
hand, and every turn is remembered forever.

**This pipeline is THE keystone** (Brandon's word). The on-device loop, intents,
vision, and tools we shipped are the *hands*; this is the *memory + nervous system*.
Everything else already has this flow — the work is wiring the phone model into it.

## Why (the teleport)

Each turn, the BlackBox semantically searches the operator's ledger on the user's
prompt and pre-assembles the relevant memory. The model never has to *know* it
should recall — the recall is delivered. Context **provenance** on each snapshot
makes the retrieved memory a navigable graph (it records the surrounding artifacts:
tools fired, media created, related snap_ids by retrieval method), so even a lean
package points to the rest of a topic's neighborhood.

## Architecture: the server-bracketed turn

Identical *flow* to the cloud chat routes (`context_builder.build_fossil_context`),
differing only in **budget** (the phone's ~16K window must also hold an agent loop)
and in **where inference runs** (on the phone). Assembly happens **once per TURN,
not once per agent STEP**.

```
PHONE                                    BLACKBOX (server-side, per-operator)
  user prompt ──────────────────────────▶ POST /local/turn/prepare
                                             • resolve operator (selected operator
                                               drives the whole package)
                                             • semantic_retrieve(prompt, op, k=2-3)
                                             • most-recent checkpoint (op)
                                             • ToolVault semantic search → top-K
                                               tool DESCRIPTIONS
                                             • persona / behavioral_core("chat")
                                             • token-budget the package (reserve
                                               loop headroom)
              ◀──── assembled package ─────  { system_prompt, callable_tools[],
                                               checkpoint, semantic_snaps[+prov],
                                               turn_id, budget_meta }
  [run E4B agent loop LOCALLY on that ONE package — multiple steps]
     ├─ direct tool call (roll_dice, flashlight_on, …) ─▶ /local/tools/execute
     ├─ search_snapshots (deeper recall) ───────────────▶ /local/tools/execute
     ├─ find_blackbox_tool (long-tail fallback) ────────▶ /local/tools/search
     │   (trim oversized tool results phone-side before re-feeding)
     └─ soft-stop if approaching token ceiling
  final transcript ───────────────────────▶ POST /local/turn/complete
                                             • auto-mint snapshot (raw turn +
                                               provenance), embed inline
                                             • existing checkpoint cadence may fire
              ◀──── snap_id ────────────────
```

## Decisions (all validated)

### D1 — Per-turn context package (LEAN)
Pre-assembled once per turn, server-side, per selected operator:
- **most-recent checkpoint** (1) — compressed session summary, hot-attention top slot
- **top 2-3 semantically-relevant snapshots** — the teleport (server-side semantic
  search on the prompt; NOT left to the model to fetch — relying on the 4B to
  *decide* to search is the same unreliability as "doesn't know its tools")
- **top-K relevant tool descriptions** injected as **directly-callable** native tools
- **persona / behavioral_core("chat")**

Rejected: rich cloud-style package (8 semantic + keyword + recent) — starves the
16K loop. Rejected: pure on-demand recall — unreliable teleport.

### D2 — Tools: direct top-K + fallback
Per turn, ToolVault semantic search → inject top ~3-5 tool **descriptions** as
directly-callable native tools (the phone registers them from the package), beside
the always-resident phone/intent actuators. The model calls `roll_dice` directly —
**no `find_blackbox_tool`/`run_blackbox_tool` indirection for the common case**
(that two-hop dance is a root cause of the 4B fumbling tools). `find_blackbox_tool`
stays as a **long-tail safety net** only; provenance gives a second discovery path.

### D3 — Context-limit handling (reserve headroom + fail soft)
- **Server budgets the package** to a `local`-provider token cap that *reserves*
  loop headroom (target: package ≤ ~4K, leaving ~12K of the 16K for the loop).
- **Phone trims oversized tool results** before re-feeding them into the loop.
- **Hard per-turn budget with graceful soft-stop**: track tokens during the loop;
  near the ceiling, stop cleanly with what it has — never the "[on-device error]"
  overflow (the 4096→16384 history).
- Keep the shipped `MAX_NATIVE_TOOL_CALLS=24` cap.

### D4 — Minting (BlackBox owns composition)
At turn completion the phone POSTs `{prompt, final_response, tool/artifact
transcript, operator}`. The BlackBox **auto-mints the raw turn + provenance** as a
snapshot and **embeds inline** (exactly like `/chat/save`). The 4B never authors
snapshot content — so the device-test blocker (`mint_snapshot` needs
`content`+`operator` the 4B can't write) dissolves. Provenance (tools fired /
artifacts created) is recorded so future teleports land on live nodes.

### D5 — Checkpoints (reuse existing cadence)
On-device turns become snapshots in the same ledger, so the **existing checkpoint
trigger fires automatically** (the `/chat/save` `checkpoint_triggered` path),
summarized **server-side with a capable model** — the 4B never writes a checkpoint.
The pre-assembled "most-recent checkpoint" is just the latest in the operator's ledger.

### D6 — Offline: degraded on-phone mode + queued mint
If the mesh is unreachable at assembly time, fall back to a **pure local turn**
(persona from the existing cache; no fresh memory/tools) and **queue the snapshot to
mint when back online** (reuse the shipped `LocalSnapshotQueue`). Basic chat
survives a dead zone; it just can't teleport until the mesh returns. Never hard-block.

### D7 — Per-operator, server-side
The **selected operator** drives the entire package (which ledger is searched, which
checkpoint/snapshots, attestation/binding) — all server-side, identical to the cloud
routes. No per-operator logic on the phone beyond sending the selected operator.

## New / changed components (sketch — refined in the plan)

**Backend (the keystone — invest here):**
- `POST /local/turn/prepare` — assemble the per-turn package (reuse
  `build_fossil_context` with a new `local` provider cap + k=2-3 + top-K tool
  descriptions + persona). Returns system_prompt + callable_tools + provenance.
- `POST /local/turn/complete` — auto-mint the raw turn + provenance, embed inline,
  let the checkpoint cadence fire. (May reuse `/chat/save` internals.)
- `context_builder`: add a `local` entry to `PROVIDER_CAPS` + a lean retrieval
  profile (k=2-3, checkpoint=1) and the token-budget/headroom-reserve logic.
- Tool-description injection: top-K ToolVault semantic search → callable-tool specs.

**Phone:**
- Turn driver: call `prepare` → run the native loop on the returned package →
  `complete`. Register the returned top-K tools as directly-callable native tools.
- Phone-side tool-result trimming + token tracking + soft-stop.
- Offline path: detect unreachable → degraded local turn + `LocalSnapshotQueue`.

## Out of scope
- Changing the cloud chat pipeline (we *reuse* it).
- On-device checkpoint *summarization* (server-side, existing).
- Multimodal/vision changes (shipped).

## Open questions for the plan
- Exact token budget numbers for the `local` cap (measure on the Fold 6).
- `prepare`/`complete` payload schemas (DTOs) + streaming of the assembled response.
- Whether `prepare` returns rendered system-prompt text vs. structured parts the
  phone templates (engine re-template / Gemma turn-tokens consideration).
- Cache key for the offline "stale package" (if we later want stale-cache, not v1).
