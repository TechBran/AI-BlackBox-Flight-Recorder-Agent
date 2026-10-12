# The Signal — Live System-Telemetry Stream (Design)

**Date:** 2026-07-13
**Status:** Design validated + aesthetic approved via interactive prototype (v3, 2026-07-13) — ready for implementation planning
**Working name:** "The Signal" *(rename candidates: Flight Feed · Live Wire · Telemetry)*
**Supersedes:** the pseudo "thinking bubble" cycling-phrase animation (one of the oldest components in the system)

---

## 1. One-line summary

Replace the fake cycling "Thinking…/Consulting the AI oracle" phrases with a **live, ephemeral HUD of honest system telemetry** — a **single red line** that **waves and morphs** through each stage of what the exosuit is *actually* doing this turn (embed · search · rank · tools · generate · mint), riding in the **foreground over a switchable particle field** (Rising Stars · Embers · Matrix). The line is **presentation-only** and dissolves when the turn ends. The real model-reasoning dropdown is left untouched.

---

## 2. Why (the core finding that shaped this)

Every client secretly runs **two** "thinking" surfaces that never talk to each other:

| Surface | Source | Verdict |
|---|---|---|
| The animated bubble (`ThinkingIndicator.kt` / `.bubble.thinking`) — pulsing dots + cycling phrases | Hardcoded phrase lists on a 2.5 s timer | **100% fake** — this is what we kill |
| The collapsible "Thinking ▸" dropdown (`ChatBubble.kt` L188 / `.thinking-content`) | Real model reasoning tokens streamed live | **100% real** — left as-is |

The lively animation showed **fake words** while the real words hid in a dropdown nobody opens. The old bubble "reads as a gimmick, not a thought stream." Rather than animate the model's reasoning (which is provider-conditional and often empty — e.g. GPT-5.1 and non-mini Grok emit nothing), we surface something *always* honest and *always* present: **what the BlackBox itself is doing.** That is uniquely on-brand — this product is a **flight recorder**, and now it shows its own black-box telemetry live.

---

## 3. Locked decisions (from the brainstorm)

