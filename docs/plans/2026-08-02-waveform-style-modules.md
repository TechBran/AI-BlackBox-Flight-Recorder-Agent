# Waveform Style Modules — Selectable Visual Languages for the Voice Layer

**Status: BUILT — all 10 styles shipped in APKs v32-v37 (approved direction from Brandon
2026-08-02: build all styles, then iterate each on-device). Committed 2026-10-17 with the
M1 hardening body; the on-device acceptance walk is
`docs/plans/2026-10-11-m1-fold-walk.md`. Android only** (the web Portal keeps its
waveform — standing exception per the aurora work).

## Goal

Turn the single hard-wired waveform into a **catalogue of selectable waveform styles**,
chosen in Settings with a live preview, driven by Brandon's reference sheet
("BLACK BOX — Waveform Concept Exploration", 8 concepts). One **global** style applies
across all three surfaces (decision: locked). Every style must render **one voice and
two voices simultaneously** (human + AI in one container — the voice-screen case that
Aurora already solves with colour + direction).

## Decisions (locked with Brandon)

1. **One global style** — a single Settings picker; every module implements the
   two-voice contract. No per-role or per-surface pickers (can be added later without
   rework; the dispatcher takes the style as a parameter).
2. **Large live preview** — style list + ONE big preview pane animating the selected
   style with a canned, deterministic synthetic voice loop (fake human + AI taking
   turns). No grid of simultaneous animations.
3. Build **all styles first**, then per-style on-device tweak passes with Brandon.

## Architectural precedent to copy: the particle FieldRegistry

`ui/components/FieldRegistry.kt` is the proven in-repo pattern: ONE catalogue list,
`id` = persisted DataStore value (byte-identical forever), `label` = picker string,
factory lambda per entry; order = display order; first entry = fallback for unknown
stored values. The waveform system is its twin:

```
ui/components/wave/
  WaveformRegistry.kt      // WaveformStyleEntry(id, label, create) + CATALOGUE
  WaveformRenderer.kt      // the per-style contract (interface)
  VoiceVisualizer.kt       // the ONE composable call sites use; dispatches by style
  WaveformFeed.kt          // shared data contract (bands + history) — see below
  WaveformDemoDriver.kt    // deterministic synthetic voices for preview + screenshots
  styles/                  // one file per style renderer
```

**Call sites** (3): `Composer.kt:213`, `AudioPlayerBar.kt:234`, `VoiceScreen.kt:1093`
switch from `AuroraWaveform(...)` to `VoiceVisualizer(style, feed, modifier)`.
`AuroraWaveform` itself becomes style module #1 — **zero visual change for existing
users until they pick something else** (aurora stays the default id).

**Persistence**: `BlackBoxStore.KEY_WAVEFORM_STYLE` (`stringPreferencesKey("waveform_style")`,
default `"aurora"`), exactly like `KEY_PARTICLE_MODE`.

**Settings UI**: new "Voice waveform" section in `SettingsSheet.kt` — style list (from
the CATALOGUE, display order) + the large live preview pane driven by
`WaveformDemoDriver`. The preview IS the real renderer at real size — no mock drawings.

## The shared feed — one analysis pipeline, N looks

All styles consume `WaveformFeed`, which wraps what already exists (the v33-fixed
`AuroraAnalyser` output — its reactivity fixes automatically benefit every style):

- **Per voice** (HUMAN / AI): 4 instantaneous band drives (0..1, post-v33 perceptual
  mapping), an overall level, and a **rolling envelope history ring** (~3 s at the
  analysis rate) — new, cheap: append `out[]`'s level per analyse call.
- **Offline/player path**: `AudioEnvelope`'s per-window envelope + bands ARE the
  history — the ring is the actual past windows around the play cursor (true waveform
  history, better than live).
- **Fine-bin option**: the Segmented Equalizer wants ~16 columns. Two candidate
  sources, decided by measurement during M1: (a) 16-probe Goertzel profile on the same
  analyser (microseconds-cheap, truthful), or (b) 16 columns interpolated from the 4
  real bands with per-column temporal offsets (zero analysis cost, "inspired by"
  rather than truthful). Start with (b); upgrade to (a) if it reads fake.

Who needs what:

| Style | bands | history ring | fine bins | notes |
|---|---|---|---|---|
| 1 Glowing Ribbon | ✓ | — | — | braided multi-strand evolution of aurora |
| 2 Mirrored Stereo Pulse | — | ✓ | — | scrolling bar history, mirrored about centre |
| 3 Segmented Equalizer | ✓ | — | ✓ | LED-block quantised columns |
| 4 Soft Blob Wave | ✓ | — | — | soft mirrored mounds + floating motes |
| 5 Sharp Reactive Line | ✓ | ✓ | — | thin jagged polyline, sharp peaks |
| 6 Circular Orb | ✓ | ✓ | — | glowing ring + radial ticks + dotted side lines |
| 7 Layered Particle Wave | ✓ | — | — | wave surfaces made of dots (reuse particle idioms) |
| 8 Cinematic TTS Wave | — | ✓ | — | dense symmetric glow bars, voiceprint mass |
| 9 Aurora Bands (current) | ✓ | — | — | v33 as-is, default |
| 10 Classic | — | ✓ | — | the pre-aurora `VoiceWaveform` (kept on disk) — free win |

