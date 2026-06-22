# Reply Parsing & Rendering Hardening — Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to execute this plan task-by-task.

**Goal:** Stop model output from leaking raw JSON / fake tool-transcripts into the UI and into snapshot memory, and stop stale "it's broken" memory from re-priming confabulation. Fix the immediate rendering symptom now; land the durable channel-separation architecture after.

**Architecture:** A model turn mixes three things — (a) prose for the human, (b) real structured tool invocations, (c) tool results. Today the chat path stores and renders the model's **raw text** verbatim, so when a model *narrates* tool calls/results as prose (or confabulates them), that JSON pollutes both the UI (mangled markdown) and the saved snapshot (which is later retrieved and re-confabulated). The durable fix is a single deterministic **reply envelope** at the turn boundary that splits raw output into `{display_text, snapshot_text, tool_calls}`; the renderer hardening is the always-on safety net beneath it.

**Tech stack:** Python (Orchestrator FastAPI), Kotlin/Compose (Android MVP), JS (Portal). Markdown: `com.mikepenz:multiplatform-markdown-renderer` (Android), Portal chat renderer.

---

## Background — the incident this fixes (2026-06-22)

A BlackBox sweep reported all four Google structural-edit tools "still failing with must-be-a-list," with a detailed root-cause writeup. **It was confabulated**, proven by: (1) the error strings (`"…received as a JSON-encoded string, not a native array"`) exist nowhere in the codebase; (2) the file IDs (`1aZ9_kQvProomBlackBoxDeckXYZ`) were hallucinated; (3) the journal has **no** `[TOOLVAULT-EXEC]`/`[ARG-COERCE]` execution lines for those calls — the whole "sweep" was saved as one blob of assistant **response text**; (4) it showed native arrays being sent yet "got string" returned (impossible). Live re-proof: a stringified `requests` through the real service returns `applied: 1` with `[ARG-COERCE]` firing. The arg-coercion fix (`270a542`) works.

**Why it happened (the loop we must break):** `search_snapshots` returned `SNAP-20260622-7584` (the *old, fixed* failure) but not `SNAP-20260622-7585` (the fix) — the failure snapshot is the better *semantic* match for "file IDs created," and the mild recency tie-break can't lift the fix above it. The model built its whole turn on stale memory, invented IDs, and narrated the expected failure as text — which then rendered as raw JSON in Android. Three defects, one chain: **stale retrieval → confabulated text → raw-JSON render**, with the confabulation persisted back into memory.

---

## Phase A — Renderer hardening (immediate, 3 surfaces, low-risk safety net)

**Item:** unfenced JSON / structured blobs in assistant text render through the markdown engine and mangle (markdown-significant chars inside JSON: `_`, `[]`, `**`, `#`). Make every chat renderer detect an unfenced JSON-ish run and render it as a code block (clean monospace), never through the markdown parser.

