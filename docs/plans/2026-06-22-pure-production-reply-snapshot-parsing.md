# Pure-Production Reply/Snapshot Parsing — FINAL Implementation Plan

> **Status:** Implementation-ready plan. Supersedes the *architecture* half of
> `docs/plans/2026-06-22-reply-parsing-and-rendering-hardening.md` (Phase C). That doc's
> Phase A (renderer fences) and Phase B1 (snapshot hygiene) are KEPT as the safety net — see §6.
> **Date:** 2026-06-22. **Scope of this document:** AUDIT + DESIGN synthesis. No code changed.
> **For Claude executing this:** use `superpowers:subagent-driven-development`, parity-test
> before every cutover, preserve per-file line endings on edit.

---

## 1. Problem + root cause (one paragraph)

Today the system **instructs the model** (via `OUTPUT_SPEC` in `Orchestrator/config.py:221-290`) to emit a
single raw JSON envelope `{"ui_reply": "...", "snapshot_perspective": "..."}` — one half for the UI, one
half for memory. Forcing the model to *both generate content AND serialize it* is the antipattern: it breaks
clean token streaming, collides with JSON escaping/markdown fences/triple-nesting, and occasionally leaks raw
JSON into the UI and into the immutable searchable ledger. **The critical, verified scoping fact that reshapes
the whole fix:** this envelope is **NOT a system-wide contract** — it is injected and parsed on **exactly one
path, the non-streaming `/chat` task worker (`Orchestrator/tasks.py:1531/1534/1537` inject, `:1657-1675`
parse).** The live interactive UI (Portal web + Android main chat) runs the **streaming** path, which is
*already* envelope-free, natively channel-separated, and bakes a `[REASONING]/[RESPONSE]` snapshot
(`chat_routes.py:5958`). The on-device Gemma path is already envelope-free. So "pure production" is mostly a
matter of (a) deleting the envelope instruction, (b) migrating the ONE non-stream worker + its consumers to
native parsing, and (c) fixing two real asymmetries (non-stream stores perspective-*only*; local stores
response-*only*) — **without** re-introducing model-authored structure anywhere.

**Verified against source (commands run for this plan):**
- `OUTPUT_SPEC`/`ui_reply`/`snapshot_perspective` appear in `chat_routes.py` **only** inside the dead helper
  `_get_system_prompt` (def `chat_routes.py:151`, refs OUTPUT_SPEC at `:163/:166`) — **zero live call sites**
  (`grep "_get_system_prompt(" chat_routes.py` returns only the def). The live streamers build prompts via
  `build_core_system_prompt()`. ⇒ Deleting the envelope is provably a no-op for streaming, *and* the dead
  helper is a latent re-introduction vector that Phase 1 must delete.
- `call_anthropic/call_openai/call_xai` return `(raw, usage)`; `call_gemini` returns `(raw, usage, media_parts)`
  (`tasks.py:1605/1607/1609/1611`). **None return a reasoning channel.** ⇒ The design's "capture reasoning the
  same way the streamers do" is real provider work, not a parenthetical (resolved in §4 Phase 2).
- The non-stream poison fallback writes literal `"(Response was truncated…)"` / `"(Could not parse…)"` into
  `snap_persp` (`tasks.py:1671/1675`) and `snap_text = snap_persp` (`:1779`) — perspective-ONLY snapshot,
  parse-error sentinels embedded into searchable memory.
- The embedding is generated from the **full rendered body** (`generate_embedding(body)`,
  `checkpoint.py:152/275/363`), truncated at `EMBEDDING_MAX_CHARS=10000` (`embeddings/registry.py:57`,
  `providers.py:61-62`) — NOT from `snap_text` in isolation. ⇒ The memory-curation analysis must reason about
  the *embedded body*, not `snap_text` (resolved in §4 Phase 2 + §5 OD-1).
- The non-stream `/chat` path is **live**: `gemini-live.js:955` and `gpt-realtime.js:1314` both POST
  `streaming:false` to summarize voice transcripts into snapshots. ⇒ "legacy" is wrong; voice snapshots are a
  first-class consumer (resolved in §4 Phase 2).

---

## 2. Target architecture — channel separation (Opus option c), justified

### 2.1 The decision and why the alternatives lose

Three options were on the table: **(a)** collapse to one artifact (snapshot == reply), **(b)** keep two fields
via native provider structured-output, **(c)** channel separation (stream prose; native tool calls; snapshot
server-assembled; optional async memory digest).

**Option (b) is disqualified by two provider-confirmed facts, both fatal:**
1. **Structured-output × streaming conflict (all 5 providers).** Enforcing a JSON schema on the reply makes the
   provider stream JSON *tokens* (`{"ui_reply":"He`, `llo`…), not natural-language prose. This degrades the
   visible token stream (UX) and forces buffer-to-end. *(Honesty correction to the original design's headline
   reason — see §2.2: the TTS-poison argument it leaned on is real in principle but already mooted on this
   codebase by buffer-to-end TTS; the load-bearing reasons against (b) are the visible token stream + the
   terminating-turn tool conflict, not TTS.)*
