# The Signal — System-Telemetry Stream Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the fake cycling "thinking" phrases with a live, ephemeral single red line that morphs through the *real* system telemetry of each chat turn (embed · search · rank · tools · generate · mint), over a switchable particle field — on Portal web, Android native, and (inherited) the WebView, with a new honest `system_activity` SSE event.

**Architecture:** A new **UI-only** `system_activity` SSE event carries honest per-stage telemetry that the backend already computes. It is **never** injected into the LLM prompt or written to a snapshot. Frontends parse it into a transient state that drives a single morphing HUD line; a separate particle canvas renders behind. Retrieval telemetry is emitted as a burst right after `stream_start` (from metrics `build_streaming_context` already returns); tool telemetry already streams; mint telemetry comes from the `/chat/save` response.

**Tech Stack:** Python/FastAPI (Orchestrator, SSE over `/chat/stream`), vanilla ES modules + Canvas 2D (Portal web), Kotlin + Jetpack Compose `Canvas` (Android). No new dependencies.

**Companion design doc:** `docs/plans/2026-07-13-system-telemetry-stream-design.md` (read it first — §7 stage menu, §6 event shape, Appendix A particle techniques). Visual reference of record: the approved prototype `the-signal-prototype.html` (scratchpad).

---

## Conventions & pre-flight (read once)

