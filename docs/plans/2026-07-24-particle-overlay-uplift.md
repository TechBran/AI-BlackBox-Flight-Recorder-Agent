# Particle Overlay Uplift — HD Repair, Effect Library, Weather Mode

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task. Prototype every effect on WEB FIRST, get Brandon's visual approval,
> THEN port to Android — the Android file is an explicit faithful port and porting an
> unapproved look doubles the rework.

**Goal:** Repair the web particle field's HD rendering (it is currently displayed at
2× magnification and half resolution on every HiDPI screen), lift the Android field to
its real smoothness ceiling, convert both engines from a hard-coded 3-way switch into a
pluggable effect registry, and ship 10 new high-quality effects plus a Weather mode that
drives the backdrop from real local conditions.

**Architecture:** Both surfaces keep their native engines (Canvas2D on web, Compose
Canvas on Android) — decision locked, no WebGL rewrite. Each engine grows a registry
seam so "add an effect" becomes one descriptor + one sim, never an edit to five call
sites. Effect *constants* that have already drifted between the two ports move into one
checked-in JSON spec consumed by both. Weather resolves server-side in the Orchestrator
and is served to both surfaces as an already-resolved effect id, so the two surfaces can
never disagree and there is one cache and one credential.

**Tech Stack:** Vanilla ES modules + Canvas2D (Portal), Kotlin + Jetpack Compose
(Android, minSdk 26), FastAPI + httpx (Orchestrator), pytest + node --test +
ParticleFieldTest (JVM).

**Brandon's locked decisions (2026-07-24):**
- **V1 scope:** foundation + 10 hero effects (not all 37).
- **Web engine:** fix and modernize Canvas2D. No WebGL path in v1.
- **Reactive accents** (generation surge, token sparks, warp burst): **NOT in v1.**
  Fields only. The `surge` plumbing stays dead until a later arc.
- **Weather source:** Android has no system weather API (researched — see M6), so weather
  resolves **server-side on the box**, BYOK via the wizard. The one item still needing
  Brandon's nod is which provider ships as the *default*: keyless Open-Meteo
  (non-commercial licence) vs Google Weather BYOK (commercial-safe). Everything else in
  M6 is provider-agnostic and can be built before that call. (Research leans Google as the
  shipped default *because* Open-Meteo's free tier is non-commercial; Open-Meteo stays as
  the keyless dev/personal-box adapter.)

---

## Context — what the research established

A six-agent Opus audit (workflow `wf_c690d323-1a5`) produced the findings below. The
two that reframe the work:

**1. The "giant, low-definition particles" bug is a CSS replaced-element trap, not a
tuning problem.** `#emberCanvas` is styled `position:absolute; inset:0` with **no
`width`/`height`** (`Portal/styles/features/_ember-fx.css:12`). A `<canvas>` is a
*replaced element*: per CSS 2.1 §10.3.8 an absolutely-positioned replaced element with
`width:auto` uses its **intrinsic** size — the `width`/`height` content attributes — and
the over-constrained `inset` is ignored. Since `resize()` sets those attributes to
`w × dpr` (`ember-fx.js:395-396`), the canvas's CSS box becomes `dpr` times its
container. The field is displayed at `dpr`× magnification, at `1/dpr` of native
resolution, with only the top-left `1/dpr²` of the simulation on screen.

Measured directly (harness replicating the exact rule, `--force-device-scale-factor`):

| device-scale-factor | container | canvas attrs | canvas CSS box | magnification |
|---|---|---|---|---|
| 1 | 1400×800 | 1400×800 | 1400×800 | **1.00×** ✅ |
| 2 | 1400×800 | 2800×1600 | 2800×1600 | **2.00×** ❌ |
| 3 | 1400×800 | 2800×1600 | 2800×1600 | **2.00×** ❌ (plus under-sampled) |

Adding `width:100%; height:100%; display:block` returns 1.00× at every DPR. This is
why it looks correct on a plain 1× monitor and wrong on every phone, Retina Mac and
Windows box at 125–200% — and why "request desktop site" doesn't help: desktop mode
changes the layout viewport, not `devicePixelRatio`.

**Provenance:** the effect was ported from `Apps/landing-page/app.js:198-224`, which
sets `width:100%; height:100%` in its inline `cssText`. The Portal port added correct
DPR backing-store scaling but dropped those two declarations. The bug has been there
since the original commit.

