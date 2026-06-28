# Plan: Generation Ember Backdrop (Portal web + Android MVP)

## Context

The marketing site (aiblackboxfc.com / `Apps/landing-page/`) runs a cinematic **ember
particle** field — warm coals rising over a black background. The product UIs (Portal web
+ Android MVP) feel flat by comparison. Brandon wants that same signature effect to appear
**while the AI is generating** — text or media — so a generation turn visibly "comes alive":
the background stays black and ember particles rise behind the (still fully readable) chat
content, then gracefully drain away when generation finishes. This is pure UI polish; no
backend, model, or data changes.

**Locked decisions (confirmed with Brandon):**
1. **Trigger** — ALL AI generation: text streaming, non-streaming polling, agent-CLI, AND media (image/video/music/voice).
2. **Layering** — embers render BEHIND readable content; bubbles/streaming text stay fully visible, embers glow in the gaps.
3. **Intensity** — match the website exactly (120 particles, 3 depth layers, red→orange→yellow→white-hot palette, additive glow). No count scaling.
4. **Android scope** — main chat screen AND all dedicated media-gen screens (Image/Video/Music/Gemini-TTS/Google-SSML).

**Source of truth for the effect:** `Apps/landing-page/app.js:191-518`
(`initCinematicParticles('embers')`). Both ports replicate its exact constants:
layers (count 40/50/30, speed .3/.5/.8, size [.5,1]/[1,2]/[1.5,3], opacity .25/.4/.7),
colors {255,74,74}/{255,120,50}/{255,180,50}/{255,220,100}/{255,250,200} (weights
.3/.3/.2/.15/.05), glowIntensity 10, turbulence .6, riseSpeed .8, flickerSpeed .015,
trailLength 2, rise-from-bottom + multi-sine turbulence/flicker + fade-near-top + reset.

---

## Approach

Two independent tracks (no shared files) — Track W (web) and Track A (Android). On both
surfaces the **simulation is a UI-free engine** (plain JS / plain Kotlin), with a thin
renderer on top, so the physics is unit-testable and the two ports stay in lockstep.

### Track W — Portal web

The Portal is already hard-locked pure black (`--bg:#000`), so "go black" needs no theme
work — only the embers fade in. The effect lives **inside `<section class="chat">`** (the
main reading area; the composer/topbar are outside it). One `<canvas>` sits behind a
raised, transparent `#history` so messages render above the embers and the embers show
through the gaps. **Trigger is read from the DOM markers the existing code already toggles**
— zero edits to `chat-send.js`/`agent-handler.js`/`task-manager.js`.

**Files:**