1. **Content = honest system telemetry**, not model reasoning. The real reasoning dropdown stays.
2. **Voice = raw flight-recorder HUD** — real metrics, model slugs, counts, IDs (e.g. `embed · gemini-embedding-2 · 3072d`). A "simple/plain-language" density toggle is a possible later add.
3. **Tools included in the live window.** Tool activity is the richest honest telemetry and is *already emitted* by the backend (see §7) — just not yet displayed.
4. **Form = a single morphing line.** One red HUD line at center-stage that **waves continuously** and **morphs into the next telemetry string** — a left-to-right, wave-swept character swap where **only the characters that actually change animate** (shared scaffolding like `· `, digits, and model prefixes holds steady). Not a multi-line stack. Two clean layers: the red line in front, the particle field behind — **not** made of particles.
5. **Background = a switchable multi-mode particle field.** Three selectable modes: **Rising Stars** (today's field, renamed), **Embers** (new — realistic: curl-noise swirl + blackbody color ramp + additive bloom + heat-persistence smear), and **Matrix** (new — digital rain). Responsive density scaled by *logical* screen area (capped). Governed by the existing backdrop setting. *(Supersedes the earlier "single lava quality-revamp, not a mode-switcher" framing — it is a mode roster, extensible with more variants.)*
6. **Motion = morph + wave, ephemeral.** The line waves continuously; each new stage morphs in via the wave-swept decode; a **minimum dwell per line** keeps it legible (retrieval fires fast; prefill/generation is the real dead air). At turn end the line dissolves to nothing.
7. **Ephemeral / live-only.** When the turn ends, the words dissolve and nothing is kept. **No flight-log, no data-model changes, no persistence.**
8. **HARD RULE — never in the prompt.** Telemetry is a UI-side presentation stream. It is **never** injected into the LLM context and **never** written into a snapshot/the ledger. Zero token cost, zero retrieval pollution. Everything real is already searchable via the existing snapshot system.
9. **Real values, read live.** No hardcoded corpus numbers. The feed reads live state at emit time (`store.count`, active embed model, dims). *(As of 2026-07-13 the live box reports `gemini-embedding-2`, 3072-dim, **8,005** snapshots in the active schema-2 store.)*
10. **Tiering — Android is the crown jewel; web + WebView are a deliberately basic tier.** Android gets the full-fidelity `Canvas` lava + smooth waving text; web gets a lighter free-floating word layer over the existing particle canvas.

---

## 4. The experience (a real turn)

For *"search my memory for the audio work and summarize it"* the field warms and red words condense low-center, hold, wave, and rise:

```
brain · claude-sonnet-4-5 · 840k window
embed · gemini-embedding-2 · 3072d
search · 8,005 snapshots → 22 cleared floor
RRF · 22 candidates · recency-weighted
MMR · top-8 selected
context · 8 memories · 42k tokens · fits window
tool · search_snapshots · ok
generating · claude-sonnet-4-5
        ┄ answer streams in ┄
mint · SNAP-20260713-8347 · 3072d     (after /chat/save)
        ┄ words dissolve into the embers ┄
```

- **No rerank line** — the reranker is dark on this box (M11 awaiting GPU), so we *skip* it rather than fake a no-op.
- **No fabrication.** Stages that don't run don't appear. Silent providers still show real *system* stages (embed/search/tools/generate) even when they emit no reasoning — that's the whole point.
- **After the turn:** the feed goes quiet and clears. Nothing to expand, nothing stored.

---

## 5. Visual design

*Approved via interactive prototype v3 (scratchpad `the-signal-prototype.html`). The prototype is the visual reference of record; the tuning-dock defaults below are the starting values for the native build.*

### 5.1 The word layer (foreground) — a single morphing line
- **One line, centered.** Red HUD text, monospace (authentic to the `label · metric · metric` cockpit readout), on the dark chat backdrop. No bubble, no outline, no banner.
- **Continuous wave.** Each character carries a per-character sine `translateY` offset (a travelling wave across the line): `y = amp · sin(phase − i·k)`. Default `amp ≈ 5px`, `speed ≈ 1.1`.
- **Morph, not retype.** On each new stage, diff old vs. new string **per character position**; only positions that differ animate — a left-to-right staggered swap (fade-through-zero + a brief scramble/decode tick + settle), stagger ≈ `clamp(520/len, 11, 34) ms`. Shared characters never move, so the meaning flows while the scaffolding holds.
  - *Open sub-choice for the plan:* keep the scramble/decode tick, or a cleaner pure fade-morph (decide during native build; trivially toggled).
- **Self-pacing.** Min dwell per line so fast back-to-back retrieval stages stay readable.
- **Ephemeral.** At turn end the line dissolves to nothing.

### 5.2 The particle field (background) — three modes
One engine, three draw routines; selectable via the existing backdrop setting; responsive density by logical screen area (see Appendix A for full parameters).
- **Rising Stars** (rename today's field): 3 parallax depth layers, power-skewed sizes (mostly tiny, few bright), **de-synced** twinkle (random per-star phase/speed — no lockstep), glow sprite only on the hero ~10%. Crisp points (clear each frame).
- **Embers** (new, realistic): **curl-noise** swirl (divergence-free flow field), **blackbody color ramp** (white-hot → deep-red by particle life), pre-rendered sprite atlas drawn twice (faint glow + bright core) under **additive blend**, **heat-persistence smear** (translucent dark fill instead of clear), buoyancy ∝ heat + drag + sparks. Optional **load-reactivity**: spawn-rate/glow surge on heavy stages (already prototyped).
- **Matrix** (new): classic column rain — `columns = width/fontSize`, translucent-fill trail fade, bright leading glyph + dim green trail, per-column speed variance, katakana/digit glyph set. Green chosen for legibility contrast under the red line (re-tint to amber if desired).
- **Palette:** the signal line stays red across all three fields.

### 5.3 Layer separation rationale
Words and particles are **two independent layers** — the wave lives on the *text* (per-char offsets), the field lives on the *canvas*. Text stays crisp and legible while the field animates freely, and each is optimized independently. This is both the better engineering call and the cleaner aesthetic.

---

## 6. Data contract — one new SSE event

A single **generic telemetry envelope**, so we never add a new event name per stage:

```
event: system_activity
data: {
  "stage":  "<slug>",      // embed_query | retrieve | context | tool_exec | mint | prefill | resolve_model
  "label":  "<display text>",   // e.g. "embed · gemini-embedding-2 · 3072d"
  "detail": { ... },       // optional metrics bag: model_slug, dims, corpus_count,
                           //   candidate_count, top_k, tokens, budget_tokens, dropped,
                           //   snapshot_id, tool_name, ok, timing_ms
  "seq":    <int>          // monotonic per turn, for ordering
}
```

- **Zero backend routing change to emit.** `generate_sse`'s relay loop (`Orchestrator/routes/chat_routes.py` L7256-7265) re-frames *any* yielded `{"type": X, "data": Y}` dict into `event: X\ndata: <json(Y)>\n\n` with no special-casing. Yielding `{"type":"system_activity","data":{…}}` from anywhere in the stream just works.
- **Purely additive on the frontends** (both already fail safe on unknown events):
  - **Portal** — add `case 'system_activity':` in the SSE switch in `Portal/modules/chat-send.js` (~L1162), after `case 'usage'` and before `case 'done'`.
  - **Android** — add `"system_activity" -> { … }` to `processSSEEvent`'s `when()` in `ChatViewModel.kt` (~L2123), in the metadata section.
- **HARD RULE restated:** the handler feeds the **animation only**. It must never append to the conversation payload, the context builder, or the snapshot body.

### 6.1 Mint is special
Mint (`perform_mint`) runs on the **separate `POST /chat/save` request, after the SSE stream has already closed** with `stream_end`. So the `mint · SNAP-… · <dims>` line **cannot** ride the stream — it comes from the **`/chat/save` JSON response**, which already returns `snap_id`; we additionally plumb `dims` out of `_embed_for_index` (`Orchestrator/checkpoint.py` L56/L59, currently journalctl-only) into that response. The frontend appends the mint line as the feed's final word before dissolving.

---

## 7. Telemetry stage menu (real, instrumentable)

All metrics below already exist at named variables; most retrieval stages currently `print()` to journalctl instead of yielding. Emit only stages that actually run (honest degradation).

| Stage | Real metrics (live) | Source |
|---|---|---|
| resolve brain + window | `provider`, `model`, per-provider window budget | `chat_routes.py:7220/7333` |
| embed query | active slug (`gemini-embedding-2`), `dims` (3072), `purpose='query'` | `embeddings/search.py:211-214`, `retrieval.py:442` |
| corpus + candidate search | `store.count` (8,005), `candidate_n` (fb 40), `junk_floor` (0.40), survivors | `retrieval.py:459/489/500` |
| RRF fuse | `rrf_c` (60), fused candidate count | `retrieval.py:554/557` |
| recency tie-break | `recency_weight` (0.005), `half_life` (90 d) | `retrieval.py:565` |
| rerank *(skip if dark)* | provider/model/top-k — **only if `rerank.is_enabled()`** | `retrieval.py:577`, `[RERANK]` L354 |
| MMR top-k | `mmr_lambda` (0.85), `k`, delivered count | `retrieval.py:630` |
| assemble context | R/K/S/C channel counts, chars/tokens, **post-drop** provenance | `context_builder.py:301/462` |
| window guard | `est` tokens vs `budget`, `dropped` | `context_builder.py:462/483` |
| tool exec | `tool_start` / `tool_executing` / `tool_result` — **already emitted, currently invisible** (no parse case on either frontend) | provider generators (openai `~2384`, anthropic `~3096`, …) |
| generate | provider token stream, `usage` | already rendered |
| mint | `snap_id` (`SNAP-YYYYMMDD-NNNN`), `dims` (3072) | `checkpoint.py:238/56/59` via `/chat/save` |

---

## 8. Backend approach

- **The seam:** today all retrieval (embed → search → rank → context) finishes *synchronously before the first SSE byte* (`stream_start`). Two options:
  - **v1 (recommended): burst-after-`stream_start`.** Emit the retrieval stages as a rapid `system_activity` burst right after `stream_start`, using the metrics `build_streaming_context` already returns. Honest values; the frontend self-paces the display. **Low risk — no surgery on the retrieval pipeline.**
  - **v2 (optional later): relocate `build_streaming_context` inside `generate_sse`** so stages yield truly interleaved with real per-stage timing. More invasive; defer.
- **Tools:** no backend work — already emitted; surfaced by adding the frontend parse case.
- **Non-stream path** (`tasks.py`, cron, custom-provider non-stream) has **no live channel**; those turns simply show no feed (acceptable — it's ephemeral).
- **Timing:** no retrieval stage has a timing variable today. Emit the free counts; add `perf_counter()` wrapping only if we want honest `timing_ms` (never fabricate timings).

---

## 9. Per-surface architecture

**No data-model changes anywhere** — the feed is ephemeral, so nothing is stored on `UiMessage`/`ChatMessage`.

### Android (crown jewel)
- **Replace `ThinkingIndicator.kt` wholesale** (self-contained, single input, single call site `ChatBubble.kt` L357) with a `Canvas`-based telemetry field reusing the ember `withFrameNanos` tech.
- Feed it a **transient list of live activities** held in the ViewModel (not on the persisted message).
- Parse `system_activity` in `ChatViewModel.processSSEEvent` (~L2123); gate on `ChatState.THINKING/STREAMING`.
- **Leave `ChatBubble.kt` L188-217 (real reasoning dropdown) untouched.**
- Revamp `EmberOverlay` (`ChatScreen.kt` L119) into the lava field.
- Path root: `AI_BlackBox_Portal_Android_MVP (2)/…/com/aiblackbox/portal/`.

### Web (basic tier)
- Rewrite the three seam functions in `Portal/modules/chat-send.js`: `createStreamingThinkingBubble` (L188), `appendThinkingContent` (L730), `markThinkingComplete` (L807). **Delete `STREAMING_REASONING_PHRASES` + `startStreamingReasoningCycle`** (L126-182).
- Non-stream fallback: replace `createAnimatedThinkingBubble` / `startThinkingAnimation` / `updateThinkingBubble` in `Portal/modules/chat-bubbles.js`; **delete `THINKING_MESSAGES`** (L23-36).
- A lighter free-floating red-waving-word layer over the existing particle canvas (`ember-fx.js`); revamp the ember CSS/canvas toward lava.
- Remove the fixed `height:48px` lock (`styles/components/_chat-bubbles.css` L376-378). Prune the dead `neural-dot`/`neural-pulse` keyframes. Keep the `.bubble.thinking` class (or update `ember-fx.js` `GEN_SELECTOR` L308).

### WebView
- `PortalActivity` loads `/ui/` → **inherits the web version for free.**
- `WizardWebViewScreen` has no chat → **untouched.**

---

## 10. Edge cases & rules

1. **Non-retrieval turns** (empty `user_text`, tool-only follow-ups) skip the memory half — show no fake retrieval stages.
2. **Silent providers** (GPT-5.1, non-mini Grok) still show real system stages; the feed never depends on model reasoning.
3. **Rerank dark** → skip the rerank line. Same for inert `cos_floor` (0.0).
4. **Prefill dead air** (14–60 s) → an honest "prefill · model warming" line may augment the keepalive; never remove the keepalive frame (it holds the connection open).
5. **Provenance = delivered, not retrieved.** Use post-drop provenance counts so we never over-claim memories the model didn't actually see.
6. **Ordering:** include a monotonic `seq`; don't rely on arrival timing (a burst arrives near-simultaneously — the frontend paces).
7. **Never in prompt/ledger** (restated — the load-bearing rule).

---

## 11. Phasing (proposed)

- **P1 — Working truth-feed:** `system_activity` event + web single-morphing-line feed + **tools** (parse case) + **mint** (via `/chat/save`) + kill the phrase cyclers. Ships value on web/WebView immediately.
- **P2 — Android native:** the Compose `Canvas` morphing-line + telemetry parsing replacing `ThinkingIndicator.kt`; full retrieval-stage burst.
- **P3 — Particle field modes + polish:** Rising Stars (rename) / Embers (curl-noise realistic) / Matrix, on both surfaces (see Appendix A); optional load-reactivity; density responsiveness.

---

## 12. Out of scope

- The real model-reasoning dropdown (untouched; possible visual polish is a *separate* future pass).
- Any persistence / flight-log / scrollback of telemetry.
- Backend fabrication of reasoning for silent providers.
- A particle **mode-switcher** (only a quality revamp + room for variants).

---

## 13. Open items for the implementation plan

- Morph style: keep the scramble/decode tick vs. pure fade-morph (trivially toggled; decide on device).
- Default hero field (Embers / Rising Stars / Matrix) and whether Matrix stays green or goes amber.
- Final min-dwell / wave amp / glow values — port the prototype defaults, fine-tune on device.
- Final name ("The Signal" working title).
- ~~Build an interactive prototype to validate the aesthetic~~ — **done (v3, approved 2026-07-13).**

---

## Appendix A — Particle field techniques (from research pass `research-particle-techniques`)

Reference for the native build; the prototype implements these in HTML Canvas, and each maps to Jetpack Compose `Canvas`.

**Shared engine (both platforms):** cap DPR at 2 (1.5 on mobile); compute particle counts from **logical/CSS px area**, not device px (`~1 particle / 8,000 px²`, floor 60, ceiling 1200, ×0.5 on coarse pointers); **delta-timed** updates (`dt` clamped ≤ 0.05 s) so motion is frame-rate independent; **object pooling** / swap-remove (no per-frame allocation); Compose equivalent = a `withFrameNanos` loop driving a single `Canvas{}` that reads a frame clock (avoid per-particle recomposition).

**Embers (biggest realism payoff, in order):**
1. **Curl-noise turbulence** — velocity = curl of a smooth scalar noise potential ψ (finite differences): `vx = ∂ψ/∂y, vy = −∂ψ/∂x`. Divergence-free → embers swirl in a heat plume, never clump/gap. Sum 2–3 fBm octaves (persistence 0.5, lacunarity 2.0). Params: spatial freq 0.003–0.01/px, time-scale 0.15–0.4, strength 40–120 px/s². (Prototype uses a cheap 2-octave sine potential; native can use OpenSimplex or a baked 64×64 grid.)
2. **Additive blend** (`'lighter'` / `BlendMode.Plus`) with low per-particle alpha (0.15–0.4) so overlap builds the white-hot core.
3. **Heat-persistence smear** — paint `rgba(0,0,0,0.12–0.20)` each frame instead of clearing → glowing trails (0.15 sweet spot).
4. **Buoyancy ∝ temperature** (rises then decelerates as it cools) + drag; **blackbody ramp** white/yellow → orange → deep red driven off `life`; **sparks** (hottest color, small, fast, arc under gravity, twinkle).
5. **Pre-rendered sprite atlas** (`drawImage` a soft radial blob per ramp color) instead of `createRadialGradient` per particle — the #1 perf win; draw twice (big faint glow + bright core) for cheap bloom.

**Rising Stars:** de-synced twinkle (random per-star phase + speed), power-skewed sizes (`pow(rand, 2.2)`), 3 parallax layers, glow sprite only on the brightest ~10%, crisp core (clear each frame). Highest count budget; cheapest mode.

**Matrix:** `columns = width/fontSize`; translucent-fill trail fade; bright leading glyph + dim trailing; per-column speed variance; half-width katakana (U+FF66–FF9D) + digits; mutate glyphs a few times/sec.

**Compose caps (phone GPU):** Stars 400–800 · Embers 200–400 (fill-rate bound) · Matrix 40–80 columns. Use `drawIntoCanvas`/`nativeCanvas.drawText` for matrix; `Brush.radialGradient` sprites only on hero particles (per-particle brush build is the cost trap).

*Full brief + source URLs: workflow transcript `research-particle-techniques` (run `wf_448c3bb1-561`).*
