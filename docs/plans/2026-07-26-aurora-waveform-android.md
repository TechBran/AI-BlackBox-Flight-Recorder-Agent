# Aurora Waveform (Android) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task.

**Goal:** Replace BlackBox's Compose `VoiceWaveform` with the aurora-sheet ribbon from
Whisper Everywhere, driven by 4-band Goertzel analysis — **red for the human, blue for
the AI, both able to render simultaneously in one container** — and fix the defects the
source has, chief among them a startup lag that is **6 seconds on the TTS path**.

**Architecture:** One Compose renderer, one analyser instance *per audio stream*. The
existing amplitude plumbing and all three call sites stay; what changes is the renderer
and a new band-analysis stage between the PCM and the view.

**Tech Stack:** Kotlin / Jetpack Compose Canvas, Android minSdk 26. No new dependency.

**Scope: ANDROID ONLY.** The web Portal keeps its current waveform — Brandon was explicit.
No three-surface obligation for this feature.

---

## Brandon's locked decisions (2026-07-26)

- **Colour: no gradient.** *"The gradient in the middle I never really liked."* **RED =
  human, BLUE = AI.** Solid identity per speaker, not a six-stop rainbow.
- **Both speakers can be live in the SAME pill at once** — *"when we have an AI model and
  the user speaking and talking at the same time, we need a way to differentiate the
  waves."* This is the load-bearing decision; see M2.
- Same red/blue treatment for **normal TTS and for transcription**, not just voice-agent.
- **Adapt per surface, one shared renderer** — geometry derived from the height it is given.
- **Ribbon only, nothing behind it.** No blob, no scrim.
- **Fix the real defects AND re-tune the per-band gain on BlackBox hardware.**

---

## Context

### What the source actually is

Despite the name, **`BarWaveformView` draws no bars.** It renders **4 translucent
"aurora" sheets** — closed paths between a crest sine and a phase-shifted bottom sine —
each drawn three times per frame: a fill body, a wide low-alpha bloom stroke, and a bright
thin crest line. The name is a leftover from a replaced EQ-bar version.

Source (Brandon's own repo, GPL-3.0, **he holds copyright** so copying into BlackBox is
unencumbered): `github.com/TechBran/-whisper-everywhere`, cloned for reference.

**What makes it look expensive is the analysis, not the drawing:**
- `AudioBands.analyze()` runs **4 Goertzel probes** (a targeted, microseconds-cheap
  alternative to a full FFT) at `[[120,220],[500,800],[1600,2600],[5000,6800]] Hz` —
  voicing / vowels / formants / sibilance — with spectral-tilt gains `[1.0,1.4,2.6,4.0]`.
- **Per-band AGC**, because absolute mic level varies wildly by device and distance;
  without it "the bands sit at 0.1–0.3 and the visuals look sleepy".
- **Asymmetric envelope**: attack `0.65` (a syllable lands in one frame), release `0.10`,
  with **staggered per-band releases** `[0.055,0.085,0.115,0.16]` so bass lingers and
  sibilance snaps away. This asymmetry is what makes it feel connected to the voice.
- **32 ms chunks** on the mic — the source's own comment records this as the fix for "the
  bubble can't track the voice" at the old 128 ms cadence.

### The defects (why this is an uplift, not a port)

**1. THE STARTUP LAG — Brandon's reported symptom, root-caused.**
`AudioBands.kt:79`: `warmScale = 0.35f + 0.65f * (warmupFrames / WARMUP_CHUNKS)` with
`WARMUP_CHUNKS = 60`. Deliberate — it replaced a "calibration slam" — but it counts
**chunks, not time**:

| Source | Chunk | Warm-up |
|---|---|---|
| Mic | 32 ms | **1.9 s** |
| TTS | 100 ms (`sampleRate/10`) | **6.0 s** |

The same constant yields a 3× longer ramp on playback purely because the TTS tap is
coarser. Compounding it, the per-band AGC starts from hardcoded `PEAK_INIT` guesses and
converges at `0.995`/frame, so early audio is also mis-scaled.

**2. Motion is frame-counted, not time-based.** `phase += 0.13f`, `ATTACK 0.65`,
`RELEASE 0.10` all apply once per vsync. On a 120 Hz Fold the wave flows **twice as fast**
as designed and the release is twice as quick. (The sibling `BlobView` does this
correctly with measured dt — so the fix is already demonstrated in the same repo.)
Equivalent 60 fps time constants: phase ≈ **7.8 rad/s**; attack τ ≈ **16 ms**; global
release τ ≈ **158 ms**; band releases τ ≈ **[295,188,137,96] ms**.

