# On-Device Gemma (`local` Provider) — Design

**Status:** Design / brainstorming output. Precedes the Superpowers implementation plan.
**Date:** 2026-06-14
**Author:** Claude (Opus 4.8) with Brandon

---

## Goal

Add a **`local` model provider** to the BlackBox whose models — **Gemma 4 E2B and E4B** — run *entirely on the user's own Android phone*, with the BlackBox acting as the model's tool/memory server rather than its host. Selecting `local → Gemma 4 E4B` (or E2B) in the BlackBox Android MVP picker behaves like any other model choice in the UI, but routes the turn to the **on-device** LiteRT-LM engine instead of the cloud `/chat` endpoint. The on-device agent loop can then:

- **Control the phone** natively from the inside (open apps, tap, type, swipe, read the screen) to do real tasks — "use my phone," "log into Facebook and check messages," "use this app."
- **Reach the full BlackBox tool suite** (the entire ToolVault) + memory search + image/video/music generation over the Tailscale mesh, on demand.
- **Render everything into the existing BlackBox UI** and use the existing **TTS/STT** endpoints — the on-device model is a drop-in for the cloud model behind the same chat + voice pipeline.

This is an **architectural inversion** of the existing `control_android_device` tool, which is *outside-in* (the cloud Orchestrator drives a phone over ADB using Gemini Computer Use). The new `local` provider is *inside-out*: **the model is the pilot, running on the device; the BlackBox is its exosuit.**

---

## Guiding principle

> The Gemma 4 model has *residency* over the phone. The BlackBox gives that model memory, senses, and the full tool suite — but the model lives on the device, and only the operator on that device can use it.

Everything below keeps the cloud-provider path **unchanged** and is purely **additive**: the Orchestrator gains a small tool-bridge + device-registry binding; the Android app gains a runtime + actuators. The existing `control_android_device` ADB/CU tool stays exactly as-is for its different (outside-in) use case.

---

## Locked decisions

| Decision | Choice | Rationale |
|---|---|---|
| **Loop topology** | **On-device loop; BlackBox = remote tool/memory server** | Gemma runs the full agentic loop on the phone. Offline = native phone control + on-device reasoning still work; only BlackBox-dependent tools go dark. |
| **Routing / UX** | **`local` is a first-class provider in the picker.** Selecting it tells the *Android app* to run the turn on the phone's LiteRT-LM engine, not POST to cloud `/chat`. | Peer to the cloud providers; only the inference engine is swapped. |
| **Multi-tenant binding** | **Strict per-operator device binding.** Each operator's phone is *their* local model; the local provider is only available to that operator on the device where their Gemma is installed. | There is no server-side path to reach another user's on-device model — it is **not** an endpoint, so "use my phone" cannot be invoked against someone else's phone. |
| **Tool surface** | **Tiered + semantic tool retrieval.** ~12 phone actuators + one `search_tools` meta-tool are always resident; all other BlackBox tools are pulled on demand via ToolVault embeddings. | A 2–4B model can't hold 100+ schemas. Reuses ToolVault v2's existing embeddings + `meta_tool.py`; any new ToolVault tool is instantly reachable from the phone with zero app changes. |
| **Screen perception** | **UI-tree first, Gemma-vision fallback.** AccessibilityService UI tree is primary (structured, cheap, reliable — real element bounds/IDs); MediaProjection screenshots feed Gemma's vision only when the tree is thin (WebViews, canvas, games, overlays). | Best accuracy/battery balance; degrades gracefully. Node IDs become stable tool args (`tap(node_id)`) instead of fragile pixels. |
| **Autonomy** | **System-menu toggle: YOLO vs Permission.** Permission is the default. | Permission gates a defined high-consequence set (login/credential submit, send message/email, public post, payment/purchase, delete, grant permission, install app) behind a one-tap confirm; YOLO resolves immediately. Enforced deterministically at the **actuator**, not in the model's judgment. |
| **Model source** | **BlackBox-mirrored over Tailscale** (default, revisitable). | Orchestrator fetches the `.litertlm` bundles once server-side (project HF token + Gemma license), phones download from the hub. No per-user HF friction; trivial verification (server served the bytes + checksum back). Cost: a few GB of server storage + honoring Gemma redistribution terms (allowed for this private/hub use). |
| **Runtime** | **LiteRT-LM** (Kotlin), `.litertlm` Q4 bundles, CPU/GPU/NPU | MediaPipe LLM Inference is maintenance-only; LiteRT-LM is the supported path. |
| **Tool calling** | **AI Edge On-Device Function Calling SDK** | Emits *structured* tool calls on-device (not just text). |
| **Phone control muscle** | **Android AccessibilityService** | Full read of the live UI tree + dispatch of taps/types/swipes/gestures into any app, no root. |
| **UI / voice** | **On-device turn is a drop-in for the cloud turn.** Tokens render into the existing chat UI; voice uses the existing `/ws/stt` (STT) + `/tts/catalog` + TTS endpoints. | A voice turn is hybrid: cloud STT → on-device Gemma → cloud TTS. We deliberately do **not** use Gemma's native audio I/O (keeps voice quality + voice-catalog SoT consistent across providers). |
| **Credentials** | **Agent orchestrates logins but never possesses the secret.** | Reuse Android Credential Manager / Autofill (passkeys + saved passwords); hand off to the user for unsaved credentials; hard-redact password fields (`isPassword`) from `read_screen`, vision screenshots, and `/chat/save` payloads. |
| **Memory** | **On-device sessions write back via the existing cheap `/chat/save` auto-mint path**, tagged `provider=local`. | Phone conversations become searchable snapshots like everything else. Offline → queue the mint, flush on reconnect. |

