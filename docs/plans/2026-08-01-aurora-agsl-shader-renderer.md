# Aurora Waveform — AGSL Shader Renderer (Architecture Proposal)

**Status: PROPOSAL — awaiting Brandon's approval. Not started.**

Product of the 2026-08-01 deep-research pass (4 opus agents: Android platform research,
visualizer design research, analyser dynamics measurement, renderer geometry measurement).
The immediate v33 tuning fixes (reactivity, drift speed, translucency, humps) were applied
to the existing Canvas renderer separately; this document is the *next-level* architecture
Brandon asked for: "the best type of waveform that we can get" from what Android offers.

## TL;DR recommendation

Replace the Canvas path renderer inside `AuroraWaveform` with an **AGSL `RuntimeShader`**
drawn via `ShaderBrush` + `drawRect(brush, blendMode = BlendMode.Plus)`:

- Band energies go in as a float uniform array each frame; the entire aurora — ribbon
  geometry, glow falloff, translucency — is computed **per-pixel on the GPU** in ONE draw
  call. No path building, no 12 draw passes (4 sheets × 3 strokes), near-zero main-thread
  cost at 120 Hz.
- **Bloom is free**: a two-term exponential falloff (`exp(-d*60)*0.9 + exp(-d*16)*0.35`)
  IS the glow — no blur pass, no stacked translucent strokes faking it.
- **Premultiplied alpha + additive blend** makes the ribbons *add light* over the particle
  field instead of occluding it — the correct answer to "can't see anything through them,"
  by construction rather than by alpha-budget tuning.
- Everything upstream is UNTOUCHED: `AuroraAnalyser`, `AuroraVoice`, the per-stream
  analyser wiring, the 3 call sites, speaker→direction mapping. Only the draw internals of
  `AuroraWaveform.kt` change.

## Why this is the "bulletproof" Android answer

| Concern | Canvas paths (current) | AGSL shader (proposed) |
|---|---|---|
| Min API | any | 33 (RuntimeShader). Fold 6 = API 34 ✓ |
| Per-frame CPU | build 4 paths × 24–120 pts, 12 stroke/fill passes on main thread | ~10 uniform floats + one `drawRect` |
| Glow | faked with wide translucent strokes (the occlusion complaint) | exponential falloff per pixel, true additive light |
| Translucency over particles | alpha stacking → 99.2% centre occlusion measured | premultiplied `col*glow` + `BlendMode.Plus`; background always visible |
| 120 Hz headroom | survivable, caps richness | sub-millisecond on Adreno 750; huge headroom for richer looks |
| Precedent | Pixel-style minimalist bars | DuWave (RuntimeShader + Canvas fallback), shady collection, Infocusp AGSL glow — the shipping pattern for premium visualizers |

`android.media.audiofx.Visualizer` was evaluated and **rejected**: it degrades PCM we
already own at full resolution (8-bit, deliberately low quality, extra permission).

## Design sketch

```kotlin
val shader = remember { RuntimeShader(AURORA_AGSL) }
val brush  = remember { ShaderBrush(shader) }
// one withFrameNanos loop drives iTime; band values read in DRAW phase only
Modifier.drawWithCache {
    onDrawBehind {
        shader.setFloatUniform("iResolution", size.width, size.height)
        shader.setFloatUniform("iTime", timeSec)
        shader.setFloatUniformArray("bandsHuman", humanBands)  // 4 floats
        shader.setFloatUniformArray("bandsAi", aiBands)        // 4 floats
        shader.setFloatUniform("travel", ...)                   // per-speaker drift dirs
        drawRect(brush, blendMode = BlendMode.Plus)
    }
}
```

Shader body: for each of 4 bands × up to 2 speakers, a sine crest
`crest = 0.5 ± drive*amp*sin(x*freq*PI + dir*speed*iTime)`, distance-to-crest `d`,
`glow = exp(-d*k1)*a1 + exp(-d*k2)*a2`, colour per band (red/blue/green/amber),
accumulate additively, return premultiplied `half4(col*glow, glow)`.

All the v33-calibrated numbers (crest speed 0.25–0.37 width/s with per-layer shear,
4–5 humps, perceptual gamma in the analyser) carry over verbatim — the shader only
changes HOW the same geometry is drawn.

### Gotchas (from research — build these in from day one)
- Keep coordinates/accumulators `float`; only colours as `half` (fp16 coordinate math
  causes visible banding in smooth gradients — the #1 aurora artifact).
- Read band state in the **draw phase** (`onDrawBehind`), never composition — otherwise
  every audio frame recomposes.
- Premultiplied output (`col * glow`, alpha `glow`) or the blend math is wrong over the
  particle field.
- Additive white-out where sheets cross: budget per-band peak brightness so 4-band overlap
  stays below saturation (same budget discipline as the Canvas alpha ladder).
- No vertex stage in AGSL — 3D-ish depth is faked in fragment math (fine for aurora).

### Fallback / portability
Gate on `Build.VERSION.SDK_INT >= 33`; keep the current (v33-tuned) Canvas renderer as the
fallback path — the DuWave pattern. **Confirmed: the app's `minSdk` is 26** (app/build.gradle),
so the Canvas fallback is REQUIRED, not optional — devices on API 26–32 keep the tuned
Canvas aurora.

### Testing strategy (the honest cost of this move)
The 39 JVM tests pin Canvas geometry functions; a shader is not JVM-testable. Plan:
- Keep every pure-Kotlin function (band mapping, speeds, budgets) testable and pinned —
  the shader receives its constants FROM tested Kotlin.
- Add an adb screenshot loop for iteration (`adb exec-out screencap`) against a debug
  preview screen that feeds synthetic band envelopes — so the look can be verified
  without a live voice session.
- Golden-image comparison is optional/later; the primary gate is Brandon on-device.

## Milestones (when approved)
1. **M0** — debug preview screen (synthetic band driver) + adb screenshot loop.
2. **M1** — AGSL shader renderer behind the same `AuroraWaveform` composable API,
   API-33 gate with Canvas fallback intact.
3. **M2** — on-device iteration with Brandon (the feel pass), then retire or keep the
   Canvas path per minSdk decision.

## Research sources (agent-verified)
- developer.android.com AGSL / RuntimeShader / Compose brush + phases docs
- github.com/POL0i/DuWave (RuntimeShader visualizer + Canvas fallback pattern)
- github.com/drinkthestars/shady, infocusp AGSL glow writeup
- SCSiriWaveformView (Siri reference: dB→linear→gamma-0.5 mapping, phase −0.15 rad/frame,
  per-layer alpha ladder ~0.33–1.0, 3px/1px strokes)
- Milkdrop/projectM band normalisation (slow reference, >1.0 headroom)
- Psychophysics: legible flow ≈ 2–8 deg/s ≈ 0.18–0.7 element-widths/s
