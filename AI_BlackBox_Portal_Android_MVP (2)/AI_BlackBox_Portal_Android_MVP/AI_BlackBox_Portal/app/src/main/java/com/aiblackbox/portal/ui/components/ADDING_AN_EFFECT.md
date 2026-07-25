# Adding a particle-field effect

Porting one of the web effects (`Portal/modules/fx/effects/*.js`) to Android.

**Worked example: `FirefliesField.kt` + `FirefliesFieldTest.kt`.** Read those two
first — everything below is already done there, correctly.

Two files change, ever:

1. **`YourField.kt`** (new, this directory) — the sim + its renderer.
2. **`FieldRegistry.kt`** — ONE line in `FieldEffects.CATALOGUE`.

Plus one new test file. You never touch `ParticleField.kt`, `EmberParticles.kt`,
`SettingsSheet.kt`, or the store. There is no central `when` to edit any more.

---

## 1. The file

```kotlin
package com.aiblackbox.portal.ui.components

class YourSim(
    countScale: Float = 1f,                       // intensity x quality budget dial
    private val rand: FieldRandom = SystemFieldRandom,
) : FieldSim {

    private var width = 0f; private var height = 0f
    private var scale = 1f                        // device px per reference px
    private val countScale = ParticleTuning.sanitizeScale(countScale)
    private var pool = emptyArray<YourParticle>()

    val particles: List<YourParticle> get() = pool.asList()   // tests only

    override fun resize(width: Float, height: Float, scale: Float, density: Float) { … }
    override fun update(nowMs: Double, dtSec: Float, active: Boolean) { … }
    override fun rearm() { … }

    override fun DrawScope.render(res: FieldResources, paints: FieldPaints, nowMs: Double) { … }
}
```

`FieldSim` (in `ParticleField.kt`) is the whole contract:

| hook | when | rules |
|---|---|---|
| `resize(w, h, scale, density)` | size or DPI change | Called from `drawWithCache`. **Return early when nothing changed** — a live field must never re-scatter. Spawn lazily on the first valid size. |
| `update(nowMs, dtSec, active)` | once per frame | `dtSec` is already clamped ≤ 0.05. `active` = "generation in progress"; when false, stop respawning and let the field thin out. |
| `rearm()` | on re-activation | Revive what went dark. Do **not** disturb live particles. |
| `render(res, paints, nowMs)` | draw phase | Read-only w.r.t. sim state. This is a `DrawScope` member extension, so `drawSpriteF(...)`, `drawContext.canvas.nativeCanvas`, `size`, etc. are all in scope. |

`update` must not draw; `render` must not mutate. Everything except `render` is
plain Kotlin, which is what makes the physics JVM-testable.

## 2. Register it — one line

`FieldRegistry.kt`, in `FieldEffects.CATALOGUE`:

```kotlin
FieldEffect("yourid", "Your Label") { n, r -> YourSim(n, r) },
```

`ParticleMode.ALL`, `parse`, `label`, `newFieldSim` and the settings picker all
derive from that list. Two hard rules:

- **The id is a persistence contract.** Lowercase, no spaces, matching the web
  effect's id. Never rename one — it is written to DataStore and read back by
  every future build.
- **Append; never reorder.** `CATALOGUE[0]` (`stars`) is the fallback for any
  unknown/absent stored value, so an id from a newer build can never crash an
  older one.

## 3. Resources

`FieldResources` is the per-`(density, effect)` bag, built once and reused for
every frame. Two ways in:

```kotlin
override fun DrawScope.render(res: FieldResources, paints: FieldPaints, nowMs: Double) {
    val ramp = res.atlas.emberNative        // shared warm blackbody ramp (6 stops)
    val star = res.atlas.starNative         // shared white-hot core
    // effect-private asset, baked on first request, memoized under YOUR key:
    val flake = res.bake("snow.flake") { d -> bakeRadialSprite(214, 236, 255, d) }
    for (p in pool) { … }
}
```

- **Hoist every `res.*` lookup above the particle loop** — once per frame, never
  per particle.
- Key private assets with your effect id (`"snow.flake"`), so two effects can
  never collide.
- `res.atlas` is lazy: an effect that never touches it (matrix) never bakes it.
- **`FieldPaints` is per-overlay scratch** (a mutable `Paint` + `RectF`). Use it,
  never cache it, never share it, never allocate your own in the loop.

Draw a sprite with `drawSpriteF(bitmap, cx, cy, radius, alpha, paints)` — the
float-destination path. `radius` is a radius, not a diameter. It culls at
`alpha <= 0.003` and `radius <= 0.1` for you.

## 4. The DPI contract (the one that ships broken)

Compose canvas coordinates are **device pixels**. `resize` hands you
`scale = density / FIELD_REFERENCE_DENSITY` (3.1 = the Fold-class device the look
was tuned on; `scale == 1` there).