---

## Architecture: the inversion

Today: *cloud providers ← Orchestrator → Portal/Android clients*. The phone is a dumb target.

New: an **on-device pilot** running inside the Android app that treats the Orchestrator as its **server**.

### Four layers (all Google-supplied except the bridge)

| Layer | What | Tech |
|---|---|---|
| **Runtime** | Run Gemma 4 on the phone | **LiteRT-LM** (Kotlin), `.litertlm` Q4 bundles, CPU/GPU/NPU |
| **Loop** | Agentic reasoning + structured tool calls | **AI Edge Function Calling SDK** |
| **Actuators** | The two "hands" the loop can use | **AccessibilityService** (phone, UI-tree-first) + **HTTP tool-bridge** (BlackBox over Tailscale) |
| **Acquisition** | Get the model onto the phone | Gallery-style downloader → **BlackBox-mirrored** bundles |

The hard, novel part is **not** running Gemma (Google hands you that) — it is the **tool-routing bridge** that lets a small on-device model reach the BlackBox's 100+ ToolVault tools without drowning in schemas.

---

## Orchestrator components (small — most code is in the app)

All five reuse existing infrastructure:

1. **`local` provider in the centralized model catalog** — add `gemma-4-e2b` / `gemma-4-e4b` (the `2026-05-18-model-list-centralization` SoT), flagged `on_device: true`. The Orchestrator has **no server-side inference path** for these; the catalog entry exists only so the picker can render them, surfaced to an operator *conditionally* (only if that operator has a verified device).

2. **Tool-bridge endpoints** (two thin routes, modeled on the proven `POST /gmail/execute` pattern that already routes external callers through `execute_tool()`):
   - `POST /local/tools/search` → wraps `meta_tool.py` semantic search → returns top-k tool schemas.
   - `POST /local/tools/execute` → `{operator, tool_name, params}` → runs through ToolVault `execute_tool()` with a `ToolContext(operator=…)` → returns `ToolResult`.

   This is the **entire** bridge. Any tool added to ToolVault is reachable from the phone with no further work.

3. **Device-registry binding** — extend `Orchestrator/device_registry/registry.py` with on-device records:
   `operator → device_id → {model_slug, version, sha256, delegate, autonomy_mode, verified_at}`.
   Endpoints: `POST /local/device/attest` (app re-attests on every open) + `GET /local/device/status` (drives picker availability).

4. **Model mirror** — `GET /local/models/catalog` (downloadable bundles + sizes + spec recommendations) and a range/resume-capable `GET /local/models/download/{slug}`, fetched once server-side with the project HF token; checksums published for verification.

5. **Persona endpoint** — `GET /local/system-prompt?operator=` returns the `behavioral_core.py` persona/anti-sycophancy text (server-injected for cloud providers) so the app can cache it and keep persona identical *and* offline-capable after first fetch.

6. **Memory write-back** — on-device sessions POST to the existing `/chat/save` auto-mint path, tagged `provider=local`.

---

## Android app components (most of the new code)