## The two-voice contract (per style)

Each renderer defines both modes up front — no style ships one-voice-only:

| Style | Two voices at once |
|---|---|
| Glowing Ribbon | red strand-bundle (human) + blue strand-bundle (AI), braided |
| Mirrored Pulse | human bars grow UP from centre, AI bars grow DOWN (tinted) |
| Segmented EQ | human columns from bottom, AI columns from top (or interleaved tint) |
| Soft Blob | red-tinted mounds + blue-tinted mounds, additive overlap |
| Sharp Line | two polylines, red + blue, slight vertical offset |
| Circular Orb | human = outer radial ticks, AI = inner ring pulse |
| Particle Wave | two particle sheets, red/blue, depth-offset |
| Cinematic | human top half, AI bottom half of the symmetric mass |
| Aurora | as shipped (colour = band, direction = speaker) |
| Classic | two lines as it did before |

Speaker colours follow the existing convention: red = human, blue = AI (band-coloured
styles like Aurora are the exception, already approved).

## Visual language notes (from the reference sheet)

All on near-black, red/white/grey palette, additive glow where applicable (the v33
`BlendMode.Plus` + alpha-budget discipline — occlusion budget ≤ ~55%, white-out sum
bounded — carries over as a REQUIREMENT for every style so the particle field behind
always survives). Small-height surfaces (40 dp player bar) get a defined degraded mode
per style (e.g. Orb shrinks ring + keeps dotted side lines; Cinematic reduces bar count)
— a style must never clip or mesa at 40 dp.

## Milestones

- **M0 — Architecture + plumbing** (the load-bearing step): registry, renderer
  contract, `VoiceVisualizer` dispatcher, `WaveformFeed` with history ring threaded at
  all 3 call sites, DataStore key, Settings section + live preview + demo driver.
  Aurora (#9) and Classic (#10) wrapped as the first two modules — proves the contract
  with zero new drawing code. Registry invariant tests (unique ids, stable persisted
  ids, fallback-to-first) mirroring FieldRegistry's discipline.
- **M1 — History/bar family**: Sharp Reactive Line, Mirrored Stereo Pulse,
  Cinematic TTS Wave, Segmented Equalizer (fine-bin decision measured here).
- **M2 — Organic family**: Glowing Ribbon, Soft Blob Wave.
- **M3 — Special geometry**: Circular Orb, Layered Particle Wave.
- **M4 — Device iteration**: APK per drop, Brandon walks the catalogue in Settings
  (live preview + real use), per-style tweak passes. Commit only after this pass.

Each of M1–M3 ends with: full JVM suite green + APK built + installed to the Fold.

## Testing

- Per-style JVM tests in the aurora tradition: geometry pins, alpha/occlusion budgets,
  two-voice invariants, NaN/idle safety, 60-vs-120 Hz time-step independence.
- The demo driver is deterministic (seeded) → styles can be screenshot-compared via
  `adb exec-out screencap` against the live preview for iteration without a voice
  session.
- The existing 1,813-test suite must stay green; Aurora's own tests continue to pin
  module #9 unchanged.

## Risks / gotchas

- **Perf**: exactly one style renders at a time (+ one in the settings preview);
  everything stays inside the existing `withFrameNanos`/idle-park discipline Aurora
  established. Particle Wave must budget its dot count like ParticleTuning does.
- **v33 is uncommitted**: this work stacks on it; nothing commits until Brandon
  approves the looks (M4).
- **AGSL**: the shader renderer proposal
  (`docs/plans/2026-08-01-aurora-agsl-shader-renderer.md`) becomes a per-style
  UPGRADE path (Glowing Ribbon and Cinematic want it most). Canvas-first for all ten
  so iteration stays fast and JVM-testable; the registry makes a later AGSL swap
  invisible to call sites.
- **Persisted ids are forever** (FieldRegistry rule): `aurora`, `classic`, `ribbon`,
  `mirrored_pulse`, `segmented_eq`, `soft_blob`, `sharp_line`, `orb`, `particle_wave`,
  `cinematic` — lowercase, never renamed.

## Non-goals

- Web Portal changes (Android-only, standing exception).
- Per-surface/per-role selection (deferred; architecture permits it later).
- User-tunable per-style parameters (colour, speed sliders) — after the catalogue
  lands and Brandon's per-style tweaks identify which knobs matter.
