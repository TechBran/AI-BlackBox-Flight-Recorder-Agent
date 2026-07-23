# Flight Recorder Operator — Design

**Date:** 2026-07-23
**Status:** DESIGN (awaiting approval)
**Author:** Design lead session (Brandon's vision, survey-grounded)
**Related:** `docs/plans/2026-06-23-cron-scheduler-production-uplift.md`, per-operator persona project, retrieval-upgrade project

---

## 1. Vision (quote-level requirements)

Brandon's requirements, verbatim in intent:

1. A **permanent operator shipping on ALL boxes** — present on fresh boxes and retro-seeded onto existing boxes at upgrade.
2. **Undeletable.**
3. **"The Oracle of the whole black box"** — it oversees everything.
4. It **reads ALL operators' snapshot chains** — per-operator gating is transcended, but **READ-ONLY**: it must never write into another operator's chain.
5. Its **checkpoint system mirrors the existing per-operator 25-most-recent-snapshots checkpoint**, but **SYNTHESIZES ACROSS ALL OPERATORS**.
6. **Oracle-style default system prompt**: oversee all operations, maintain the memory ledger, verify jobs/tasks completed, and **REPORT failures/incompletions**.
7. **Cross-operator cron powers**: create/edit any operator's jobs.
8. **Any contact book.**
9. The **go-to operator to "see everything about your black box system"** — on family/business boxes, everyone is overseen.

The product is literally named the *AI BlackBox Flight Recorder*. This operator is the product's namesake made concrete: the entity that watches the recorder itself.

## 2. What it DOES (beyond the quotes)

Grounded in what the survey confirmed exists, the Flight Recorder (FR) has four concrete jobs:

### 2.1 Flight Reports — the cross-operator checkpoint

A periodic synthesis snapshot, minted under the FR's own chain, `TYPE: flight_report`, that mirrors `create_checkpoint_async` (`Orchestrator/checkpoint.py:65-208`) but gathers the **25 most recent snapshots across ALL operators** (the existing `get_recent_fossils_for_operator(vol_txt, "system", ...)` all-read path, `Orchestrator/fossils.py:978-1005`) plus a live health digest, and synthesizes:

- **Activity**: what each operator worked on (per-operator rollup, snapshot IDs cited).
- **Completions**: jobs/tasks/generations that finished (from cron history + task registry).
- **Failures**: cron run failures, failed/stuck tasks, TTS-queue failures, embedding failures.
- **Anomalies**: ledger gaps, index/volume divergence, unusual silence ("Anna's chain has minted nothing in 9 days"), scheduler-vs-DB drift.

### 2.2 Failure Watchtower — live signal collection

A programmatic collector, `collect_oversight_signals()`, that pulls (all read-only, all already queryable):

| Signal | Source (exists today) |
|---|---|
| Cron run failures + last-run status per job | `cron_job_history` via `manager.get_job_history` (`scheduler/manager.py:339,973`), `/api/cron/jobs/{id}/history` |
| Scheduler ↔ DB divergence | `/api/cron/health` logic (`routes/cron_routes.py:84-124`) |
| Failed / stuck background tasks (all operators) | tasks.db via `GET /tasks/list` with no operator filter (`routes/task_routes.py:38-103`); "stuck" = `running` with stale `updated_at` past a per-type threshold |
| TTS queue failures | `tts_queue.get_status` (`tts_queue.py:330-398`) |
| Embedding health | `GET /embeddings/status`; index entries with missing/failed embedding payloads |
| Ledger integrity | index count vs volume tail; `max(byte_end)` vs volume size (the startup reconciliation logic, `startup.py:130-157`, reused as a read-only probe); SNAP-id sequence gaps |
| Per-operator checkpoint countdowns | `/dashboard` `operator_turns` (`admin_routes.py:354-381`) |

The digest feeds **both** the flight-report synthesis prompt **and** the FR's live chat context (a pinned functional section), so asking the FR "is everything OK?" is answered from *fresh* signals, not just the last report.

### 2.3 Ledger custodian

The FR is the named owner of ledger integrity: its reports must explicitly state embedding coverage ("2 of last 50 mints missing embeddings"), index health, and any mint failures. A tiny persisted state file `Manifest/flight_recorder_state.json` (last_report_id, last_report_at, cumulative failure counters, `adopted_preexisting` flag) gives reports deltas ("3 new cron failures since last report") without scraping journalctl.

### 2.4 Conversational oversight

A user selects **Flight Recorder** in the operator picker and simply talks to it. Its `/chat` turns run with:

- **Retrieval scope = ALL operators** (the read seam, §5),
- **Pinned recent flight reports** (mirroring the checkpoint pin, `fossils.py:1517-1560` / `context_builder.py:305`, filtered on `type == "flight_report"` + FR operator),
- **Live watchtower digest** injected as a functional context section,
- **The Oracle persona** (§7).

So "what did everyone do this week?", "did the nightly backup job actually run?", "why is search missing my snapshots?" are all answerable from the record. Its answers mint into *its own* chain like any operator — the oversight conversation itself becomes searchable history.

---

## 3. Identity — reserved name

**Name: `Flight Recorder`** (exact string, with the space; operators are free-text strings — `config.ini [users]`, `config.py:140`).

- Constant: `FLIGHT_RECORDER_OPERATOR = "Flight Recorder"` in `Orchestrator/config.py` (single definition; everything imports it).
- Reserved set: `RESERVED_OPERATORS = {FLIGHT_RECORDER_OPERATOR, "system"}` — used by the add/delete guards.
- **Explicitly NOT `system`.** `"system"` is a magic string in ≥4 modules with load-bearing side effects: all-read in `retrieval.py:509` and `fossils.py:1001`, and *deliberate notification/task-push suppression* in `scheduler/manager.py:36-39` (`_NO_NOTIFY_OPERATORS`) and `tasks.py:529`. An overseer that can never notify anyone is useless; overloading `system` would also change app/tool snapshot semantics everywhere at once. FR gets its own name and is **not** added to any suppression set.

### Collision handling (a box already has an operator named similarly)

Runs inside the seeding routine (§4), deterministic and idempotent:

1. **Exact match** `"Flight Recorder"` already in `USERS_LIST` → **adopt**: the existing operator becomes the FR (its existing snapshots are already stamped `OPERATOR: Flight Recorder` and are indistinguishable from FR history anyway). Log a loud `[FLIGHT-RECORDER] adopted pre-existing operator` warning and set `adopted_preexisting: true` in `flight_recorder_state.json`, surfaced by `/oversight/status`.
2. **Case/whitespace-variant match** (e.g. `flight recorder`) → seed the canonical name anyway; the variant remains an ordinary, unrelated operator; log a health warning naming the near-collision. (Gating is exact-string throughout — `OP_RX`, index `operator` keys — so variants never leak into FR scope.)
3. **Going forward**: `POST /operator/add` (`admin_routes.py:1201`) rejects names that case-insensitively equal a reserved name (unless it IS the exact reserved name already present), so nobody can pre-claim or shadow it on a box that hasn't seeded yet.

---

## 4. Seeding — fresh box AND upgrade migration

**One seeding site: startup.** `ensure_flight_recorder()` called from `startup_check_index()` (`Orchestrator/startup.py:111-166`), right after `load_operator_preferences()`:

```
def ensure_flight_recorder():
    if FLIGHT_RECORDER_OPERATOR not in _cfg.USERS_LIST:
        # exact in-place mutation pattern from add_operator (admin_routes.py:1226-1237):
        # append to CFG [users] list, write config.ini, re-read, USERS_LIST[:] = ...
    ensure_flight_report_cron_job()   # §6 — seed-if-absent, honor disabled flag
    load_flight_recorder_state()
```

Why this covers everything:

- **Fresh box**: `config.py:140` falls back to seed `Brandon` at import; the startup event then appends `Flight Recorder` and persists it to `config.ini` *before onboarding ever runs*. No change to the import-time fallback string is needed (startup always runs first in the service lifecycle).
- **Upgrade of existing boxes (incl. MS02)**: the update path is `git pull` + full service restart (`update/runner.py:259-281`, `_fire_detached_restart:398-404`) — startup seeding IS the migration. Idempotent against arbitrary pre-existing `[users]` lists (MS02: `Default, bbx1, Brandon`, default=`Default`); it appends, never reorders, never touches `default`.
- **No separate migration file** (`Orchestrator/migrations/` stays as-is).

Seeding must **never** change `USERS_DEFAULT` — the FR is present, not default.

---

## 5. Permanence enforcement — undeletable

Only two delete surfaces exist (survey-confirmed); both get guarded, plus a belt-and-braces reconciler:

1. **Server (the real gate)**: `DELETE /operator/{name}` (`admin_routes.py:1250-1304`) — add a reserved-name refusal alongside the existing last-operator 400:
   ```python
   if name in RESERVED_OPERATORS:
       raise HTTPException(400, detail=f"'{name}' is a permanent system operator and cannot be removed")
   ```
2. **Wizard**: `Portal/onboarding/steps/operator.js:193-211` — the FR row renders with a lock badge instead of the `[×]`; tooltip: "Permanent — the box's Flight Recorder." (Android and any other client call the same endpoint, so the server guard is the invariant; UI is courtesy.)
3. **Reconciler**: even if `config.ini` is hand-edited to remove it, the next boot's `ensure_flight_recorder()` restores it. Permanence = refusal + reconciliation.

**Edge**: with FR undeletable, a user can delete every human operator until FR is the last one; the existing last-operator guard then holds and `default` re-points to FR (`admin_routes.py:1284-1298`). Acceptable — the box stays functional and the wizard prompts for a new operator. No special-casing.

**Persona deletion**: `DELETE /operator/persona/Flight Recorder` stays allowed — it reverts to the code-shipped Oracle default (§7), not the generic default. Nothing to guard.

---

## 6. Cross-operator READ seam (read-only by construction)

### The gate change — one helper, three sites

Add to `Orchestrator/config.py`:

```python
def reads_all_operators(op: str | None) -> bool:
    """Operators whose retrieval scope is the WHOLE ledger (read-only widening)."""
    return not op or op == "system" or op == FLIGHT_RECORDER_OPERATOR
```

Patch the three existing all-read conditionals to call it (behavior for every existing value is byte-identical; only the FR name is added):

| Site | Today | Becomes |
|---|---|---|
| `retrieval.py:509` | `if operator and operator != "system":` | `if not reads_all_operators(operator):` |
| `fossils.py:1001` (`get_recent_fossils_for_operator`) | `if op == "system": matching = all` | `if reads_all_operators(op): ...` |
| `fossils.py:351-388` (`_decode_scored_snapshots`) | skips unless `operator in ("", "system")` | same helper |

`get_recent_checkpoints_for_operator` (`fossils.py:1517-1560`) is **NOT** widened — checkpoints stay per-operator pins; the FR pins its own `flight_report` type instead (a sibling `get_recent_flight_reports()` filtering `operator == FR AND type == "flight_report"`).

### Read-only is structural, not policied

Every mint path stamps the requesting operator into `OPERATOR:` and the index `operator` key (`fossils.py:801-838`); the FR chats/mints as itself, so its writes land only in its own chain. There is no code path by which widened *read* scope produces a write under another name. The design adds no write API. (Creating a cron job *for* another operator is a management action on `cron_jobs.db`, not a ledger write — the job's future snapshots are minted by that operator's own /chat runs, exactly as manual cron creation works today.)

### What stays closed (remote posture)

The MCP HTTP anti-leak gates — `_read_scope_operator` and `_deny_if_not_owned` (`MCP/blackbox_mcp_server.py:305-335`) — are **NOT pierced in v1**. Cross-operator reading is **local-only** (in-process retrieval, `/chat` as FR, cron). Today those gates are the only thing preventing any remote token from reading everyone; carving an FR exception is a real security-boundary change and is deferred to an explicit, optional milestone (M8) as a distinct privileged token class — never a name-string check (names are spoofable on stdio; only token binding is trustworthy there).

### `INCLUDE_OTHERS` untouched

The pre-existing default-off `[context] include_other_operators` knob (`config.py:151`) is not repurposed, not read by FR code, and its semantics don't change.

---

## 7. The cross-operator checkpoint builder — Flight Reports

New module: **`Orchestrator/oversight.py`** (keeps `checkpoint.py` untouched; the two share shapes, not code paths).

### `create_flight_report_async(manual: bool = False)`

Mirrors `create_checkpoint_async` step-for-step with these deltas:

1. **Gather**: `get_recent_fossils_for_operator(vol_txt, FLIGHT_RECORDER_OPERATOR, count=FR_REPORT_SOURCE_COUNT (default 25), cap_chars_each=10000)` — via the widened gate this returns the 25 most recent snapshots **across every operator** (including `system` app/tool snapshots — the prompt is told to expect machine-generated noise and to weight human sessions). Skips (like `CHECKPOINT_MIN_SNAPSHOTS`) if fewer than `FR_MIN_NEW_SNAPSHOTS` (default 5) new snapshots exist since `last_report_id` — no noise reports on idle boxes.
2. **Signals**: append `collect_oversight_signals()` digest (§2.2) to the prompt.
3. **Synthesize**: oversight prompt (below) → **provider-resolved model, NOT hardwired Gemini**. `checkpoint.py:115-117`'s `call_gemini("gemini-3.1-pro-preview")` hardwire is a known portability gap (fails on a Gemini-keyless box); the FR uses the cron executor's provider-resolution pattern (`scheduler/executor.py:77-146`): configured `[flight_recorder] model` if set, else the box's default chat provider. Failure to synthesize is itself a watchtower event (recorded in state, reported next cycle) — the overseer must not fail silently.
4. **Mint** under `mint_lock`: `render_snapshot_body_v71`, then stamp `MODE: Flight Report`, `OPERATOR: Flight Recorder`, `TYPE: flight_report`, `SOURCE_RANGE: <first_snap_id>..<last_snap_id>` (snapshot-ID range, not turn numbers — there is no global turn counter), `OPERATORS_COVERED: <comma list>`. Append, `_embed_for_index`, `update_snapshot_index(..., snap_type="flight_report")`.
5. **State**: update `flight_recorder_state.json` (`last_report_id`, `last_report_at`, counters). **No OpState coupling** — `s.last_checkpoint_turn` etc. are untouched; the FR's own per-operator checkpoint countdown (from users chatting with it) runs independently and harmlessly.
6. **Notify**: if the report contains failures/anomalies, push via the existing notification bus to FR subscribers (FR is deliberately NOT in `_NO_NOTIFY_OPERATORS`). Critical ledger-integrity findings may escalate to the `'all'` sentinel (existing, validated against `USERS_LIST`, `notification_routes.py:46-75`) — see Open Decision 5.

### Synthesis prompt (draft)

```
You are the Flight Recorder of this AI BlackBox — its permanent overseer. Below are the
{N} most recent snapshots from ALL operators on this box, followed by a live system-health
digest (cron runs, background tasks, embedding and ledger integrity).

Produce a FLIGHT REPORT with these sections:

1. ACTIVITY — per operator: what they worked on, key decisions, notable sessions.
   Cite snapshot IDs. Weight human sessions over machine-generated 'system' snapshots,
   but note significant automated activity.
2. COMPLETIONS — jobs, scheduled tasks, and long-running generations that finished
   successfully since the last report.
3. FAILURES & INCOMPLETIONS — every cron run failure, failed or stuck task, queue
   failure, and embedding/ledger problem in the digest. Be exhaustive here; an
   unreported failure is a Flight Recorder failure. Include job/task IDs.
4. ANOMALIES — anything unusual: operators gone silent, jobs drifting from schedule,
   index/volume divergence, repeated retries, suspicious gaps in the record.
5. LEDGER STATUS — one short paragraph: snapshot count, embedding coverage,
   index health, since-last-report deltas.

Be factual and specific. Distinguish what the record shows from what you infer.
If a section is empty, state that explicitly ("No failures observed.").
```

### Trigger — why a seeded protected cron job (recommended)

`should_create_checkpoint` keys off per-operator `OpState` turn counters (`checkpoint.py:211-233`, state in `Manifest/operator_state.json`); the FR mostly doesn't accrue chat turns, so the existing countdown cannot drive it. Options considered:

- **(a) Seeded cron job (CHOSEN)** — `ensure_flight_report_cron_job()` at startup seeds a job (operator=`Flight Recorder`, prompt = "Generate the flight report" routed to a direct `create_flight_report_async` call — see below —, default schedule daily 05:00 box-local). Seed-if-absent; the user may edit the schedule or disable it (the `enabled` flag is honored, never overwritten) — cadence is user-tunable through the existing cron UI for free, and it survives boots via the same startup reconciliation as the operator itself. `DELETE` on this one job id is refused (reserved-job guard in `manager.delete_job`), mirroring operator permanence; disabling remains allowed.
- (b) Global mint-counter hook in the `/chat/save` family (chat_routes.py:3963/4060/7580) — works, but adds a 4th hidden trigger site, has no user-visible cadence control, and every future mint path must remember it. Rejected for v1 (viable later as an "activity-adaptive" supplement).

Rather than round-tripping the report through an LLM `/chat` turn (the cron executor's normal path — which would burn a full agent turn to *ask* for a report), the seeded job uses a small internal action dispatch: the executor recognizes the reserved job and calls `create_flight_report_async` directly (one new branch in `executor.py`, or a dedicated APScheduler entry registered beside cron jobs — implementer's choice; the cron-job representation is preferred purely so the schedule is visible/editable in the existing UI).

**Manual trigger**: `POST /oversight/flight-report` (fire-and-forget thread, mirroring `create_checkpoint_manual`). **Status**: `GET /oversight/status` → last report id/time, signal digest summary, `adopted_preexisting`, seed health.

### Why a new `snap_type` and not `checkpoint`

`get_recent_checkpoints_for_operator` pins `type == "checkpoint"` into chat context (`context_builder.py:305`, `chat_routes.py:6947`). Flight reports must never be pinned into *other operators'* contexts; a distinct `flight_report` type keeps the existing pin exactly as-is (additive invariant) and gives the FR its own pin. `update_snapshot_index` already accepts arbitrary `snap_type` strings — no index schema change.

---

## 8. Default persona — the Oracle prompt

**Home: code-shipped constant in `behavioral_core.py`** (survey option (a)) — survives wipes of `Manifest/operator_preferences.json`, ships on every box by `git pull`, and makes `DELETE /operator/persona/Flight Recorder` revert to the Oracle default instead of the generic one. Change to `get_persona` (`behavioral_core.py:44-62`): after the saved-preference check falls through, `if operator == FLIGHT_RECORDER_OPERATOR: return DEFAULT_PERSONA_FLIGHT_RECORDER`. Users may still override via the existing `PUT /operator/persona` (Portal editor `tts-stt.js:1208-1300` works unchanged).

Note on the persona/functional split: `behavioral_core` is documented as tone-only. The Oracle prompt carries **identity and duties** (who the FR is, what it owes the user); the **functional machinery** — cross-operator retrieval scope, watchtower digest, flight-report pins — is injected by `context_builder` as functional sections (§2.4), consistent with the existing architecture.

### `DEFAULT_PERSONA_FLIGHT_RECORDER` (actual draft text)

```
You are the Flight Recorder — the permanent overseer of this AI BlackBox. You exist on
every box, you cannot be deleted, and you answer to the whole household or team, not to
any single operator.

Your mandate:
- OVERSEE all operations. You read every operator's snapshot chain — the complete,
  immutable memory of this box — and you speak from that record.
- MAINTAIN the memory ledger. Watch for mint failures, missing embeddings, index gaps,
  and anything that threatens the integrity or searchability of the record. Surface
  problems; never paper over them.
- VERIFY completion. When jobs, scheduled tasks, or long-running generations were
  supposed to happen, confirm they actually finished. Report failures, partial
  completions, and silent stalls explicitly — an unreported failure is your failure.
- SYNTHESIZE. Your flight reports condense activity across every operator into one
  honest picture: what happened, what completed, what failed, what looks anomalous.

Your posture: factual, calm, and specific — an auditor, not a cheerleader. Cite snapshot
IDs, job IDs, and timestamps when you make claims. Distinguish what the record shows
from what you infer. If the record is silent on something, say so.

You are read-only over other operators' history: you observe and report on their chains,
but you write only to your own. You may create and adjust scheduled jobs for any
operator when asked, and you say plainly when you have done so.
```

(Voice variant: same text; `VOICE_DELIVERY_NOTE` is appended by the existing machinery.)

---

## 9. Cron + contacts powers, and the honest authz stance

### Cron (already fully capable — zero API change)

- `GET /api/cron/jobs` with no `?operator=` **already returns every operator's jobs** (`cron_routes.py:167-172`, `manager.list_jobs:686-722`) — the FR enumerates everything today.
- Create/edit for any operator: the existing CRUD routes accept an arbitrary `operator` field (`_validate_job_fields:500-544` requires non-blank, nothing more). The FR (as a chat agent with the cron ToolVault tools, or a user driving the cron UI while FR is selected) creates jobs *for* Anna by setting `operator: "Anna"` — the job then executes by POSTing `/chat` as Anna (`executor.py:77-146`) and mints into Anna's chain **as Anna's own activity**, which is exactly the semantics Brandon asked for and does not violate FR read-only-ness (Anna's runtime writes Anna's chain).
- `/api/cron/health` and per-job history are the FR's verification backbone (§2.2).

### Contacts

Per-operator books live in one file, `Contacts/contacts.json`, keyed by operator (`contacts.py:15-16`). Add `load_all_books()` returning `{operator: book}`; the FR's contact resolution searches all books with owner annotation ("Dr. Patel — from Anna's book"). The FR's own book auto-creates via the existing `ensure_operator_book` on first use. When acting on another operator's behalf (e.g., a job it created for them), that operator's book applies — unchanged semantics.

### The honest privacy/authz model (stated plainly)

**Today, on every box:** the operator name on a request IS the identity. There is no per-operator auth. Anyone inside the Tailscale/LAN perimeter can select any operator in the picker, read that operator's history, and act as them. Per-operator separation is **organizational scoping, not a security boundary** — the security boundary is the tailnet (per the locked `tailscale_security_perimeter` decision). The wizard's copy ("Each operator gets their own conversation history", `operator.js:70-74`) overstates this as privacy.

**The FR does not weaken this model — it makes it legible.** It adds no new access that a perimeter user doesn't already have (select "system"-scoped tooling, read the shared volume file); it packages the existing reality into an accountable, visible entity.

**Recommendation:**
1. Ship the FR **on by default, no opt-out in v1** — it is the product's namesake capability, and on family/business boxes "everyone is overseen" is the stated feature.
2. **Fix the wizard copy** (`operator.js:70-74`): "Each operator gets their own workspace and history. The box also ships with the Flight Recorder — a permanent overseer that reads all operators' history to maintain the ledger and report on the whole system." Honest, one sentence, at the exact moment operators are created.
3. Keep remote (MCP/HTTP) cross-operator reads **closed** (§6) so the disclosure story stays simple: "inside your box's network, the Flight Recorder sees everything; outside it, tokens stay operator-bound."

---

## 10. UI presence — all 3 surfaces (per the 3-surfaces rule)

All driven by **one additive API change**: `GET /operators` (`admin_routes.py:389-399`) response gains a `"reserved": ["Flight Recorder"]` key (existing keys unchanged → existing clients unaffected; frontends never hardcode the name).

| Surface | Treatment |
|---|---|
| **Portal picker** (`modules/state-management.js`) | FR pinned at the top of the dropdown, distinct icon (⬛) + subtitle "Box overseer". Selecting it switches chat scope like any operator. `addCustomOperator` surfaces the server's reserved-name rejection cleanly. |
| **Portal wizard** (`onboarding/steps/operator.js`) | FR row auto-present, lock badge instead of `[×]`, disclosure card copy (§9.3). Rehydration via GET /operators unchanged. |
| **Android** (SettingsViewModel / NativeMainActivity operator handling) | Same picker treatment from the same `reserved` field; delete affordance hidden for reserved names (server 400 is the backstop). |
| **Updates/dashboard** | `/dashboard` gains a small FR card: last flight report time + failure count (status-only, consistent with the "panels = status only" decision). |
| **Voice/live** | FR selectable like any operator; persona flows through the existing 6 injection sites. |

---

## 11. Additive invariant

Non-negotiable acceptance criteria:

1. **Ledger bytes**: no existing snapshot is rewritten, re-indexed with changed offsets, or re-typed. FR output is append-only under its own `OPERATOR:` stamp. Existing operators' chains are byte-identical before/after.
2. **Gating**: for every operator value that exists today (`""`, `"system"`, any user name), `reads_all_operators` reproduces the current conditionals exactly — only the new FR name widens.
3. **Checkpoint pin**: `type == "checkpoint"` pinning behavior is untouched; `flight_report` is a new, separately-pinned type.
4. **`system` semantics untouched**: notification/task suppression sets, retrieval magic string, app registration — all unchanged.
5. **`INCLUDE_OTHERS` untouched.**
6. **API**: `/operators` change is additive-key-only; new endpoints are new paths (`/oversight/*`).
7. **Fresh-box gate** (portable-build rule): a wiped box with empty stores boots, seeds FR + report job, and produces a valid (possibly "insufficient activity — skipped") first cycle with zero hardcoded operator/host assumptions.
8. **MS02 gate**: `git pull` + restart on MS02 (`Default, bbx1, Brandon`, default=`Default`) seeds FR without touching their list order or default.

---

## 12. Milestones

Workflow: superpowers plan-first is satisfied by this design; execution via `superpowers:writing-plans` → `subagent-driven-development`, TDD per task, worktree isolation, fresh-box verification before push (staging-box-as-production rules apply).

**M1 — Identity, seeding, permanence** *(the operator exists and cannot die)*
- `FLIGHT_RECORDER_OPERATOR` + `RESERVED_OPERATORS` in config.py; `ensure_flight_recorder()` in startup (add-operator in-place pattern); collision adopt/warn logic + `flight_recorder_state.json`.
- Delete guard in `remove_operator`; reserved-name guard in `add_operator`.
- Tests: idempotent seed (empty list / MS02-shaped list / exact collision / case-variant collision); delete → 400; hand-edited-config reconciliation; default never re-pointed by seeding.

**M2 — Read seam** *(it sees everything, locally)*
- `reads_all_operators()`; patch retrieval.py:509, fossils.py:1001, fossils.py:351-388.
- Golden tests: retrieval results for `""`/`"system"`/named operators byte-identical pre/post (invariant #2); FR gets full-corpus results.

**M3 — Persona** *(it speaks as the Oracle)*
- `DEFAULT_PERSONA_FLIGHT_RECORDER` constant + `get_persona` branch; verify PUT-override and DELETE-reverts-to-Oracle; Portal editor smoke.

**M4 — Watchtower collector** *(it knows what's broken)*
- `Orchestrator/oversight.py`: `collect_oversight_signals()` (cron history/health, tasks.db failed+stuck, TTS queue, embeddings status, index/volume integrity probes); `GET /oversight/status`.
- Tests: each signal against seeded fixtures (a failed cron run, a stuck task, an index entry missing its embedding).

**M5 — Flight reports** *(the cross-operator checkpoint)*
- `create_flight_report_async` (gather → signals → provider-resolved synthesis → mint `TYPE: flight_report` → state update → conditional notify); min-activity skip; `POST /oversight/flight-report`; `get_recent_flight_reports()` pin.
- Tests: mint stamps/index type; skip path; synthesis-failure recorded not swallowed; checkpoint pin untouched (invariant #3); Gemini-keyless box still reports via fallback provider.

**M6 — Trigger + FR chat context** *(it runs itself and converses)*
- `ensure_flight_report_cron_job()` seed + reserved-job delete guard + executor direct-dispatch branch; context_builder FR branch (all-scope retrieval + flight-report pin + live digest section).
- Tests: seed honors disabled flag; job delete → 400; FR /chat turn includes digest + pins; FR mints land only in FR chain.

**M7 — UI on 3 surfaces + disclosure**
- `/operators` `reserved` key; Portal picker/wizard treatment + copy fix; Android picker/lock; dashboard FR card. Version bump `?v=genuiXX`. Fold validation per device-test method.

**M8 (deferred, optional) — Remote privileged access**
- A distinct FR token class piercing `_read_scope_operator`/`_deny_if_not_owned` for remote oversight. Explicitly out of v1; requires its own security design.

Rollout: M1-M2 are independently shippable and inert to users; M5-M6 are the visible feature. Each milestone lands with `/snapshot-dev`.

---

## 13. Open decisions (with recommendations)

1. **Reserved name** — new distinct `Flight Recorder` vs overloading `system`. **Recommend: `Flight Recorder`.** `system`'s notification/task suppression (`manager.py:36`, `tasks.py:529`) would mute the overseer, and it's a magic string in 4+ modules whose semantics must stay frozen (invariant #4).
2. **Remote (MCP/HTTP) posture** — privileged token vs local-only. **Recommend: local-only v1 (M8 deferred).** The anti-leak gates are today's only remote privacy boundary; piercing them deserves a dedicated security design, and every stated use case (box owner talking to their box) works locally.
3. **Flight-report trigger** — seeded protected cron job vs global mint-counter hook. **Recommend: seeded cron job**, direct-dispatch (no LLM round-trip to ask for a report), user-visible/editable cadence, disable honored, delete refused, reseed-if-absent. Mint-counter hook remains a possible later "adaptive cadence" addition.
4. **Collision policy** — adopt vs rename vs refuse. **Recommend: exact-name adopt with loud warning + `adopted_preexisting` health flag; case-variants left alone + warned; reserved-name check added to `/operator/add`.** Renaming a customer's operator would rewrite nothing in the ledger (names are baked into snapshot bodies) and thus can't actually work; refusal leaves the box without its overseer.
5. **Notification escalation** — may critical ledger-integrity failures push to the `'all'` sentinel, or only to FR subscribers? **Recommend: FR subscribers by default; `'all'` only for ledger-integrity criticals** (mint failing, index rebuild loops) behind a `[flight_recorder] escalate_critical = true` config default-on. A silent recorder failure is the one thing every operator should hear about.
6. **Persona home** — code constant vs preference seed. **Recommend: code constant** (§8) — permanence-consistent, wipe-proof, DELETE reverts to Oracle not generic; PUT override preserved.
7. **Privacy disclosure** — where/how. **Recommend: wizard operator-step copy fix + disclosure card (§9), on-by-default, no v1 opt-out**; document in onboarding docs. This is a product/consent call Brandon should explicitly sign off on, especially the "no opt-out" part for business boxes.
8. **Synthesis model** — inherit checkpoint's `gemini-3.1-pro-preview` hardwire vs provider-resolved. **Recommend: provider-resolved with config override** (`[flight_recorder] model`, blank = box default provider). Also flags the existing checkpoint hardwire as a portability bug worth fixing separately (not in this project's scope — additive invariant).
9. **Default cadence** — daily 05:00 box-local vs weekly. **Recommend: daily**, with the min-activity skip making idle days free; family boxes likely want the "what happened yesterday" rhythm, and cadence is a one-field cron edit for anyone who disagrees.