2. **Structured-output × tool-calling conflict (terminating turn).** Schema enforcement and mid-loop `tool_use`
   are contradictory on the terminating turn — apply a schema only on a final tool-free turn. **Local Gemma is
   hardest:** litertlm allows **only ONE constraint per Conversation** (`enableConversationConstrainedDecoding`
   is a global flag toggled around `createConversation`, `LiteRtEngine.kt:510-527`), so you cannot run the
   native tool loop AND force a final reply schema in the same conversation.

**RECOMMENDATION: channel separation (option c), with snapshot = `[REASONING]+[RESPONSE]` as the DEFAULT
(option a as the zero-cost base case), and an OPTIONAL deterministic/async memory digest only where recall
needs it.** This is the lowest-delta path: the streaming half is the existence proof it works in production.

### 2.2 Honesty correction the original design requires (adversarial: streaming-tts)

The original design repeatedly justified channel separation with *"you cannot feed half-formed JSON to a
real-time synthesizer."* **Verified false for this codebase's TTS:** both surfaces do **post-complete
(buffer-to-end) TTS** — Portal fires a single `/tts/batch` from the `done` handler on the full `fullResponse`
(`chat-send.js:1083-1086`; `:1031-1034` explicitly "We no longer use streaming TTS"); Android emits
`_autoTtsEvent` only after stream end (`ChatViewModel.kt:1933-1936`). The real-time voice path
(gemini_live/grok_live/realtime/twilio/asterisk) is *separate native speech-to-speech* and never consumes the
chat text deltas. **Therefore:** (i) rewrite §2.1's anti-(b) rationale to "visible token stream + terminating-turn
tool conflict," not TTS; (ii) add the permanent invariant: *the reply channel MUST always be raw prose so that
IF sentence-level streaming TTS (`StreamingTTSQueue`, `tts-stt.js:86-218`) is ever re-enabled, it is already
safe.* Channel separation guarantees this regardless.

### 2.3 The target contract (one design, five providers)

```
REPLY     = plain prose, streamed on the provider's native text channel. NO schema. NO envelope.
THINKING  = provider's native reasoning channel (summarized), streamed as `thinking` SSE events.
TOOLS     = native function calling; args on the provider's own tool channel (never the text channel).
SNAPSHOT  = server-assembled. snap_text = "[REASONING]\n{thinking}\n\n[RESPONSE]\n{reply}"
            — but the [REASONING] header is OMITTED ENTIRELY when reasoning is blank (no empty header
            ever embedded, on ANY path). The MODEL authors NEITHER field. (Streaming already does this.)
MEMORY+   = curated recall aid (keywords/digest). DEFAULT: deterministic server-side extraction (no LLM,
            no model-authored content). OPTIONAL escalation: async schema-enforced summarizer on a
            SEPARATE, tool-free, non-streamed call (tools=[]). Never touches the reply stream.
SPEAKABLE = client-side sanitizer applied to the text handed to TTS (strip [ARTIFACT] blocks, bare
            /ui/uploads URLs, fenced code/JSON, and unwrap a whole-string envelope) — see §3.5.
```

### 2.4 Per-provider plan (each adversarial hole resolved inline)

- **Anthropic.** Streaming already sets `thinking:{type:"adaptive",display:"summarized"}`
  (`chat_routes.py:2393-2395`) — the original design mis-stated this as pending; it is DONE on the streamer.
  **Remaining Anthropic work is the NON-stream `call_anthropic` only** (§4 Phase 2): parse the `thinking`
  content block from the non-stream response so the snapshot `[REASONING]` is non-empty. The "don't replay
  native thinking blocks" rule governs passing encrypted blocks back in `messages[]`; storing *summarized
  thinking text* in a snapshot is unaffected (Brandon's stateless TEXT rebuild never replays native blocks).
  *Resolves provider-parity hole #7 (already-shipped mischaracterization) + hole #4 (non-stream reasoning gap).*
- **OpenAI.** Drop any reply `response_format`. **Migrate `stream_openai_with_reasoning` + `call_openai` from
  Chat Completions (`config.py:396`) to the Responses API** to get streamable
  `response.reasoning_summary_text.delta` (reverses the stale "GPT-5 exposes no thinking" assumption at
  `chat_routes.py:1712-1718`); set `reasoning={"effort":…,"summary":"auto"}`; add `"strict":true` to every
  tool's `parameters`. **This is a TOOL-LOOP REWRITE, not just reasoning wiring** — Responses uses
  `function_call`/`function_call_output` items, not Chat-Completions `tool_calls`. Phase 3's checklist MUST:
  (1) port the catch-all `else:` branch into the new loop; (2) `json.loads` the function-call arguments string,
  then pass a **dict** to `BlackBoxToolExecutor` so `_coerce_stringified_json_args` still fires; (3) thread
  results via `function_call_output`; (4) enforce a **channel-mapping contract**:
  `response.reasoning_summary_text.delta` → `thinking` ONLY, `response.output_text.delta` → `content` ONLY
  (assert zero reasoning text in the content buffer). *Resolves tool-interaction holes #3 (catch-all/coercion
  rewrite) and streaming-tts hole #3 (reasoning leaking into content → TTS reads reasoning aloud).*