- **Three-surface rule:** every frontend change ships Portal web + Android Kotlin; the WebView (`PortalActivity`) inherits the web version for free (verify, don't re-implement).
- **HARD RULE (load-bearing):** `system_activity` is presentation-only. It must NEVER be appended to the conversation payload, the context builder, or the snapshot body. A dedicated test enforces this (Task 1.9).
- **Line numbers drift** — every "Modify `file:NNN`" below is from the 2026-07-13 architecture map; the executor MUST re-`grep`/read the symbol before editing.
- **Backend run/restart:** service on `localhost:9091`; restart with `sudo systemctl restart blackbox.service` (pre-authorized; ~60–90 s warm-up for index rebuild). Watch logs: `journalctl -u blackbox.service -f`.
- **Android build gate:** `cd <android-root> && ./gradlew :app:testDebugUnitTest --offline` (~35 s). SDK `~/Android/Sdk`, JDK 17, gradle offline. Copy `local.properties` into any worktree. Android root: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/`.
- **Python tests:** check for an existing suite (`ls Orchestrator/tests 2>/dev/null`, `pytest` availability). Task 1.1 is written as a **pure, importable helper** so it is unit-testable without booting the server; if no pytest harness exists, create `Orchestrator/tests/test_system_activity.py` and run with `python -m pytest`.
- **Ports:** don't touch 9091. Don't re-commit `embeddings.json` (gitignored, per-box).
- **Commit style:** small, frequent, conventional commits; end messages with the Co-Authored-By trailer. Work on `main` per this box's staging-as-production convention unless a worktree is requested.

---

# PHASE 1 — Backend event + Web single-morphing-line + tools + mint

*Goal of P1: a working, honest truth-feed on Portal web (and thus the WebView), with the existing particle field untouched. Ships value before any native work.*

### Task 1.1: Pure `system_activity` event-builder (backend, TDD)

**Files:**
- Create: `Orchestrator/telemetry_events.py`
- Test: `Orchestrator/tests/test_system_activity.py`

**Step 1 — Write the failing test.** The builder takes a metrics dict (what `build_streaming_context` knows) and returns an ordered list of `{"type":"system_activity","data":{stage,label,detail,seq}}` dicts, **omitting stages that didn't run** (honest degradation).

```python
# Orchestrator/tests/test_system_activity.py
from Orchestrator.telemetry_events import build_retrieval_activity

def test_full_retrieval_turn():
    m = {"provider":"anthropic","model":"claude-sonnet-4-5","window_tokens":840000,
         "embed_model":"gemini-embedding-2","embed_dims":3072,"corpus_count":8005,
         "candidates":22,"rrf_c":60,"mmr_topk":8,"rerank_enabled":False,
         "memories":8,"context_tokens":42000,"dropped":0}
    ev = build_retrieval_activity(m)
    stages = [e["data"]["stage"] for e in ev]
    assert stages == ["resolve_model","embed","search","rrf","mmr","context"]
    assert all(e["type"]=="system_activity" for e in ev)
    assert [e["data"]["seq"] for e in ev] == list(range(len(ev)))  # monotonic
    assert "3072" in ev[1]["data"]["label"]           # embed shows real dims
    assert "rerank" not in stages                      # dark reranker -> omitted

def test_non_retrieval_turn_emits_nothing():
    assert build_retrieval_activity({"provider":"gpt-5.1","model":"gpt-5.1"}) == []  # no memory searched

def test_window_guard_drop_is_honest():
    m = {"provider":"gpt-5.1","model":"gpt-5.1","embed_model":"gemini-embedding-2","embed_dims":3072,
         "corpus_count":8005,"candidates":19,"mmr_topk":8,"memories":8,"context_tokens":38000,"dropped":3}
    labels = " ".join(e["data"]["label"] for e in build_retrieval_activity(m))
    assert "trimmed 3" in labels or "3 dropped" in labels
```

**Step 2 — Run, verify it fails.** `python -m pytest Orchestrator/tests/test_system_activity.py -v` → FAIL (module missing).

**Step 3 — Implement the minimal builder.**

```python
# Orchestrator/telemetry_events.py
"""Pure builders for the UI-only `system_activity` SSE event (The Signal).
NEVER persisted to prompt/ledger — presentation only."""

def _ev(seq, stage, label, **detail):
    return {"type": "system_activity",
            "data": {"stage": stage, "label": label, "detail": detail, "seq": seq}}

def build_retrieval_activity(m: dict) -> list:
    """Given the metrics build_streaming_context already computed, return the
    ordered honest telemetry for the retrieval half. Omits stages that didn't run."""
    out, seq = [], 0
    def add(stage, label, **d):
        nonlocal seq
        out.append(_ev(seq, stage, label, **d)); seq += 1

    # resolve model (always, if we have a provider+model)
    if m.get("provider") and m.get("model"):
        win = m.get("window_tokens")
        win_s = f" · {round(win/1000)}k window" if win else ""
        add("resolve_model", f"brain · {m['model']}{win_s}",
            provider=m["provider"], model=m["model"])
    # everything below only if memory was actually searched
    if not m.get("embed_model"):
        return out if m.get("_keep_model_only") else []  # non-retrieval turn: nothing honest to say
    add("embed", f"embed · {m['embed_model']} · {m['embed_dims']}d",
        model=m["embed_model"], dims=m["embed_dims"])
    if m.get("corpus_count") is not None and m.get("candidates") is not None:
        add("search", f"search · {m['corpus_count']:,} snapshots → {m['candidates']} cleared floor",
            corpus=m["corpus_count"], candidates=m["candidates"])
    if m.get("rerank_enabled"):
        add("rerank", f"rerank · {m.get('rerank_model','reranker')} · {m['candidates']}→{m['mmr_topk']}")
    if m.get("mmr_topk") is not None:
        add("mmr", f"MMR · top-{m['mmr_topk']} selected", top_k=m["mmr_topk"])
    if m.get("memories") is not None:
        tok = m.get("context_tokens")
        tok_s = f" · {round(tok/1000)}k tokens" if tok else ""
        drop = m.get("dropped") or 0
        fit = f" · trimmed {drop} to fit" if drop else ""
        add("context", f"context · {m['memories']} memories{tok_s}{fit}",
            memories=m["memories"], tokens=tok, dropped=drop)
    return out
```

*Note:* the design's §7 lists more possible stages (RRF/recency); include only those you can source a real number for. Keep it honest — a stage with no metric is omitted, not faked.

**Step 4 — Run, verify pass.** `python -m pytest Orchestrator/tests/test_system_activity.py -v` → PASS.

**Step 5 — Commit.** `git add Orchestrator/telemetry_events.py Orchestrator/tests/test_system_activity.py && git commit -m "feat(signal): pure system_activity event builder for honest telemetry"`

---

### Task 1.2: Surface retrieval metrics from `build_streaming_context`

**Files:** Modify `Orchestrator/context_builder.py` (`build_fossil_context` ~L195, provenance ~L301, drop ~L462); `Orchestrator/routes/chat_routes.py` (`build_streaming_context` ~L7034).

**Step 1 — Locate & read** `build_fossil_context` and confirm where it computes: active embed model/dims (`get_active_store()`), `store.count`, candidate counts, `mmr` top-k, post-drop provenance lengths, context token estimate, `dropped`.

**Step 2 — Add a `telemetry` dict to the return.** `build_streaming_context` already returns provenance; extend it to also return a `telemetry` metrics dict shaped for `build_retrieval_activity` (Task 1.1). Pull each value from the variable that already holds it (do NOT recompute). Use **post-drop** provenance counts for `memories` (design §10.5 — never over-claim).

**Step 3 — Verify (manual).** Add a temporary `print(json.dumps(telemetry))` in `build_streaming_context`, send one memory-backed chat via the Portal, and confirm the journalctl line shows real values (`gemini-embedding-2`, `3072`, live corpus count, etc.). Remove the print.

**Step 4 — Commit.** `git commit -am "feat(signal): expose retrieval telemetry metrics from build_streaming_context"`

---

### Task 1.3: Emit the `system_activity` burst in `generate_sse`

**Files:** Modify `Orchestrator/routes/chat_routes.py` — `generate_sse` (~L7226 GET / ~L7339 POST), right after the `stream_start` frame (~L7228/L7340). Relay loop at L7256-7265 needs **no change** (it already serializes any `{type,data}` dict).

**Step 1 — Read** `generate_sse` and confirm `stream_start` is the first yielded frame and that `telemetry` (Task 1.2) is in scope there.

**Step 2 — Yield the burst.** Immediately after `stream_start`:
```python
from Orchestrator.telemetry_events import build_retrieval_activity
for _ev in build_retrieval_activity(telemetry):
    yield _ev
```
This is the **v1 "burst after stream_start"** approach (design §8). Do NOT relocate `build_streaming_context` into the generator in P1 (that's the optional v2).

**Step 3 — Verify (manual, curl).** With the service running:
```bash
curl -N -s -X POST localhost:9091/chat/stream -H 'Content-Type: application/json' \
  -d '{"message":"search my memory for the audio work and summarize","operator":"<op>"}' \
  | grep -E "^event: (stream_start|system_activity)" | head
```
Expected: `stream_start` then several `system_activity` frames with real labels. `grep system_activity` count > 0.

**Step 4 — Commit.** `git commit -am "feat(signal): stream honest retrieval telemetry as system_activity after stream_start"`

---

### Task 1.4: Mint telemetry via the `/chat/save` response

**Files:** Modify `Orchestrator/checkpoint.py` (`perform_mint` ~L238, `_embed_for_index` ~L303, dims log ~L56/59) and `Orchestrator/routes/chat_routes.py` (`chat_save` ~L7401, `perform_mint` call ~L7509).

**Step 1 — Plumb `dims`.** `_embed_for_index`/`perform_mint` currently drop the embedding dimension after logging it. Return `dims` in `perform_mint`'s result dict.

**Step 2 — Return it from `/chat/save`.** The endpoint already returns `snap_id`/`minted`; add `dims` (and the mint `reason`).

**Step 3 — Verify.** `curl` the `/chat/save` response after a turn (or read the Portal network tab) → JSON contains `snap_id` + `dims:3072`.

**Step 4 — Commit.** `git commit -am "feat(signal): return mint dims from /chat/save for the mint telemetry line"`

---

### Task 1.5: Kill the pseudo phrase cyclers (web)

**Files:** Modify `Portal/modules/chat-send.js` (delete `STREAMING_REASONING_PHRASES` L126-147 + `startStreamingReasoningCycle`/`stopStreamingReasoningCycle` L156-182 and their call sites ~L267/L809); `Portal/modules/chat-bubbles.js` (delete `THINKING_MESSAGES` L23-36 + `startThinkingAnimation`/`stopThinkingAnimation` L148-174 + call sites).

**Step 1 — Delete** the phrase arrays + cyclers; leave the bubble scaffolding (`createStreamingThinkingBubble` / `createAnimatedThinkingBubble`) in place for now — Task 1.6 repoints them.

**Step 2 — Verify (manual).** Load the Portal; the old cutesy phrases must be gone (bubble may be temporarily bare). No console errors.

**Step 3 — Commit.** `git commit -am "refactor(signal): remove fake cycling thinking phrases (web)"`

---

### Task 1.6: The single morphing line component (web, visual-verified)

**Files:** Create `Portal/modules/signal-feed.js`; Modify `Portal/modules/chat-send.js` (SSE switch ~L1162).

**Step 1 — Port the morph from the prototype.** Create a `SignalLine` module exporting `mount(container)`, `push(label, heavy)`, `dissolve()`. Copy the diff-morph + per-char wave logic verbatim from `the-signal-prototype.html` (the `setLine`/`waveFrame`/`ensureCells` functions — they are framework-free and drop in directly). Render red monospace, no chrome, positioned over the chat/streaming area.

**Step 2 — Wire the events.** In the SSE switch, add:
```js
case 'system_activity':
    SignalLine.push(data.label, (data.detail && data.detail.heavy) || 0.6);
    break;   // UI-ONLY — never touch fullThinking/fullResponse/save payload
```
Also feed the existing `tool_start`/`tool_result` events into `SignalLine.push` (e.g. `tool · <name> · <ok>`), and call `SignalLine.dissolve()` on `stream_end`. After `/chat/save` resolves, `SignalLine.push` the mint line from its response (`mint · <snap_id> · <dims>d`) then dissolve.

**Step 3 — Verify (visual).** Restart, send a memory-backed prompt in the Portal. Confirm: one red line that morphs through the real stages, tool lines appear, mint line lands after the answer, then it dissolves. Compare against the approved prototype.

**Step 4 — Commit.** `git commit -am "feat(signal): single morphing telemetry line on Portal web"`

---

### Task 1.7: Non-stream & edge-case honesty (web, TDD-light)

**Files:** `Orchestrator/telemetry_events.py` (already handles omission); `Portal/modules/signal-feed.js`.

**Step 1 — Guard non-retrieval turns.** Confirm (test from Task 1.1 already covers backend) that a turn with no memory search yields no retrieval stages; the line should simply show `generating · <model>` → mint. No empty/fake lines.

**Step 2 — Keepalive/prefill.** Ensure `heartbeat`/`: keepalive` frames are ignored by `SignalLine` (they already have no `system_activity` type). Optionally push a `prefill · model warming` line on the first long gap (nice-to-have; skip if noisy).

**Step 3 — Commit.** `git commit -am "feat(signal): honest degradation for non-retrieval and prefill (web)"`

---

### Task 1.8: WebView inheritance check

**Step 1 — Verify** `PortalActivity` (Android) loads `/ui/` and thus renders the new web line with zero native work; `WizardWebViewScreen` is unaffected. No code change expected — just confirm on device or by reading the loader. Document the result. (No commit unless a fix is needed.)

---

### Task 1.9: HARD-RULE enforcement test (never-in-prompt)

**Files:** Test `Orchestrator/tests/test_signal_not_persisted.py`.

**Step 1 — Write the test.** After a simulated turn + save, assert the persisted snapshot body and the outbound context contain **none** of the telemetry labels (e.g. no `"system_activity"`, no `"embed · gemini-embedding-2"`). If a full integration harness is impractical, assert structurally: grep the `/chat/save` code path to prove `system_activity` data is never read into the save payload, and add a code comment marker.

**Step 2 — Run / verify, Step 3 — Commit.** `git commit -am "test(signal): enforce telemetry is never persisted to prompt/ledger"`

**✅ Phase 1 exit criteria:** curl shows `system_activity` frames; Portal shows the morphing line end-to-end incl. tools + mint; snapshots contain no telemetry; the WebView inherits it.

---

# PHASE 2 — Android native (crown jewel)

*Android root:* `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/`

### Task 2.1: Parse `system_activity` into transient state (TDD)

**Files:** Modify `ui/chat/ChatViewModel.kt` (`processSSEEvent` `when()` ~L2123); Test `app/src/test/java/.../SystemActivityParseTest.kt`.

**Step 1 — Failing test:** feed a fake `SSEEvent("system_activity", "{\"stage\":\"embed\",\"label\":\"embed · gemini-embedding-2 · 3072d\",\"seq\":1}")` and assert it lands in a new **transient** `MutableStateFlow<SignalActivity?>` (or a bounded list) and is **NOT** written onto the persisted `UiMessage.reasoning`/content.

**Step 2 — Run, verify fail** (`./gradlew :app:testDebugUnitTest --offline`).

**Step 3 — Implement:** add `"system_activity" -> { … }` case decoding the JSON into a `SignalActivity(stage,label,seq)` and emitting to a `_signal` StateFlow scoped to the in-flight turn. Ephemeral — cleared on `stream_end`. No `updateLastMessage` / no persistence.

**Step 4 — Run, verify pass. Step 5 — Commit.** `git commit -am "feat(signal): parse system_activity into transient state (android)"`

### Task 2.2: The morphing-line Composable (visual-verified)

**Files:** Create `ui/components/SignalLine.kt`; Modify `ui/components/ChatBubble.kt` (replace `ThinkingIndicator(...)` call ~L356-357); **do NOT touch** the real reasoning dropdown (L188-217).

**Step 1 — Build `SignalLine`** as a Compose `Canvas`/text hybrid: observe the `_signal` flow; render one red monospace line; drive the per-char wave + diff-morph via a `withFrameNanos` frame clock reading the current label (mirror the prototype's algorithm; Appendix A shared-engine notes). Delete/retire `ThinkingIndicator.kt` (and its `THINKING_MESSAGES`).

**Step 2 — Verify (device):** build + install, send a memory-backed prompt, watch the single morphing line; compare to prototype. Gate: `./gradlew :app:testDebugUnitTest --offline` green.

**Step 3 — Commit.** `git commit -am "feat(signal): morphing telemetry line composable (android)"`

### Task 2.3: Tool + mint telemetry (android)

**Files:** `ChatViewModel.kt` (tool events + `/chat/save` response dims). Feed tool lines + the mint line into `_signal`. **Step: Verify on device; Commit.**

### Task 2.4: Confirm reasoning dropdown untouched & real-reasoning still works

Manual: expand the "Thinking" dropdown on an Anthropic/Gemini turn — real reasoning still streams there. No regression. Document; commit only if a fix is needed.

**✅ Phase 2 exit criteria:** native chat shows the morphing line end-to-end; reasoning dropdown intact; unit tests green; device-validated.

---

# PHASE 3 — Particle field modes (Rising Stars / Embers / Matrix)

*Ships the switchable field. See design Appendix A for the full technique parameters.*

### Task 3.1: Web 3-mode particle engine

**Files:** Modify `Portal/modules/ember-fx.js` + `Portal/styles/features/_ember-fx.css`.

**Step 1 — Port the prototype engine.** Bring over the three draw routines (Rising Stars / Embers / Matrix), the pre-rendered sprite atlas, curl-noise, delta-timed loop, and responsive `baseCount()` from `the-signal-prototype.html`. Rename the existing field internally to **Rising Stars**. Keep `GEN_SELECTOR` behavior (fade in while generating).

**Step 2 — Verify (visual)** each mode against the prototype. **Step 3 — Commit.** `git commit -am "feat(signal): 3-mode particle field on web (stars/embers/matrix)"`

### Task 3.2: Android Compose 3-mode field

**Files:** Modify `ui/chat/ChatScreen.kt` `EmberOverlay` (~L119) → multi-mode Compose `Canvas` using `withFrameNanos` + `BlendMode.Plus` (Appendix A Compose notes + caps: Stars 400–800, Embers 200–400, Matrix 40–80 cols). **Verify on device; Commit.**

### Task 3.3: Mode setting across surfaces

**Files:** extend the existing backdrop setting (Off / While-gen / Always) with a **field mode** (Rising Stars / Embers / Matrix) — Portal settings UI + Android settings + persistence. Three-surface. Additive catalog contract. **Verify; Commit.**

### Task 3.4 (optional): Load-reactivity

Wire field intensity/spawn to the `system_activity` cadence (surge on heavy stages), as prototyped. **Verify; Commit.**

**✅ Phase 3 exit criteria:** all three fields selectable and correct on web + Android; setting persists; device-validated.

---

## Final verification (all phases)

- `python -m pytest Orchestrator/tests/ -v` green.
- `./gradlew :app:testDebugUnitTest --offline` green.
- Live: memory-backed turn on web AND native shows the morphing line + tools + mint, then dissolves; silent-model turn stays honest; snapshot ledger contains no telemetry (HARD RULE).
- `/snapshot-dev` to record the session.

## Out of scope (do not do)
- Touch the real model-reasoning dropdown (leave as-is).
- Persist telemetry anywhere (no flight-log, no data-model field).
- Backend v2 relocation of `build_streaming_context` (only if per-stage real timing is later wanted).
- Fabricate reasoning for silent providers.