**2. Both engines are already registry-shaped — the seam exists, it's just not used.**
Android has a `sealed interface FieldSim { resize/update/rearm }`
(`ParticleField.kt:120-132`) and a `ParticleMode` object whose `ALL` list already drives
the settings picker generically (`ParticleField.kt:72-93`, consumed at
`SettingsSheet.kt:906-931`) — adding an effect there needs **zero settings-UI code**.
Web has a 3-entry `const DRAW = {stars, embers, matrix}` dispatch table
(`ember-fx.js:355`) plus a mode whitelist **triplicated** across `loadParticleMode`
(:485), `setParticleMode` (:490) and the `window.EmberFX` surface (:543).

### Defect inventory (from the audits — all cited, all confirmed)

**Web — 14 defects.** Beyond the root cause: `dpr` clamped to 2 under-samples 3×/4×
screens (:393); the ember sprite atlas is a fixed 64 px never scaled by DPR, so Embers
is upscaled mush by construction (:106); `baseCount()` viewport-density logic is **dead
code** (zero call sites) so Rising Stars is hard-coded to 120 particles regardless of
viewport; `drawStarParticle` allocates **two radial gradients per particle per frame**
(:284, :290) — the perf wall that forces the low count and therefore the large particle
size; star motion is not delta-timed (:241) so the field runs ~2× fast on a 120 Hz
display; Matrix glyph size is viewport-derived with no upper clamp (:319) → giant glyphs
on wide monitors; no `devicePixelRatio`-change listener; `resize()` wipes the field on
every ResizeObserver tick; the oversized canvas turns `.chat` into a scroll container.

**Android — 16 defects.** The headline: **every sprite is snapped to integer pixels**
(`ParticleField.kt:308`), destroying sub-pixel motion — slow far-layer stars visibly
*step* at 120 Hz, and that is the single most perceptible smoothness defect on exactly
the device Brandon uses. Also: `StarSim` never re-scatters on size change, so unfolding
a Fold leaves the newly revealed half of the screen permanently empty; `graphicsLayer`
alpha with the default compositing strategy forces a full-screen offscreen buffer that
**silently disables additive blending for the whole 300 ms fade**; seven separate sprite
textures defeat GPU quad batching (~480 texture binds/frame where 1 would do); Matrix
issues ~1,040 individual `drawText` calls per frame; the field renders at full rate
**behind an opaque settings sheet**; 64 px sprites minified to 1–3 px with no mip chain
produce shimmer that is mistaken for twinkle.

**minSdk is 26** (`app/build.gradle:14`) — AGSL `RuntimeShader` needs API 33,
`RenderEffect` needs 31, `RenderNode` needs 29. None can be used unconditionally; each
needs a `Build.VERSION.SDK_INT` branch **and** a working fallback, and both branches must
be visually acceptable. This is why the Android uplift is batching + sub-pixel + mips
rather than a shader rewrite.

### The three universal "premium vs cheap" rules

Research converged on these; every new effect must satisfy all three:
1. A **size-over-life curve** with a per-particle envelope multiplier (never constant size).
2. A **colour ramp indexed by life** (never a flat colour).
3. At least **two sub-populations** with different parameters (the existing ember
   "spark" pattern generalized).

Plus: bake **noise into the sprites once** — 64 px radial gradients magnified 4× on black
is the textbook banding case and is our most visible cheap artifact today.

---

## Milestones

M0 and M1 are **prerequisites**: until the web canvas is fixed, every new effect also
renders 2× magnified at half resolution, so no new work can be evaluated honestly.

---

### M0 — Web HD repair (the prerequisite)

**Files:**
- Modify: `Portal/styles/features/_ember-fx.css:12-19`
- Modify: `Portal/styles/main.css:43` (cache-bust — see the trap below)
- Modify: `Portal/modules/ember-fx.js` (`resize()` :389-404, `drawStarParticle` :274-295,
  star integration :241, matrix sizing :319, sprite bake :92-108)
- Create: `Portal/modules/fx/sizing.test.mjs`
- Create: `Portal/onboarding/../scripts` — none; verification is a headless-Chrome script

**DEPLOYMENT TRAP — read before editing CSS:** cache-busting here is per-file.
`index.html` carries `?v=genuiNNN` but `_ember-fx.css` is `@import`ed with its **own**
`?v=genui298` from `Portal/styles/main.css:43`. Bumping `index.html` does **not** bust an
`@import`ed sub-stylesheet. Every CSS change in this plan must bump that query string.