- **xAI.** Maps perfectly onto what `stream_xai_with_reasoning` already does (`delta.content` reply,
  `delta.reasoning_content` thinking, `delta.tool_calls[]` args). No `response_format` sent today; removing the
  envelope is a prompt edit. Strict schema = Grok-4 family only; resolve ids from `GET /models`, keep substring
  matching. Non-stream `call_xai` reasoning capture follows the same Phase-2 pattern.
- **Gemini.** Keep `streamGenerateContent?alt=sse` with `thinkingConfig.includeThoughts=true` and **NO**
  `responseMimeType`/`responseSchema` (schema-mode streams partial JSON → kills the token stream). **Preserve
  `thoughtSignature` replay** (re-append `response_parts`, streaming `L4402-4410`) — any refactor that strips
  response_parts breaks stateless function-calling. **Audit the NON-stream `call_gemini` tool-result threading
  (`chat_routes.py:948-1485`) for thoughtSignature preservation BEFORE enabling reasoning-capture there**, and
  add a 2-3-step non-stream Gemini tool-chain acceptance test. Do not mix `thinkingBudget`+`thinkingLevel`
  (400 on Gemini 3); do not copy Interactions-API field names (codebase is 100% classic `generateContent`).
  *Resolves tool-interaction hole #5 (non-stream thoughtSignature).*
- **Local Gemma (NO reply schema).** **Demote to a GATED SPIKE, not a planned change** — the original design's
  `message.channels["thought"]` + `extraContext=mapOf("enable_thinking" to "true")` APIs **do not exist in the
  tree** (grep of `app/src/main` returns ZERO `channels`/`"thought"`/`enable_thinking`; the only extractor
  `Message.plainText()` hard-filters `Content.Text` and drops non-text, `LiteRtEngine.kt:949-950`; `LlmEvent`
  has only TextDelta/ToolCall/ToolOutcome — no Thinking variant; native callback passes `emptyMap()` as
  extraContext, `:546`). **Hard precondition before any local-thinking work:** a device probe must prove
  litertlm 0.13.1 exposes a thought channel **separable** from `Content.Text` AND that enabling it does not
  interleave thought tokens into `plainText()`. If EITHER fails, the documented local contract is
  **snapshot == reply (no `[REASONING]`)**, default thinking OFF — because turning thinking on without a
  separable channel would route chain-of-thought through `plainText()` → into the UI bubble AND the immutable
  ledger (`text==snap_text==assistant_response`, `chat_routes.py:3366`) with no parser to strip it, on the
  weakest model. *Resolves provider-parity holes #1, #2 (local thinking fiction + re-leak risk).*

### 2.5 The `snapshot_perspective` "Keywords:" line — DECIDE UP FRONT, do not defer

The `Keywords:` line (`config.py:229`: kebab-case entities/IDs/aliases) is the single highest-recall artifact
the model authors — and the embedding is generated from the **full body** (verified §1), truncated at 10K. The
original design's "drop it, zero cost; thresholds already tuned for full-text" is **wrong twice**: (1) the
per-model `semantic_threshold` (gemini 0.60 / qwen 0.54) was tuned on the *existing* corpus that *contains*
Keywords, so it is not evidence keyword-free bodies retrieve as well; (2) **snapshots are immutable** — a window
of un-curated minting permanently poisons memory before regression is even measurable.

**RECOMMENDATION (resolves all memory-curation holes):**
1. **Commit the keyword strategy in the SAME phase that removes `snapshot_perspective`** — never ship a window
   that mints bodies without curated keywords.
2. **Default = deterministic server-side keyword extraction** (RAKE/YAKE/TF-IDF over the response text — no LLM,
   no model-authored content, no cost/latency/race). Append it to the embedded body **response-first,
   keywords-second, reasoning LAST** so 10K truncation eats *reasoning*, never the answer/keywords.
3. **Exclude verbose `[REASONING]` from the embedding INPUT** (store it in the body for context-rebuild/human
   use, but embed a reasoning-free digest — or compose a separate digest string for `generate_embedding`).
4. **Validate by A/B recall eval BEFORE cutover** (replay a sample of real queries against perspective-bodies
   vs new digest-bodies; compare hit@k at current thresholds). Add a truncation-hit log line so silent 10K
   drops become observable. Re-tune `semantic_threshold` if the eval shows drift.
