# Android Voice Agent UI — HD Waveform + Collapsible Settings (Design)

**Date:** 2026-05-30
**Scope:** Android MVP only (`AI_BlackBox_Portal_Android_MVP (2)/.../com/aiblackbox/portal`). Web Portal is intentionally left unchanged.
**Builds on:** `docs/plans/2026-05-19-live-models-upgrade.md` (per-provider live config), `docs/plans/2026-05-19-live-api-ga-migration.md`.

## Goal

Elevate the Android Voice Agent screen (`ui/voice/VoiceScreen.kt`) from a flat, always-visible
settings column into a polished live-voice experience:

1. **Collapsible settings pane** with per-setting **dropdowns**, that **auto-collapses on connect**
   to a one-line summary pill, handing the screen to the visualization.
2. **HD flowing "ribbon" waveform** (Siri-style), amplitude-driven from **real audio** in both
   directions, with distinct warm (user) / cool (AI) palettes fitting the BlackBox aesthetic.
3. **Default Gemini Live model → `gemini-3.1-flash-live-preview`** (newest version + `thinkingLevel`).

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Gemini default model | `gemini-3.1-flash-live-preview` (deliberate override of GA-over-preview rule for this case; unlocks `thinkingLevel` by default) |
| Settings behavior | Auto-collapse on connect → summary pill; tap to re-expand |
| Waveform style | Flowing ribbon (Siri-style), layered translucent gradient sine paths |
| Waveform data | Real RMS amplitude from existing PCM at both ends |
| Web Portal | No changes |

## Section 1 — Architecture & amplitude pipeline

The ribbon is driven by **real loudness**, computed from PCM buffers that already pass through the app:

- **User speaking:** in `VoiceViewModel.startMic()` read loop (`VoiceScreen.kt:322`), each
  `ShortArray` buffer already exists — compute **RMS** (`sqrt(mean(sample/32768f)²)` → 0f..1f) there.
- **AI speaking:** in the playback drain (`VoiceScreen.kt:501`), each decoded PCM chunk gets the
  same RMS before `AudioTrack.write`.

New ViewModel state:
```kotlin
private val _amplitude = MutableStateFlow(0f)      // 0f..1f, latest RMS
val amplitude: StateFlow<Float>
private val _waveSpeaker = MutableStateFlow(WaveSpeaker.IDLE)
val waveSpeaker: StateFlow<WaveSpeaker>            // USER | AI | IDLE
enum class WaveSpeaker { USER, AI, IDLE }
```
- `USER` set while mic is actively sending (not in AI-speaking/post-speech window).
- `AI` set on `audio_delta` / while `isAISpeaking`; back to `IDLE` shortly after `response_complete`.
- Audio writes on `Dispatchers.IO`; UI samples via `collectAsState`. No blocking either side
  (same decoupling philosophy as `audioPlaybackQueue`).

**Why RMS not FFT:** a ribbon expresses loudness-over-time, not per-frequency detail. RMS is one
cheap pass over buffers we already hold. "HD" comes from smoothing + interpolation + layered
rendering, not frequency bins.

## Section 2 — Ribbon rendering (`VoiceWaveform` composable)

New file: `ui/voice/VoiceWaveform.kt`.

- Compose `Canvas`, full-width, ~140dp tall, centered vertical axis.
- **3 layered sine paths** at differing phase offsets, amplitudes, and alphas (e.g. 1.0 / 0.55 / 0.3)
  → translucent depth = the "HD" feel.
- Each path filled with `Brush.horizontalGradient` of the active speaker palette.
- **Amplitude** (eased via `animateFloatAsState`, spring) scales wave height; loud transients glide.
- **Phase** driven by `rememberInfiniteTransition` so the ribbon always flows, even at idle.
- **Idle:** ~8% baseline amplitude, `BbxDim`-tinted — a calm "breathing" ribbon while connected/silent.
- Palette (grounded in `ui/theme/Color.kt`):
  - **USER (warm):** `BbxAccent #FF4A4A → BbxRed #E10600`
  - **AI (cool):** `SolidGreen #27D980 → teal`
  - Palette cross-fades on `waveSpeaker` change (`animateColorAsState`).
- Performance: redraw gated by amplitude/phase state only; no per-sample invalidation.
- Replaces the decorative mic-circle pulse as the primary motion; mic button stays as the
  connect/mute control, optionally nested in/under the ribbon.

## Section 3 — Settings redesign

- **`LabeledDropdown`** (new, in `VoiceScreen.kt` or `ui/components/`): Material3
  `ExposedDropdownMenuBox` wrapper taking `label`, `options: List<Pair<String,String>>`,
  `selectedId`, `enabled`, `onSelect`. Replaces `ChipRowPicker` usages (backend, voice, model,
  VAD start/end, eagerness, thinking level). `enabled=false` while CONNECTED keeps the existing
  audit-I4 binding rule (model/VAD bound at connect time).
- **`SettingsPane`**: `glassSurface` card wrapping all dropdowns in an `AnimatedVisibility` body.
  - Header row always visible: gear icon + title/summary + chevron.
  - `expanded: Boolean` state; `LaunchedEffect(isConnected){ if (isConnected) expanded = false }`
    auto-collapses on connect. User can tap to re-expand any time.
  - **Collapsed summary pill:** `⚙ {backend.displayName} · {voice} ▾` (e.g. "Gemini Live · Orus").
- Backend selector may stay as segmented chips (only 3) or become a dropdown — TBD in plan,
  default to dropdown for consistency with "dropdowns for each setting".
- Transcript + provenance unchanged below the visualization.

## Section 4 — Gemini default model

- `util/Constants.kt:150`: `"gemini-live" to "gemini-3.1-flash-live-preview"`.
- Single source of truth: `VoiceScreen`'s `geminiModel` initial state reads
  `LIVE_MODEL_DEFAULTS["gemini-live"]`; `GEMINI_LIVE_THINKING_CAPABLE_MODELS` already contains
  `gemini-3.1-flash-live-preview`, so the **Thinking level** control shows by default.
- Verify `gemini-3.1-flash-live-preview` is first (or clearly selected) in
  `MODEL_CONFIG["gemini-live"]` so the dropdown reflects the default.

## File change map

| File | Change |
|---|---|
| `util/Constants.kt` | Flip `LIVE_MODEL_DEFAULTS["gemini-live"]`; confirm model-list ordering |
| `ui/voice/VoiceScreen.kt` | RMS calc in mic loop + playback drain; add `amplitude`/`waveSpeaker` flows; mount `VoiceWaveform`; replace `ChipRowPicker` with `LabeledDropdown`; add `SettingsPane` + auto-collapse + summary pill |
| `ui/voice/VoiceWaveform.kt` (new) | Layered-ribbon Canvas composable + palettes |
| `ui/components/` (optional) | `LabeledDropdown` if shared |

## Non-goals / YAGNI

- No FFT / frequency-band visualizer.
- No mid-session model/voice hot-swap (unchanged; bound at connect).
- No web Portal changes.
- No new audio capture paths — reuse existing buffers only.

## Success criteria

- Ribbon visibly reacts to user speech (warm) and AI speech (cool); breathes at idle; smooth (no jitter), no audio glitches/added latency.
- Settings collapse to the summary pill on connect; every setting is a dropdown; re-expandable.
- Fresh install defaults Gemini Live to 3.1 preview with Thinking level visible.
- Web Portal untouched.