**Step 1: Write the failing sizing test.**

`Portal/modules/fx/sizing.test.mjs` — pure functions, no DOM:

```js
import test from "node:test";
import assert from "node:assert/strict";
import { backingStore, apparentScale } from "./sizing.js";

test("backing store uses a pixel budget, not a ratio clamp", () => {
    // 3x phone: full crispness (small screen, few total pixels)
    assert.equal(backingStore(412, 915, 3).dpr, 3);
    // 4K desktop at 2x: clamped by total pixels, not by ratio
    const big = backingStore(3840, 2160, 2);
    assert.ok(big.w * big.h <= 6e6, `budget exceeded: ${big.w * big.h}`);
});

test("apparent particle size is DPR-independent", () => {
    assert.equal(apparentScale(1400, 800, 1), apparentScale(1400, 800, 3));
});
```

**Step 2: Run it — expect failure** (`sizing.js` does not exist):
`node --test Portal/modules/fx/sizing.test.mjs` → FAIL "Cannot find module".

**Step 3: Extract the sizing math into `Portal/modules/fx/sizing.js`.**

```js
// Backing-store budget: ~6M px ≈ 24 MB RGBA. Clamping the RATIO (the old
// Math.min(dpr,2)) starves small dense screens of resolution while doing
// nothing for huge ones; clamping total PIXELS is the correct axis.
export const MAX_BACKING_PX = 6e6;

export function backingStore(cssW, cssH, rawDpr) {
    const dprWanted = Math.max(1, rawDpr || 1);
    const area = cssW * cssH * dprWanted * dprWanted;
    const dpr = area <= MAX_BACKING_PX
        ? dprWanted
        : Math.max(1, Math.sqrt(MAX_BACKING_PX / (cssW * cssH)));
    return { dpr, w: Math.floor(cssW * dpr), h: Math.floor(cssH * dpr) };
}

// Particle geometry is a fraction of the short edge, NEVER of dpr — dpr must
// affect backing-store resolution only.
export function apparentScale(cssW, cssH) {
    return Math.min(cssW, cssH) / 900;  // 900 = the reference short edge
}
```

**Step 4: Run the test — expect PASS.**

**Step 5: Fix the root cause (CSS).** In `_ember-fx.css`, the `#emberCanvas` rule:

```css
#emberCanvas {
    position: absolute;
    inset: 0;
    /* A <canvas> is a REPLACED element: `inset:0` does NOT stretch it — with
       width:auto it lays out at its INTRINSIC (attribute) size, which resize()
       sets to w*dpr, displaying the whole field at dpr x magnification and
       1/dpr resolution. These two declarations are the entire fix; the
       landing-page original this was ported from had them. Do not remove. */
    width: 100%;
    height: 100%;
    display: block;
    z-index: 0;
    pointer-events: none;
    opacity: 0;
    transition: opacity 420ms ease;
}
```

Then bump `Portal/styles/main.css:43` `?v=genui298` → `?v=genui332`.

**Step 6: Belt-and-braces in JS.** In `resize()`, after setting the attributes, pin the
CSS box explicitly so the field can never be magnified again even if the rule is lost:

```js
const { dpr: d, w: bw, h: bh } = backingStore(w, h, window.devicePixelRatio || 1);
if (canvas.width === bw && canvas.height === bh) { width = w; height = h; return; }  // no-op guard
dpr = d;
canvas.width = bw; canvas.height = bh;
canvas.style.width = w + "px";      // never rely on the stylesheet alone
canvas.style.height = h + "px";
ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
```

**Step 7: Verify the fix at 1×, 2× and 3× with real screenshots.**

```bash
for s in 1 2 3; do
  google-chrome --headless=new --disable-gpu --no-sandbox \
    --force-device-scale-factor=$s --window-size=1400,900 \
    --virtual-time-budget=3000 \
    --screenshot=/tmp/particles-dpr$s.png http://localhost:9091/
done
```
Expected: particle apparent size **identical** across all three; at 3× visibly crisper
than before. `dpr=1` hides the bug completely — any verification that only tests 1× is
worthless.

**Step 8: Commit.**