### Task A1: Android — MarkdownText auto-fences unfenced JSON
**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/…/ui/components/MarkdownText.kt` (`splitContent`)
- Test: add `…/test/…/MarkdownTextSplitTest.kt` (or nearest existing test module)

**Approach:** in `splitContent`, before emitting a prose `Markdown` segment, scan it for a run that begins (at line start) with `{` or `[` and balances to a matching close spanning ≥1 line OR >~80 chars — emit that run as a `ContentSegment.CodeBlock(language="json")` instead of `Markdown`. Conservative: only whole-line runs that *parse* as JSON (try a cheap brace/bracket-balance + `org.json`/`kotlinx.serialization` parse; on parse failure leave as markdown). Existing fenced blocks are untouched.

**Steps:** TDD — write the split test first (input with inline JSON → expect a CodeBlock segment; input with normal prose+bold → unchanged; fenced block → unchanged), watch it fail, implement, pass. Then `./gradlew assembleDebug`.

### Task A2: Portal web — same guard in the chat markdown renderer
**Files:** Modify the Portal chat message renderer (locate the markdown render in `Portal/app.js` / `Portal/modules/*`; mirror the contacts `escapeHtml` discipline). Bump `?v=genuiNN` in `Portal/index.html`.
**Approach:** same heuristic — detect an unfenced JSON run and wrap it in a `<pre><code>` (escaped) instead of feeding the markdown parser. WebView wrappers inherit this automatically.

### Task A3 (verify): a confabulation-shaped fixture renders cleanly
Render a saved blob containing inline `{"results":[...]}` + prose on both surfaces; confirm JSON shows as a clean code block, prose still formats.

---

## Phase B — Memory: don't let stale failure-memory re-prime confabulation

**Item:** retrieval surfaced a resolved-bug snapshot as if current; the fix snapshot didn't ride along.

### Task B1: Snapshot hygiene convention (cheap, high-leverage)
When minting a snapshot that records a **resolved** problem, the snapshot text must state the resolution + reference the superseding snapshot id (we already do this in prose — make it a checklist item in `.claude/commands/snapshot-dev.md`). Rationale: the durable fix for "stale memory" is that the *failure* snapshot itself carries "FIXED in SNAP-XXXX," so any retrieval of it self-corrects.

### Task B2: Retrieval — surface resolutions with problems (design-first)
**Investigate:** `Orchestrator/fossils.hybrid_retrieve`. Add a light post-step: if a top result's text contains failure language AND a newer snapshot references the same artifact/topic, include the newer one (or boost snapshots whose text says "FIXED/RESOLVED/SHIPPED" for the same subject). Keep it conservative — this is a tie-break/augment, not a re-rank. Write a test over a small fixture (failure snap + later fix snap on the same topic → both returned). **Gate:** if B3 (clean snapshots) lands, much of this pressure drops — do B1 first, then measure before over-engineering B2.

### Task B3: depends on Phase C — clean snapshot text means fewer false "failure" memories
A confabulated tool-transcript should never have become a snapshot. Phase C's `snapshot_text` channel prevents that at the source.

---

## Phase C — The reply envelope (durable architecture; the bigger refactor)

**Item / Brandon's design:** parse the model's raw turn output **once**, deterministically, into three channels instead of storing/rendering it verbatim:

```
parse_reply(raw_turn, structured_tool_uses) -> ReplyEnvelope {
    display_text  : clean prose for the UI (JSON fenced; tool-narration stripped)
    snapshot_text : clean prose for memory (no tool-transcript noise)
    tool_calls    : the REAL structured tool_use blocks (validated JSON), executed separately
}
```

**Key principles:**
- **Tool calls come from the provider's structured channel** (Anthropic `tool_use` blocks, etc.), never parsed out of prose. Real tool JSON is validated there (and arg-coercion already normalizes stringified arrays — keep it).
- **Tool *narration in prose* is extracted, not trusted.** If the prose contains `toolname(...) -> {...}` patterns or large raw-JSON result blobs, the envelope strips them from `display_text`/`snapshot_text` (optionally surfacing a compact "the model described a tool call" note) — so a confabulated transcript never pollutes the UI or memory.
- **One parser, all consumers.** The chat stream save (`chat_routes.py` `process_chat_save` / `/chat/save` ~line 3253 & 5903), the snapshot mint, and the UI payload all consume the SAME envelope. Single source of truth for "what the model actually said."

**Files (investigation targets):** `Orchestrator/routes/chat_routes.py` (the per-provider `stream_*_with_thinking` accumulators + the save path), the snapshot mint path, and the UI event contract. New: `Orchestrator/reply_envelope.py` + tests.

**Sequencing:** Phase C is the largest change and touches the live chat path (which is prod). Do it **after** A (symptom gone) and B1 (snapshots self-correct). Design-review the envelope contract before implementing; land it behind a parity test that proves normal turns are byte-identical and only contaminated turns differ.

---

## Open design decisions (need Brandon's call before Phase C build)
1. **Strip vs. fence tool-narration in prose:** when the model narrates a tool as text, do we (a) strip it entirely from display+snapshot, (b) keep it but fenced, or (c) keep in display fenced but strip from snapshot? (Recommend c — humans may want to see it; memory should stay clean.)
2. **How aggressive is JSON detection** in Phase A — only multi-line/large blobs, or any inline `{...}`? (Recommend large/multi-line only, to avoid fencing a stray `{x}` in prose.)
3. **B2 scope** — ship B1 + measure first, or build the retrieval augment now?

## Testing & validation
- A: renderer split unit tests (Android + Portal) + a confabulation-fixture visual check on device.
- B: retrieval fixture test (failure+fix on one topic → both surface); snapshot-dev checklist updated.
- C: envelope unit tests (clean turn unchanged; narrated-tool turn → stripped snapshot, fenced display; real tool_use unaffected) + a golden parity test on the live chat save path.
- Full backend suite green (known pre-existing `test_ollama_keep_alive_passthrough` excepted); Android `assembleDebug`.

## Follow-ups / risks
- Phase C touches the live prod chat path — parity-test before cutover (cf. ToolVault v2 migration discipline).
- JSON-detection heuristics can misfire — keep conservative + test both directions.
- The arg-coercion fix (`270a542`) stays; envelope handles the *prose-narration* class, coercion handles the *stringified-real-arg* class — different layers, both needed.
- **(A2 known cosmetic edge, accepted)** Portal `fenceUnfencedJson` runs before `convertMediaUrlsToHtml`, so a `/ui/uploads/...` URL *inside* a fenced JSON blob gets its `<img>` markup spliced into the code text (HTML-escaped by marked → renders as literal bloated text; no XSS, no correctness impact). Clean fix = fence-aware media conversion (convert only non-fenced segments); deferred — larger than this safety-net warrants.