5. **OPTIONAL escalation only if the deterministic digest under-performs:** async LLM summarizer
   (Haiku 4.5 / small Responses call), schema-enforced `{summary, keywords[]}`, **distinct code path with
   `tools=[]`** (never reuse `call_*`, which unconditionally attach `_get_tools` — a schema+tools request 400s
   or emits a tool_call instead of the summary), computed BEFORE `generate_embedding(body)` (never embed-then-
   patch), with a hard fallback (on summarizer failure, embed the deterministic keyword digest). *Resolves
   tool-interaction hole #4 (summarizer must not carry tools).*

Do NOT make the live model emit the memory field inline — that re-creates the envelope.

---

## 3. Preservation contract (must NOT break — falsifiable invariants)

### 3.1 Tool calling
Tool args travel on each provider's native channel (`input_json_delta`/`delta.tool_calls[]`/`functionCall`/
`msg.toolCalls`), parsed by existing accumulators, never via the envelope. The arg-coercion fix
(`_coerce_stringified_json_args`, `blackbox_tools.py:47`) lives inside `BlackBoxToolExecutor.execute()` (`:121`)
and fires for every catch-all ToolVault tool on every provider — **unaffected by envelope removal.** **Invariant:**
never enforce a final-reply JSON schema on a turn that may emit a tool call. 30-iteration tool loops stay as-is.
**Phase-1 hazard:** `OUTPUT_SPEC_CORE` Rules 6-9 + the "you MUST call the tool" imperative are interleaved with
the envelope in the same string literal (`config.py:276-289`); deleting the envelope must **RELOCATE** those
tool rules into `CORE_SYSTEM_PROMPT` (which today only warns about *over*-using toolvault search) BEFORE
deleting — otherwise weaker models regress to *describing* actions instead of calling tools. *Resolves
tool-interaction hole #2.*

### 3.2 Media placeholders — `image_task` / `video_task` / `music_task` (the CRITICAL non-stream gap)
**Streaming path (already correct):** SSE events `{type:"image_task"|"video_task"|"music_task",
data:{task_id, prompt, …, predicted_url?}}` are emitted by the streamers (`chat_routes.py:1909/1976/2035/…`),
triggered by tool calls. Portal (`chat-send.js:1159-1200` → `taskManager.addTask` → polls `/tasks/status/{id}`)
and Android (`ChatViewModel.kt:2063-2466`) both consume them. **`predicted_url` is OPTIONAL** (present on
anthropic/openai emit sites `:1910/:5184`, absent on gemini/secondary `:2622/:4497`); `/tasks/status` polling is
the authoritative resolver — never let cleanup remove the polling fallback. **Migration rule:** keep the full
SSE event-name set verbatim (`thinking/thinking_start/thinking_end, content/content_start, tool_start/
tool_result, image_task/video_task/music_task, image, usage, done`).

**CRITICAL non-stream hole (must be fixed IN Phase 2, before Phase 1 ships):** on the non-stream `/chat` path
there is NO SSE channel. The ONLY renderable placeholder today comes from the **text-regex parser**
(`tasks.py:1694-1751`) matching `generate_image:` colon-syntax in `ui_reply` and substituting a
`<div class="image-loading-placeholder" data-task-id=...>` that Portal polls (`chat-send.js:2472-2478`). But the
non-stream `call_*` **native** tool loops (`call_anthropic:525-548` etc.) create the task and return only a
**predicted-URL text** tool_result with NO `data-task-id` div — and `OUTPUT_SPEC` Rule 7 *tells the model to
call the tool natively*, which makes the regex unreachable. So **two parallel mechanisms exist and only the
non-native one renders.** Phase 2's "route non-stream through native tool calling" would make the broken native
path the default → media silently never appears (affects scheduler/SMS/MCP/voice-triggered generation).
**Resolution (pick ONE, implement IN Phase 2):**
- **(A) PREFERRED — unify on a structured contract:** non-stream worker accumulates created
  `(task_id, type, predicted_url)` from the native media branches and emits a `result_data["media_tasks"]`
  array; Portal (and, as a follow-up, Android) start polling from it. OR
- **(B) Bridge to the existing placeholder:** the native media branches inject the exact
  `image-loading-placeholder` div (reuse strings at `tasks.py:1712/1730/1749`) into `ui_reply` keyed on
  `task_id`, so `chat-send.js:2475` polling fires unchanged.
- **Do NOT** rely on the colon-regex (Rule 7 makes it unreachable) and do NOT leave it half-removed.
- **Acceptance test:** trigger `generate_image` via non-stream `POST /chat` → assert a pollable placeholder /
  `media_tasks` entry exists, NOT a bare predicted URL. *Resolves media holes #1, #2.*