```bash
git add Portal/styles/features/_ember-fx.css Portal/styles/main.css \
        Portal/modules/ember-fx.js Portal/modules/fx/sizing.js Portal/modules/fx/sizing.test.mjs
git commit -m "fix(particles): canvas laid out at intrinsic size — field was dpr x magnified"
```

**Step 9–14: the remaining web HD defects**, each its own test-then-fix-then-commit cycle:

| # | Defect | Fix | File:line |
|---|---|---|---|
| 9 | Stars allocate 2 radial gradients/particle/frame | Route through the already-baked-but-never-read `STAR_SPR`; pre-tint one sprite per palette colour; draw wide-faint + small-crisp under `lighter` | `ember-fx.js:274-295`, atlas `:88,:107` |
| 10 | Star motion not delta-timed | `const dt60 = dt * 60` and multiply the integration — mathematically identical at 60 fps so the approved look is preserved exactly | `ember-fx.js:241` |
| 11 | Hard-coded 120 particles | Call the dead `baseCount()`; scale layer counts by viewport area with floor/ceiling clamps | `ember-fx.js:115-120,193-196` |
| 12 | Matrix glyphs unclamped on wide screens | `Math.max(13, Math.min(22, Math.round(width/78)))` | `ember-fx.js:319` |
| 13 | No DPR-change listener | Self-rearming `matchMedia('(resolution: Xdppx)')`; re-`resize()` **and** re-bake sprites | `ember-fx.js:374-388` |
| 14 | Sprite banding | Add triangular-distributed noise inside `makeSprite()` — one-time bake cost, zero per-frame cost | `ember-fx.js:92-103` |

**Constraint — the Rising Stars look is Brandon-approved verbatim** (restored in
`640f13e5`, documented at `ember-fx.js:187-190`). These are *repairs*, not
re-art-direction: at `dpr=1` the on-screen result must be unchanged.

---

### M1 — Android smoothness repair

**Files:** `ui/components/ParticleField.kt`, `ui/components/EmberParticles.kt`,
`ui/components/ParticleFieldTest.kt` (JVM), `ui/settings/SettingsSheet.kt`