- **NEW `Portal/modules/ember-fx.js`** — faithful engine port + DOM-driven controller.
  - UI-free `EmberSimulation` (spawn 120 across 3 layers; `update(timeMs, active)`;
    `isDrained()`); renderer draws to `#emberCanvas` with `clearRect`-only each frame
    (NOT the reference's dead `fillRect(rgba(0,0,0,.15))`) and `globalCompositeOperation='lighter'`.
  - Drop the reference's mouse-interaction + window-`resize` code (no mouse in a WebView);
    size the canvas to `.chat` via a **`ResizeObserver`**, DPR capped at 2.
  - rAF runs **only while active OR draining**; on drain-complete → `cancelAnimationFrame`
    + `clearRect` (no idle 60fps loop burning WebView battery). Pause on `document.hidden`.
  - Detection: a **`MutationObserver` scoped to `#history` + `#thinkingIndicator`** only
    (`childList`+`subtree`+`attributes`/`attributeFilter:['class']`, no `characterData`),
    coalesced via an rAF dirty-flag. Active when
    `querySelector('.streaming-bubble, .bubble.thinking, .generating-image, .generating-video, .generating-music')`
    is non-null OR `#thinkingIndicator:not(.hide)`.
  - Export `initEmberFX()`; expose `window.EmberFX.markGenerating(reason,on)` (refcounted)
    for any future path lacking a marker. Honor `prefers-reduced-motion` (skip entirely).
- **NEW `Portal/styles/features/_ember-fx.css`**
  - `#emberCanvas{position:absolute;inset:0;z-index:0;pointer-events:none;opacity:0;transition:opacity 420ms ease}` + `#emberCanvas.on{opacity:1}`
  - `.chat{isolation:isolate}` — establishes a local stacking context so canvas(0) < history(1) is robust.
  - `.chat > .history{background:transparent;position:relative;z-index:1}` — messages above embers; embers visible through gaps. (Idle look is pixel-identical: `.chat` behind is the same `#000`.)
  - Do **not** touch `#helpHints` (it's `z-index:10`, `display:none` during generation).
- **EDIT `Portal/styles/main.css`** — add `@import url('./features/_ember-fx.css?v=genui295');` under FEATURES.
- **EDIT `Portal/modules/app-init.js`** — import `initEmberFX`; call it in `initApp()` after `initHistory()` (~line 702).
- **EDIT `Portal/index.html`** — bump `?v=genui294` → `genui295` (lines 11 and 21).

*Black assistant bubbles (`--bubble:#000`) render as opaque cut-outs over the embers — this is on-spec for decision #2 (readable content, embers in the gaps).*

### Track A — Android (Jetpack Compose)

A reusable component dropped behind each screen's content, gated on that screen's own
generating state (state flows **down** — per-screen overlays beat a global bus: zero
plumbing, navigation auto-scopes the effect, disposal auto-cancels the loop, no leak).

**Files (base: `AI_BlackBox_Portal_Android_MVP (2)/…/app/src/main/java/com/aiblackbox/portal/`):**

- **NEW `ui/components/EmberParticles.kt`** — header comment maps to `app.js:191-518`.
  - UI-free `EmberSimulation` (plain Kotlin): spawn 120 across the 3 website layers,
    rise/turbulence/flicker/reset, `update(nanos, active)`, `isDrained()`.
  - **Pre-baked glow sprites:** in `remember`, render 5 radial-gradient `ImageBitmap`s
    (one per palette color, exact website stops) ONCE. Per frame, per particle:
    `drawImage(sprite, dstSize = size*glowIntensity, alpha = opacity, blendMode = blend)`
    + one tiny white core circle. **No per-frame `Brush` allocation.**
  - `@Composable EmberOverlay(active, modifier)` with battery-safe gating:
    ```kotlin
    var running by remember { mutableStateOf(false) }
    val frame = remember { mutableLongStateOf(0L) }
    LaunchedEffect(active) { if (active) running = true }
    LaunchedEffect(running) {
        while (running) {
            withFrameNanos { t -> sim.update(t, active); frame.longValue = t }
            if (!active && sim.isDrained()) running = false   // stop AFTER drain
        }
    }
    ```
    Read `frame.longValue` **only inside the `Canvas` onDraw lambda** (draw-phase invalidate,
    not recomposition); fade via `Modifier.graphicsLayer { alpha = animatedAlpha }`
    (`animateFloatAsState(if(active)1f else 0f, tween(DurationSlow))`).
  - `@Composable EmberBackdrop(active, modifier, content)` — a `Box` wrapper that layers
    `EmberOverlay` then `content` (for screens whose root is a scrolling `Column`).
  - `blendMode` is a single constant defaulting to `BlendMode.Plus`, with a `SrcOver`
    fallback note (Plus is spotty on hardware canvas pre-API 28 — verify on API 26).
- **EDIT `ui/theme/Color.kt`** — add `EmberRed/EmberOrange/EmberYellowOrange/EmberYellow/EmberWhiteHot` (the 5 website RGBs). `EmberRed` == existing `BbxAccent` (0xFFFF4A4A).
- **EDIT `ui/chat/ChatScreen.kt`** — insert `EmberOverlay(active = isStreaming, Modifier.matchParentSize())` as the **first child** of the messages `Box` at line 103 (reuses the existing `isStreaming = STREAMING||THINKING` local at line 92). Embers behind the `LazyColumn`; transparent bubble gaps reveal them.
- **EDIT 5 media screens** — `ui/generation/{ImageGenScreen,VideoGenScreen,MusicGenScreen,GeminiProTtsScreen,GoogleSsmlScreen}.kt`: wrap each root scrolling `Column` in `EmberBackdrop(active = <predicate>) { … }`. Predicate = `state == GenState.SUBMITTING || state == GenState.POLLING` (image/video/music/tts, e.g. `ImageGenScreen.kt:221`) or the screen's `isGenerating` (GoogleSsmlScreen). The overlay must be a **sibling layer**, never inside `verticalScroll`.
- **NEW `app/src/test/java/com/aiblackbox/portal/EmberSimulationTest.kt`** — assert: 120 particles spawned across 3 layers; velocity is upward; off-screen particle resets while active; `isDrained()` true after deactivation once particles exit.
- *(Optional polish)* add `val isGenerating get() = chatState.value in setOf(STREAMING,THINKING)` to `ChatViewModel` and reuse at the 3 duplicated predicate sites. Not required for the feature; skip if it adds risk.

---

## Bugs/traps explicitly handled (from adversarial review)

- **Web:** rAF stops after drain (no idle loop); `clearRect`-only (skip reference's dead black `fillRect`); no mouse listeners; `ResizeObserver` on `.chat`; `.chat{isolation:isolate}` for robust stacking; leave `#helpHints` alone; observer scoped to `#history`+`#thinkingIndicator` not `.app`.
- **Android:** pre-baked sprites (no per-frame Shader alloc); exact drain-then-stop frame gating; `frame` read only in draw lambda + `mutableLongStateOf` (no whole-screen recompose); media overlays are siblings outside the scroll; `BlendMode.Plus` verified on API 26 with a `SrcOver` fallback.
- **Both:** simulation engine is UI-free → unit-testable.

---

## Execution

Subagent-driven (Track W and Track A in parallel; ordered within each track):
1. Persist this plan to `docs/plans/2026-06-28-generation-ember-backdrop.md` (project convention).
2. Track W: create engine → create CSS → wire main.css + app-init.js → bump version.
3. Track A: create `EmberParticles.kt` + sprites → add colors → wire ChatScreen → wrap 5 media screens → add unit test.
4. Per-track code review (superpowers:requesting-code-review / code-reviewer) against this plan.
5. Verify (below). Then commit **only the ember-fx paths** (the working tree has unrelated
   changes — stage explicitly, do not `git add -A`). Commit on `main` per the
   staging-as-prod convention. Finish with `/snapshot-dev`.

## Verification

- **Web (real end-to-end):** Portal is served at `http://localhost:9091/ui` by the running
  `blackbox.service`. Hard-refresh (cache bust `genui295`), then drive via `claude-in-chrome`:
  send a chat message → embers rise behind the streaming reply and **drain** on completion;
  request an image → `.generating-image` keeps embers on through the task; check DevTools
  Performance for no per-frame GC sawtooth and that rAF **stops** (CPU→0) after drain; set
  `prefers-reduced-motion: reduce` → no canvas. Visually compare against the landing page.
- **Android:** `./gradlew assembleDebug` (compile gate) + `./gradlew testDebugUnitTest`
  (runs the new `EmberSimulationTest`). Visual check on an **API-26** emulator (confirm
  `BlendMode.Plus`) and a modern API: stream a chat reply → embers behind bubbles; open each
  media screen and generate → embers; navigate away mid-generation → frame loop stops
  (profiler, no battery drain). Final on-device visual sign-off is Brandon's (device-validation).

## Out of scope / follow-ups

- Tuning bubble translucency so embers glow *through* assistant bubbles (would change bubble look + legibility) — deliberately not done.
- `ActivityManager.isLowRamDevice` count-reduction tier — left off (decision #3 = match website).