1. **LiteRT-LM runtime wrapper** — loads the `.litertlm` bundle, selects delegate (CPU/GPU/NPU), manages the session, exposes a streaming token callback.
2. **FC-SDK agent loop** — drives reasoning, parses structured tool calls, dispatches them, feeds results back, streams assistant text to the chat sink.
3. **Actuator A — AccessibilityService** — the ~12 resident phone tools (`tap`, `type`, `swipe`, `scroll`, `open_app`, `back`, `home`, `read_screen`…). UI-tree-first; MediaProjection screenshot → Gemma-vision fallback when the tree is thin. Wraps high-consequence tools in the confirm-gate per the autonomy toggle. **Password-field redaction at this layer**, upstream of both the model and the ledger.
4. **Actuator B — tool-bridge client** — HTTP to `/local/tools/search` + `/local/tools/execute` over Tailscale; injects retrieved schemas into the next loop turn.
5. **Chat UI integration** — on-device tokens flow into the *existing* message renderer (tool calls + results inline); picker shows `local / Gemma 4 …` only when the device is verified.
6. **Voice integration** — reuse `/ws/stt` + `/tts/catalog` + TTS; only the reasoning middle is on-device.
7. **System-menu Model Manager** — download from the BlackBox mirror (progress/resume), checksum-verify + attest, switch/delete model, spec-based E2B-vs-E4B recommendation, the **YOLO/Permission** toggle, and the AccessibilityService enablement flow.

---

## End-to-end data flow

Representative turn — *"Check my Facebook messages and save anything important to memory"* (voice, Permission mode):

1. **Input** — voice → `/ws/stt` (cloud) → transcript. (Text input skips this.)
2. **Prompt assembly** (on-device) — persona (cached) + the 12 resident actuators + `search_tools` + history.
3. **Loop** (LiteRT-LM + FC SDK, on-device):
   - `open_app("Facebook")` → AccessibilityService dispatches, returns UI-tree.
   - `tap(node_id=Messenger)` → `read_screen()` → reasons over messages.
   - Needs a BlackBox capability → `search_tools("save important note to memory")` → `/local/tools/search` returns the matching schema → next turn Gemma calls it → `/local/tools/execute` runs it server-side → result returns.
   - If it decided to *reply* (high-consequence) → Permission mode pops the confirm-gate before `type`/send.
4. **Render** — streaming tokens → existing chat UI.
5. **Output** — final text → `/tts/catalog` TTS (cloud) → playback.
6. **Persist** — session → `/chat/save` auto-mint, tagged `provider=local`.

### Offline degradation matrix

| Capability | Online (mesh) | Offline |
|---|---|---|
| On-device chat / reasoning (text) | ✓ | ✓ |
| Phone control (Accessibility + vision) | ✓ | ✓ |
| BlackBox tools (`search`/`execute`) | ✓ | ✗ → graceful "needs connection" |
| Memory search / write-back | ✓ | ✗ → **queue mint, flush on reconnect** |
| Voice (STT/TTS) | ✓ | ✗ → text-only |
| Model download | ✓ | n/a |

**Honest split:** cognition + actuation are local; knowledge + voice are cloud. The **queued mint** keeps the memory ledger gap-free even after offline use.

---

## Credentials & security

- **Never possess the secret.** Reuse Android Credential Manager / Autofill: the agent navigates to the login screen and focuses the field; the system picker surfaces the saved password/passkey (user-tap in Permission mode; biometric-gated auto-fill in YOLO). The secret flows OS→app, never through Gemma.
- **No saved credential?** Agent navigates to the form and **hands off** — the confirm-gate becomes "enter your password"; the user types it directly; the agent resumes.
- **Hard redaction.** AccessibilityService flags `isPassword` nodes; we mask them out of `read_screen`, vision screenshots, and any `/chat/save` payload. Credentials are structurally unable to reach the model or the ledger.
- **Isolation.** The local provider is never server-reachable; cross-user invocation is impossible by construction. Operator is caller-asserted, consistent with the Tailscale perimeter trust model.
- **AccessibilityService is god-mode** — Phase 5 includes a dedicated security review of this surface + the redaction guarantees.

---

## Multimodal scope

- **In:** text + image natively on-device (Gemma 4 is multimodal) — "what's in this photo / on this screen" works *offline*. Audio-in still uses cloud `/ws/stt` for voice-quality consistency.
- **Out:** Gemma emits text only. Image/video/music generation route to ToolVault tools (cloud) over the bridge — "generate an image" is a `search_tools`→`execute` hop, not an on-device capability.
- **Camera/mic:** camera frames can feed Gemma vision for live offline "what am I looking at"; mic → cloud STT.

---

## Surface note (exception to "frontend = 3 surfaces")

`local` is **Android-only by nature** (it requires an on-device model). The web Portal picker should render it as "available on your phone only," not as a usable option. This is an intrinsic exception to the three-surfaces rule, not an oversight.