| # | Defect | Fix | File:line |
|---|---|---|---|
| 1 | **Integer-pixel snap kills sub-pixel motion** (worst smoothness defect at 120 Hz) | Drop the Int-only `drawImage` overload; use `nativeCanvas.drawBitmap(bitmap, srcRect, dstRectF, paint)` (float destinations) | `ParticleField.kt:308` |
| 2 | Fold unfold leaves half the screen empty | Give `StarSim` the re-scatter `MatrixSim` already has: on a real width delta, resample `x` across the new width | `ParticleField.kt` StarSim.resize |
| 3 | Additive blending silently off during every fade | `compositingStrategy = CompositingStrategy.ModulateAlpha` (sets `hasOverlappingRendering=false`, so alpha multiplies per draw op instead of building a layer) | `EmberParticles.kt:145` |
| 4 | 7 textures defeat batching (~480 binds/frame) | Bake ONE atlas (4×2 grid of 64 px cells) and select with `srcOffset`/`srcSize` (already parameters, currently hardcoded) | `ParticleField.kt:185-188` |
| 5 | Matrix: ~1,040 `drawText`/frame + per-frame `FontMetrics` alloc | Pre-render the glyph set to an `ImageBitmap` atlas once per font size; draw glyphs as textured quads | `ParticleField.kt:519-547` |
| 6 | Sprite shimmer mistaken for twinkle | Bake a mip chain (64/32/16/8/4/2 px cells, folds into the atlas from #4); pick the cell nearest destination size | `ParticleField.kt:154-182` |
| 7 | Renders full-rate behind the settings sheet | `repeatOnLifecycle(STARTED)` + a `LocalFieldSuppressed` CompositionLocal set by `SettingsSheet` and dialogs | `EmberParticles.kt:113-133` |
| 8 | Ember spawn 4× oversubscribed (~1,100 wasted probes/frame) | Add the free-list the comment already promises: `IntArray` stack of dead indices, O(1) spawn | `ParticleField.kt:399,412` |
| 9 | `resize` mutates state inside the draw lambda | `Modifier.drawWithCache`; `onDrawBehind` becomes a pure read-and-draw | `EmberParticles.kt:142-153` |
| 10 | Reduced-motion sampled once per composition | `ContentObserver` on `ANIMATOR_DURATION_SCALE` in a `DisposableEffect` | `EmberParticles.kt` |
| 11 | Atlas rebuilt per overlay instance | Hoist to a process-level cache keyed by density, next to the CompositionLocal providers | `NativeMainActivity.kt:525-526` |

**Test gate (each step):** `./gradlew :app:testDebugUnitTest --offline`
**Note:** `ParticleFieldTest.kt:52` hard-asserts the 240-star count *by design* — when an
effect forks `StarSim`, **fork it, don't mutate it**, or that test is meant to break.

**Device gate:** install on the Fold and check the far-layer stars at 120 Hz — the step
should be gone. Fold/unfold and confirm the field fills the inner screen.

---

### M2 — Effect registry (both surfaces)

The refactor that makes every later effect additive.

**Web** — replace `const DRAW = {stars, embers, matrix}` (`ember-fx.js:355`) with:

```js
// Portal/modules/fx/registry.js
export const EFFECTS = new Map();
export function registerEffect(descriptor) { /* {id,label,blend,clearPolicy,init,resize,update,draw,drain,dispose} */ }
```

- `clearPolicy` is `'clear' | 'fade:rgba(...)' | 'none'` and is applied **by the engine**,
  so no effect ever touches `globalCompositeOperation` itself.
- `blend` is **per-effect**: additive (`lighter`) is correct for embers/stars/aurora and
  **wrong** for snow/fog/bokeh/petals, which blow out to white and destroy text contrast.
  This must land *before* the first non-emissive effect.
- Move each field to `Portal/modules/fx/effects/<id>.js`, dynamic-`import()`ed on
  selection, so a large catalogue costs nothing at load.
- **Kill the triplicated whitelist** (`:485`, `:490`, `:543`) — derive from `EFFECTS`.
- Generate the settings option list from `EFFECTS` so registering an effect is the only
  step needed to expose it.
- Inject `env.rand` and a clock instead of calling `Math.random()`/`performance.now()`
  directly, so effect physics becomes headlessly testable (matching Android, which
  already has `ParticleFieldTest`).

**Android** — the `FieldSim` seam already exists; the missing half is rendering:

```kotlin
interface FieldRenderer { fun DrawScope.render(sim: FieldSim, res: FieldResources, nowMs: Double) }
```

- Splits the `when (sim)` type-switch (`ParticleField.kt:552-563`) so one simulation can
  have several renderers — e.g. sprite renderer on API 26–32, `RenderEffect` bloom on 31+.
  Use the existing `FIELD_BLEND` capability-gate pattern (`:113-114`) verbatim.
- Generalize `FieldSprites` → `FieldResources` (atlas + cell table + mip chain + glyph
  atlas), built once per (density, effect, quality tier), cached process-wide.

**Shared constants** — create `shared/particle-spec.json` for the values that have
already drifted between the two ports (blackbody ramp, star layer counts/speeds/sizes/
opacities, glow multiplier, turbulence, rise speed, flicker). Consume as an ES module on
web; generate a Kotlin object at build time. **Scope it to constants only** — do not try
to share simulation code; research was explicit that over-sharing here backfires.

---

### M3 — Prototype gallery + Brandon's visual approval (WEB FIRST)

**Do not port anything to Android before this gate passes.**

Build `Portal/fx-lab.html` — a local gallery rendering every candidate effect live at
full quality, each in a card with a **real chat transcript behind it** so legibility is
judged honestly, plus per-effect sliders (density, intensity, speed) and a DPR readout.

Brandon reviews, tunes, and approves each effect's look. Approved constants are written
back into `shared/particle-spec.json`. Only then does M4 begin.

---

### M4 — The ten hero effects

Every effect satisfies the three premium rules (size-over-life curve, life-indexed colour
ramp, ≥2 sub-populations) and carries an explicit **legibility budget**. Ship order is
cheapest-and-safest first so the registry is exercised early.

| # | Effect | Why | Legibility budget |
|---|---|---|---|
| 1 | **Slipstream** (flow field) | wow 5 at low cost — reuses the existing curl potential + smear-trail verbatim; 1 px threads. **Best legibility in the catalogue.** | Threads ≤1.5 px, no large bright mass can form |
| 2 | **Cinders on the Wind** | Parameter fork of shipped embers (wind term, kill buoyancy). Zero visual risk, and it's the natural carrier for `wind_speed_10m` in M6 | Identical to shipped Embers |
| 3 | **Fireflies** | 15–40 particles, safest possible backdrop, battery win. **Rule:** pulses must be desynchronised in period *and* phase or it reads as a cheap CSS demo | Trivial |
| 4 | **Deep Field / Bokeh Depth** | Donut (annulus) sprite + 4 correlated parallax planes = real depth. Strongest candidate to replace Rising Stars as the default | Near discs ≤6% alpha — alpha *is* the entire budget |
| 5 | **Drift Snow** | Flagship weather mapping (WMO 71/73/75, 85/86). Downward motion doesn't fight the upward-scrolling message list | Cap ~250 flakes; **source-over, not additive** |
| 6 | **Rainfall** | Core weather mapping (51-55, 61-65, 80-82). Velocity-stretched streaks; per-column wind shear (uniform angle is the cheap giveaway) | 10–15° lean (vertical competes with the message column); near-plane <25% alpha |
| 7 | **Meteor Fall** | Overlay on any starfield, ~40 lines. Bias spawns to the edges | Thin, brief, out of the centre 40%. **Never sync to generation** — users read it as status |
| 8 | **Ledger Rain** | Most on-brand thing in the catalogue: Matrix rain whose glyphs resolve into **real snapshot IDs**, tinted accent-red | Same as shipped Matrix; do **not** raise trail 0.5 / lead 0.95 |
| 9 | **Low Fog** | Cheapest premium look — 8–20 huge soft sprites at 3–6% alpha. The 64→900 px upscale that's a *defect* for embers is a *feature* here. Weather: WMO 45/48 | **Hard 6% alpha ceiling + a measured contrast check** — this is the one class that raises average luminance uniformly |
| 10 | **Aurora Curtain** | The showpiece. 24–48 baked gradient strips, non-smooth noise sum (a smooth gradient reads as a blob). Tint accent-red→violet | Clamp to the top ~55% with vertical alpha falloff reaching zero before the message area; peak ≤8% |

**Ledger Rain hard constraint** (carried from The Signal telemetry line): this is
**UI-ONLY decoration**. Snapshot slugs come from a display-only endpoint and must
**never** enter a prompt or the ledger. Guard it with a test.

**Deferred, recorded so the question is closed:** Nebula Veil (ship as a *layer* under a
starfield, not standalone — alone it's a coloured smudge), Splash Line, Petal/Leaf,
Murmuration, Sonar Sweep, Circuit Traces, Data Streams, God Rays, Caustics, Plankton,
Ash Fall, Warp/Token/Surge reactive accents (Brandon: not in v1). **Gated:** Distant
Storm and all violent weather — full-screen luminance flashes are a WCAG 2.3.1
photosensitivity hazard (max 3 flashes/sec) needing a dedicated opt-out; ship at most
one, later. **Rejected:** heat shimmer / mirage (needs screen-space refraction of a
source image; our backdrop is pure black and Canvas2D has no cheap path).

---

### M5 — Settings: the particle menu with its own options

Brandon asked for the particle section to have **its own dropdown of settings**.

- **Effect picker** generated from the registry (never a hard-coded list again), with a
  live preview thumbnail per option.
- **Visibility** — keep the existing Off / While generating / Always.
- **Intensity / density** slider, and a **Quality tier** (Low / High / Ultra) that maps
  to particle-count multiplier and sprite LOD, so a weak device degrades gracefully
  instead of dropping frames.
- **Weather mode toggle** (M6) with the resolved-effect readout.
- Persist: web `localStorage`, Android DataStore (`BlackBoxStore` key + Flow + setter,
  four lines per setting) + the CompositionLocal provider block.

**Frontend = 3 surfaces** — Portal web, Android Compose, and the WebView wrappers must
all land together.

---

### M6 — Weather mode

**Brandon asked: "does Android not just publish the weather data itself?" Researched
against primary sources — the answer is no, and the feature belongs on the box anyway.**

**There is no Android system weather API.** The only one Google ever shipped was the
Awareness API's `SnapshotApi.getWeather()`, deprecated 2019-08-07 and **turned off
2020-01-31**, with Google stating verbatim: *"Google does not offer alternate
functionality for the Weather contextual signal."* Nothing replaced it in `android.*`,
Play services, AndroidX, or Wear OS complications (verified against the `SystemDataSources`
source — 14 constants, none of them weather). Google removed its *own* Weather app from
Wear OS 6 in 2025. A `WeatherManager` exists only in dead CyanogenMod-lineage custom ROMs.

**The near-miss:** Samsung genuinely does ship a weather ContentProvider —
`com.samsung.android.weather.content.provider.level.dangerous`, with a **runtime-grantable
`dangerous` permission** and user-facing consent strings, still present in 2026 One UI
firmware. (Note: the `com.samsung.android.weather.provider` authority that circulates in
"system URI" lists is fiction.) It is nonetheless **not viable**: zero documentation
(Samsung's own tutorials tell developers to go get an OpenWeatherMap key), no known
third-party consumer anywhere on GitHub — Samsung provisions it to its own apps via
`privapp-permissions`, so it may be privileged-only in practice — the underlying data is
licensed from The Weather Channel, making resale/redistribution a Play Store risk, and it
does nothing on a Pixel. For Pixel the proof is decisive: Smartspacer, the most capable
"read other apps' data" tool on Android, resorts to **scraping the Pixel Weather widget's
`RemoteViews` view tree by index** — that is what a developer does when no API exists.

**Sensors give a garnish, not a report.** The Fold 6 has a **barometer only** — no ambient
temperature, no humidity (Samsung dropped both after the S4/Note 3 era). The Pixel "thermometer"
is an IR thermopile for *surface* temperature at 5 cm, physically incapable of reading air
temperature, with no public API. A barometer yields 3-hour pressure tendency and nothing
else: it cannot tell you whether it is raining right now. Altitude alone (~1 hPa per 10 m)
swamps the weather signal, and an elevator ride exceeds the "falling very rapidly" storm
band by an order of magnitude.

**Decisive architectural reason to put this on the box, independent of all the above:**
ToolVault executors run **inside the Orchestrator process**. A phone-side weather read is
unreachable from chat, cron, MCP, voice/phone and CU — every consumer that matters. Add
that the web Portal has no Android APIs at all (phone-native = build it twice, ship two
different answers), one credential instead of an extractable key per APK, one cache and
one quota (a single box polling every 10 min ≈ 4,320 calls/month, inside Google's 10k free
tier), and the fact that **the box is stationary and always-on at home — its location *is*
the "what's the weather" location**. The phone is the roaming exception, not the default.

**Architecture:**

```
Orchestrator/weather.py          provider adapters -> internal BbxCondition enum
GET /weather/current             -> {effect, intensity, drift, gust, is_day,
                                     condition, observed_at, stale}
ToolVault/tools/get_weather/     schema.json + executor.py (greenfield - there is
                                 no weather tool today; the agent gets it too)
config.ini [weather]             provider, home lat/lon/label, units, cache_ttl
```

The endpoint returns an **already-resolved effect id**, not raw weather, so the two
surfaces can never disagree and neither ships a mapping table.

**Provider — two adapters behind one interface, BYOK via the wizard, matching every other
credential in this product. Which one ships as DEFAULT is Brandon's call:**
- **Google Maps Platform Weather API — recommended default for a shipping product.** GA
  since 2025-06-30, 10,000 free calls/month then $0.15/1k, plain server-side REST, and it
  fits the existing GCP billing. Each operator's own key = each operator's own free tier,
  so it costs us nothing per box. Requires the "Includes weather data from Google"
  attribution; note the **Japan/Korea coverage gap**, and keep the cache a short-TTL
  refresh cache (not an archive) to stay clear of Maps ToS §3.2.3.
- **Open-Meteo — the keyless zero-setup path.** No key at all, emits WMO codes natively,
  perfect for a personal self-hosted box and for development before a key exists. Its free
  tier is **explicitly non-commercial** (CC-BY 4.0, <10k calls/day; commercial use needs a
  paid plan), so for a product with public pricing it is the *dev / no-key-yet* adapter
  rather than the commercial default. The wizard must state this inline where it is chosen.
- **Normalize at the adapter boundary into our own `BbxCondition` enum.** Google's ~56
  named types and WMO's ~28 numeric codes do **not** map cleanly onto each other; isolating
  this now makes a provider swap painless later.

**Confirmed greenfield (nothing to retrofit):** there is no weather tool among the ~95 in
`ToolVault/tools/`, and `config.ini` has **no location or timezone keys at all** (sections
today: paths, auto_mint, checkpoint, budget, users, context, snapshot, audio, pairing,
models, computer_use, control_phone, retrieval, rerank, benchmark). Add a `[weather]`
section mirroring the shape of `[computer_use]`: `provider`, `home_lat`, `home_lon`,
`units`, `cache_ttl_seconds` (default 600). **Home coordinates come from the onboarding
wizard, never hard-coded** — fresh-box portability rule.

**Location — operator-entered, zero permissions.** A city field in the wizard, geocoded
via the keyless Open-Meteo geocoding API, persisted server-side as lat/lon/timezone/label
so both surfaces agree. **Do not add `ACCESS_*_LOCATION` to the Android manifest** — the
app declares no location permissions today, and that is an asset: adding them triggers a
Play Console Data Safety disclosure and a refusable runtime prompt, to buy accuracy far
finer than any weather grid needs. An optional Portal "Detect my location" *button* (never
an on-load prompt, gated on `navigator.permissions.query()`) can write to the same setting
later.

**Condition → effect mapping** (drives count/speed/drift from intensity and wind):

| Condition | Effect | Modulation |
|---|---|---|
| Clear, day | Bokeh Depth *(or user default)* | — |
| Clear, night | Deep Field + **Meteor Fall** | — |
| Partly cloudy | Slipstream | low density |
| Overcast | **Low Fog**, de-tuned | ≤4% alpha |
| Fog / rime fog | **Low Fog** | full (6% ceiling) |
| Drizzle | **Rainfall**, sparse warm variant | count ×0.4 |
| Rain / showers | **Rainfall** | count + lean ← precipitation + wind |
| Snow / snow showers | **Drift Snow** | count + fall speed ← severity |
| Windy (any condition) | **Cinders on the Wind** | lateral term ← `wind_speed_10m` |
| Thunderstorm | **Rainfall** heavy — *no lightning flash in v1* | gated (WCAG 2.3.1) |

**Failure behavior:** cache last-known-good and serve `stale: true` with a timestamp; on
hard failure fall back to **the user's last manual effect pick**, never to a blank field.
Weather mode is a *wrapper over the existing effect axis* — it writes the resolved effect
id into the same setting the manual picker uses, with the manual choice remembered
separately (`bb_particle_mode_manual` / `particle_mode_manual`), so turning weather off
restores exactly what the user had.

**Optional garnish (only if it delights):** read `TYPE_PRESSURE` on Android (the Fold *does*
have it), report 3-hour tendency with the Met Office 4-band vocabulary, gate on device
motion, and never present it as a forecast.

---

## Verification

1. **The DPR battery** — headless screenshots at `--force-device-scale-factor` 1, 2 and 3
   show identical apparent particle size, increasing crispness. This is the acceptance
   gate for M0 and must be re-run after every web effect lands.
2. `node --test Portal/modules/fx/*.test.mjs` green (sizing + per-effect physics).
3. `./gradlew :app:testDebugUnitTest --offline` green.
4. **Fold device check** — 120 Hz far-layer stars show no stepping; fold/unfold fills the
   inner screen; no frame drops with the settings sheet open (it must stop rendering).
5. **Legibility gate, per effect, both surfaces** — measured contrast of `--muted`
   (#C9C9C9) body text over the effect at peak density must stay ≥ 4.5:1. An effect that
   fails is re-tuned, not shipped.
6. Full suite `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/ -q`.

## Non-goals / guardrails

- No WebGL/WebGPU renderer in v1 (locked). Keep the SoA particle-store refactor in mind
  as the seam that would make one a drop-in later, but do not build it.
- No reactive/audio-reactive accents in v1 (locked).
- The Rising Stars look stays visually unchanged at `dpr=1` — M0 is repair, not redesign.
- Never make `.chat` a stacking context (`_ember-fx.css:25-29` documents why: it re-scopes
  task-monitor z1100, scroll-to-bottom z200, composer z100).
- The "no idle rAF" battery contract is load-bearing: the loop stops when idle, pauses
  when hidden, and generation *detection* stays on `setTimeout` (rAF pauses on hidden tabs).
- `prefers-reduced-motion` continues to disable the field entirely while still persisting
  the settings.
- The Orchestrator serves the Portal **live from the working tree** — a broken
  intermediate state is an immediate outage. Small commits, never a half-refactor.