> **Every spatial length — size, radius, velocity, edge margin, noise sampling
> distance — is authored at the reference density and multiplied by `scale`.
> Density may reach NOTHING else: not a count, not an alpha, not a period, not a
> lifetime.**

That is the Android spelling of the web module's law ("devicePixelRatio must
never reach particle geometry"): a 2x-density phone gets 2x the pixels and the
field must look **the same physical size**, just sharper.

Two consequences worth copying from `FirefliesField.kt`:

- Sample noise in *logical* space — `flow(p.x / g, p.y / g, …)` — so the spatial
  frequency of the flow field is the same on every screen.
- Keep the scale factor *out* of stored state and apply it at use, so a density
  or size change re-scales the live field instead of stranding it.

Counts are the exception: they come from **dp area** (`width / density`), never
from pixels — see `EmberSim.targetMax`.

Porting web numbers: web lengths are CSS px through `apparentScale`, which clamps
to 0.5 on a phone-shaped box, and 1 dp = 3.1 reference px. So
`reference px = web CSS px x 0.5 x 3.1 = x 1.55`. Keep that factor as a named
constant (`WEB_TO_REF_PX`) so the tuning tables stay byte-identical to the `.js`
and the two can be diffed forever.

## 5. The dt contract

Pick ONE and say which in a comment at the top of the file:

- **px per second** (fireflies, embers): `p.x += p.vx * dtSec`. Preferred for new
  ports — it is what the web modules already use, so the constants transfer.
- **px per 60 Hz tick** (rising stars): the constants are per-frame, so you must
  normalize: `val dt = (dtSec * 60f).coerceIn(0.25f, 4f)`.

Never mix them inside one effect. A test proves it: run the same sim at 1/60 for
N frames and 1/120 for 2N and require the same path (see
`FirefliesFieldTest.60 Hz and 120 Hz travel the same path`).

Also: advance any private phase clock **by `dtSec`**, never by reading `nowMs` —
otherwise a parked frame loop teleports the whole field to a new common phase on
resume, and everything flashes in unison.

## 6. No allocation in the hot loop

`update` and `render` run up to 120x/second. They must allocate **nothing**.

- Fixed-capacity `Array<T>` pool, recycled in place. A slot object lives for the
  whole session; "death" is a flag, not a `remove()`.
- No lambdas, no `map`/`filter`/`forEach` with capture, no boxing, no `Pair`, no
  string building, no `Brush`/`Paint`/`Path`/`RectF` construction.
- Randomness through `FieldRandom` (a `fun interface` returning a primitive
  `Double`, so it does not box — `() -> Double` would).
- Bake every bitmap through `FieldResources`.
- Use an `IntArray` free-list if you need O(1) spawn (see `EmberSim.kill`).

## 7. The test

`app/src/test/java/com/aiblackbox/portal/YourFieldTest.kt`. Plain JUnit, no
Compose, no Robolectric — drive the sim with a **seeded** `FieldRandom` so every
assertion is deterministic:

```kotlin
private fun seeded(seed: Int = 1): FieldRandom { … mulberry32 … }
private fun sim() = YourSim(1f, seeded(42)).also { it.resize(1080f, 2400f, 1f, 3.1f) }
```

Assert on **invariants, never coordinates**. The checklist that caught real bugs:

- population/count stays inside its bounds across the whole `countScale` range
  and on tiny + huge canvases;
- nothing escapes the viewport and nothing goes non-finite after 1000 frames;
- 60 Hz and 120 Hz travel the same path;
- **density changes resolution, never apparent geometry** — same seed at
  `(w, h, scale 1, density 3.1)` and `(2w, 2h, scale 2, density 6.2)` must give
  exactly 2x every coordinate and the *same* particle count;
- idle thins the field out; `rearm` revives it without desynchronising anything;
- slots are reused (`p === originalSlot`) — proves the pool is not reallocating;
- the run is reproducible from the seed (proves nothing calls `Math.random()`);
- whatever "makes or breaks" YOUR effect. For fireflies that is the pulse
  desync — distinct periods **and** distinct phases, spread across the cycle, and
  under 60% of the population peaking on any frame over a full minute. Port the
  equivalent assertion from the web `.test.mjs`.

Run them (gradle is serial here — one task at a time):

```bash
cd "…/AI_BlackBox_Portal"
./gradlew :app:testDebugUnitTest --offline
./gradlew :app:compileDebugKotlin --offline
```

## 8. Don't

- Don't touch `StarSim` / `EmberSim` / `MatrixSim`. Rising Stars is approved
  verbatim; `ParticleFieldTest` pins the 240-star count **by design**. Fork,
  never mutate.
- Don't change `EmberOverlay` / `EmberBackdrop` signatures — six call sites.
- Don't reorder or rename catalogue ids.
- Don't add a field to `FieldSprites` — use `FieldResources.bake`.
- Don't read `density` for anything but geometry and dp-derived counts.