---

## Testing strategy (TDD per project norms)

- **Backend (Python):** unit-test the two bridge endpoints through `execute_tool()` with operator context; device-registry attest/status; mirror catalog + range/resume download + checksum; conditional `local` surfacing in the catalog. All testable with no phone.
- **Android:** LiteRT-LM load/generate smoke; **FC-loop tests with a mock model** emitting canned tool calls → assert actuator dispatch + result feedback; AccessibilityService instrumented tests (UIAutomator) for tap/type/read against a test app + a **password-redaction test**; tool-bridge client vs. mock Orchestrator; confirm-gate tests (YOLO vs Permission).
- **Integration:** real-device "open app → read → summarize → save to memory" with mesh up; airplane-mode degradation → text-only + **queued-mint flush on reconnect**.

---

## Phased rollout (dependency-ordered, each independently demoable)

| Phase | Deliverable | Demo milestone |
|---|---|---|
| **0 — Contracts** | Bridge contract (`search`/`execute`), device-registry schema, `local` catalog entry (conditional surfacing), persona endpoint | Backend tests green, no phone needed |
| **1 — Acquisition** | BlackBox mirror (server-side HF fetch-once + license) + Android Model Manager UI (download/progress/resume/verify/attest, E2B-vs-E4B spec recommendation, delete/switch) | Model on phone, BlackBox device-registry shows it verified, `local` appears in picker |
| **2 — Runtime + chat** | LiteRT-LM wrapper + FC-SDK loop + chat UI integration + persona fetch + `/chat/save` write-back; voice via existing STT/TTS | Hold a text/voice conversation on Gemma, rendered in the BlackBox UI, snapshotted to memory |
| **3 — Tool-bridge** | `search_tools` + `execute` wired into the loop; minimal resident core | On-device Gemma searches memory, generates images (cloud), etc. — full ToolVault reachable from the phone |
| **4 — Phone control** ⭐ | AccessibilityService actuator (UI-tree-first), the 12 resident phone tools, MediaProjection vision fallback, YOLO/Permission toggle + confirm-gate, credential handling/redaction | "Use my phone" works |
| **5 — Hardening** | Offline queue/flush, battery/thermal management, error recovery, multi-user isolation verification, **security review** of the Accessibility surface + redaction | Production-ready |

**Why this order:** value is front-loaded at low risk — by the end of Phase 2 there is a genuinely useful offline on-device assistant rendered in your own UI, *before* touching the scary AccessibilityService. Phase 4 (the headline) is deliberately last and gated so the riskiest surface lands on a verified foundation.

---

## Open / revisitable decisions

- **Model source** (Locked table) defaults to **BlackBox-mirrored**; revisit if server storage or Gemma redistribution terms become a concern → fall back to direct device→Hugging Face (Gallery-style) or a hybrid broker.
- **Exact Gemma 4 bundle slugs + quantization** (Q4_0 vs alternatives) to be pinned in Phase 1 against what's published in the LiteRT community repos.
- **Resident actuator set** (the "~12") to be finalized in Phase 4 against the AI Edge FC SDK's function-declaration ergonomics.

---

## References

- LiteRT-LM / LLM Inference for Android — https://ai.google.dev/edge/mediapipe/solutions/genai/llm_inference/android
- On-device Function Calling SDK (AI Edge) — https://developers.googleblog.com/on-device-function-calling-in-google-ai-edge-gallery/
- `google-ai-edge/gallery` (reference downloader + model manager, Apache 2.0) — https://github.com/google-ai-edge/gallery
- AccessibilityService for agent control — https://www.runanywhere.ai/blog/android-use-agent
- Gemma 4 E2B vs E4B edge models — https://www.mindstudio.ai/blog/gemma-4-e2b-vs-e4b-edge-models-audio-vision-phone

---

## Related existing infra (reused, not rebuilt)

- **ToolVault v2** — per-tool modules, `embeddings.json`, `meta_tool.py` semantic search, `execute_tool()` + `ToolContext(operator)`.
- **`POST /gmail/execute`** — the proven pattern for routing external callers through `execute_tool()`.
- **`device_registry/registry.py`** — extended with on-device-model records.
- **`/chat/save`** — cheap direct-persist auto-mint path for memory write-back.
- **`behavioral_core.py`** — persona/anti-sycophancy source, surfaced to the device via the new persona endpoint.
- **`/ws/stt`, `/tts/catalog` + TTS** — reused unchanged for voice.
- **Model-list centralization SoT** (`2026-05-18`) — where the `local` provider's catalog entries live.