**3. TTS feeds 24 kHz PCM into a 16 kHz-hardcoded analyser.** Kokoro runs at 24 kHz;
`AudioBands.SAMPLE_RATE` is `16000f`, so the probes land at **1.5×** their nominal
frequencies — the designed speech bands are not where they are meant to be on playback.
It still animates, which is why nobody noticed.

**4. `AudioBands` is a global mutable singleton.** Shared `peaks`/`warmupFrames`. Fine
when one source is live at a time — **fatal here**, because Brandon's red+blue decision
means mic and TTS render *simultaneously* and would fight over one normaliser.

### What BlackBox already has (the good news)

`VoiceWaveform(amplitude, speaker, …)` — 3 stroked sine paths, symmetric easing, single
global RMS, no bands. Called from exactly three places, **all of which stay**:

| Surface | Call site |
|---|---|
| Chat composer mic | `ui/chat/Composer.kt:205` |
| TTS playback | `ui/components/AudioPlayerBar.kt:221` |
| Voice screen | `ui/voice/VoiceScreen.kt:1326` |

RMS already exists for both directions (`rmsAmplitude(ShortArray)` /
`rmsAmplitudeFromBytes(ByteArray)`). **The plumbing is done; this is a renderer swap plus
a band stage.**

### ⚠️ CORRECTION after reading BlackBox's actual audio paths (2026-07-26)

The three call sites are fed by **four sources that are not the same shape**, and two facts
here overturn assumptions carried over from the source repo:

| # | Source | Feed | Rate | Cadence |
|---|---|---|---|---|
| 1 | Composer mic (STT stream) | `SttStreamClient.kt:357`, live PCM | **24000** | AudioRecord buffer |
| 2 | Voice-screen mic | `VoiceScreen.kt:609`, live PCM | **16000 _or_ 24000** (`:549` — GPT_REALTIME is 24k) | AudioRecord buffer |
| 3 | Voice-screen AI stream | `VoiceScreen.kt:808`, live PCM | **24000** (`:698`) | queue chunk |
| 4 | TTS file playback | `AudioEnvelope.decode` → `AudioPlaybackManager:217` | the **file's own** rate | **20 ms, PRE-DECODED** |

**FACT 1 — the sample rate is not a constant, it is a runtime variable.** Parameterising it
is therefore not a tidy-up of an upstream bug; it is a hard correctness requirement here.
A hardcoded `16000f` would mis-probe **three of the four sources** by 1.5×, including the
composer mic that Brandon uses most.

**FACT 2 — the TTS playback path has no live tap and needs none, so its startup lag can be
deleted outright rather than shortened.** `AudioEnvelope.decode()` already walks the whole
decoded PCM offline and builds a 20 ms RMS envelope peak-normalised **over the entire clip**.
The 4 Goertzel bands belong in that same loop, at the file's true sample rate,
peak-normalised per band over the whole clip. Consequences:

- **No warm-up, no AGC, no convergence on this path at all** — you know the entire clip
  before drawing frame one. The 6-second TTS lag does not get faster; it ceases to exist.
- It is already 20 ms — *finer* than the mic's ~32 ms. The plan's earlier "cut the TTS tap
  to 32 ms" item is **void**; there was never a `sampleRate/10` tap in BlackBox. That was
  the source repo's TTS path, not this one.
- Cost is negligible: 8 multiply-adds per sample inside a loop already doing MediaCodec work.

Live band analysis (warm-up, AGC, envelope) is therefore needed for sources **1, 2 and 3
only**. Source 4 gets an offline analyser with a different, simpler contract.

---

## Milestones

### M1 — The analyser: `AuroraAnalyser`, instantiable and time-based

**Files:** create `ui/components/aurora/AuroraAnalyser.kt`, `AuroraAnalyserTest.kt`

Port `AudioBands` as a **class, not an object** — one instance per stream. This is
non-negotiable given red+blue run concurrently.

1. **Test first.** Two instances fed different signals must not affect each other's
   output. Assert cross-contamination is impossible, because a shared singleton is
   exactly the bug being designed out.
2. **Parameterise the sample rate.** Constructor takes it; the Goertzel probe
   coefficients derive from it. Test that a 24 kHz stream and a 16 kHz stream carrying the
   *same tone* report that tone in the *same band* — three of BlackBox's four sources run
   at 24 kHz, so a hardcoded 16 kHz is wrong almost everywhere here (see the correction
   table above), not just on TTS.
3. **Time-based warm-up.** Replace `warmupFrames/60` with elapsed **milliseconds**
   (~600 ms, down from an effective 1.9 s / 6.0 s). Test that mic-cadence and
   TTS-cadence chunk streams reach full scale at the *same wall-clock time* — the exact
   defect Brandon reported, pinned.