### 3.3 `[ARTIFACT:...]` downloads
`parse_and_process_artifacts` (`artifacts.py:163-212`) is text-based, NOT envelope-coupled, runs on the
extracted reply at 3 sites (`tasks.py:1756`, `chat_routes.py:5952`, `admin_routes.py:977`) — survives envelope
removal untouched. **Known pre-existing 3-surface gaps (enumerate; do NOT introduce new ones):**
(1) Portal streaming works (re-renders from `modified_response`, `chat-send.js:2214`); (2) Portal non-stream
works (`parse_and_process_artifacts` mutates `ui_reply` in place); (3) **Android streaming** never consumes
`modified_response`; (4) **Android non-stream** has no Kotlin `[ARTIFACT]` handler. So (1)(2) work, (3)(4) show
bracket text. **Follow-up (its own phase, §4 Phase 6):** the cleanest fix is moving artifact creation to a
**ToolVault tool** so it rides the native tool channel like media — BUT validate that on local Gemma first
(tool reliability is weakest there; the text-regex path is the cross-provider floor). Until validated, keep the
text-regex path AND teach Android to consume `modified_response` (lower-risk). *Resolves media holes #4 +
provider-parity hole #8.*

### 3.4 Non-stream `result_data` contract (the real backward-compat floor)
Keep `result_data["ui_reply"]`, `["reply"]`, `["text"]` populated with the FINAL human text (post media/artifact
substitution). Consumers that break otherwise: **Portal** (`chat-send.js:2438`, has a JSON-unwrap band-aid),
**Android Overlay** (`OverlayService.kt:3018`, no unwrap), **Scheduler** (`executor.py:340`, **RAISES
RuntimeError** if all absent → jobs hard-fail), **SMS** (`sms/router.py:281`, silent `''`), **MCP**
(`blackbox_mcp_server.py:576`, "No response"), **AND the two voice-save paths** (`gemini-live.js:944`,
`gpt-realtime.js:1301`, POST `streaming:false`). Keep `snapshot_perspective` as a populated back-compat key
(= reasoning summary or empty) so nothing index-errors. *Resolves migration hole #1 (voice consumers).*

### 3.5 Speakable-text sanitizer (CRITICAL — live TTS-poison the design missed)
TTS is fed **raw `fullResponse`** (`triggerAutoTTS(fullResponse,…)`, `chat-send.js:1085`) BEFORE artifact
processing; `extractSpeakableText` (`tts-stt.js:1033-1046`) only DOM-strips HTML media elements, so
`[ARTIFACT:report.pdf:pdf]…[/ARTIFACT]` and any leaked envelope string are **read aloud verbatim** on the
surface where artifacts "work." **Add a client-side speakable-text hardening step (both surfaces), applied to
the text handed to TTS:** strip `[ARTIFACT:…]…[/ARTIFACT]`, strip bare `/ui/uploads/*` URLs, strip fenced
code/JSON blocks (replace with "code block"/"see screen"), unwrap a whole-string `{ui_reply,…}` envelope —
BEFORE `/tts/batch`. Better: pass the **finalized (post-`modified_response`) bubble text** to TTS, not the raw
accumulator. Mirror in Android's `_autoTtsEvent` emit. **This is a streaming-path preservation invariant, not a
follow-up.** *Resolves streaming-tts holes #2 (artifact audio poison) + #5 (legit-JSON read aloud).*

### 3.6 Local Gemma media path (provider-parity hole #6)
Document how a cloud-tool `generate_image` triggered by on-device Gemma surfaces a placeholder in
`FcLoop`/`ChatViewModel.streamLocalAgentTurn` (it has no SSE `image_task` and no `parse_and_process_artifacts`),
and whether `[ARTIFACT]` from a local turn is handled or explicitly out of scope. Add an acceptance test:
local Gemma turn calling a cloud media tool renders the result on-device. Do not ship Phase 4 (local thinking)
without re-verifying local media still renders.

---

## 4. Phased tasks (sequenced so the live prod chat path is never broken)

**Guiding principle:** make non-stream and local **converge on the streaming target**, not the reverse. Ship
independent, individually-revertable phases. Preserve per-file line endings (`config.py`, `tasks.py`).