4. **Prime the AGC from the first real frame** rather than converging from `PEAK_INIT`.
   Seed `peaks[b]` from the first analysed chunk (clamped to sane bounds), so the first
   audible syllable is already correctly scaled. Keep the `0.6` peak-rise blend so one
   plosive still cannot rescale the whole session.
5. **Time-based envelope.** Convert attack/release/band-releases to per-second constants
   using measured dt. Test that a 60 Hz and a 120 Hz frame sequence produce the same
   level after the same elapsed time.
6. **A second, offline entry point for pre-decoded audio.** `analyzeOffline(pcm, rate)`
   returning per-window bands **peak-normalised over the whole clip** — no warm-up, no
   AGC, no time dependence, because the entire signal is in hand. Test that identical
   audio at 16 kHz and 24 kHz yields the same normalised band envelope, and that the
   result is **deterministic** (the same input twice gives byte-identical output) — which
   the live path, being stateful, can never be. This is what M3 wires into
   `AudioEnvelope`.

### M2 — The renderer: `AuroraWaveform`, two voices in one box

**Files:** create `ui/components/aurora/AuroraWaveform.kt`, `AuroraWaveformTest.kt`

The visual, per Brandon: **red = human, blue = AI, both able to be live at once.**

1. **API takes a LIST of voices**, not one amplitude:
   `AuroraWaveform(voices: List<AuroraVoice>, modifier)` where an `AuroraVoice` carries
   its speaker (HUMAN/AI), its band levels, and its own analyser instance. One voice is
   the ordinary case; two is the voice-agent case.
2. **No gradient.** Solid hue per speaker with depth conveyed by the existing per-sheet
   **alpha ladder** (fill 70/57/44/32, bloom 34/28/23/18, crest 200/160/120/80) rather
   than a rainbow. Red for HUMAN, blue for AI, taken from theme tokens.
3. **Differentiate the two when both are live.** Colour alone is not enough if the sheets
   sit on top of each other — offset their phase and vertical drift so the two ribbons
   weave visibly rather than overlapping into mush. This is a *look* decision: build it,
   then show Brandon before tuning further.
4. **Geometry derived from the given height**, not the source's 160×56dp pill. The
   amplitude coefficient (`height*0.46`), the drift and the inset all scale from actual
   size. Test the same voice at composer, player-bar and voice-screen heights and assert
   the sheets stay inside bounds at full drive.
5. **Fully transparent** — no background, no scrim. The particle field (Ledger Rain by
   default) must show straight through the ≤27%-alpha sheets. **This project has twice
   shipped an opaque container that hid the field** (the assistant bubble, and
   `AudioPlayerBar`'s `WaveBg`); a test asserts no fill covers the bounds.
6. **No allocation in the draw path**, and **no idle animation loop** — the project's
   battery contract. When every voice is silent and settled, stop requesting frames.

### M3 — Wire the four sources into the three call sites

Replace `VoiceWaveform` at `Composer.kt:205`, `AudioPlayerBar.kt:221`,
`VoiceScreen.kt:1326`. Every **live** source gets its **own analyser instance**, seeded
with **that source's actual sample rate** — never a constant:

- **Composer mic** — `SttStreamClient.kt:357`, 24 kHz. One live analyser.
- **Voice-screen mic** — `VoiceScreen.kt:609`; the rate is chosen at `:549` per backend
  (24 k for GPT Realtime, else 16 k), so the analyser must be built *after* that choice
  and rebuilt if the backend changes.
- **Voice-screen AI stream** — `VoiceScreen.kt:808`, 24 kHz. A **separate** live analyser
  from the mic's; this pair is the red+blue case and is precisely why they cannot share.
- **TTS playback** — extend `AudioEnvelope.decode` to emit per-window **bands** alongside
  the existing RMS envelope, using `analyzeOffline` at the file's real sample rate, and
  have `AudioPlaybackManager` carry them through to `AudioPlayerBar`. **No live analyser
  on this path at all.**

Voice screen is where **both voices can be live** — wire the mic analyser and the AI-stream
analyser into one `AuroraWaveform` as two `AuroraVoice`s.

Delete `VoiceWaveform` only once all three sites are migrated and Brandon has seen it.

#### M3 decisions recorded (2026-07-26, in review)

Three things came up in review that a later reader would otherwise re-litigate:

1. **The player bar rests FLAT, the mic surfaces do not.** `AuroraWaveform` takes a `restLevel`
   (default `Aurora.BASELINE = 0.06`); `AudioPlayerBar` passes `0f`. A silent passage *inside* a
   playing clip keeps its voice PRESENT with all-zero bands, so without this the bar would settle
   ~1.1dp up on its 40dp height instead of flat — which is not what `VoiceWaveform`'s
   `idleLevel = 0f` did there, and the bar's ribbon sits directly on the scrubbable progress
   track. A live mic keeps the breathing baseline deliberately: a mic ribbon that vanishes between
   syllables reads as broken. Pinned by two tests in `AuroraWaveformTest` (the engine floor and the
   call site).
2. **A PRESENT voice always paints — "flat" means a flat ribbon, never a blank box.** Resting at
   `restLevel = 0f` puts every sheet at exactly 0, which is *below* `Aurora.MIN_VISIBLE_DRIVE`, so
   the renderer's cull would have skipped the player bar's sheets entirely: not-playing bars in a
   message list would have gone from `VoiceWaveform`'s flat line (`drawRibbon` at
   `heightFraction = 0` strokes the centre line across the bar) to an empty 40dp box with a
   progress track, and a silent passage mid-clip would have blinked the ribbon out and back.
   `Aurora.paintsSheet(present, drive)` therefore gates on the *drive* only for an **absent**
   voice; a present one is drawn however flat it is, and at drive 0 the geometry still yields the
   flat crest plus the body sliver hanging under it. `AudioPlayerBar` matches this by keeping its
   voice present (`AURORA_SILENT_BANDS`) when nothing is playing rather than passing `null` —
   absence is reserved for a stream that has genuinely gone, which must still retire off the
   surface. It costs no frames: a present, silent, settled voice parks exactly as an absent one
   does. Pinned by `a present but settled zero-drive voice still paints a flat ribbon` and
   `the player bar keeps its ribbon present while idle`.
3. **The band pass has NO length cap.** The first implementation buffered the decoded clip's PCM
   (Goertzel needs contiguous samples; the per-band normalisation needs the whole clip measured
   before anything is published) and capped it at 4M samples — which silently dropped bands past
   ~2.9 min at 24 kHz / ~1.6 min at 44.1 kHz, i.e. **every `elevenlabs_music` track**, since those
   run up to five minutes through this same player bar. The cap was **removed, not raised**:
   `AuroraOfflineBands` accumulates 4 floats per 20 ms window as the decoder hands PCM over, so
   memory is O(windows) (~240 KB for five minutes) instead of O(samples) (~26 MB), and
   `AuroraAnalyser.analyzeOffline` now runs through the same class so the two cannot drift. Bands
   are therefore unconditional; the RMS-only fallback in `AudioPlaybackManager` survives for the
   one case left — a container that declares an unusable sample rate. `AuroraAnalyserTest` proves
   a five-minute 44.1 kHz clip is analysed to its last window.

### M4 — Re-tune the per-band gain on BlackBox hardware

Brandon chose this explicitly. The shipped `PEAK_INIT`/`PEAK_FLOOR` were measured on **one
Fold 6 speaker→mic loop** (2026-07-18, 305 voiced chunks, far-field p90
`[0.017,0.045,0.077,0.006]`).

Port the source's instrumented harness (`AudioBandsCalibrationTest`, which plays a known
WAV through the speaker into the mic and logs per-band raw/peak/output), run it on the
Fold, and re-derive. Note the TTS path needs its **own** figures — it is a direct PCM tap,
not an acoustic loop, so its levels are completely different.

### M5 — Upstream the fixes

Per Brandon: fixes that belong to Whisper Everywhere go back as a **PR on that repo**, so
BlackBox does not quietly fork a bug he then has in two places. Candidates: the
chunk-counted warm-up, the frame-counted motion, the 24 kHz/16 kHz mismatch, and the
singleton analyser.

---

## Verification

1. `./gradlew :app:testDebugUnitTest --offline` green (gradle runs **serially**).
2. **The reported defect, measured:** instrument first-visible-response time on both mic
   and TTS. Target well under a second on both, and **the same** on both — the whole
   point is that the two paths no longer differ by 3×.
3. **120 Hz check on the Fold:** the wave must flow at the same speed as at 60 Hz.
4. **Device look-check with Brandon** before deleting `VoiceWaveform`: mic alone, TTS
   alone, and both at once on the voice screen.
5. **Backdrop check:** ribbon over Ledger Rain — particles visible through the sheets,
   text still legible.

## Non-goals

- The web Portal. Android only.
- `BlobView` and the bubble chrome — no blob, no scrim, no processing ring, no timer.
- No new dependency; Goertzel is a few lines of arithmetic, not a DSP library.
- Do not "restore parity" with the source's constants where this plan deliberately
  diverges (time-based units, per-stream analysers, solid colours) — those are the fixes.