### Phase 0 — Belt-and-suspenders shims (no behavior change; ship first) — LOW risk
- **0a.** Add a server-side unwrap shim in `chat_save` (`chat_routes.py:5965`): if `assistant_response` parses
  as `{"ui_reply":…}` *(after stripping ```json fences and attempting one nested-parse — the original shim only
  caught the exact shape; Brandon's pain is fenced/triple-nested)*, unwrap before building `snap_text`. Closes L5.
- **0b. (the design omitted this — the worst leaks are non-stream)** Add the SAME defensive unwrap to the
  non-stream parser at `tasks.py:1657` AND to the local persist path `persist_local_turn_and_mint`
  (`chat_routes.py:3342-3367`, currently zero parse). On parse failure: **store reply as-is; NEVER write a
  parse-error sentinel** (`"(Could not parse…)"`) into `snap_text`. This closes the HIGH non-stream leaks
  (L1/L2/L3) and the local re-leak risk *now*, making later phases clean simplifications.
- **Tests:** feed a fenced envelope, a triple-nested envelope, and freeform JSON to each shim → assert clean
  unwrap or store-as-is, never a sentinel embedded.
- **Rollback:** pure addition; revert the shim functions. No contract change.
- *Resolves migration holes #4 (shim mis-scoped) + provider-parity hole #6 (shim blind to fences/local).*

### Phase 1 — Delete the envelope instruction + dead helper (prompt + dead-code only)
- Relocate `OUTPUT_SPEC_CORE` tool Rules 6-9 + the "you MUST call the tool" imperative into
  `CORE_SYSTEM_PROMPT` (§3.1) **before** removing the `{ui_reply, snapshot_perspective}` block + Rules 1-5/10
  from `OUTPUT_SPEC_CORE` (`config.py:221-290`).
- Remove `GM_FORMAT_RULES` envelope restatement (`tasks.py:1938-1946`).
- **Delete the dead `_get_system_prompt` helper** (`chat_routes.py:151-170`) + its unused `_inject_cache`
  "instructions" wiring — it re-derives OUTPUT_SPEC and is a re-introduction vector.
- **Streaming = verified no-op.** **Coupling note:** Phase 1 may ship the *streaming* prompt cleanup alone, but
  the non-stream prompt delete is **atomically coupled to Phase 2** (see Phase 2 — a Phase 1 that deletes the
  non-stream spec without the parser rewrite would make `json.loads` fail on prose and embed
  `"(Could not parse…)"` into memory during the window). **For the non-stream path, treat Phase 1's spec-delete
  and Phase 2's parser-rewrite as ONE commit.** *Resolves migration hole #3 (ordering poison) + #5 (dead
  helper) + tool-interaction hole #2 (rule relocation).*
- **Tests:** (1) diff rendered system prompt for a `/chat/stream` request pre/post → identical; (2) diff
  rendered NON-stream prompt pre/post → tool-imperative survives; (3) post a media request on BOTH paths →
  native `tool_call` emitted, not prose describing it.
- **Rollback:** revert config/tasks edits; restore helper.

### Phase 2 — Rewrite the non-stream worker (the only risky surface; ATOMIC with Phase-1 non-stream delete)
- Stop injecting `dynamic_spec`/OUTPUT_SPEC (`tasks.py:1531/1534/1537`). Replace the `json.loads` parse
  (`:1657-1675`) with `ui_reply = raw` (prose is the reply).
- **Capture native reasoning — REAL provider work (verified: `call_*` return no reasoning):** extend each
  non-stream `call_*` to return a third reasoning element parsed from the provider response (Anthropic thinking
  block; OpenAI reasoning summary — see Phase 3; Gemini thought parts; xAI `reasoning_content`), and unpack it
  in `process_chat_task` (`tasks.py:1605-1611`). Where a provider's reasoning is unavailable (e.g. OpenAI
  pre-Phase-3), set `snap_text = assistant_response` (option-a) with **NO empty `[REASONING]` header** — never
  embed `[REASONING]\n\n[RESPONSE]`. Add per-provider acceptance tests asserting `[REASONING]` is non-empty when
  the model thought. *Resolves migration hole #2 + provider-parity holes #4, #5.*
- **Switch `snap_text` from `snap_persp` to the streaming shape** `[REASONING]\n{reasoning}\n\n[RESPONSE]\n
  {ui_reply}` (`tasks.py:1779`) — fixes the answer-blind snapshot (L3). Compose the embedded body
  response-first/keywords-second/reasoning-last per §2.5; embed a reasoning-free digest.
- **Resolve the media-trigger gap (§3.2) IN this phase** — implement option (A) `media_tasks` array (preferred)
  so non-stream native media renders + polls.
- **Audit `call_gemini` thoughtSignature threading** (`chat_routes.py:948-1485`) before enabling reasoning-
  capture there; add a 2-3-step non-stream Gemini tool-chain test (§2.4).
- Keep `result_data["ui_reply"/"reply"/"text"]` populated; keep `snapshot_perspective` back-compat key.
- **Voice-save (§3.4):** decide explicitly — keep the OPTIONAL async digest (§2.5.5) ON for voice-transcript
  saves to preserve searchable quality, OR accept raw-summary snapshots. Add `gemini-live.js`/`gpt-realtime.js`
  to acceptance test #2.
- **Tests (parity-gated before cutover):** scheduled job + SMS + MCP `chat` + Android overlay poll + both
  voice-save paths → all return non-empty reply; generate_image non-stream → placeholder/`media_tasks`; Gemini
  2-3-step tool chain non-stream → completes.
- **Rollback:** the non-stream worker is one function; revert restores `snap_persp` + json parse. Existing
  snapshots untouched (immutable text; pre-Phase-2 perspective-only bodies are a pre-existing recall weakness,
  not a regression).

### Phase 3 — OpenAI Responses API migration (separate, gated; largest blast radius)
- Migrate `stream_openai_with_reasoning` + `call_openai` to `/v1/responses`. **Checklist (§2.4):** port the
  catch-all `else:` branch; `json.loads` function-call args → dict → `BlackBoxToolExecutor` (coercion fires);
  thread `function_call_output`; wire `response.reasoning_summary_text.delta` → `thinking` events; add
  `strict:true` to tool params.
- **Channel-mapping gate (acceptance test, MUST pass before ship):** reasoning summary maps ONLY to `thinking`,
  `output_text.delta` ONLY to `content`; assert the content buffer / `fullResponse` contains ZERO reasoning text
  (else TTS reads reasoning aloud). Re-run the full ToolVault matrix on OpenAI (image/video/music + a
  stringified-array tool to exercise coercion; reuse `test_arg_coercion.py`).
- **Interim parity note:** between Phase 2 and Phase 3, OpenAI snapshots are **response-only by design** (no
  `[REASONING]` header emitted). Document this; the §2.3 omit-empty-header rule keeps it clean. *Resolves
  provider-parity hole #5 + tool-interaction hole #3 + streaming-tts hole #3.*
- **Rollback:** Responses migration is isolated to the OpenAI streamer/caller; revert to Chat Completions.

### Phase 4 — Local Gemma thinking capture (GATED SPIKE — precondition first)
- **Precondition device probe (go/no-go):** confirm litertlm 0.13.1 exposes a thought channel separable from
  `Content.Text` AND that enabling it does not interleave thought tokens into `plainText()`. **If either fails:
  STOP** — documented local contract is `snap_text = assistant_response` (option-a), thinking OFF; keep the
  existing `[TOOLS USED]` block as provenance.
- **If both pass:** add a 4th `LlmEvent` (Thinking), route it to a separate stream + the snapshot
  (`reasoning = collected thought`, `assistantResponse = reply` in `buildSaveRequest`, `ChatViewModel.kt`
  ~3477/3683); thread `enable_thinking` via `extraContext` (currently `emptyMap()`).
- **Snapshot-shape parity (§3.6, provider-parity hole #3):** pick ONE local body shape — send the real
  structured `tool_transcript` (wire `ChatViewModel.kt:1026` from `emptyList()` to the actual `ToolOutcome`
  list) so the server composes a clean `[TOOLS USED]` block, and STRIP inline tool-render lines
  (`renderToolOutcome`, `:3051`) from the snapshot body (keep them for the live UI only). Do not claim
  uniformity the code can't deliver.
- **Do NOT block the snapshot-unification goal on a litertlm version bump.**
- **Tests:** local turn → snapshot has both `[REASONING]` (from thought channel) and `[RESPONSE]`; local media
  tool renders on-device; local recall validated separately (noisier 4B thought channel).
- **Rollback:** local-thinking flag defaults OFF; Android ships additively (3-surface rule).

### Phase 5 — Cleanup (after 0-2 validated)
- Phase 0's non-stream shim makes the L1/L2/L3 fallback dead; remove the parse-error sentinels.
- **KEEP the renderer band-aids permanently** (`fenceUnfencedJson` `markdown-renderer.js:161`,
  `extractJsonBlocks` `MarkdownText.kt:126`) — they are cheap, cover the legitimate "user asked for JSON" case,
  and keep a leaked-envelope reply legible. Do NOT gate any media behavior on their removal. *Resolves media
  hole #5 (keep band-aids) + streaming-tts hole #1 (do not assume streaming TTS protection).*
- Portal's `pickReplyFromAny` JSON-unwrap branch may be removed after a soak, or kept as belt-and-suspenders.

### Phase 6 — `[ARTIFACT]` 3-surface convergence (follow-up; its own phase)
- Either teach Android to consume `modified_response` (streaming) + add a Kotlin `[ARTIFACT]` handler
  (non-stream), OR move artifact creation to a ToolVault tool — but **validate artifact-as-tool on local Gemma
  before deprecating the text-regex floor** (§3.3). Acceptance tests for `[ARTIFACT]` on all four surfaces
  (Portal stream/non-stream, Android stream/non-stream). *Resolves media hole #4 + provider-parity hole #8.*

### Backward-compat & existing-snapshot safety
Existing snapshots are immutable text; the stateless rebuild reads them regardless of transport. No client app
ships in lockstep with Phases 1-2 (`result_data` keys preserved). Android updates are needed only for Phase 4
(local thinking) and Phase 6 (`[ARTIFACT]`) — both additive. Memory poisoning is avoided by committing the
keyword strategy IN Phase 2 (§2.5), not deferring it.

---

## 5. OPEN DECISIONS for Brandon (with recommendation)

- **OD-1 — Memory digest strategy.** Deterministic server-side keyword extraction (default, no LLM) vs. async
  schema-enforced summarizer vs. keep snapshot==reply verbatim. **Recommend: deterministic keywords as default
  (zero model-authored content, satisfies your "pure production" bar), escalate to async summarizer ONLY if the
  pre-cutover A/B recall eval regresses.** Decide before Phase 2.
- **OD-2 — Voice-save snapshot quality.** Keep the async digest ON for voice-transcript saves (preserve
  searchable quality) vs. accept raw-summary bodies. **Recommend: digest ON for voice** — voice/diarization is
  a proven high-value memory source and these are summarization turns, not interactive ones.
- **OD-3 — OpenAI Responses migration timing.** Ship Phase 3 now (parity sooner) vs. defer (accept a window of
  response-only OpenAI snapshots). **Recommend: defer** — it's the largest blast radius; ship behind its
  channel-mapping + ToolVault-matrix gate, after 0-2 are live and stable.
- **OD-4 — Local Gemma thinking.** Block on the litertlm probe vs. accept snapshot==reply for local
  permanently. **Recommend: run the probe; if it requires a runtime bump, accept snapshot==reply for now** and
  treat thinking as an independent follow-up — do not couple the unification goal to an on-device dependency
  bump.
- **OD-5 — `[ARTIFACT]` long-term.** Text-regex floor + teach Android to consume `modified_response` vs. move
  to a ToolVault tool. **Recommend: keep text-regex floor short-term (lower risk, cross-provider), evaluate
  artifact-as-tool only after local-Gemma tool reliability is proven.**

---

## 6. Salvage vs. rewrite — `docs/plans/2026-06-22-reply-parsing-and-rendering-hardening.md`

- **KEEP (salvage, already shipped / still valid):**
  - **Phase A (renderer fences)** — `extractJsonBlocks`/`fenceUnfencedJson` SHIPPED 2026-06-22. Keep
    **permanently** as the display safety net (§Phase 5). It is display-only and never sanitizes what gets
    embedded — that's now Phase 0's job.
  - **Phase B1 (snapshot hygiene checklist)** — resolved snapshots referencing their superseding id. Cheap,
    high-leverage, orthogonal to this work. Keep.
  - **Phase B2 (retrieval surface resolutions-with-problems)** — still valid as a conservative tie-break;
    lower priority once clean bodies land. Keep as a measured follow-up.
- **REWRITE / DROP:**
  - **Phase C (the `ReplyEnvelope` prose-parser)** — **DROP.** Its contract
    `parse_reply(raw_turn, structured_tool_uses) -> {display_text, snapshot_text, tool_calls}` **parses PROSE to
    strip tool-narration**, which is the *opposite* of channel separation and would add a parsing layer to the
    live streaming path (which currently does zero reply parsing). The dual-envelope discovery shows the durable
    fix is *stop the model authoring structure at all + server-compose the snapshot* — i.e. this plan's
    Phases 0-2 — not a prose parser. The "confabulated tool-narration in prose" problem Phase C targeted is a
    *model-behavior/retrieval* issue (Phase B) plus the renderer band-aids (Phase A); real tool calls already
    never enter the text channel, so there is nothing to "parse out."
  - **Open decision #1 in that doc (strip vs. fence tool-narration)** — moot once Phase C is dropped.

---

## 7. Effort + risk (honest)

| Phase | Effort | Risk | Notes |
|---|---|---|---|
| 0 — shims | S | LOW | Pure addition; closes the HIGH non-stream/local leaks immediately. Ship first. |
| 1 — delete envelope + dead helper | S | LOW (stream) / coupled (non-stream) | Stream = no-op; non-stream spec-delete is atomic with Phase 2. |
| 2 — non-stream worker | **L** | **MED-HIGH** | Real per-provider reasoning capture + media-task contract + voice-save + keyword strategy. The one genuinely risky surface. Parity-test before cutover. |
| 3 — OpenAI Responses | **L** | **HIGH** | Tool-loop rewrite; gate on channel-mapping + ToolVault matrix. Defer (OD-3). |
| 4 — local thinking | M | MED | Gated SPIKE; may resolve to "no-op + document snapshot==reply." |
| 5 — cleanup | S | LOW | Keep band-aids; remove sentinels only. |
| 6 — [ARTIFACT] 3-surface | M | MED | Follow-up; validate artifact-as-tool on local before deprecating regex. |

**Bottom line:** "pure production" is ~80% already shipped on the streaming path. The irreducible work is
Phase 2 (non-stream convergence — including the previously-missed media-task gap, voice-save consumers, and a
committed keyword strategy) plus the Phase 0 shims that make the cutover safe. Phase 3 is the only large
optional piece and should be gated and deferred. Every CRITICAL adversarial hole is resolved by a concrete
phase task above; nothing is left to "fix later" that would poison the immutable ledger in the meantime.
