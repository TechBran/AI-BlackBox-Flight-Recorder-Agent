# On-Box Local Model Stack + CU Virtual Displays — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement SPEC v2.1 (docs/plans/2026-07-20-local-model-stack-design.md, decisions D1–D12): STT/TTS/embeddings/rerank on-box behind a llama-swap front door (:9098, two exclusive GPU groups), full Qwen3-TTS integration, install-script + wizard delivery, and per-session CU virtual displays with live view.

**Architecture:** A single systemd unit (`blackbox-models.service`) runs a llama-swap binary on :9098 as the front door for every on-box model capability, spawning member servers on demand across two mutually-exclusive GPU groups — `retrieval` (Qwen3-Embedding-8B Q8_0 + Qwen3-Reranker-0.6B, both llama-server) and `audio` (Speaches/faster-whisper STT + our Qwen3-TTS FastAPI server) — with drain-then-swap, built-in request queueing, and 600s idle TTL. A new thin `Orchestrator/local_stack.py` module is the single source of truth for install/health state and per-capability routing precedence: an explicit user pick wins, else the wizard-seeded default resolves on-box (`localstack`/`onbox`) → existing custom-server audio → cloud. CU virtual displays are an independent workstream that generalizes `browser/display.py` from a singleton into a per-session Xvfb/openbox/x11vnc/websockify allocator with noVNC live view, so each computer-use session gets a private virtual screen at the model's native resolution while other users see an in-use indicator.

**Tech Stack:** llama-swap, llama.cpp llama-server (Qwen3-Embedding-8B Q8_0, Qwen3-Reranker-0.6B GGUF), Speaches/faster-whisper, Qwen3-TTS via FastAPI, Xvfb/x11vnc/websockify+noVNC, FastAPI/pytest, Portal JS, Android Kotlin.

**Read first:** the spec §4 config.yaml template, and §14 hardening log (refuted alarms — do not re-litigate them). **House rules:** never `git add -A` (stage explicit paths only); the tree must stay runnable at every commit (prod runs from the working tree); `config.ini`/`.env` are per-box — edit templates only, never the live per-box files; a restart of `blackbox.service` is pre-authorized.

## Milestone map

| # | Title | Depends on | Tasks |
|---|-------|-----------|-------|
| M0 | Prerequisite bug fixes | — | 2 |
| M1 | Orchestrator local-stack resolver module, `[local_models]` config, and `GET /local-models/status` | — | 5 |
| M2 | Install layer | M1 | 7 |
| M3 | Embeddings on localstack | M1, M2 | 8 |
| M4 | Reranker on localstack + G2 validation harness | M1, M2 | 5 |
| M5 | STT On-Box + D12 Orchestrator Serialization + D10 Loading Affordance | M1 (M3/M4 for 5.6/5.7) | 8 |
| M6 | The qwen-tts server (`LocalModels/qwen_tts_server/`) | — | 9 |
| M7 | TTS integration across the three surfaces (Qwen3-TTS on-box) | M0, M5, M6 | 9 |
| M8 | Onboarding wizard "local_models" step + Updates panel status | M1–M5 | 6 |
| M9 | CU per-session virtual displays + live view + in-use flag | — (independent of model stack) | 11 |
| M10 | MS02 Phase-2 Runbook — Gate Harnesses, Deploy, G1–G6, Acceptance & Rollback | M1–M9 (runs on MS02) | 17 |

**Total: 87 tasks across 11 milestones.**

## Execution order

- **M0 first** — prerequisite bug fixes that unblock later routing tokens.
- **M1 → M2** are the foundation (resolver module, then install layer); do them in order.
- **M3 / M4** come after M1 + M2 (they need the installed llama-swap front door for integration, though their code is inert-safe/mock-tested before it lands).
- **M5** needs M1 (Tasks 5.6/5.7 additionally consume M3/M4).
- **M6** is independent (the qwen-tts server is self-contained).
- **M7** comes after M0 + M5 + M6 (it integrates the M6 server, uses M0's Android provider param, and reuses M1's resolver; D10 affordance is client-side so it does not hard-depend on M5).
- **M8** comes after M1–M5 (the wizard step surfaces their capabilities, fail-open before they all land).
- **M9** is independent of the model stack and may land in parallel.
- **M10** is last — the MS02 Phase-2 runbook and gate harnesses run once M1–M9 are code-complete and merged.

---

## Milestone 0: Prerequisite bug fixes

**Depends on:** nothing

Two standalone correctness fixes, each shippable on its own **before** any local-stack work — they only remove pre-existing bugs that would otherwise silently break local:/qwen: TTS routing and on-box STT selection later. (a) The Android `TtsRepository` hardcodes provider `"openai"` in its generic synthesis branch (spec §5.4, recon find [17]), so a selected `local:`/`qwen:` voice is mislabeled and 400s — pass the parsed provider through generically. (b) The `speech_to_text` ToolVault schema's provider enum omits the local and on-box tokens (spec §5.3 "Tool schema fix", recon find [16]), so those providers cannot be passed through the tool at all — add `"local"` and `"onbox"`. Neither task depends on the local stack existing; both leave the tree fully runnable.

---

### Task 0.1: Android `TtsRepository` — pass the parsed provider through generically

The `generateWithVoice` else-branch calls `generateTts`, which writes a hardcoded `"provider":"openai"` into the `/tts/batch` body (`TtsRepository.kt:112`). Any voice that isn't `elevenlabs:`/`gemini-*:` (i.e. `openai:`, and the future `local:`/`qwen:`) falls into that branch and is sent as provider `openai` — so a `qwen:Vivian` or `local:af_heart` voice reaches the backend mislabeled and is rejected. Fix: thread `config.provider` from the parsed `VoiceConfig` through `generateTts`. `SettingsViewModel.kt:98` (`repo.generateTts(text, cfg.voice, cfg.model)`) passes only three positional args, so inserting a defaulted `provider` param before `operator` is source-compatible.

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/data/repository/TtsRepository.kt` (signature at :94-100, body provider at :112, else-branch at :239-247)
- Test: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/data/repository/TtsProviderRoutingTest.kt` (Create)

> **Android project root** (absolute — every `./gradlew` Run below is executed from here):
> `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal`
> Per the Android build env: gradle runs `--offline` (9.0-M1), JDK17; `local.properties` (SDK path) must already exist in this project dir.

**Steps:**

1. **Write the failing test.** Create `TtsProviderRoutingTest.kt` exactly as below. It uses the repo's established convention (MockWebServer `mockwebserver3` + a real `BlackBoxApi` + real `TtsRepository`, matching `TtsVoiceParseTest.kt`) and asserts the `provider` field of the JSON actually sent to `/tts/batch`:

```kotlin
package com.aiblackbox.portal.data.repository

import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.Headers.Companion.headersOf
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test

/**
 * generateWithVoice must pass the PARSED provider through to /tts/batch instead
 * of hardcoding "openai" — otherwise on-box voices (local:/qwen:) are mislabeled
 * and 400. openai:/bare-legacy voices keep provider "openai" (regression guard).
 */
class TtsProviderRoutingTest {

    private lateinit var server: MockWebServer
    private lateinit var repo: TtsRepository

    @Before fun setUp() {
        server = MockWebServer()
        server.start()
        // BlackBoxApi expects a baseUrl WITHOUT a trailing slash (matches
        // TtsVoiceParseTest): "$baseUrl$path" keeps the leading slash.
        val baseUrl = server.url("").toString().trimEnd('/')
        repo = TtsRepository(BlackBoxApi(baseUrl))
    }

    @After fun tearDown() {
        server.close()
    }

    private fun enqueueOk() {
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"status":"ok","audio_url":"http://x/a.mp3"}""")
                .build()
        )
    }

    /** Drive generateWithVoice once and return (target, provider) actually sent. */
    private suspend fun routeVoice(voiceValue: String): Pair<String, String> {
        enqueueOk()
        repo.generateWithVoice(text = "hello", voiceValue = voiceValue)
        val rec = server.takeRequest()
        val provider = Json.parseToJsonElement(rec.body!!.utf8())
            .jsonObject["provider"]!!.jsonPrimitive.content
        return rec.target!! to provider
    }

    @Test fun `qwen voice routes to tts batch with provider qwen not openai`() = runTest {
        val (target, provider) = routeVoice("qwen:Vivian")
        assertEquals("/tts/batch", target)
        assertEquals("qwen", provider)
    }

    @Test fun `local voice routes with provider local`() = runTest {
        val (target, provider) = routeVoice("local:af_heart")
        assertEquals("/tts/batch", target)
        assertEquals("local", provider)
    }

    @Test fun `openai voice still routes with provider openai`() = runTest {
        val (_, provider) = routeVoice("openai:nova")
        assertEquals("openai", provider)
    }

    @Test fun `bare legacy voice still routes with provider openai`() = runTest {
        val (_, provider) = routeVoice("onyx")
        assertEquals("openai", provider)
    }
}
```

2. **Run the test — expect FAIL.**
   - Run (from the Android project root): `./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.repository.TtsProviderRoutingTest"`
   - Expected: `BUILD FAILED`; the two on-box cases fail because the current code sends `openai`, e.g.:
     ```
     TtsProviderRoutingTest > qwen voice routes to tts batch with provider qwen not openai FAILED
         org.junit.ComparisonFailure: expected:<[qwen]> but was:<[openai]>
     TtsProviderRoutingTest > local voice routes with provider local FAILED
         org.junit.ComparisonFailure: expected:<[local]> but was:<[openai]>
     ```
     (`openai` and `onyx` cases already pass — that's the regression floor.)

3. **Add the `provider` parameter to `generateTts`.** In `TtsRepository.kt`, replace the signature (`:94-100`):

```kotlin
    suspend fun generateTts(
        text: String,
        voice: String = "onyx",
        model: String = "tts-1-hd",
        format: String = "mp3",
        operator: String = "Brandon"
    ): TtsResponse {
```

   with (new `provider` param inserted before `operator` — keeps `SettingsViewModel.kt:98`'s 3-positional-arg call source-compatible):

```kotlin
    suspend fun generateTts(
        text: String,
        voice: String = "onyx",
        model: String = "tts-1-hd",
        format: String = "mp3",
        provider: String = "openai",
        operator: String = "Brandon"
    ): TtsResponse {
```

4. **Use the parameter in the request body.** In the same method, replace the hardcoded provider line (`:112`):

```kotlin
            append(",\"provider\":\"openai\"")
```

   with:

```kotlin
            append(",\"provider\":\"$provider\"")
```

5. **Pass the parsed provider through from `generateWithVoice`.** Replace the else-branch (`:239-247`):

```kotlin
            else -> {
                // OpenAI TTS — synchronous
                generateTts(
                    text = text,
                    voice = config.voice,
                    model = config.model,
                    operator = operator
                )
            }
```

   with:

```kotlin
            else -> {
                // Generic synchronous /tts/batch path. Pass the PARSED provider
                // through instead of hardcoding "openai", so on-box voices
                // (local:/qwen:) reach their real backend branch instead of
                // being mislabeled "openai" (which 400s on a non-openai id).
                generateTts(
                    text = text,
                    voice = config.voice,
                    model = config.model,
                    provider = config.provider,
                    operator = operator
                )
            }
```

6. **Run the focused test — expect PASS.**
   - Run (from the Android project root): `./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.data.repository.TtsProviderRoutingTest"`
   - Expected: `BUILD SUCCESSFUL`; all 4 tests pass, 0 failures.

7. **Run the full unit gate — expect PASS.**
   - Run (from the Android project root): `./gradlew :app:testDebugUnitTest --offline`
   - Expected: `BUILD SUCCESSFUL` — the whole suite green (nothing else references `generateTts`'s positional tail).

8. **Manual device validation (house rule — Fold).** Build/run the app on the Fold, open the TTS voice picker, select an `openai:` voice and confirm speech still plays (the `local:`/`qwen:` catalog groups don't exist until later milestones, so this only re-confirms no regression to the existing OpenAI path). This is a no-code confirmation step; note it in the commit body if run.

9. **Commit (explicit paths only — never `git add -A`).**
   - Run:
     ```
     git add "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/data/repository/TtsRepository.kt" \
             "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/data/repository/TtsProviderRoutingTest.kt"
     git commit -m "fix(android): TtsRepository passes the parsed provider through generically

Generic /tts/batch branch hardcoded provider=openai, so a selected local:/qwen:
voice was mislabeled and 400d. Thread config.provider through generateTts;
openai/legacy voices keep provider=openai. Prereq for on-box TTS (spec §5.4).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
     ```
   - Expected: one commit, exactly two files changed.

---

### Task 0.2: ToolVault `speech_to_text` — add `local` + `onbox` to the provider enum

The `speech_to_text` schema's `provider` enum lists only `openai`/`google`/`elevenlabs` (`schema.json:20-24`), so the existing custom-server `local` STT provider — and the new on-box `onbox` token (canonical STT routing token, spec §5.3) — cannot be passed through the tool. Add both to the enum. This is a schema-only unblock; the backend routing that consumes `onbox` (in `resolve_stt_provider`) lands in the STT milestone. The top-level `description` is deliberately left unchanged so the tool's cached embedding vector (`ToolVault/embeddings.json`, keyed on description hash) does not change and full embedding coverage stays intact.

**Files:**
- Modify: `ToolVault/tools/speech_to_text/schema.json` (provider enum at :20-24)
- Test: `Orchestrator/toolvault/tests/test_speech_to_text_provider_enum.py` (Create)

> **Backend test convention** (verified): pytest with `pythonpath = .` (root `pytest.ini`). `testpaths = Orchestrator/tests`, but the ToolVault module tests live under `Orchestrator/toolvault/tests/` and are run by **explicit path** (as the existing `test_validate.py` etc. are). All commands below run from the repo root: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc`.

**Steps:**

1. **Write the failing test.** Create `Orchestrator/toolvault/tests/test_speech_to_text_provider_enum.py`:

```python
"""speech_to_text's provider enum must accept the custom-server `local` token
and the on-box `onbox` token (spec §5.3 STT routing fix). Reads the shipping
schema.json directly via registry.TOOLS_DIR so it tracks the real module.
"""

import json

from Orchestrator.toolvault import registry


def _provider_enum() -> list:
    schema_path = registry.TOOLS_DIR / "speech_to_text" / "schema.json"
    data = json.loads(schema_path.read_text())
    return data["parameters"]["properties"]["provider"]["enum"]


def test_provider_enum_includes_local_and_onbox():
    enum = _provider_enum()
    assert "local" in enum, f"custom-server 'local' STT token missing: {enum}"
    assert "onbox" in enum, f"on-box 'onbox' STT token missing: {enum}"


def test_provider_enum_keeps_existing_cloud_providers():
    # Regression floor: the fix is additive, not a rewrite.
    enum = _provider_enum()
    for p in ("openai", "google", "elevenlabs"):
        assert p in enum, f"existing provider {p!r} was dropped: {enum}"
```

2. **Run the test — expect FAIL.**
   - Run: `python -m pytest Orchestrator/toolvault/tests/test_speech_to_text_provider_enum.py -v`
   - Expected: `test_provider_enum_includes_local_and_onbox` FAILS with an `AssertionError` reporting `custom-server 'local' STT token missing: ['openai', 'google', 'elevenlabs']`; `test_provider_enum_keeps_existing_cloud_providers` PASSES.

3. **Add the two tokens to the enum.** In `ToolVault/tools/speech_to_text/schema.json`, replace the enum block (`:20-24`):

```json
        "enum": [
          "openai",
          "google",
          "elevenlabs"
        ],
```

   with:

```json
        "enum": [
          "openai",
          "google",
          "elevenlabs",
          "local",
          "onbox"
        ],
```

4. **Run the test — expect PASS.**
   - Run: `python -m pytest Orchestrator/toolvault/tests/test_speech_to_text_provider_enum.py -v`
   - Expected: both tests PASS (`2 passed`).

5. **Run the ToolVault validator (CI gate) — expect clean.**
   - Run: `python -m Orchestrator.toolvault.validate`
   - Expected: exit code 0; first line `ToolVault validation: OK` and last line `  errors:          none`. Embedding coverage is unchanged/full (`embeddings:      N/N embedded`) because the description was not touched. (Optionally confirm the exit code: `echo $?` → `0`.)

6. **Confirm the real-tree validate test still passes** (guards that the edit didn't break the shipping tree or coverage).
   - Run: `python -m pytest Orchestrator/toolvault/tests/test_validate.py -v`
   - Expected: `test_validate_all_real_tree_ok` and `test_cli_main_real_tree_exits_zero` PASS along with the rest (`... passed`).

7. **Commit (explicit paths only — never `git add -A`; do NOT stage `ToolVault/embeddings.json`).**
   - Run:
     ```
     git add ToolVault/tools/speech_to_text/schema.json \
             Orchestrator/toolvault/tests/test_speech_to_text_provider_enum.py
     git commit -m "fix(toolvault): add local + onbox to speech_to_text provider enum

The provider enum omitted the custom-server 'local' token and the new on-box
'onbox' STT routing token, so neither could be passed through the tool. Additive
enum-only fix; description unchanged so the tool embedding stays coherent.
Prereq for on-box STT routing (spec §5.3).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
     ```
   - Expected: one commit, exactly two files changed. (`git status` must show `ToolVault/embeddings.json` untracked/unstaged — it is gitignored-in-practice and never committed.)

8. **Make it live (runtime activation; BlackBox restart is pre-authorized, so a reload is the lighter touch).** The live chat injector already picks up the schema edit via the registry mtime cache; `reload` additionally refreshes the registry-derived tool lists.
   - Run: `curl -sS -X POST http://localhost:9091/toolvault/reload`
   - Expected: JSON `{"reloaded":true,"tool_count":<N>,"embedded":<N>,"errors":{}}` with `errors` empty. (`tool_count` matches the validator's tool count.)


---

## Milestone 1: Orchestrator local-stack resolver module, `[local_models]` config, and `GET /local-models/status`

**Depends on:** nothing

Build the single Orchestrator-side source of truth for the on-box local model stack: `Orchestrator/local_stack.py`. It answers — for STT / TTS / embeddings / rerank — is the on-box stack installed? reachable? is this capability *seeded* to resolve on-box? and where do I reach it? Every later milestone's resolver (STT/TTS routing, the `localstack` embeddings/rerank providers, the wizard) calls this module. Per-capability enable flags are re-read FRESH from `config.ini`'s `[local_models]` section on every call (custom_servers.py E8 lesson) so a wizard flip applies with no restart, and `is_healthy()` keys on install + config + llama-swap *front-door* process-liveness — never live per-member VRAM residency, so a normal group swap never flaps a capability to cloud (design §4/§6, correction [30]). Also ship `GET /local-models/status`, which aggregates llama-swap `/health`+`/running`, the hardware tier, disk headroom, per-model download state, and the per-capability on-box routing decision, mirroring the `GET /embeddings/status` shape conventions.

### Canonical names used in this milestone (do not paraphrase)

- Resolver module: `Orchestrator/local_stack.py`
- config.ini section: `[local_models]` — keys `enabled`, `base_url` (default `http://127.0.0.1:9098/v1`), per-capability flags `stt` / `tts` / `embeddings` / `rerank`
- llama-swap front door: `http://127.0.0.1:9098/v1` (root `http://127.0.0.1:9098`)
- llama-swap member ids (must match `installer/templates/llama-swap-config.yaml.template`): `embed-qwen3-8b`, `rerank-qwen3-0.6b`, `speaches`, `qwen-tts`
- Status endpoint: `GET /local-models/status`
- Download-state contract file (written by the later download milestone, read fail-soft here): `Manifest/local_models/downloads.json`

### Contracts this milestone exposes to other milestones

- `local_stack.is_healthy()` / `local_stack.is_installed()` / `local_stack.enabled(cap)` / `local_stack.should_route_onbox(cap)` — the on-box availability + routing-seed primitives the STT/TTS/embeddings/rerank resolvers consume. **Each capability's resolver MUST honor an explicit credentialed user pick BEFORE calling these (D2).**
- `local_stack.base_url()` (with `/v1`) and `local_stack.base_url_root()` (llama-swap admin endpoints `/health`, `/running`, `/upstream/*`).
- `local_stack.model_downloaded(model_id) -> bool` — that member's weights are present (per the download-state contract). Consumed by the localstack embeddings/rerank preflight (M3 Task 3.5 / M4). Added in Task 1.4 beside `read_download_state()`.
- `local_stack.MEMBERS` (the four members + capability/group/label), `local_stack.CAPABILITIES`, `local_stack.DEFAULT_BASE_URL`, `local_stack.DISK_GATE_MB` (40 GB, the download gate the download milestone enforces), `local_stack.DOWNLOAD_STATE_PATH` (the JSON the download milestone writes).
- `local_stack._transport` — httpx.MockTransport seam (tests inject; mirrors `embeddings/ollama_io.py`).
- `hardware.disk_free_mb()` — the fail-soft disk probe the download gate and status endpoint share.
- `GET /local-models/status` JSON — additive binding contract for the wizard `local_models` step and the Updates panels.

---

### Task 1.1: Declare the `[local_models]` config section in `config.py`

Adds the discoverable, documented import-time anchor + fallbacks for the new section, exactly like `[computer_use]`/`[embeddings]` are declared. The LIVE routing reads happen in `local_stack.py` (fresh re-read); these constants are the canonical fallbacks and the master/base_url snapshot. Per-capability flags are documented here but read fresh by `local_stack` (declaring four never-read import-time constants would be dead code — YAGNI).

**Files:**
- Modify: `Orchestrator/config.py` (insert after line 258, `EMBEDDINGS_QUERY_FAIL_THRESHOLD = ...`)
- Test: `Orchestrator/tests/test_local_stack.py` (create; grows across Tasks 1.1, 1.3, 1.4)

Steps:

1. Create `Orchestrator/tests/test_local_stack.py` with the config-defaults test:

```python
"""Tests for the on-box local model stack resolver (M1).

Config is re-read FRESH from a tmp config.ini via the local_stack.CONFIG_PATH
seam (custom_servers.py pattern); all llama-swap HTTP is mocked via the
local_stack._transport seam (httpx.MockTransport, exactly like
embeddings/ollama_io.py). No real network, no real config.ini touched.
"""
import json

import httpx
import pytest

from Orchestrator import config, local_stack


# ── Task 1.1: config.py [local_models] declaration ────────────────────────────

def test_config_local_models_defaults():
    assert isinstance(config.LOCAL_MODELS_ENABLED, bool)
    assert config.LOCAL_MODELS_BASE_URL.endswith("/v1")
    # On a box with no [local_models] section (the dev box), the fallbacks apply.
    if not config.CFG.has_section("local_models"):
        assert config.LOCAL_MODELS_ENABLED is False
        assert config.LOCAL_MODELS_BASE_URL == "http://127.0.0.1:9098/v1"
```

2. Run it, expect FAIL (constants don't exist yet):
   - Run: `python -m pytest Orchestrator/tests/test_local_stack.py::test_config_local_models_defaults -q`
   - Expected: `ImportError` / `AttributeError: module 'Orchestrator.config' has no attribute 'LOCAL_MODELS_ENABLED'` — 1 error.

3. In `Orchestrator/config.py`, insert immediately after line 258 (`EMBEDDINGS_QUERY_FAIL_THRESHOLD = CFG.getint(...)`):

```python

# ── [local_models] — on-box local model stack (M1, design 2026-07-20) ────────
# Master enable + base_url snapshot at import for discoverability alongside
# [computer_use]/[embeddings]. The LIVE source of truth for routing is
# Orchestrator/local_stack.py, which RE-READS config.ini fresh per request so a
# wizard flip applies with NO restart (custom_servers.py E8 lesson). The
# per-capability enable flags (local_models.stt / .tts / .embeddings / .rerank)
# are read fresh by local_stack.enabled(cap); they default false — nothing
# routes on-box until the wizard flips each ("Nothing activates implicitly on
# install", design §8).
LOCAL_MODELS_ENABLED  = CFG.getboolean("local_models", "enabled", fallback=False)
LOCAL_MODELS_BASE_URL = CFG.get("local_models", "base_url",
                                fallback="http://127.0.0.1:9098/v1").strip()
```

4. Run again, expect PASS:
   - Run: `python -m pytest Orchestrator/tests/test_local_stack.py::test_config_local_models_defaults -q`
   - Expected: `1 passed`.

5. Commit:
   - Run: `git add Orchestrator/config.py Orchestrator/tests/test_local_stack.py && git commit -m "config: declare [local_models] section (master enable + base_url)"`

---

### Task 1.2: Add the fail-soft disk probe `hardware.disk_free_mb()`

`hardware.py` has `probe()`/`derive_tier` but no disk field (design §7 recon). The status endpoint reports disk headroom and the later download milestone gates on ≥40 GB free; both share this one probe. Fail-soft (never raises), lazy `paths` import to keep `hardware.py`'s import surface minimal.

**Files:**
- Modify: `Orchestrator/hardware.py` (add `import shutil` near line 30; add `disk_free_mb()` after `probe()`, i.e. after line 142)
- Test: `Orchestrator/tests/test_hardware.py` (append; existing file)

Steps:

1. Append to `Orchestrator/tests/test_hardware.py`:

```python
# ── disk_free_mb (M1: local-model download gate) ──────────────────────────────

def test_disk_free_mb_happy(monkeypatch):
    class _Usage:
        free = 50 * 1024 * 1024 * 1024  # 50 GB in bytes
    monkeypatch.setattr(hardware.shutil, "disk_usage", lambda p: _Usage)
    assert hardware.disk_free_mb("/anywhere") == 50 * 1024  # 51200 MB


def test_disk_free_mb_failsoft(monkeypatch):
    def _boom(p):
        raise OSError("no such path")
    monkeypatch.setattr(hardware.shutil, "disk_usage", _boom)
    assert hardware.disk_free_mb("/missing") is None


def test_disk_free_mb_default_path_uses_root(monkeypatch):
    seen = {}
    class _Usage:
        free = 10 * 1024 * 1024 * 1024
    def _capture(p):
        seen["path"] = p
        return _Usage
    monkeypatch.setattr(hardware.shutil, "disk_usage", _capture)
    assert hardware.disk_free_mb() == 10 * 1024
    assert seen["path"]  # a concrete root path was resolved, not None
```

2. Run, expect FAIL (`disk_free_mb` and `hardware.shutil` don't exist yet):
   - Run: `python -m pytest Orchestrator/tests/test_hardware.py -q -k disk_free_mb`
   - Expected: `AttributeError: module 'Orchestrator.hardware' has no attribute 'shutil'` (or `disk_free_mb`) — 3 errors.

3. In `Orchestrator/hardware.py`, add `import shutil` to the stdlib import block (line 30 area, alongside `import subprocess`):

```python
import shutil
import subprocess
import threading
import time
```

4. In `Orchestrator/hardware.py`, add after `probe()` (after line 142, the `return dict(result)` that closes `probe`):

```python


def disk_free_mb(path: "str | None" = None) -> "int | None":
    """Free space (MB) on the filesystem holding `path` (default: the BlackBox
    root). None on ANY failure — fail-soft, never raises. Feeds the local-model
    download gate (~40GB for the full GPU-tier weight set, design §7) and the
    `disk` block of GET /local-models/status. Lazy `paths` import keeps this
    module's import surface minimal (subprocess/shutil only)."""
    try:
        if path is None:
            from Orchestrator.utils.paths import blackbox_root
            path = str(blackbox_root())
        return shutil.disk_usage(path).free // (1024 * 1024)
    except Exception:
        return None
```

5. Run, expect PASS:
   - Run: `python -m pytest Orchestrator/tests/test_hardware.py -q -k disk_free_mb`
   - Expected: `3 passed`.

6. Commit:
   - Run: `git add Orchestrator/hardware.py Orchestrator/tests/test_hardware.py && git commit -m "hardware: add fail-soft disk_free_mb() probe for the local-model download gate"`

---

### Task 1.3: `local_stack.py` — config fresh-read + capability resolvers

The core of the module: fresh config.ini reads (`master_enabled`, `base_url`, `base_url_root`, `enabled(cap)`, `is_installed`). No HTTP yet. Follows the custom_servers.py fresh-read discipline: a module-level `CONFIG_PATH` attr tests repoint, a fresh `ConfigParser` per call.

**Files:**
- Create: `Orchestrator/local_stack.py`
- Test: `Orchestrator/tests/test_local_stack.py` (append)

Steps:

1. Append to `Orchestrator/tests/test_local_stack.py`:

```python
# ── Task 1.3: config fresh-read + capability resolvers ────────────────────────

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Point local_stack at a tmp config.ini the test writes; returns a writer."""
    path = tmp_path / "config.ini"
    monkeypatch.setattr(local_stack, "CONFIG_PATH", path)

    def write(body: str):
        path.write_text(body, encoding="utf-8")
    return write


def test_master_enabled_absent_file_is_false(cfg):
    # No file written at all — fail-soft to the fallback.
    assert local_stack.master_enabled() is False
    assert local_stack.is_installed() is False


def test_master_enabled_true(cfg):
    cfg("[local_models]\nenabled = true\n")
    assert local_stack.master_enabled() is True
    assert local_stack.is_installed() is True


def test_base_url_default_and_root(cfg):
    cfg("[local_models]\nenabled = true\n")  # no base_url -> fallback
    assert local_stack.base_url() == "http://127.0.0.1:9098/v1"
    assert local_stack.base_url_root() == "http://127.0.0.1:9098"


def test_base_url_override_and_root_strip(cfg):
    cfg("[local_models]\nenabled = true\nbase_url = http://127.0.0.1:9500/v1/\n")
    assert local_stack.base_url() == "http://127.0.0.1:9500/v1"
    assert local_stack.base_url_root() == "http://127.0.0.1:9500"


def test_base_url_root_without_v1(cfg):
    cfg("[local_models]\nenabled = true\nbase_url = http://127.0.0.1:9098\n")
    assert local_stack.base_url_root() == "http://127.0.0.1:9098"


def test_enabled_requires_master(cfg):
    # master off -> every capability off even if the per-cap flag is set.
    cfg("[local_models]\nenabled = false\nstt = true\n")
    assert local_stack.enabled("stt") is False


def test_enabled_per_capability(cfg):
    cfg("[local_models]\nenabled = true\nstt = true\ntts = false\n")
    assert local_stack.enabled("stt") is True
    assert local_stack.enabled("tts") is False
    assert local_stack.enabled("embeddings") is False  # unset -> fallback false


def test_enabled_unknown_capability_is_false(cfg):
    cfg("[local_models]\nenabled = true\nvision = true\n")
    assert local_stack.enabled("vision") is False


def test_enabled_is_fresh_across_edits(cfg):
    cfg("[local_models]\nenabled = true\nembeddings = false\n")
    assert local_stack.enabled("embeddings") is False
    cfg("[local_models]\nenabled = true\nembeddings = true\n")   # wizard flip
    assert local_stack.enabled("embeddings") is True             # no restart
```

2. Run, expect FAIL (module doesn't exist):
   - Run: `python -m pytest Orchestrator/tests/test_local_stack.py -q -k "master_enabled or base_url or enabled"`
   - Expected: collection error `ModuleNotFoundError: No module named 'Orchestrator.local_stack'` (the top-of-file `from Orchestrator import ... local_stack` fails).

3. Create `Orchestrator/local_stack.py`:

```python
"""On-box local model stack — the single Orchestrator-side resolver (M1).

llama-swap (blackbox-models.service, :9098) fronts the on-box STT / TTS /
embeddings / reranker members. This module is the ONE source of truth every
consumer (STT/TTS resolvers, the localstack embeddings & rerank providers, the
wizard, GET /local-models/status) calls to answer: is the on-box stack
installed? reachable? is this capability SEEDED to resolve on-box? where do I
reach it?

Fresh-read discipline (custom_servers.py E8 lesson): the per-capability enable
flags live in config.ini's [local_models] section and are RE-READ from disk on
every call, so a wizard flip takes effect with NO restart. No import-time
config snapshot is trusted for routing.

Anti-flap invariant (design §4/§6, correction [30]): is_healthy() keys on
install + config + process-liveness of the llama-swap FRONT DOOR — never on
live per-member VRAM residency. A normal audio<->retrieval group swap takes the
demanded group's members transiently down; llama-swap's request queue absorbs
that, so a mid-swap request WAITS rather than routing to cloud. Routing
decisions are config/install state, not turn-to-turn health flapping.

HTTP is mocked in tests via the module `_transport` seam (httpx.MockTransport),
exactly like Orchestrator/embeddings/ollama_io.py.
"""
from __future__ import annotations

import configparser
import json
import logging

import httpx

from Orchestrator.utils.paths import resolve  # honors BLACKBOX_ROOT first

logger = logging.getLogger(__name__)

# ── canonical names (design "CANONICAL NAMES"; keep in lock-step across the box)
DEFAULT_BASE_URL = "http://127.0.0.1:9098/v1"   # llama-swap front door + /v1
CAPABILITIES = ("stt", "tts", "embeddings", "rerank")
SECTION = "local_models"

# config.ini is a per-box, gitignored file (config.py reads it CWD-relative at
# import; resolve() honors BLACKBOX_ROOT first). Module attr so tests repoint it.
CONFIG_PATH = resolve("config.ini")


# ── config fresh-read ─────────────────────────────────────────────────────────

def _read_config() -> configparser.ConfigParser:
    """Parse config.ini FRESH (never the import-time config.CFG snapshot).
    Fail-soft: a missing/corrupt/unreadable file yields an empty parser, so
    every getter falls back to its default."""
    cfg = configparser.ConfigParser()
    try:
        cfg.read(str(CONFIG_PATH))
    except (configparser.Error, OSError) as exc:
        logger.warning("local_stack: unreadable config.ini at %s (%s)", CONFIG_PATH, exc)
    return cfg


def master_enabled() -> bool:
    """[local_models] enabled — the installer/wizard flips this true when the
    stack is installed and its service should run. The 'installed' signal."""
    return _read_config().getboolean(SECTION, "enabled", fallback=False)


def base_url() -> str:
    """[local_models] base_url — the llama-swap /v1 front door."""
    val = _read_config().get(SECTION, "base_url", fallback=DEFAULT_BASE_URL).strip()
    return val or DEFAULT_BASE_URL


def base_url_root() -> str:
    """Front-door ROOT (no /v1) for llama-swap admin endpoints (/health,
    /running, /upstream/*)."""
    root = base_url().rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3].rstrip("/")
    return root


def enabled(cap: str) -> bool:
    """True iff `cap` is SEEDED to resolve on-box: master [local_models] enabled
    AND the per-capability flag. Fresh read — a wizard flip applies with no
    restart. An unknown capability is always False. This is the persisted
    wizard-time DEFAULT (D2); it does NOT override an explicit credentialed user
    pick — each capability's own resolver checks that BEFORE calling here."""
    if cap not in CAPABILITIES:
        return False
    cfg = _read_config()
    if not cfg.getboolean(SECTION, "enabled", fallback=False):
        return False
    return cfg.getboolean(SECTION, cap, fallback=False)


def is_installed() -> bool:
    """The on-box stack is installed + configured (master [local_models]
    enabled). Cheap, no HTTP — install/config state only."""
    return master_enabled()
```

4. Run, expect PASS:
   - Run: `python -m pytest Orchestrator/tests/test_local_stack.py -q -k "config_local_models or master_enabled or base_url or enabled"`
   - Expected: `10 passed` (1 config + 9 resolver tests).

5. Commit:
   - Run: `git add Orchestrator/local_stack.py Orchestrator/tests/test_local_stack.py && git commit -m "local_stack: config fresh-read + per-capability routing-seed resolvers"`

---

### Task 1.4: `local_stack.py` — llama-swap process-liveness probes + download-state reader

Add the HTTP layer: `llama_swap_health()` (front-door `/health`), `is_healthy()`, `should_route_onbox(cap)`, `running_members()` (front-door `/running`, tolerant parse), plus `read_download_state()` and the member/gate constants the status endpoint needs. HTTP is mocked via the `_transport` seam (httpx.MockTransport), identical to `ollama_io.py`.

**Files:**
- Modify: `Orchestrator/local_stack.py` (add httpx-probe layer + constants after `is_installed()`)
- Test: `Orchestrator/tests/test_local_stack.py` (append)

Steps:

1. Append to `Orchestrator/tests/test_local_stack.py`:

```python
# ── Task 1.4: llama-swap probes + download-state ──────────────────────────────

def _transport(routes: dict):
    """Sync httpx.MockTransport: path -> httpx.Response (or a raising callable).
    Mirrors test_embeddings_ollama.py's _get_transport."""
    def handler(request):
        target = routes.get(request.url.path)
        if callable(target):
            return target(request)
        if target is None:
            return httpx.Response(404)
        return target
    return httpx.MockTransport(handler)


@pytest.fixture
def installed_cfg(cfg):
    """A tmp config.ini with the stack installed (master enabled)."""
    cfg("[local_models]\nenabled = true\n")
    return cfg


def test_llama_swap_health_reachable(monkeypatch, installed_cfg):
    monkeypatch.setattr(local_stack, "_transport", _transport({
        "/health": httpx.Response(200, text="OK"),
    }))
    h = local_stack.llama_swap_health()
    assert h == {"reachable": True, "status_code": 200}


def test_llama_swap_health_non_200(monkeypatch, installed_cfg):
    monkeypatch.setattr(local_stack, "_transport", _transport({
        "/health": httpx.Response(503, text="loading"),
    }))
    assert local_stack.llama_swap_health() == {"reachable": False, "status_code": 503}


def test_llama_swap_health_unreachable(monkeypatch, installed_cfg):
    def refuse(request):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(local_stack, "_transport", _transport({"/health": refuse}))
    assert local_stack.llama_swap_health() == {"reachable": False, "status_code": None}


def test_is_healthy_true_when_installed_and_reachable(monkeypatch, installed_cfg):
    monkeypatch.setattr(local_stack, "_transport", _transport({
        "/health": httpx.Response(200),
    }))
    assert local_stack.is_healthy() is True


def test_is_healthy_false_when_not_installed(monkeypatch, cfg):
    cfg("[local_models]\nenabled = false\n")
    # No probe should even be attempted; a raising transport proves short-circuit.
    def boom(request):
        raise AssertionError("must not probe when not installed")
    monkeypatch.setattr(local_stack, "_transport", _transport({"/health": boom}))
    assert local_stack.is_healthy() is False


def test_is_healthy_false_when_unreachable(monkeypatch, installed_cfg):
    def refuse(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(local_stack, "_transport", _transport({"/health": refuse}))
    assert local_stack.is_healthy() is False


def test_should_route_onbox(monkeypatch, cfg):
    cfg("[local_models]\nenabled = true\nstt = true\ntts = false\n")
    monkeypatch.setattr(local_stack, "_transport", _transport({
        "/health": httpx.Response(200),
    }))
    assert local_stack.should_route_onbox("stt") is True    # seeded + healthy
    assert local_stack.should_route_onbox("tts") is False    # not seeded


def test_should_route_onbox_seeded_but_down(monkeypatch, cfg):
    cfg("[local_models]\nenabled = true\nstt = true\n")
    def refuse(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(local_stack, "_transport", _transport({"/health": refuse}))
    assert local_stack.should_route_onbox("stt") is False    # seeded but unhealthy


def test_running_members_object_shape(monkeypatch, installed_cfg):
    monkeypatch.setattr(local_stack, "_transport", _transport({
        "/running": httpx.Response(200, json={"running": [
            {"model": "embed-qwen3-8b", "state": "ready"},
            {"model": "rerank-qwen3-0.6b"},          # state omitted -> "ready"
            {"missing_model_key": True},              # ignored
        ]}),
    }))
    assert local_stack.running_members() == [
        {"model": "embed-qwen3-8b", "state": "ready"},
        {"model": "rerank-qwen3-0.6b", "state": "ready"},
    ]


def test_running_members_bare_list(monkeypatch, installed_cfg):
    monkeypatch.setattr(local_stack, "_transport", _transport({
        "/running": httpx.Response(200, json=[{"model": "speaches", "state": "loading"}]),
    }))
    assert local_stack.running_members() == [{"model": "speaches", "state": "loading"}]


def test_running_members_empty_when_idle(monkeypatch, installed_cfg):
    monkeypatch.setattr(local_stack, "_transport", _transport({
        "/running": httpx.Response(200, json={"running": []}),
    }))
    assert local_stack.running_members() == []   # up, nothing resident


def test_running_members_none_when_unreachable(monkeypatch, installed_cfg):
    def refuse(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(local_stack, "_transport", _transport({"/running": refuse}))
    assert local_stack.running_members() is None  # distinct from [] (idle)


def test_read_download_state_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(local_stack, "DOWNLOAD_STATE_PATH", tmp_path / "downloads.json")
    assert local_stack.read_download_state() == {}


def test_read_download_state_happy(monkeypatch, tmp_path):
    p = tmp_path / "downloads.json"
    p.write_text(json.dumps({"embed-qwen3-8b": {"state": "downloaded"}}), encoding="utf-8")
    monkeypatch.setattr(local_stack, "DOWNLOAD_STATE_PATH", p)
    assert local_stack.read_download_state() == {"embed-qwen3-8b": {"state": "downloaded"}}


def test_read_download_state_corrupt_is_empty(monkeypatch, tmp_path):
    p = tmp_path / "downloads.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(local_stack, "DOWNLOAD_STATE_PATH", p)
    assert local_stack.read_download_state() == {}


def test_members_and_gate_constants():
    ids = [m["model"] for m in local_stack.MEMBERS]
    assert ids == ["embed-qwen3-8b", "rerank-qwen3-0.6b", "speaches", "qwen-tts"]
    caps = {m["capability"] for m in local_stack.MEMBERS}
    assert caps == set(local_stack.CAPABILITIES)
    assert local_stack.DISK_GATE_MB == 40 * 1024
```

2. Run, expect FAIL (`llama_swap_health` etc. don't exist):
   - Run: `python -m pytest Orchestrator/tests/test_local_stack.py -q -k "llama_swap or is_healthy or should_route or running_members or download_state or constants"`
   - Expected: `AttributeError: module 'Orchestrator.local_stack' has no attribute 'llama_swap_health'` — errors across the new tests.

3. In `Orchestrator/local_stack.py`, add to the imports/constants block. Update the top constants (after `SECTION = "local_models"`) to add the members, gate, download path, and HTTP seam:

```python
SECTION = "local_models"

# The four llama-swap members (ids MUST match the config.yaml template, §8).
# capability/group drive the status rollup + the per-capability routing block.
MEMBERS = (
    {"model": "embed-qwen3-8b",    "capability": "embeddings", "group": "retrieval",
     "label": "Qwen3-Embedding-8B (Q8_0)"},
    {"model": "rerank-qwen3-0.6b", "capability": "rerank",     "group": "retrieval",
     "label": "Qwen3-Reranker-0.6B"},
    {"model": "speaches",          "capability": "stt",        "group": "audio",
     "label": "Speaches (faster-whisper)"},
    {"model": "qwen-tts",          "capability": "tts",        "group": "audio",
     "label": "Qwen3-TTS (On-Box)"},
)

# Full GPU-tier weight set is ~27.5GB (design §14); gate downloads at 40GB free.
DISK_GATE_MB = 40 * 1024

# Download-state contract: the later download milestone's POST /local-models/
# download writes {"<member>": {"state": str, ...}} here; read fail-soft (absent
# file => every member reports "pending"). Module attr so tests repoint it.
DOWNLOAD_STATE_PATH = resolve("Manifest", "local_models", "downloads.json")

# Fail-fast loopback probes (ollama_io GET_TIMEOUT precedent).
GET_TIMEOUT = httpx.Timeout(2.0, connect=2.0)
# httpx.MockTransport injected by tests; None => real network.
_transport: "httpx.BaseTransport | None" = None
```

4. In `Orchestrator/local_stack.py`, append after `is_installed()`:

```python


# ── llama-swap process-liveness (NOT per-member VRAM residency) ───────────────

def llama_swap_health(timeout: "httpx.Timeout | None" = None) -> dict:
    """Probe the llama-swap FRONT DOOR /health. Returns
    {"reachable": bool, "status_code": int|None}. Fail-soft (never raises).
    The front-door /health is up whenever the proxy PROCESS is up, independent
    of which group is resident — so it does not flap on group swaps (this is the
    proxy-level endpoint, distinct from each member's own checkEndpoint)."""
    root = base_url_root()
    try:
        with httpx.Client(timeout=timeout or GET_TIMEOUT, transport=_transport) as client:
            resp = client.get(f"{root}/health")
            return {"reachable": resp.status_code == 200, "status_code": resp.status_code}
    except Exception:
        return {"reachable": False, "status_code": None}


def is_healthy(timeout: "httpx.Timeout | None" = None) -> bool:
    """Installed AND the llama-swap front door is reachable. Keys on install +
    config + process-liveness of llama-swap ITSELF — never live per-member VRAM
    residency (correction [30]). The on-box availability signal for routing.
    Short-circuits with NO probe when not installed."""
    if not is_installed():
        return False
    return llama_swap_health(timeout)["reachable"]


def should_route_onbox(cap: str) -> bool:
    """The shared on-box availability signal for each capability's resolver:
    `cap` is seeded on-box (D2) AND the stack is reachable now. The resolver
    MUST honor an explicit credentialed user pick BEFORE calling this."""
    return enabled(cap) and is_healthy()


def running_members(timeout: "httpx.Timeout | None" = None) -> "list[dict] | None":
    """llama-swap /running -> [{"model": str, "state": str}]. None when the
    proxy is UNREACHABLE (distinct from [] = up but nothing resident). Tolerant
    of both {"running": [...]} and a bare list; drops items without a str
    model; defaults a missing state to "ready"."""
    root = base_url_root()
    try:
        with httpx.Client(timeout=timeout or GET_TIMEOUT, transport=_transport) as client:
            resp = client.get(f"{root}/running")
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None
    items = data.get("running") if isinstance(data, dict) else data
    out = []
    for it in (items or []):
        if isinstance(it, dict) and isinstance(it.get("model"), str):
            out.append({"model": it["model"], "state": str(it.get("state", "ready"))})
    return out


# ── download-state contract (writer = the later download milestone) ───────────

def read_download_state() -> dict:
    """{member_id: {"state": str, ...}} from DOWNLOAD_STATE_PATH; {} fail-soft
    on absent/corrupt/wrong-shape."""
    try:
        data = json.loads(DOWNLOAD_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def model_downloaded(model_id: str) -> bool:
    """True iff `model_id`'s weights are present, per the download-state contract
    (read_download_state / DOWNLOAD_STATE_PATH, written by the download milestone).
    A member counts as downloaded when its recorded state is a terminal success
    ("downloaded" or "done"). Fail-soft: an absent/corrupt state file or an
    unlisted member => False. This is the on-disk-presence signal the localstack
    embeddings/rerank preflight (M3 Task 3.5 / M4) consumes; those tests
    monkeypatch it, but a real installed+healthy box calls THIS implementation, so
    it MUST exist here (missing it => AttributeError -> HTTP 500 on
    GET /embeddings/status)."""
    entry = read_download_state().get(model_id)
    return isinstance(entry, dict) and entry.get("state") in ("downloaded", "done")
```

5. Run, expect PASS:
   - Run: `python -m pytest Orchestrator/tests/test_local_stack.py -q`
   - Expected: `27 passed` (1 config + 9 resolver + 17 probe/state/constant tests).

6. Commit:
   - Run: `git add Orchestrator/local_stack.py Orchestrator/tests/test_local_stack.py && git commit -m "local_stack: llama-swap front-door liveness probes + download-state reader"`

---

### Task 1.5: `GET /local-models/status` route module + registration

Assemble the status response: install/enable/health flags, `base_url`, hardware tier (verbatim), disk headroom vs the 40 GB gate, per-member running + download state, and the per-capability on-box routing decision. Mirrors the `/embeddings/status` route conventions (plain `def` → threadpool for the sync httpx probes; `Cache-Control: no-store`; additive binding contract). A NEW router module (`local_models_routes.py`) keeps the `/local-models` prefix cleanly separate from `local_routes.py`'s on-device-Gemma `/local/*` endpoints and stays testable with a minimal FastAPI + just the router.

**Files:**
- Create: `Orchestrator/routes/local_models_routes.py`
- Modify: `Orchestrator/app.py` (register the router after line 133, the rerank router include)
- Test: `Orchestrator/tests/test_local_models_status.py` (create)

Steps:

1. Create `Orchestrator/tests/test_local_models_status.py`:

```python
"""Tests for GET /local-models/status (M1).

The JSON shape is an ADDITIVE binding contract (local_models wizard step +
Updates panels) — shape assertions are deliberate lock-in. A minimal FastAPI
with just this router; every local_stack/hardware seam is monkeypatched (same
recipe as test_embeddings_routes.py). No real network, no real config.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator import hardware, local_stack
from Orchestrator.routes.local_models_routes import router

STATUS_KEYS = {
    "installed", "enabled", "healthy", "base_url",
    "hardware", "disk", "llama_swap", "models", "routing",
}
MODEL_KEYS = {"model", "capability", "group", "label", "running", "state", "download"}
DISK_KEYS = {"free_mb", "required_mb", "ok"}
ROUTING_KEYS = {"enabled", "healthy", "decision"}

FAKE_HW = {
    "gpu": True, "gpu_name": "NVIDIA RTX 2000 Ada Generation", "vram_mb": 16380,
    "ram_mb": 128000, "source": "nvidia-smi", "tier": "HIGH",
}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _wire(monkeypatch, *, installed=True, reachable=True, running=None,
          downloads=None, enabled_caps=(), disk_free=50 * 1024):
    monkeypatch.setattr(local_stack, "is_installed", lambda: installed)
    monkeypatch.setattr(local_stack, "master_enabled", lambda: installed)
    monkeypatch.setattr(local_stack, "base_url", lambda: "http://127.0.0.1:9098/v1")
    monkeypatch.setattr(local_stack, "llama_swap_health", lambda: {
        "reachable": reachable, "status_code": 200 if reachable else None,
    })
    monkeypatch.setattr(local_stack, "running_members", lambda: running)
    monkeypatch.setattr(local_stack, "read_download_state", lambda: downloads or {})
    monkeypatch.setattr(local_stack, "enabled", lambda cap: cap in enabled_caps)
    monkeypatch.setattr(hardware, "probe", lambda: dict(FAKE_HW))
    monkeypatch.setattr(hardware, "disk_free_mb", lambda: disk_free)


def test_status_shape_and_no_store(client, monkeypatch):
    _wire(monkeypatch, running=[{"model": "embed-qwen3-8b", "state": "ready"}],
          enabled_caps=("embeddings",))
    r = client.get("/local-models/status")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == STATUS_KEYS
    assert body["installed"] is True
    assert body["healthy"] is True
    assert body["base_url"] == "http://127.0.0.1:9098/v1"
    assert body["hardware"]["tier"] == "HIGH"


def test_disk_block(client, monkeypatch):
    _wire(monkeypatch, disk_free=50 * 1024)
    disk = client.get("/local-models/status").json()["disk"]
    assert set(disk) == DISK_KEYS
    assert disk == {"free_mb": 50 * 1024, "required_mb": 40 * 1024, "ok": True}


def test_disk_block_insufficient(client, monkeypatch):
    _wire(monkeypatch, disk_free=10 * 1024)
    assert client.get("/local-models/status").json()["disk"]["ok"] is False


def test_disk_block_unknown(client, monkeypatch):
    _wire(monkeypatch, disk_free=None)
    disk = client.get("/local-models/status").json()["disk"]
    assert disk["free_mb"] is None and disk["ok"] is None


def test_models_rollup(client, monkeypatch):
    _wire(monkeypatch,
          running=[{"model": "speaches", "state": "loading"}],
          downloads={"embed-qwen3-8b": {"state": "downloaded"}})
    models = client.get("/local-models/status").json()["models"]
    assert [m["model"] for m in models] == \
        ["embed-qwen3-8b", "rerank-qwen3-0.6b", "speaches", "qwen-tts"]
    for m in models:
        assert set(m) == MODEL_KEYS
    by_id = {m["model"]: m for m in models}
    assert by_id["speaches"]["running"] is True
    assert by_id["speaches"]["state"] == "loading"
    assert by_id["embed-qwen3-8b"]["running"] is False
    assert by_id["embed-qwen3-8b"]["state"] is None
    assert by_id["embed-qwen3-8b"]["download"] == {"state": "downloaded"}
    assert by_id["qwen-tts"]["download"] == {"state": "pending"}  # absent -> pending


def test_llama_swap_running_passthrough_and_null(client, monkeypatch):
    _wire(monkeypatch, running=None, reachable=False, installed=True)
    body = client.get("/local-models/status").json()
    assert body["llama_swap"]["running"] is None       # unreachable -> null
    assert body["llama_swap"]["reachable"] is False
    assert body["healthy"] is False


def test_routing_decisions(client, monkeypatch):
    _wire(monkeypatch, reachable=True, enabled_caps=("embeddings", "rerank"))
    routing = client.get("/local-models/status").json()["routing"]
    assert set(routing) == set(local_stack.CAPABILITIES)
    for cap in local_stack.CAPABILITIES:
        assert set(routing[cap]) == ROUTING_KEYS
    assert routing["embeddings"]["decision"] == "on-box"   # seeded + healthy
    assert routing["rerank"]["decision"] == "on-box"
    assert routing["stt"]["decision"] == "off"             # not seeded
    assert routing["tts"]["decision"] == "off"


def test_routing_seeded_but_unhealthy(client, monkeypatch):
    _wire(monkeypatch, reachable=False, enabled_caps=("stt",))
    routing = client.get("/local-models/status").json()["routing"]
    assert routing["stt"]["decision"] == "unhealthy"       # seeded on, stack down


def test_not_installed(client, monkeypatch):
    _wire(monkeypatch, installed=False, reachable=True, enabled_caps=())
    body = client.get("/local-models/status").json()
    assert body["installed"] is False
    assert body["healthy"] is False
    assert all(v["decision"] == "off" for v in body["routing"].values())
```

2. Run, expect FAIL (route module doesn't exist):
   - Run: `python -m pytest Orchestrator/tests/test_local_models_status.py -q`
   - Expected: collection error `ModuleNotFoundError: No module named 'Orchestrator.routes.local_models_routes'`.

3. Create `Orchestrator/routes/local_models_routes.py`:

```python
"""On-box local model stack status endpoint (M1).

GET /local-models/status — aggregates llama-swap /health + /running, the host
hardware tier, disk headroom, per-model download state, and the per-capability
on-box routing decision. The JSON shape is an ADDITIVE binding contract (mirrors
GET /embeddings/status conventions) consumed by the local_models wizard step and
the Updates panels (status-only). Read-only: never mutates state.

Later capability milestones enrich routing[cap] ADDITIVELY (explicit-user-pick +
cloud-fallback target); M1 reports the on-box view. Plain `def` — the httpx
probes are sync and FastAPI runs sync routes in the threadpool (embeddings/status
precedent), so the front-door probes never stall the event loop.
"""
from fastapi import APIRouter, Response

from Orchestrator import hardware, local_stack

router = APIRouter(prefix="/local-models", tags=["local-models"])


def _routing_decision(cap: str, healthy: bool) -> dict:
    """On-box routing view for one capability. `seeded` = the wizard-time D2
    default (local_stack.enabled). decision: "on-box" (seeded + reachable),
    "unhealthy" (seeded but the stack is down -> per-capability degradation),
    or "off" (not seeded -> an explicit pick / cloud owns it)."""
    seeded = local_stack.enabled(cap)
    if not seeded:
        decision = "off"
    elif healthy:
        decision = "on-box"
    else:
        decision = "unhealthy"
    return {"enabled": seeded, "healthy": healthy, "decision": decision}


@router.get("/status")
def local_models_status(response: Response):
    # no-store: routing/health/download state flips on wizard activation and
    # service up/down; a WebView caching this would draw a stale panel.
    response.headers["Cache-Control"] = "no-store"

    installed = local_stack.is_installed()
    health = local_stack.llama_swap_health()
    healthy = installed and health["reachable"]
    running = local_stack.running_members()
    running_by_id = {r["model"]: r for r in (running or [])}
    downloads = local_stack.read_download_state()
    hw = hardware.probe()

    free_mb = hardware.disk_free_mb()
    disk = {
        "free_mb": free_mb,
        "required_mb": local_stack.DISK_GATE_MB,
        "ok": (free_mb >= local_stack.DISK_GATE_MB) if free_mb is not None else None,
    }

    models = []
    for m in local_stack.MEMBERS:
        run = running_by_id.get(m["model"])
        dl = downloads.get(m["model"])
        models.append({
            "model": m["model"],
            "capability": m["capability"],
            "group": m["group"],
            "label": m["label"],
            "running": run is not None,
            "state": run["state"] if run else None,
            "download": dl if isinstance(dl, dict) else {"state": "pending"},
        })

    routing = {cap: _routing_decision(cap, healthy) for cap in local_stack.CAPABILITIES}

    return {
        "installed": installed,
        "enabled": local_stack.master_enabled(),
        "healthy": healthy,
        "base_url": local_stack.base_url(),
        "hardware": hw,               # verbatim probe() shape, incl. tier
        "disk": disk,
        "llama_swap": {
            "reachable": health["reachable"],
            "health_status": health["status_code"],
            "running": running,       # null when the proxy is unreachable
        },
        "models": models,
        "routing": routing,
    }
```

4. Register the router in `Orchestrator/app.py`. Replace the rerank-router include block (lines 132-133) so the new include follows it:

   Old:
   ```python
   from Orchestrator.routes.rerank_routes import router as rerank_router
   app.include_router(rerank_router)
   ```
   New:
   ```python
   from Orchestrator.routes.rerank_routes import router as rerank_router
   app.include_router(rerank_router)

   from Orchestrator.routes.local_models_routes import router as local_models_status_router
   app.include_router(local_models_status_router)
   ```

5. Run the route tests, expect PASS:
   - Run: `python -m pytest Orchestrator/tests/test_local_models_status.py -q`
   - Expected: `9 passed`.

6. Run the whole M1 suite to confirm nothing regressed:
   - Run: `python -m pytest Orchestrator/tests/test_local_stack.py Orchestrator/tests/test_local_models_status.py Orchestrator/tests/test_hardware.py -q`
   - Expected: `39 passed` (27 local_stack + 9 status + 3 disk in the ~existing hardware file; existing hardware tests also pass).

7. Verify the app imports cleanly (route registration doesn't break startup — the tree must stay runnable):
   - Run: `python -c "import Orchestrator.app"`
   - Expected: no traceback (exit 0). (Requires the repo's config.ini + venv, as any app import does.)

8. Commit:
   - Run: `git add Orchestrator/routes/local_models_routes.py Orchestrator/app.py Orchestrator/tests/test_local_models_status.py && git commit -m "feat(local-stack): GET /local-models/status — llama-swap + hardware + disk + routing rollup"`

---

### Milestone 1 done-check

- `Orchestrator/local_stack.py` is the sole on-box resolver: fresh-read `[local_models]` config, front-door-liveness `is_healthy()` (never per-member VRAM), and the `enabled(cap)`/`should_route_onbox(cap)`/`base_url()` primitives later milestones consume.
- `GET /local-models/status` returns the additive binding-contract JSON for the wizard + Updates panels.
- Full pytest coverage with all llama-swap HTTP mocked (httpx `_transport` seam); no real network, no real config.ini, no restart needed for a wizard flag flip.
- Nothing routes on-box yet — the per-capability flags default false, and no capability resolver has been rewired (that is later milestones). The tree stays fully runnable at every commit (all additions are new/additive).


---

## Milestone 2: Install layer

**Depends on:** Milestone 1 (creates `Orchestrator/routes/local_models_routes.py` with `GET /local-models/status` and registers its router in `Orchestrator/app.py`; Task 2.7 appends the download route to that same `/local-models` router). Tasks 2.1–2.6 depend on nothing. Task 2.7's `Orchestrator/localstack_downloads.py` module and its tests are self-contained; only the route wiring touches M1's file (a fallback create-form is given for out-of-order execution).

**Goal.** Deliver everything a fresh box needs to *provision* the on-box stack from `Scripts/install.sh` + the wizard, without activating anything implicitly. This lands the llama-swap `config.yaml` design template (spec §8, speaches corrected to the static canonical port 9099), a self-gating `blackbox-install-localstack.sh` (llama-swap + llama.cpp `llama-server` + Speaches/qwen-tts venvs, sha256-pinned, CUDA-behind-`nvidia-smi`), the `blackbox-models.service` unit + its `models-unit` dispatch target + sudoers grants, the CU framebuffer apt packages, a fail-soft disk probe, and the disk-gated `POST /local-models/download` NDJSON weight-download endpoint. The install step is non-fatal and re-run-safe so a broken member never blocks the rest of `install.sh`; weights download later (disk-gated) so install stays fast.

Canonical anchors used below (verified against the working tree 2026-07-20):
- llama-swap front door port **9098**; Speaches member pinned to static **9099** (Design-B direct-WS needs a known port — §5.3/§6/D12).
- `LOCALSTACK_HOME=$REAL_HOME/.blackbox/localstack`, `LOCALSTACK_BIN=$LOCALSTACK_HOME/bin`, `LOCALSTACK_MODELS=$LOCALSTACK_HOME/models`, `SPEACHES_VENV=$LOCALSTACK_HOME/speaches-venv`, `QWEN_TTS_VENV=$LOCALSTACK_HOME/qwen-tts-venv`. (Big artifacts live under `$REAL_HOME`, never in the repo/git — same placement rule as `$REAL_HOME/rerank-venv`, and `blackbox.service` runs `ProtectHome=no` so the Orchestrator can write weights there.)

---

### Task 2.1: CU framebuffer apt packages (allowlist)

The apt dispatcher (`installer/templates/blackbox-apt-install.sh:62`) and `Scripts/install.sh:75` install ONLY packages whose line matches `^[a-zA-Z0-9._+-]+\s+#\s+(MUST_HAVE|SHOULD_HAVE)` in `Scripts/onboarding/system-packages.txt`. `xvfb`/`websockify`/`novnc` are absent, so a fresh box cannot install the CU framebuffer (spec §8 item 7, §9). `xdotool`/`scrot`/`openbox`/`x11vnc` are already present.

**Files:**
- Modify: `Scripts/onboarding/system-packages.txt` (SHOULD_HAVE block, after `x11vnc` at line 23)

1. Add the three packages. Edit `Scripts/onboarding/system-packages.txt`, replacing the `x11vnc` line:

   Old:
   ```
   x11vnc                   # SHOULD_HAVE # remote desktop CU
   ```
   New:
   ```
   x11vnc                   # SHOULD_HAVE # remote desktop CU
   # ── CU virtual displays (local-model-stack §9) ──
   xvfb                     # MUST_HAVE # CU per-session virtual X displays (CU gates on this)
   websockify               # SHOULD_HAVE # CU live-view WS bridge (noVNC transport)
   novnc                    # SHOULD_HAVE # CU live-view browser client
   ```
   Rationale for buckets (spec §8/§9): `xvfb` is MUST_HAVE (per-session CU displays gate on it); `websockify`/`novnc` are SHOULD_HAVE (live view). MUST_HAVE + SHOULD_HAVE both pass the allowlist grep, so all three become installable and get installed by `install.sh` Step 1.

2. Verify the allowlist parser now emits them (the exact grep both install paths use):
   - Run: `grep -E '^[a-zA-Z0-9._+-]+\s+#\s+(MUST_HAVE|SHOULD_HAVE)' Scripts/onboarding/system-packages.txt | awk '{print $1}' | grep -E '^(xvfb|websockify|novnc)$' | sort | tr '\n' ' '`
   - Expected: `novnc websockify xvfb `

3. Commit.
   - Run: `git add Scripts/onboarding/system-packages.txt && git commit -m "installer: add xvfb/websockify/novnc to the system-package allowlist for CU virtual displays"`
   - Expected: one commit, one file changed.

---

### Task 2.2: Disk-free probe — already provided by M1 (NO-OP)

**The single §7 disk probe is `hardware.disk_free_mb()`, landed in M1 Task 1.2** (fail-soft, MB, default = the BlackBox root; `import shutil` added there). It is the ONE probe shared by BOTH the `GET /local-models/status` endpoint (M1 Task 1.5) AND this milestone's download gate (Task 2.7) — exactly as the M1 "Contracts this milestone exposes" line states. There is **no second probe**: an earlier draft added a duplicate `hardware.disk_free_gb()` (GB, default = `~`), which produced a duplicate `import shutil` and two probes with different units/default paths for one commitment. That duplicate is deleted; the download gate (Task 2.7) calls `hardware.disk_free_mb()` and compares against the same 40 GB threshold (`local_stack.DISK_GATE_MB` / `40 * 1024` MB) the status endpoint reports.

**Files:** none (this task adds no code — the probe lives in M1 Task 1.2).

1. Confirm the M1 probe exists and no `disk_free_gb` duplicate was introduced:
   - Run: `python -c "from Orchestrator import hardware; assert hasattr(hardware, 'disk_free_mb'); assert not hasattr(hardware, 'disk_free_gb'), 'duplicate probe present — delete it'; print('OK: single disk probe')"`
   - Expected: `OK: single disk probe`.
2. No commit (nothing changed here). The probe + its tests are committed in M1 Task 1.2.

---

### Task 2.3: llama-swap `config.yaml` design template

Transcribe the spec §8 DESIGN TEMPLATE verbatim as `installer/templates/llama-swap-config.yaml.template`, with the ONE required correction: the spec template binds `speaches` to `${PORT}`, but the canonical names + D12 require Speaches on a **static** port so the Orchestrator's direct-WS STT bridge (`/ws/stt` `_local_bridge` → Speaches `/v1/realtime`) has a known target. Correct `speaches` to `127.0.0.1:9099` (see the DEVIATION comment). Everything else is verbatim. `${PORT}` (llama-swap's own per-member loopback assignment), `${llama-server}`, `${models-dir}` stay literal; only `${LOCALSTACK_BIN}`/`${LOCALSTACK_MODELS}`/`${SPEACHES_VENV}`/`${QWEN_TTS_VENV}` are shell-substituted at install time (Task 2.5).

**Files:**
- Create: `installer/templates/llama-swap-config.yaml.template`

1. Create the template with this exact content:
   ```yaml
   # installer/templates/llama-swap-config.yaml.template — DESIGN TEMPLATE
   # Landed by install.sh Step 2f (blackbox-install-localstack.sh), tier-adjusted
   # (§7). ${LOCALSTACK_BIN}/${LOCALSTACK_MODELS}/${SPEACHES_VENV}/${QWEN_TTS_VENV}
   # are shell-substituted at write time; ${PORT} is llama-swap's OWN per-member
   # assigned loopback port (left literal for llama-swap to fill); ${llama-server}
   # and ${models-dir} are llama-swap MACROS (left literal). One binary
   # (blackbox-models.service) supervises all four members, each bound to 127.0.0.1.
   #
   # DEVIATION FROM SPEC §8 TEMPLATE: the "speaches" member is pinned to the
   # STATIC port 9099 instead of ${PORT}. Design-B streaming STT (D12) opens a
   # direct WebSocket from the Orchestrator to Speaches /v1/realtime, which needs
   # a KNOWN port; llama-swap's ${PORT} is assigned dynamically per load and is
   # not discoverable by the Orchestrator. Pinning 9099 gives a stable direct-WS
   # target. All other members keep ${PORT} (they are reached only through the
   # :9098 proxy / /upstream). Canonical: front door 9098, speaches 9099.

   healthCheckTimeout: 120        # global default (seconds); per-member overrides below
   logLevel: info

   macros:
     llama-server: "${LOCALSTACK_BIN}/llama-server"
     models-dir:   "${LOCALSTACK_MODELS}"

   models:
     # ── retrieval group ────────────────────────────────────────────────
     "embed-qwen3-8b":
       # Official Qwen/Qwen3-Embedding-8B-GGUF @ Q8_0 (8.05GB). Last-token pooling.
       # -b/-ub forced to the full input seq (non-causal pooling); -fa bounds the
       # compute buffer. --pooling last only if the build doesn't auto-detect it.
       cmd: |
         ${llama-server}
         --model ${models-dir}/Qwen3-Embedding-8B-Q8_0.gguf
         --host 127.0.0.1 --port ${PORT}
         --embeddings --pooling last
         -c 8192 -b 8192 -ub 8192 -fa
         -ngl 99 --no-warmup
       proxy: "http://127.0.0.1:${PORT}"
       checkEndpoint: "/health"
       healthCheckTimeout: 300      # 8GB weights load is slow — long startup gate
       ttl: 600                     # keep-warm ⇒ set ttl: 0 (immune to idle unload,
                                    #   still yields to a cross-group swap; §6)
       concurrencyLimit: 4

     "rerank-qwen3-0.6b":
       # SELF-CONVERTED GGUF from a llama.cpp build post-dating the
       # convert_hf_to_gguf.py fix (extracts cls.output.weight, pooling_type=RANK).
       # G2 gates score validity before this member can be selected.
       cmd: |
         ${llama-server}
         --model ${models-dir}/Qwen3-Reranker-0.6B-f16.gguf
         --host 127.0.0.1 --port ${PORT}
         --reranking --pooling rank
         -c 8192 -ngl 99 --no-warmup
       proxy: "http://127.0.0.1:${PORT}"
       checkEndpoint: "/health"
       healthCheckTimeout: 60
       ttl: 600
       concurrencyLimit: 2

     # ── audio group ────────────────────────────────────────────────────
     "speaches":
       # Pinned Speaches version (pre-1.0; capture /v1/realtime schema in G4/G6).
       # At most ONE whisper model resident (§5.3). Streaming via Design B
       # (direct-to-9099); llama-swap WS proxy (#754) is unavailable.
       # DEVIATION: static port 9099 (not ${PORT}) — see header.
       cmd: |
         ${SPEACHES_VENV}/bin/uvicorn --factory speaches.main:create_app
         --host 127.0.0.1 --port 9099
       proxy: "http://127.0.0.1:9099"
       checkEndpoint: "/health"
       healthCheckTimeout: 120
       ttl: 600
       concurrencyLimit: 4

     "qwen-tts":
       # Our in-repo FastAPI server (LocalModels/qwen_tts_server). Three variants
       # managed in-process with FREE-BEFORE-LOAD (§5.4). Clone/design routed via
       # /upstream/qwen-tts/... (not body-model auto-routed); speech/voices are.
       cmd: |
         ${QWEN_TTS_VENV}/bin/uvicorn qwen_tts_server.app:app
         --host 127.0.0.1 --port ${PORT}
       proxy: "http://127.0.0.1:${PORT}"
       checkEndpoint: "/health"
       healthCheckTimeout: 180      # first variant load
       ttl: 600
       concurrencyLimit: 2

   groups:
     # persistent:false on BOTH groups (persistent:true on both would deadlock
     # VRAM: >16GB). ttl:600 idle-unload applies per member within a live group.
     "retrieval":
       swap: false                  # embed + rerank CO-RESIDENT
       exclusive: true              # loading retrieval unloads the audio group
       persistent: false
       members:
         - "embed-qwen3-8b"
         - "rerank-qwen3-0.6b"
     "audio":
       swap: false                  # whisper + qwen-tts CO-RESIDENT
       exclusive: true              # loading audio unloads the retrieval group
       persistent: false
       members:
         - "speaches"
         - "qwen-tts"
   ```

2. Verify it parses as YAML and that the speaches deviation is present (the `${...}` shell/macros tokens are plain strings to a YAML parser):
   - Run: `python3 -c "import yaml; d=yaml.safe_load(open('installer/templates/llama-swap-config.yaml.template')); assert set(d['models'])=={'embed-qwen3-8b','rerank-qwen3-0.6b','speaches','qwen-tts'}; assert '9099' in d['models']['speaches']['proxy']; assert set(d['groups'])=={'retrieval','audio'}; print('OK', d['groups']['retrieval']['exclusive'], d['groups']['audio']['exclusive'])"`
   - Expected: `OK True True`
   - Run: `grep -c '9099' installer/templates/llama-swap-config.yaml.template`
   - Expected: `3` (header note + cmd `--port 9099` + `proxy`), and `grep -c '${PORT}' installer/templates/llama-swap-config.yaml.template` → `6` (embed cmd+proxy, rerank cmd+proxy, qwen-tts cmd+proxy; speaches uses NONE).

3. Commit.
   - Run: `git add installer/templates/llama-swap-config.yaml.template && git commit -m "installer: land llama-swap config.yaml design template (speaches pinned to static :9099)"`

---

### Task 2.4: `blackbox-models.service` unit + `models-unit` dispatch target + sudoers grants

Three coupled pieces (spec §8 item 5): the systemd unit template; the new `models-unit` `target_kind` in the root dispatch helper (so the update pipeline can rewrite the unit from inside `blackbox.service`'s `ProtectSystem=strict` namespace); and the sudoers grants (`restart blackbox-models.service`, plus the Phase-2 Step-0 `stop` grants for `vllm-reranker.service`/`ollama`).

**Files:**
- Create: `installer/templates/blackbox-models.service`
- Modify: `installer/templates/blackbox-write-systemd.sh` (header list lines 24-28; case block after line 70; error message line 77)
- Modify: `installer/templates/sudoers-blackbox-system` (append new section)

1. Create the unit template `installer/templates/blackbox-models.service`:
   ```
   # /etc/systemd/system/blackbox-models.service
   # Generated by BlackBox blackbox-install-localstack.sh — DO NOT EDIT BY HAND.
   #
   # llama-swap front door on 127.0.0.1:9098 supervising the on-box local-model
   # stack: the "retrieval" group (embed-qwen3-8b + rerank-qwen3-0.6b) and the
   # "audio" group (speaches STT + our qwen-tts server). One binary spawns/kills
   # the member servers on demand per the generated
   # LOCALSTACK_HOME_PLACEHOLDER/llama-swap-config.yaml. --watch-config
   # auto-restarts the whole proxy on any config edit (§6).
   #
   # REAL_USER_PLACEHOLDER / REAL_HOME_PLACEHOLDER / LOCALSTACK_BIN_PLACEHOLDER /
   # LOCALSTACK_HOME_PLACEHOLDER are substituted at install time (same
   # sed-template flow as vllm-reranker.service / zellij-web.service).
   #
   # GPU and CPU boxes both run this unit; only the member builds/models differ
   # (§7). Members lazy-load on first request, so the front door answers /health
   # with ZERO weights resident — weights download later in the wizard
   # (disk-gated, POST /local-models/download). LD_LIBRARY_PATH points at the
   # localstack bin dir so a CUDA llama-server prebuilt finds its bundled libs.

   [Unit]
   Description=BlackBox local model stack (llama-swap front door :9098)
   After=network-online.target
   Wants=network-online.target

   [Service]
   User=REAL_USER_PLACEHOLDER
   WorkingDirectory=REAL_HOME_PLACEHOLDER
   Environment=LD_LIBRARY_PATH=LOCALSTACK_BIN_PLACEHOLDER
   ExecStart=LOCALSTACK_BIN_PLACEHOLDER/llama-swap --config LOCALSTACK_HOME_PLACEHOLDER/llama-swap-config.yaml --listen 127.0.0.1:9098 --watch-config
   Restart=on-failure
   RestartSec=15
   TimeoutStartSec=120

   [Install]
   WantedBy=multi-user.target
   ```

2. Add the `models-unit` target to the dispatch helper. Edit `installer/templates/blackbox-write-systemd.sh`.

   Header list — Old (lines 23-28):
   ```
   # Valid target_kind values:
   #   unit                  → /etc/systemd/system/blackbox.service
   #   override              → /etc/systemd/system/blackbox.service.d/override.conf
   #   cli-agent-overrides   → /etc/systemd/system/blackbox.service.d/cli-agent-overrides.conf
   #   zellij-web-unit       → /etc/systemd/system/zellij-web.service
   #   sudoers-system        → /etc/sudoers.d/blackbox-system
   ```
   New:
   ```
   # Valid target_kind values:
   #   unit                  → /etc/systemd/system/blackbox.service
   #   override              → /etc/systemd/system/blackbox.service.d/override.conf
   #   cli-agent-overrides   → /etc/systemd/system/blackbox.service.d/cli-agent-overrides.conf
   #   zellij-web-unit       → /etc/systemd/system/zellij-web.service
   #   models-unit           → /etc/systemd/system/blackbox-models.service
   #   sudoers-system        → /etc/sudoers.d/blackbox-system
   ```

   Case block — Old (lines 67-70):
   ```bash
       zellij-web-unit)
           DEST="/etc/systemd/system/zellij-web.service"
           IS_SUDOERS=0
           ;;
   ```
   New:
   ```bash
       zellij-web-unit)
           DEST="/etc/systemd/system/zellij-web.service"
           IS_SUDOERS=0
           ;;
       models-unit)
           DEST="/etc/systemd/system/blackbox-models.service"
           IS_SUDOERS=0
           ;;
   ```

   Error message — Old (line 77):
   ```bash
           echo "[blackbox-write-systemd] (Valid: unit | override | cli-agent-overrides | zellij-web-unit | sudoers-system)" >&2
   ```
   New:
   ```bash
           echo "[blackbox-write-systemd] (Valid: unit | override | cli-agent-overrides | zellij-web-unit | models-unit | sudoers-system)" >&2
   ```

3. Add the sudoers grants. Append this section to the end of `installer/templates/sudoers-blackbox-system` (after the zellij block, line 113). `daemon-reload`/`enable` are NOT re-granted — `daemon-reload` is already covered (general-purpose, zellij block line 112) and unit `enable` runs installer-time as root:
   ```
   
   # ── Local model stack (local-model-stack M2) ────────────────────────────
   # blackbox-models.service is the llama-swap front door (:9098) supervising
   # the on-box STT/TTS/embeddings/rerank members. `restart` is normal lifecycle
   # (config regen on update, hung-member recovery, post-update bounce); `stop`
   # is used by the Phase-2 MS02 migration. daemon-reload is already granted
   # above (general-purpose) and unit INSTALL+enable is installer-time root.
   #
   # The Phase-2 Step-0 reset must RETIRE the pre-stack always-resident pair
   # before the retrieval group can first load (a pointer flip alone leaves the
   # keep_alive=-1-pinned ~7GB Ollama 8B resident → CUDA OOM at the re-embed):
   # stop vllm-reranker.service AND ollama to free VRAM. These are /etc-free
   # runtime PID-1 transitions; exact-command grants keep the perimeter tight.
   REAL_USER_PLACEHOLDER ALL=(root) NOPASSWD: /usr/bin/systemctl restart blackbox-models.service
   REAL_USER_PLACEHOLDER ALL=(root) NOPASSWD: /usr/bin/systemctl stop blackbox-models.service
   REAL_USER_PLACEHOLDER ALL=(root) NOPASSWD: /usr/bin/systemctl disable --now vllm-reranker.service
   REAL_USER_PLACEHOLDER ALL=(root) NOPASSWD: /usr/bin/systemctl stop vllm-reranker.service
   REAL_USER_PLACEHOLDER ALL=(root) NOPASSWD: /usr/bin/systemctl stop ollama.service
   ```

4. Verify the dispatcher still parses and the sudoers template is syntactically valid once rendered (visudo -c needs a concrete user, so render the placeholder first):
   - Run: `bash -n installer/templates/blackbox-write-systemd.sh && echo SYNTAX_OK`
   - Expected: `SYNTAX_OK`
   - Run: `sed 's/REAL_USER_PLACEHOLDER/bbx/g; s#BLACKBOX_ROOT_PLACEHOLDER#/home/bbx/x#g' installer/templates/sudoers-blackbox-system > /tmp/sudoers.rendered && visudo -c -f /tmp/sudoers.rendered`
   - Expected: `/tmp/sudoers.rendered: parsed OK`
   - Run: `grep -c 'blackbox-models.service' installer/templates/sudoers-blackbox-system`
   - Expected: `3` (restart, stop, comment reference — plus the two vllm/ollama lines are separate).

5. Commit.
   - Run: `git add installer/templates/blackbox-models.service installer/templates/blackbox-write-systemd.sh installer/templates/sudoers-blackbox-system && git commit -m "installer: blackbox-models.service unit + models-unit dispatch target + sudoers grants"`

---

### Task 2.5: `blackbox-install-localstack.sh` + pinned versions

The provisioner, modeled closely on `blackbox-install-reranker.sh` (arg convention, user/home/root resolve, sed-templated unit, health poll, distinct exit codes) — but it installs on BOTH GPU and CPU tiers (the `nvidia-smi` gate selects the llama.cpp *build*, it is NOT an exit-0 skip; only vLLM stays GPU-only). Downloads a sha256-verified llama-swap release binary (zellij pattern) and a llama.cpp `llama-server` prebuilt (CUDA behind the gate, CPU otherwise), creates the Speaches + qwen-tts venvs, writes the tier-adjusted `llama-swap-config.yaml` from the template, installs + starts `blackbox-models.service`, and health-polls `:9098/health`. **No weights** here (§8 item 6).

**Files:**
- Create: `installer/templates/llama-swap-version`
- Create: `installer/templates/llamacpp-version`
- Create: `installer/templates/blackbox-install-localstack.sh`

1. Create the llama-swap version pin `installer/templates/llama-swap-version` (bare version on the first non-comment line, same parser as `zellij-version`):
   ```
   # Pinned llama-swap release (github.com/mostlygeek/llama-swap).
   # The installer downloads llama-swap_<VER>_linux_amd64.tar.gz + the release's
   # published checksums.txt, verifies the tarball sha256, then installs the
   # binary to $LOCALSTACK_BIN/llama-swap. Bump on upgrade. Confirm the asset
   # name on the release page if goreleaser naming changes.
   # Format: bare version tag (no leading v) on the first non-comment line.
   240
   ```

2. Create the llama.cpp version pin `installer/templates/llamacpp-version`. llama.cpp releases do NOT ship a stable machine-readable checksums file, so the per-asset sha256 IS pinned here (this file is the trust anchor). Three non-comment lines: build tag, CPU-asset sha256, CUDA-asset sha256:
   ```
   # Pinned llama.cpp release (github.com/ggml-org/llama.cpp) for the llama-server
   # prebuilt. Three non-comment lines below: <build-tag>, <cpu-zip-sha256>,
   # <cuda-zip-sha256>. The installer picks the CUDA asset when nvidia-smi shows
   # >=8000 MB VRAM, else the CPU asset, and verifies its zip against the pinned
   # sha before extracting llama-server.
   #
   # EXECUTION-TIME STEP (fill the two shas before the FIRST run on a new pin —
   # they cannot be known at authoring time; the installer hard-fails on the
   # FILL_* placeholders):
   #   V=b6620
   #   curl -fsSL -o /tmp/cpu.zip  https://github.com/ggml-org/llama.cpp/releases/download/$V/llama-$V-bin-ubuntu-x64.zip      && sha256sum /tmp/cpu.zip
   #   curl -fsSL -o /tmp/cuda.zip https://github.com/ggml-org/llama.cpp/releases/download/$V/llama-$V-bin-ubuntu-cuda-x64.zip && sha256sum /tmp/cuda.zip
   # Also confirm the two asset filenames on the release page (naming has drifted
   # historically) and update ASSET_CPU/ASSET_CUDA in blackbox-install-localstack.sh
   # if they differ.
   b6620
   FILL_CPU_SHA256_BEFORE_RUN
   FILL_CUDA_SHA256_BEFORE_RUN
   ```

3. Create `installer/templates/blackbox-install-localstack.sh` with this exact content:
   ```bash
   #!/usr/bin/env bash
   # blackbox-install-localstack — provision the on-box local-model stack
   # (local-model-stack plan, Milestone 2). Stands up llama-swap (the :9098 front
   # door), the llama.cpp llama-server binary, the Speaches + qwen-tts venvs, the
   # generated llama-swap config.yaml, and blackbox-models.service. Modeled on
   # blackbox-install-reranker.sh.
   #
   # Unlike the reranker, this runs on BOTH GPU and CPU boxes: the nvidia-smi
   # gate selects the llama.cpp BUILD (CUDA vs CPU), it is NOT an exit-0 skip.
   # (vLLM stays GPU-only in blackbox-install-reranker.sh; this is the always-on
   # stack.) NO weights are downloaded here — that happens in the wizard,
   # disk-gated (POST /local-models/download).
   #
   # IDEMPOTENT / re-run safe:
   #   - llama-swap binary at pinned version -> skip download
   #   - llama-server marker == pinned tag   -> skip download
   #   - venv exists                         -> pip --upgrade path
   #   - unit exists                         -> re-written + daemon-reload
   #   - service running                     -> restarted + re-verified
   #
   # Usage (install.sh Step 2f passes all three; standalone auto-detects):
   #   sudo bash installer/templates/blackbox-install-localstack.sh \
   #       [real_user] [real_home] [blackbox_root]
   #
   # Exit codes:
   #   0 — provisioned + llama-swap answering on :9098/health
   #   2 — could not resolve user/home
   #   4 — download/verify or venv creation failed
   #   6 — blackbox-models.service never answered on :9098 in time
   #
   # install.sh invokes this NON-FATALLY: cloud STT/TTS/embeddings/rerank keep
   # working and the wizard's local_models step shows the remediation.

   set -euo pipefail

   REAL_USER="${1:-}"
   REAL_HOME="${2:-}"
   BLACKBOX_ROOT="${3:-}"

   # ── Resolve user/home/root (mirrors blackbox-install-reranker.sh) ──────────
   if [[ -z "$REAL_USER" ]]; then
       if [[ $EUID -eq 0 && -n "${SUDO_USER:-}" ]]; then
           REAL_USER="$SUDO_USER"
       else
           REAL_USER="${USER:-}"
       fi
   fi
   if [[ -z "$REAL_HOME" && -n "$REAL_USER" ]]; then
       REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
   fi
   if [[ -z "$REAL_USER" || -z "$REAL_HOME" ]]; then
       echo "[install-localstack] ERROR: could not resolve user/home (got user='$REAL_USER' home='$REAL_HOME')" >&2
       exit 2
   fi
   if [[ -z "$BLACKBOX_ROOT" ]]; then
       BLACKBOX_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
   fi

   TEMPLATE_DIR="$BLACKBOX_ROOT/installer/templates"
   LOCALSTACK_HOME="$REAL_HOME/.blackbox/localstack"
   LOCALSTACK_BIN="$LOCALSTACK_HOME/bin"
   LOCALSTACK_MODELS="$LOCALSTACK_HOME/models"
   SPEACHES_VENV="$LOCALSTACK_HOME/speaches-venv"
   QWEN_TTS_VENV="$LOCALSTACK_HOME/qwen-tts-venv"
   CONFIG_DEST="$LOCALSTACK_HOME/llama-swap-config.yaml"
   FRONT_PORT=9098
   VERIFY_TIMEOUT_S=180
   # Speaches pin (pre-1.0; §5.3). qwen-tts deps come from the repo requirements
   # (TTS milestone); a fastapi/uvicorn floor keeps the member's uvicorn present.
   SPEACHES_PIN="speaches==0.9.0rc3"

   PYBIN="$(command -v python3.12 || command -v python3)"

   # Everything lives under the user's home, owned by REAL_USER.
   sudo -u "$REAL_USER" mkdir -p "$LOCALSTACK_BIN" "$LOCALSTACK_MODELS"

   # ── GPU build selector (NOT a skip) ────────────────────────────────────────
   USE_CUDA=0
   if command -v nvidia-smi >/dev/null 2>&1; then
       VRAM_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
           | sort -nr | head -n1 | tr -d '[:space:]' || true)"
       if [[ "$VRAM_MB" =~ ^[0-9]+$ ]] && (( VRAM_MB >= 8000 )); then
           USE_CUDA=1
           echo "[install-localstack] GPU detected (${VRAM_MB} MB VRAM) — CUDA llama-server build."
       else
           echo "[install-localstack] GPU present but VRAM <8000 MB (or unreadable) — CPU llama-server build."
       fi
   else
       echo "[install-localstack] No NVIDIA GPU — CPU llama-server build (honest CPU tier, §7)."
   fi

   # ── 1. llama-swap release binary (sha256-verified; zellij pattern) ─────────
   LS_VER="$(grep -vE '^[[:space:]]*(#|$)' "$TEMPLATE_DIR/llama-swap-version" | head -n1 | tr -d '[:space:]')"
   if [[ -z "$LS_VER" ]]; then
       echo "[install-localstack] ERROR: could not parse pinned llama-swap version" >&2
       exit 4
   fi
   need_ls=1
   if [[ -x "$LOCALSTACK_BIN/llama-swap" ]]; then
       cur="$("$LOCALSTACK_BIN/llama-swap" --version 2>/dev/null | grep -oE '[0-9]+' | head -n1 || true)"
       [[ "$cur" == "$LS_VER" ]] && { need_ls=0; echo "[install-localstack] llama-swap $LS_VER already installed, skipping."; }
   fi
   if (( need_ls )); then
       TMP_LS="$(mktemp -d /tmp/llama-swap-XXXXXX)"
       trap 'rm -rf "${TMP_LS:-}"' EXIT
       LS_TARBALL="llama-swap_${LS_VER}_linux_amd64.tar.gz"
       LS_BASE="https://github.com/mostlygeek/llama-swap/releases/download/v${LS_VER}"
       echo "[install-localstack] Downloading llama-swap v${LS_VER}..."
       if ! curl --fail --location --silent --show-error -o "$TMP_LS/$LS_TARBALL" "$LS_BASE/$LS_TARBALL" \
          || ! curl --fail --location --silent --show-error -o "$TMP_LS/checksums.txt" "$LS_BASE/checksums.txt"; then
           echo "[install-localstack] ERROR: llama-swap download failed ($LS_BASE)" >&2
           exit 4
       fi
       LS_EXPECTED="$(awk -v f="$LS_TARBALL" '$2==f || $2=="*"f {print $1}' "$TMP_LS/checksums.txt" | head -n1)"
       LS_ACTUAL="$(sha256sum "$TMP_LS/$LS_TARBALL" | awk '{print $1}')"
       if [[ -z "$LS_EXPECTED" || "$LS_EXPECTED" != "$LS_ACTUAL" ]]; then
           echo "[install-localstack] ERROR: llama-swap sha256 mismatch (expected='$LS_EXPECTED' actual='$LS_ACTUAL')" >&2
           exit 4
       fi
       tar -xzf "$TMP_LS/$LS_TARBALL" -C "$TMP_LS"
       LS_SRC="$(find "$TMP_LS" -type f -name llama-swap | head -n1)"
       if [[ -z "$LS_SRC" ]]; then
           echo "[install-localstack] ERROR: llama-swap binary not found after extract" >&2
           exit 4
       fi
       sudo install -m 0755 -o "$REAL_USER" -g "$REAL_USER" "$LS_SRC" "$LOCALSTACK_BIN/llama-swap"
       echo "[install-localstack] Installed $LOCALSTACK_BIN/llama-swap (v$LS_VER, sha256 $LS_ACTUAL)"
       rm -rf "$TMP_LS"; trap - EXIT
   fi

   # ── 2. llama.cpp llama-server prebuilt (CUDA behind the gate; sha-pinned) ──
   mapfile -t LC_PINS < <(grep -vE '^[[:space:]]*(#|$)' "$TEMPLATE_DIR/llamacpp-version")
   LC_VER="${LC_PINS[0]:-}"; LC_CPU_SHA="${LC_PINS[1]:-}"; LC_CUDA_SHA="${LC_PINS[2]:-}"
   if [[ -z "$LC_VER" ]]; then
       echo "[install-localstack] ERROR: could not parse pinned llama.cpp version" >&2
       exit 4
   fi
   # Confirm asset names on the release page if goreleaser/CI naming drifts.
   ASSET_CPU="llama-${LC_VER}-bin-ubuntu-x64.zip"
   ASSET_CUDA="llama-${LC_VER}-bin-ubuntu-cuda-x64.zip"
   if (( USE_CUDA )); then LC_ASSET="$ASSET_CUDA"; LC_SHA="$LC_CUDA_SHA"; else LC_ASSET="$ASSET_CPU"; LC_SHA="$LC_CPU_SHA"; fi
   if [[ "$LC_SHA" == FILL_* || -z "$LC_SHA" ]]; then
       echo "[install-localstack] ERROR: llama.cpp sha256 not pinned for $LC_ASSET." >&2
       echo "[install-localstack] Fill it in $TEMPLATE_DIR/llamacpp-version (see the header remediation), then re-run." >&2
       exit 4
   fi
   need_lc=1
   LC_MARKER="$LOCALSTACK_BIN/.llamacpp-version"
   if [[ -x "$LOCALSTACK_BIN/llama-server" && -f "$LC_MARKER" ]]; then
       [[ "$(cat "$LC_MARKER" 2>/dev/null)" == "$LC_VER" ]] && { need_lc=0; echo "[install-localstack] llama-server $LC_VER already installed, skipping."; }
   fi
   if (( need_lc )); then
       TMP_LC="$(mktemp -d /tmp/llamacpp-XXXXXX)"
       trap 'rm -rf "${TMP_LC:-}"' EXIT
       LC_URL="https://github.com/ggml-org/llama.cpp/releases/download/${LC_VER}/${LC_ASSET}"
       echo "[install-localstack] Downloading llama.cpp $LC_VER ($LC_ASSET)..."
       if ! curl --fail --location --silent --show-error -o "$TMP_LC/$LC_ASSET" "$LC_URL"; then
           echo "[install-localstack] ERROR: llama.cpp download failed ($LC_URL)" >&2
           exit 4
       fi
       LC_ACTUAL="$(sha256sum "$TMP_LC/$LC_ASSET" | awk '{print $1}')"
       if [[ "$LC_SHA" != "$LC_ACTUAL" ]]; then
           echo "[install-localstack] ERROR: llama.cpp sha256 mismatch (expected='$LC_SHA' actual='$LC_ACTUAL')" >&2
           exit 4
       fi
       # python3 zipfile — no unzip dependency (python3 is MUST_HAVE).
       "$PYBIN" -m zipfile -e "$TMP_LC/$LC_ASSET" "$TMP_LC/x"
       LC_SRC="$(find "$TMP_LC/x" -type f -name llama-server | head -n1)"
       if [[ -z "$LC_SRC" ]]; then
           echo "[install-localstack] ERROR: llama-server not found in $LC_ASSET after extract" >&2
           exit 4
       fi
       # Copy the WHOLE bin dir (CUDA prebuilt bundles libllama/libggml *.so beside
       # the binary; LD_LIBRARY_PATH in the unit points at $LOCALSTACK_BIN).
       LC_SRCDIR="$(dirname "$LC_SRC")"
       sudo -u "$REAL_USER" cp -a "$LC_SRCDIR/." "$LOCALSTACK_BIN/"
       sudo -u "$REAL_USER" chmod +x "$LOCALSTACK_BIN/llama-server"
       echo "$LC_VER" | sudo -u "$REAL_USER" tee "$LC_MARKER" >/dev/null
       echo "[install-localstack] Installed $LOCALSTACK_BIN/llama-server ($LC_VER, sha256 $LC_ACTUAL)"
       rm -rf "$TMP_LC"; trap - EXIT
   fi

   # ── 3. Speaches venv (own lean venv — the MCP lean-venv lesson) ────────────
   if [[ ! -x "$SPEACHES_VENV/bin/pip" ]]; then
       echo "[install-localstack] Creating Speaches venv at $SPEACHES_VENV..."
       if ! sudo -u "$REAL_USER" "$PYBIN" -m venv "$SPEACHES_VENV"; then
           echo "[install-localstack] ERROR: Speaches venv creation failed" >&2
           exit 4
       fi
   fi
   echo "[install-localstack] Installing/upgrading Speaches ($SPEACHES_PIN)..."
   if ! sudo -u "$REAL_USER" "$SPEACHES_VENV/bin/pip" install --upgrade "$SPEACHES_PIN"; then
       echo "[install-localstack] ERROR: pip install $SPEACHES_PIN failed (check the pinned version/network)" >&2
       exit 4
   fi

   # ── 4. qwen-tts venv (server code + deps come from the TTS milestone) ──────
   if [[ ! -x "$QWEN_TTS_VENV/bin/pip" ]]; then
       echo "[install-localstack] Creating qwen-tts venv at $QWEN_TTS_VENV..."
       if ! sudo -u "$REAL_USER" "$PYBIN" -m venv "$QWEN_TTS_VENV"; then
           echo "[install-localstack] ERROR: qwen-tts venv creation failed" >&2
           exit 4
       fi
   fi
   QWEN_REQ="$BLACKBOX_ROOT/LocalModels/qwen_tts_server/requirements.txt"
   if [[ -f "$QWEN_REQ" ]]; then
       echo "[install-localstack] Installing qwen-tts requirements..."
       if ! sudo -u "$REAL_USER" "$QWEN_TTS_VENV/bin/pip" install --upgrade -r "$QWEN_REQ"; then
           echo "[install-localstack] ERROR: qwen-tts requirements install failed" >&2
           exit 4
       fi
   else
       # Floor so the member's uvicorn entrypoint exists; the TTS milestone
       # lands qwen_tts_server + its full requirements later.
       echo "[install-localstack] (qwen-tts requirements.txt absent — installing fastapi/uvicorn floor; TTS milestone lands the server.)"
       sudo -u "$REAL_USER" "$QWEN_TTS_VENV/bin/pip" install --upgrade fastapi uvicorn || true
   fi

   # ── 5. Write llama-swap config.yaml from the template ──────────────────────
   # Substitute ONLY the four localstack path vars; ${PORT}/${llama-server}/
   # ${models-dir} stay literal for llama-swap. sed with | delimiter; $ before {
   # is literal in BRE, backslash-escaped so bash does not expand it.
   TMP_CFG="$(mktemp)"
   sed -e "s|\${LOCALSTACK_BIN}|$LOCALSTACK_BIN|g" \
       -e "s|\${LOCALSTACK_MODELS}|$LOCALSTACK_MODELS|g" \
       -e "s|\${SPEACHES_VENV}|$SPEACHES_VENV|g" \
       -e "s|\${QWEN_TTS_VENV}|$QWEN_TTS_VENV|g" \
       "$TEMPLATE_DIR/llama-swap-config.yaml.template" > "$TMP_CFG"
   sudo install -m 0644 -o "$REAL_USER" -g "$REAL_USER" "$TMP_CFG" "$CONFIG_DEST"
   rm -f "$TMP_CFG"
   echo "[install-localstack] Wrote $CONFIG_DEST"

   # ── 6. Install blackbox-models.service (sed-template flow like reranker) ────
   sed -e "s/REAL_USER_PLACEHOLDER/$REAL_USER/g" \
       -e "s|REAL_HOME_PLACEHOLDER|$REAL_HOME|g" \
       -e "s|LOCALSTACK_HOME_PLACEHOLDER|$LOCALSTACK_HOME|g" \
       -e "s|LOCALSTACK_BIN_PLACEHOLDER|$LOCALSTACK_BIN|g" \
       "$TEMPLATE_DIR/blackbox-models.service" | sudo tee /etc/systemd/system/blackbox-models.service >/dev/null
   sudo systemctl daemon-reload
   sudo systemctl enable blackbox-models.service >/dev/null 2>&1
   sudo systemctl restart blackbox-models.service
   echo "[install-localstack] blackbox-models.service written + enabled + (re)started"

   # ── 7. Verify: llama-swap front door answers /health (members lazy-load, so
   #      the front door is up with zero weights resident) ──────────────────────
   echo "[install-localstack] Waiting for http://127.0.0.1:$FRONT_PORT/health (up to ${VERIFY_TIMEOUT_S}s)..."
   ELAPSED=0
   while (( ELAPSED < VERIFY_TIMEOUT_S )); do
       if curl --silent --fail --max-time 5 "http://127.0.0.1:$FRONT_PORT/health" >/dev/null 2>&1; then
           echo "[install-localstack] llama-swap front door is up on :$FRONT_PORT."
           echo "[install-localstack] Download weights + activate per capability in the wizard's"
           echo "[install-localstack] 'Local models' step; nothing activates implicitly on install."
           exit 0
       fi
       if sudo systemctl is-failed --quiet blackbox-models.service; then
           echo "[install-localstack] ERROR: blackbox-models.service entered the failed state" >&2
           echo "[install-localstack] Check 'journalctl -u blackbox-models.service -n 100'." >&2
           exit 6
       fi
       sleep 5; ELAPSED=$(( ELAPSED + 5 ))
   done
   echo "[install-localstack] ERROR: llama-swap did not answer on :$FRONT_PORT within ${VERIFY_TIMEOUT_S}s" >&2
   echo "[install-localstack] The service stays enabled; check 'journalctl -u blackbox-models.service -f'." >&2
   exit 6
   ```

4. Verify bash syntax:
   - Run: `bash -n installer/templates/blackbox-install-localstack.sh && echo SYNTAX_OK`
   - Expected: `SYNTAX_OK`
   - Run: `grep -vE '^[[:space:]]*(#|$)' installer/templates/llama-swap-version | head -n1`
   - Expected: `240`
   - Run: `grep -c FILL_ installer/templates/llamacpp-version`
   - Expected: `3` (two placeholder shas on the pin lines + one in the header remediation) — a reminder these must be filled at execution time on MS02.

5. Manual verification (root/systemd — runs on MS02 during Phase 2, not in CI):
   - Fill the two llama.cpp shas in `installer/templates/llamacpp-version` per its header, confirm the two asset filenames on the pinned release page (adjust `ASSET_CPU`/`ASSET_CUDA` if naming drifted).
   - `sudo bash installer/templates/blackbox-install-localstack.sh bbx /home/bbx /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main`
   - Expected: exits 0; `systemctl is-active blackbox-models.service` → `active`; `curl -s http://127.0.0.1:9098/health` answers; `ls ~/.blackbox/localstack/bin` shows `llama-swap` + `llama-server`; re-running is a no-op on the binaries (skip messages).

6. Commit.
   - Run: `git add installer/templates/blackbox-install-localstack.sh installer/templates/llama-swap-version installer/templates/llamacpp-version && git commit -m "installer: blackbox-install-localstack.sh + pinned llama-swap/llama.cpp versions"`

---

### Task 2.6: Wire Step 2f into `install.sh`

Add the provisioner call after Step 2e (CPU reranker deps end at `Scripts/install.sh:513`), before Step 3 (`.env`, line 515). Non-fatal and re-run-safe, mirroring the Step 2d/2e call convention exactly.

**Files:**
- Modify: `Scripts/install.sh` (after the Step 2e block, lines 509-513)

1. Edit `Scripts/install.sh`. Anchor on the end of the Step 2e block and append Step 2f.

   Old:
   ```bash
   if ! bash "$BLACKBOX_ROOT/installer/templates/blackbox-install-reranker-cpu.sh" \
           "$REAL_USER" "$REAL_HOME" "$BLACKBOX_ROOT"; then
       echo "[install] WARN: CPU reranker deps did not install — search works un-reranked."
       echo "[install]       Re-run later: sudo bash installer/templates/blackbox-install-reranker-cpu.sh"
   fi
   ```
   New:
   ```bash
   if ! bash "$BLACKBOX_ROOT/installer/templates/blackbox-install-reranker-cpu.sh" \
           "$REAL_USER" "$REAL_HOME" "$BLACKBOX_ROOT"; then
       echo "[install] WARN: CPU reranker deps did not install — search works un-reranked."
       echo "[install]       Re-run later: sudo bash installer/templates/blackbox-install-reranker-cpu.sh"
   fi

   # ── Step 2f: on-box local model stack (llama-swap front door — GPU + CPU) ──
   # Provisions llama-swap (:9098), the llama.cpp llama-server (CUDA/CPU per the
   # nvidia-smi gate), the Speaches + qwen-tts venvs, the tier-adjusted
   # llama-swap config.yaml, and blackbox-models.service. Self-gating (build
   # selector, not a skip) + re-run-safe. NON-FATAL like the reranker step —
   # cloud STT/TTS/embeddings/rerank keep working and the wizard's local_models
   # step shows remediation. NO weights here (downloaded later in the wizard,
   # disk-gated). Nothing activates implicitly on install.
   if ! bash "$BLACKBOX_ROOT/installer/templates/blackbox-install-localstack.sh" \
           "$REAL_USER" "$REAL_HOME" "$BLACKBOX_ROOT"; then
       echo "[install] WARN: local model stack provisioning did not complete — on-box STT/TTS/embeddings/rerank unavailable; cloud fallbacks work."
       echo "[install]       Re-run later: sudo bash installer/templates/blackbox-install-localstack.sh"
   fi
   ```

2. Verify bash syntax and the wiring:
   - Run: `bash -n Scripts/install.sh && echo SYNTAX_OK`
   - Expected: `SYNTAX_OK`
   - Run: `grep -n 'blackbox-install-localstack.sh' Scripts/install.sh`
   - Expected: two hits (the invocation + the re-run hint) inside a `Step 2f` block located after Step 2e and before Step 3.

3. Commit.
   - Run: `git add Scripts/install.sh && git commit -m "installer: wire Step 2f local model stack provisioning into install.sh"`

---

### Task 2.7: `POST /local-models/download` — HF-CDN weight download (NDJSON, ≥40GB disk gate)

The wizard's one-click weight downloads (spec §8), cloned from the `/embeddings/ollama/pull` + `ollama_io.start_pull` singleton pattern: a process-wide single-flight guard (409 on concurrent), NDJSON progress streamed directly in the response, a fail-soft `_async_transport` test seam, atomic `.part`→final rename, re-run-safe skip when the file is already present. Gated on `hardware.disk_free_mb() >= 40 GB` — the ONE shared M1 probe (Task 1.2), same 40 GB threshold (`local_stack.DISK_GATE_MB` = `40 * 1024` MB) the status endpoint reports (the full GPU-tier weight set is ~27.5GB; ≥40GB is the sound floor — spec §7/§14).

**Files:**
- Create: `Orchestrator/localstack_downloads.py`
- Create: `Orchestrator/tests/test_localstack_downloads.py`
- Modify: `Orchestrator/routes/local_models_routes.py` (M1 created it with `GET /local-models/status` and its router registration in `Orchestrator/app.py`; append the download route). *Fallback if M1 has not landed:* create the file with the minimal header + `router` shown in step 4, and add the two `app.py` include lines shown there.

1. Write the failing tests. Create `Orchestrator/tests/test_localstack_downloads.py`:
   ```python
   """Tests for the localstack weight-download endpoint (local-model-stack M2).

   Mirrors the ollama_io test recipe: all HTTP mocked via httpx.MockTransport
   injected through localstack_downloads._async_transport; the download singleton
   is module state reset per test; MODELS_DIR + hardware.disk_free_mb monkeypatched
   so nothing touches the real filesystem or the real disk gate.
   """
   import json

   import httpx
   import pytest
   from fastapi import FastAPI
   from fastapi.testclient import TestClient

   from Orchestrator import hardware
   from Orchestrator import localstack_downloads as dl
   from Orchestrator.routes.local_models_routes import router

   ARTIFACT = "embed-qwen3-0.6b"
   FAKE = b"GGUF" + b"\x00" * (3 * 1024)  # a few KB of fake weights


   @pytest.fixture(autouse=True)
   def reset_state(tmp_path, monkeypatch):
       dl._DL = None
       monkeypatch.setattr(dl, "MODELS_DIR", tmp_path / "models")
       monkeypatch.setattr(dl, "_async_transport", None)
       # Default: plenty of disk so the gate passes unless a test overrides it.
       monkeypatch.setattr(hardware, "disk_free_mb", lambda *a, **k: 500 * 1024)
       yield
       dl._DL = None


   def _bytes_transport(payload: bytes):
       """Async MockTransport that streams `payload` with a content-length."""
       def handler(request):
           return httpx.Response(200, content=payload,
                                 headers={"content-length": str(len(payload))})
       return httpx.MockTransport(handler)


   def _client():
       app = FastAPI()
       app.include_router(router)
       return TestClient(app)


   def _lines(resp):
       return [json.loads(l) for l in resp.text.splitlines() if l.strip()]


   def test_download_streams_progress_and_writes_file(monkeypatch):
       monkeypatch.setattr(dl, "_async_transport", _bytes_transport(FAKE))
       resp = _client().post("/local-models/download", json={"artifact": ARTIFACT})
       assert resp.status_code == 200
       lines = _lines(resp)
       assert lines[-1]["state"] == "done"
       assert lines[-1]["completed"] == len(FAKE)
       # progress is monotonic non-decreasing
       comp = [l["completed"] for l in lines]
       assert comp == sorted(comp)
       dest = dl.MODELS_DIR / dl.DOWNLOAD_MANIFEST[ARTIFACT]["dest"]
       assert dest.read_bytes() == FAKE
       assert not (dl.MODELS_DIR / (dest.name + ".part")).exists()  # renamed away


   def test_download_disk_gate_507(monkeypatch):
       monkeypatch.setattr(hardware, "disk_free_mb", lambda *a, **k: 10 * 1024)
       resp = _client().post("/local-models/download", json={"artifact": ARTIFACT})
       assert resp.status_code == 507
       assert "40" in resp.json()["detail"]
       assert not (dl.MODELS_DIR / dl.DOWNLOAD_MANIFEST[ARTIFACT]["dest"]).exists()


   def test_download_disk_gate_failsoft_allows_when_unknown(monkeypatch):
       """disk_free_mb None (unreadable) → gate allows (fail-soft)."""
       monkeypatch.setattr(hardware, "disk_free_mb", lambda *a, **k: None)
       monkeypatch.setattr(dl, "_async_transport", _bytes_transport(FAKE))
       resp = _client().post("/local-models/download", json={"artifact": ARTIFACT})
       assert resp.status_code == 200
       assert _lines(resp)[-1]["state"] == "done"


   def test_download_unknown_artifact_404():
       resp = _client().post("/local-models/download", json={"artifact": "nope"})
       assert resp.status_code == 404


   def test_download_concurrent_409(monkeypatch):
       dl._DL = {"artifact": ARTIFACT, "status": "downloading", "completed": 1,
                 "total": 2, "state": "running", "error": None}
       resp = _client().post("/local-models/download", json={"artifact": ARTIFACT})
       assert resp.status_code == 409


   def test_download_already_present_is_done(monkeypatch):
       dl.MODELS_DIR.mkdir(parents=True, exist_ok=True)
       dest = dl.MODELS_DIR / dl.DOWNLOAD_MANIFEST[ARTIFACT]["dest"]
       dest.write_bytes(FAKE)
       # No transport set — if it tried to download, it would fail; it must not.
       resp = _client().post("/local-models/download", json={"artifact": ARTIFACT})
       assert resp.status_code == 200
       assert _lines(resp)[-1]["state"] == "done"
       assert dest.read_bytes() == FAKE
   ```
   - Run: `python -m pytest Orchestrator/tests/test_localstack_downloads.py -q`
   - Expected: FAIL — `ModuleNotFoundError: No module named 'Orchestrator.localstack_downloads'` (and, if M1 not landed, the `local_models_routes` import — implement below).

2. Create the download module `Orchestrator/localstack_downloads.py`:
   ```python
   """On-box local-model weight downloads from the Hugging Face CDN (M2).

   Cloned from the Ollama pull pattern (embeddings/ollama_io.py): a process-wide
   single-flight singleton (409 on a concurrent download), NDJSON progress
   streamed directly in the POST /local-models/download response, an
   httpx.MockTransport test seam, atomic .part -> final rename, re-run-safe skip
   when the destination already exists. The disk gate (>=40GB free) lives in the
   route (hardware.disk_free_mb, the ONE shared M1 probe) so the module stays
   HTTP-only and easy to test.

   Weights land under ~/.blackbox/localstack/models (LOCALSTACK_MODELS) — the
   same dir the generated llama-swap config's ${models-dir} macro points at.
   blackbox.service runs ProtectHome=no, so the Orchestrator can write there.
   """
   import json
   import os
   import threading
   from pathlib import Path

   import httpx

   # Monkeypatchable in tests (MEMINFO_PATH pattern). Resolved once at import;
   # blackbox.service runs as REAL_USER so ~ is $REAL_HOME.
   MODELS_DIR = Path(os.path.expanduser("~/.blackbox/localstack/models"))

   MIN_FREE_GB = 40.0                 # full GPU-tier weight set ~27.5GB (§7/§14)
   CHUNK = 1 << 20                    # 1 MiB progress granularity
   DOWNLOAD_TIMEOUT = httpx.Timeout(None, connect=15.0)  # long file, no read cap

   # Test seam (providers.py / ollama_io _transport pattern): httpx.MockTransport.
   _async_transport: "httpx.AsyncBaseTransport | None" = None


   def _qwen_tts_model_dir() -> Path:
       """Where the Qwen3-TTS variant checkpoints land — the SAME dir the qwen-tts
       member reads (LocalModels/qwen_tts_server/settings.model_dir): the
       QWEN_TTS_MODEL_DIR override, else <repo>/LocalModels/weights/qwen3-tts.
       blackbox.service sets BLACKBOX_ROOT; fall back to this module's repo root."""
       env = os.environ.get("QWEN_TTS_MODEL_DIR")
       if env:
           return Path(env)
       root = os.environ.get("BLACKBOX_ROOT")
       base = Path(root) if root else Path(__file__).resolve().parents[1]
       return base / "LocalModels" / "weights" / "qwen3-tts"


   # Two artifact kinds (correction — the GPU-tier weight set is not all single
   # GGUFs): "file" = a single HF-CDN GGUF into MODELS_DIR (embeddings); "hf_snapshot"
   # = a multi-file HF repo pulled via huggingface_hub.snapshot_download (the
   # Qwen3-TTS variant checkpoints — ~13.5GB, the bulk of the disk gate — go to
   # QWEN_TTS_MODEL_DIR). NOT downloaded through this endpoint (documented, not gaps):
   #   • whisper (Speaches) — auto-pulled by the Speaches member on first
   #     transcription (its own HF cache); nothing to fetch here.
   #   • rerank-qwen3-0.6b — SELF-CONVERTED from a pinned llama.cpp build (Task 4.4),
   #     not a direct download.
   #   • embed-qwen3-0.6b — CPU-tier fallback only (not fetched on a GPU box).
   DOWNLOAD_MANIFEST: dict[str, dict] = {
       "embed-qwen3-8b": {
           "kind": "file",
           "repo": "Qwen/Qwen3-Embedding-8B-GGUF",
           "filename": "Qwen3-Embedding-8B-Q8_0.gguf",
           "dest": "Qwen3-Embedding-8B-Q8_0.gguf",
           "approx_gb": 8.1,
       },
       "embed-qwen3-0.6b": {
           "kind": "file",
           "repo": "Qwen/Qwen3-Embedding-0.6B-GGUF",
           "filename": "Qwen3-Embedding-0.6B-Q8_0.gguf",
           "dest": "Qwen3-Embedding-0.6B-Q8_0.gguf",
           "approx_gb": 0.6,
       },
       # The three Qwen3-TTS 1.7B variant checkpoints (Base/CustomVoice/VoiceDesign,
       # ~4.5GB each ≈ 13.5GB, §14). Multi-file HF repos → snapshot into
       # QWEN_TTS_MODEL_DIR/<variant>, matching what the qwen-tts variant manager
       # loads (variant_manager.backend.load(variant, model_dir)). Exact repo ids are
       # confirmed at G3 (Task 6.9, the same seam that pins the streaming-fork
       # signatures); update here if the open-weights repo names differ.
       "qwen-tts": {
           "kind": "hf_snapshot",
           "repos": {
               "custom_voice": "Qwen/Qwen3-TTS-1.7B-CustomVoice",
               "base":         "Qwen/Qwen3-TTS-1.7B-Base",
               "voice_design": "Qwen/Qwen3-TTS-1.7B-VoiceDesign",
           },
           "approx_gb": 13.5,
       },
   }

   # ── download singleton ────────────────────────────────────────────────────
   _DL: dict | None = None            # None = idle / never downloaded this process
   _DL_LOCK = threading.Lock()


   def download_status() -> dict | None:
       """Copy of the live download state (consumed by GET /local-models/status);
       None when idle / never downloaded this process."""
       with _DL_LOCK:
           return dict(_DL) if _DL is not None else None


   def _set(**fields) -> None:
       with _DL_LOCK:
           if _DL is not None:
               _DL.update(fields)


   def _line() -> bytes:
       with _DL_LOCK:
           payload = dict(_DL) if _DL is not None else {}
       return (json.dumps(payload) + "\n").encode()


   def _finish() -> None:
       """Guard a generator that died before a terminal state (client disconnect
       / cancellation) — otherwise the singleton is stuck 'running' (permanent
       409 until restart), the same scar ollama_io._log_pull_task_outcome fixes."""
       with _DL_LOCK:
           if _DL is not None and _DL["state"] == "running":
               _DL["state"] = "error"
               _DL["status"] = "interrupted"
               _DL["error"] = "download interrupted"


   def start_download(artifact: str):
       """Claim the download singleton and RETURN the NDJSON async generator.

       The claim is synchronous (before the generator runs) so two racing POSTs
       can never double-start. RuntimeError when a download is already running
       (route -> 409); KeyError for an unknown artifact (route validates first).
       """
       global _DL
       if artifact not in DOWNLOAD_MANIFEST:
           raise KeyError(artifact)
       with _DL_LOCK:
           if _DL is not None and _DL["state"] == "running":
               raise RuntimeError(f"a download of {_DL['artifact']!r} is already running")
           _DL = {
               "artifact": artifact, "status": "starting", "completed": 0,
               "total": 0, "state": "running", "error": None,
           }
       return _stream(artifact)


   async def _stream(artifact: str):
       """Yield NDJSON progress for one artifact. "file" artifacts stream a single
       HF-CDN GGUF to <dest>.part then atomically rename; "hf_snapshot" artifacts
       pull a multi-file HF repo set via snapshot_download. Terminal line carries
       state 'done' (success or already-present) or 'error'."""
       entry = DOWNLOAD_MANIFEST[artifact]
       if entry.get("kind") == "hf_snapshot":
           async for _l in _stream_hf_snapshot(entry):
               yield _l
           return
       dest = MODELS_DIR / entry["dest"]
       part = dest.with_name(dest.name + ".part")
       url = (f"https://huggingface.co/{entry['repo']}"
              f"/resolve/main/{entry['filename']}?download=true")
       try:
           MODELS_DIR.mkdir(parents=True, exist_ok=True)
           if dest.exists() and dest.stat().st_size > 0:
               size = dest.stat().st_size
               _set(status="already present", completed=size, total=size, state="done")
               yield _line()
               return
           completed = 0
           total = 0
           async with httpx.AsyncClient(
               timeout=DOWNLOAD_TIMEOUT, transport=_async_transport, follow_redirects=True
           ) as client:
               async with client.stream("GET", url) as resp:
                   resp.raise_for_status()
                   total = int(resp.headers.get("content-length", 0) or 0)
                   _set(status="downloading", completed=0, total=total)
                   yield _line()
                   with open(part, "wb") as fh:
                       async for chunk in resp.aiter_bytes(CHUNK):
                           fh.write(chunk)
                           completed += len(chunk)
                           _set(status="downloading", completed=completed, total=total)
                           yield _line()
           os.replace(part, dest)
           _set(status="success", completed=completed, total=total or completed, state="done")
           yield _line()
       except Exception as e:  # network, HTTP, disk — all surface as one error line
           _set(status="error", state="error", error=f"{type(e).__name__}: {e}")
           try:
               part.unlink()
           except OSError:
               pass
           yield _line()
       finally:
           _finish()


   async def _stream_hf_snapshot(entry: dict):
       """Pull the Qwen3-TTS variant checkpoints (multi-file HF repos) via
       huggingface_hub.snapshot_download into QWEN_TTS_MODEL_DIR/<variant>. Coarse
       progress (completed/total count REPOS, not bytes — snapshot_download exposes
       no byte granularity); re-run-safe (snapshot_download skips already-present
       files). Terminal line state 'done' or 'error'."""
       import asyncio
       repos = entry["repos"]
       root = _qwen_tts_model_dir()
       total = len(repos)
       try:
           from huggingface_hub import snapshot_download
       except Exception as e:
           _set(status="error", state="error",
                error=f"huggingface_hub unavailable: {e}")
           yield _line()
           _finish()
           return
       try:
           root.mkdir(parents=True, exist_ok=True)
           _set(status="downloading", completed=0, total=total)
           yield _line()
           done_n = 0
           for variant, repo in repos.items():
               _set(status=f"downloading {variant} ({repo})", completed=done_n, total=total)
               yield _line()
               await asyncio.to_thread(
                   snapshot_download, repo_id=repo,
                   local_dir=str(root / variant), local_dir_use_symlinks=False,
               )
               done_n += 1
               _set(status=f"{variant} ready", completed=done_n, total=total)
               yield _line()
           _set(status="success", completed=total, total=total, state="done")
           yield _line()
       except Exception as e:
           _set(status="error", state="error", error=f"{type(e).__name__}: {e}")
           yield _line()
       finally:
           _finish()
   ```

3. Add the download route. Append to `Orchestrator/routes/local_models_routes.py` (the file M1 created). Add these imports near the top (with the existing imports) if absent:
   ```python
   from fastapi import HTTPException
   from fastapi.responses import StreamingResponse
   from pydantic import BaseModel

   from Orchestrator import hardware
   from Orchestrator import localstack_downloads as _dl
   ```
   Then append the route (uses the `router` M1 already defined with `prefix="/local-models"`):
   ```python
   class LocalModelDownloadRequest(BaseModel):
       artifact: str  # key into localstack_downloads.DOWNLOAD_MANIFEST


   @router.post("/download")
   async def local_models_download(req: LocalModelDownloadRequest):
       """Stream an on-box model weight download from the HF CDN as NDJSON
       progress lines. 404 unknown artifact, 507 when <40GB free, 409 when a
       download is already running, else a streaming NDJSON body (poll
       GET /local-models/status for the same state out-of-band). Cloned from
       POST /embeddings/ollama/pull's singleton pattern."""
       if req.artifact not in _dl.DOWNLOAD_MANIFEST:
           raise HTTPException(status_code=404, detail=f"Unknown artifact: {req.artifact!r}")
       # The ONE shared M1 probe (Task 1.2), in MB; gate against the same 40 GB
       # threshold the status endpoint reports (MIN_FREE_GB * 1024 MB).
       free_mb = hardware.disk_free_mb()
       if free_mb is not None and free_mb < _dl.MIN_FREE_GB * 1024:
           raise HTTPException(
               status_code=507,
               detail=(f"Need >= {_dl.MIN_FREE_GB:g} GB free to download model weights; "
                       f"only {free_mb / 1024:.0f} GB available. Free up disk and retry."),
           )
       try:
           stream = _dl.start_download(req.artifact)
       except RuntimeError as e:
           raise HTTPException(status_code=409, detail=str(e))
       return StreamingResponse(stream, media_type="application/x-ndjson")
   ```

4. *Fallback only if `Orchestrator/routes/local_models_routes.py` does not yet exist (M1 not landed).* Create it with:
   ```python
   """Local model stack routes (/local-models/*)."""
   from fastapi import APIRouter, HTTPException
   from fastapi.responses import StreamingResponse
   from pydantic import BaseModel

   from Orchestrator import hardware
   from Orchestrator import localstack_downloads as _dl

   router = APIRouter(prefix="/local-models", tags=["local-models"])
   ```
   (then the `LocalModelDownloadRequest` class + `@router.post("/download")` handler from step 3), and register it in `Orchestrator/app.py` after the `voice_agent_router` include (currently line 146):
   ```python
   from Orchestrator.routes.local_models_routes import router as local_models_router
   app.include_router(local_models_router)
   ```

5. Run the tests.
   - Run: `python -m pytest Orchestrator/tests/test_localstack_downloads.py -q`
   - Expected: PASS (6 passed).

6. Commit.
   - Run: `git add Orchestrator/localstack_downloads.py Orchestrator/tests/test_localstack_downloads.py Orchestrator/routes/local_models_routes.py && git commit -m "local-models: HF-CDN weight download endpoint (NDJSON progress, >=40GB disk gate)"`
   - (If the fallback in step 4 was used, also `git add Orchestrator/app.py` in this commit.)

---

**Milestone 2 done when:** `python -m pytest Orchestrator/tests/test_hardware.py Orchestrator/tests/test_localstack_downloads.py -q` is green; `bash -n` passes on the two edited/new shell scripts; the config template parses as YAML with speaches pinned to 9099; and (manual, MS02) a `install.sh` re-run provisions `blackbox-models.service` answering on `:9098/health` with zero weights resident, then the wizard can `POST /local-models/download` the embedding GGUF with NDJSON progress under the disk gate.


---

## Milestone 3: Embeddings on localstack

**Depends on:** Milestone 1 (the `Orchestrator/local_stack.py` resolver + the `[local_models]` config section). M3's code calls `local_stack.base_url()`, `local_stack.is_installed()`, `local_stack.is_healthy()`, `local_stack.model_downloaded(model_id)`, and `local_stack.config_path()`; every one is monkeypatched in M3's tests, so M3 is fully testable without M2 having physically installed anything. Runtime keep-warm and preflight additionally rely on M2's installed llama-swap `config.yaml` and its `blackbox-models.service`, but only at integration time.

Bring wholesale-local embeddings onto the box: a net-new `localstack` provider that speaks OpenAI-compatible `/embeddings` against the llama-swap front door (`:9098`), two registry entries (`qwen3-embedding-8b-local` 4096d GPU-tier and `qwen3-embedding-0.6b-local` 1024d CPU-tier), and the keep-warm/placement/preflight/watcher plumbing that treats an on-box slug as first-class without ever health-switching the active corpus off a dim-incompatible store. The wizard-time re-embed cutover stays the sole writer of `active.json` (correction [6]); the cutover additionally busts the ToolVault caches (correction [27]).

> **Cross-milestone contract surfaced by this milestone (for M2):** the llama-swap `config.yaml` member names MUST equal the registry `model_id`s — `embed-qwen3-8b` on GPU-tier boxes, `embed-qwen3-0.6b` on CPU-tier boxes. `store.set_keep_alive`/`get_keep_alive`, `_model_preflight`, and the watcher all route to a member by `EMBEDDING_MODELS[slug]["model_id"]`. Diverge and keep-warm/preflight silently no-op.
>
> **Dependency contract M3 relies on from `Orchestrator/local_stack.py` (M1):**
> - `base_url() -> str` — e.g. `"http://127.0.0.1:9098/v1"` (from `[local_models] base_url`).
> - `is_installed() -> bool` — install + config present (NOT live VRAM residency, §4).
> - `is_healthy() -> bool` — llama-swap process live + configured (NOT per-member residency, §4).
> - `model_downloaded(model_id: str) -> bool` — that member's weights present on disk.
> - `config_path() -> Path | None` — path to the live llama-swap `config.yaml`; None when not installed. **Task 3.3 adds this if M1 has not; reconcile at Read time.**
>
> M3 **adds** to `local_stack.py`: `TTL_WARM`, `TTL_COLD`, `get_member_ttl()`, `set_member_ttl()` (Task 3.3).

---

### Task 3.1: Registry entries for the two on-box embedding models

**Files:**
- Modify: `Orchestrator/embeddings/registry.py` (insert after the `qwen3-embedding-8b` entry, before the closing `}` at line 131)
- Modify: `Orchestrator/tests/test_embeddings_registry.py` (`VALID_PROVIDERS` line 14; `test_exact_slugs_present_with_dims` params line 52-57; `test_ollama_query_instruction_fits_well_inside_clamp_budget` line 132)
- Test: `Orchestrator/tests/test_embeddings_registry.py`

1. In `test_embeddings_registry.py`, widen the provider whitelist and add the two exact-slug rows. Change line 14 from `VALID_PROVIDERS = {"gemini", "openai", "ollama"}` to:
   ```python
   VALID_PROVIDERS = {"gemini", "openai", "ollama", "localstack"}
   ```
   Add to the `test_exact_slugs_present_with_dims` parametrize list (after line 56 `("qwen3-embedding-8b", 4096),`):
   ```python
       ("qwen3-embedding-8b-local", 4096),
       ("qwen3-embedding-0.6b-local", 1024),
   ```
   Extend the query-instruction budget guard to on-box models — change line 133 `if e["provider"] != "ollama" or not e.get("query_instruction"):` to:
   ```python
       if e["provider"] not in ("ollama", "localstack") or not e.get("query_instruction"):
   ```
   Append a focused mandatory-tokenizer guard at the end of the file:
   ```python
   @pytest.mark.parametrize(
       "slug", [s for s, e in EMBEDDING_MODELS.items() if e["provider"] == "localstack"]
   )
   def test_localstack_entries_declare_a_real_tokenizer(slug):
       """The on-box entries embed at 4096/1024 dims through llama.cpp — exact
       token clamping is mandatory (a floor tokenizer would over/under-truncate a
       whole-snapshot ordinal-0 vector). Every localstack slug MUST name a real
       (hf:/tiktoken:) tokenizer, never None."""
       tok = EMBEDDING_MODELS[slug]["tokenizer"]
       assert tok and not tok.startswith("remote:"), (
           f"{slug}: localstack entries need an exact local tokenizer, got {tok!r}"
       )
   ```

2. Run the tests, expect FAIL — the slugs don't exist yet.
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_registry.py -q`
   - Expected: failures in `test_exact_slugs_present_with_dims[qwen3-embedding-8b-local-4096]`, `[qwen3-embedding-0.6b-local-1024]`, and `test_localstack_entries_declare_a_real_tokenizer` collecting zero params → `KeyError`/assert on the missing slugs.

3. Add the two entries to `EMBEDDING_MODELS` in `registry.py`, immediately after the `qwen3-embedding-8b` entry's closing `},` (line 130) and before the dict's closing `}` (line 131):
   ```python
       "qwen3-embedding-8b-local": {
           "provider": "localstack", "model_id": "embed-qwen3-8b", "dims": 4096,
           "label": "Qwen3 8B (on-box, max quality)", "ram_gb": 8.1, "cost_per_1m_tokens": 0.0,
           "privacy": "local",
           "quality_note": "MTEB #1 open-source; GPU-served on-box via llama-swap (Q8_0)",
           "query_instruction": "Instruct: Given a search query, retrieve relevant conversation snapshots\nQuery: ",
           # On-box keep-warm is the llama-swap member ttl (0 = warm), read by
           # store.get_keep_alive — NOT keep_alive.json. Registry default = cold.
           "keep_alive": None,
           # Seeded from the Ollama qwen3-embedding-8b entry pending G1 recalibration
           # on the RTX 2000 Ada Q8_0 store (per-model thresholds are mandatory).
           "semantic_threshold": 0.50,
           "junk_floor": 0.35,
           # Same Qwen3 tokenizer as the Ollama qwen entries (vendored hf:qwen3);
           # mandatory — llama-server pooling needs exact-length inputs.
           "tokenizer": "hf:qwen3",
           # llama-server launches with -c/-b/-ub 8192 (non-causal last-token
           # pooling forces ub >= full input seq); covers p99 whole snapshots.
           "max_input_tokens": 8192,
       },
       "qwen3-embedding-0.6b-local": {
           "provider": "localstack", "model_id": "embed-qwen3-0.6b", "dims": 1024,
           "label": "Qwen3 0.6B (on-box, light / CPU tier)", "ram_gb": 1.0, "cost_per_1m_tokens": 0.0,
           "privacy": "local",
           "quality_note": "Fast on CPU; on-box CPU-tier default via llama-swap",
           "query_instruction": "Instruct: Given a search query, retrieve relevant conversation snapshots\nQuery: ",
           "keep_alive": None,
           "semantic_threshold": 0.54,
           "junk_floor": 0.35,
           "tokenizer": "hf:qwen3",
           "max_input_tokens": 8192,
       },
   ```

4. Run the full registry suite, expect PASS.
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_registry.py -q`
   - Expected: `... passed` — every parametrized guard (`test_entry_has_required_fields`, `test_cloud_zero_ram_local_zero_cost`, `test_every_model_declares_explicit_semantic_threshold`, `test_every_model_declares_nullable_junk_floor_below_semantic_threshold`, `test_every_model_declares_max_input_tokens`, `test_ollama_query_instruction_fits...`) green for the two new slugs.

5. Commit.
   - Run: `git add Orchestrator/embeddings/registry.py Orchestrator/tests/test_embeddings_registry.py && git commit -m "feat(embeddings): register on-box localstack embedding slugs (8B-Q8_0 4096d, 0.6B 1024d)"`

---

### Task 3.2: `LocalStackProvider` — OpenAI-compatible embeds against `:9098`

**Files:**
- Modify: `Orchestrator/embeddings/providers.py` (add the class after `OllamaProvider` ends at line 266; register in `_PROVIDER_CLASSES` line 269-273)
- Modify: `Orchestrator/tests/test_embeddings_providers.py` (imports line 20-26; append a LocalStack section; extend `test_get_provider_class_per_registry_provider` line 484)
- Test: `Orchestrator/tests/test_embeddings_providers.py`

1. Add the failing tests. In `test_embeddings_providers.py`, extend the provider imports (line 20-26) to include `LocalStackProvider`:
   ```python
   from Orchestrator.embeddings.providers import (
       EmbeddingProviderError,
       GeminiProvider,
       LocalStackProvider,
       OllamaProvider,
       OpenAIProvider,
       get_provider,
   )
   ```
   Add module-level constants beside the existing slug constants (after line 35):
   ```python
   LOCALSTACK_SLUG = "qwen3-embedding-8b-local"
   LOCALSTACK_DIMS = EMBEDDING_MODELS[LOCALSTACK_SLUG]["dims"]
   LOCALSTACK_BASE = "http://127.0.0.1:9098/v1"
   ```
   Append this whole section at the end of the file:
   ```python
   # ── LocalStack (on-box llama-swap :9098) ─────────────────────────────────────

   def _localstack_with_mock_transport(provider, requests_seen, dims, status=200):
       """Route the provider's httpx client through a MockTransport that records
       the request (payload + headers) and answers with the OpenAI /embeddings
       response shape ({data:[{index, embedding}]})."""

       def handler(request):
           body = json.loads(request.content.decode())
           requests_seen.append({
               "url": str(request.url),
               "json": body,
               "headers": {k.lower(): v for k, v in request.headers.items()},
           })
           if status != 200:
               return httpx.Response(status, json={"error": "boom"})
           return httpx.Response(200, json={"data": [
               {"index": i, "embedding": [0.0] * dims}
               for i, _ in enumerate(body["input"])
           ]})

       provider._transport = httpx.MockTransport(handler)
       return provider


   @pytest.fixture
   def _localstack_base(monkeypatch):
       from Orchestrator import local_stack
       monkeypatch.setattr(local_stack, "base_url", lambda: LOCALSTACK_BASE)
       return LOCALSTACK_BASE


   @pytest.mark.asyncio
   async def test_localstack_document_posts_openai_shape_to_front_door(_localstack_base):
       provider = get_provider(LOCALSTACK_SLUG)
       seen = []
       _localstack_with_mock_transport(provider, seen, LOCALSTACK_DIMS)
       result = await provider.embed(["first", "second"], purpose="document")
       assert len(result) == 2 and all(len(v) == LOCALSTACK_DIMS for v in result)
       assert seen[0]["url"] == f"{LOCALSTACK_BASE}/embeddings"
       assert seen[0]["json"]["model"] == EMBEDDING_MODELS[LOCALSTACK_SLUG]["model_id"]
       assert seen[0]["json"]["input"] == ["first", "second"]
       # loopback → NEVER an Authorization header (keyless front door)
       assert "authorization" not in seen[0]["headers"]


   @pytest.mark.asyncio
   async def test_localstack_query_prefixes_instruction(_localstack_base):
       provider = get_provider(LOCALSTACK_SLUG)
       seen = []
       _localstack_with_mock_transport(provider, seen, LOCALSTACK_DIMS)
       await provider.embed(["find the css fix"], purpose="query")
       instruction = EMBEDDING_MODELS[LOCALSTACK_SLUG]["query_instruction"]
       assert seen[0]["json"]["input"] == [instruction + "find the css fix"]


   @pytest.mark.asyncio
   async def test_localstack_output_follows_input_order(_localstack_base):
       # response indices deliberately scrambled — output must follow input order
       provider = get_provider(LOCALSTACK_SLUG)

       def handler(request):
           body = json.loads(request.content.decode())
           n = len(body["input"])
           data = [{"index": i, "embedding": [float(i)] + [0.0] * (LOCALSTACK_DIMS - 1)}
                   for i in range(n)]
           return httpx.Response(200, json={"data": list(reversed(data))})

       provider._transport = httpx.MockTransport(handler)
       result = await provider.embed(["a", "b", "c"], purpose="document")
       assert [v[0] for v in result] == [0.0, 1.0, 2.0]


   def test_localstack_read_timeout_is_generous_for_cold_group_swaps():
       # A cross-group swap + 8B GGUF cold-load holds the request open through
       # llama-swap's queue; the read cap must outlast it, connect stays short.
       assert LocalStackProvider.TIMEOUT.read >= 300.0
       assert LocalStackProvider.TIMEOUT.connect <= 10.0


   @pytest.mark.asyncio
   async def test_localstack_http_error_retries_then_raises(_localstack_base):
       provider = get_provider(LOCALSTACK_SLUG)
       sleeps = _record_sleeps(provider)
       seen = []
       _localstack_with_mock_transport(provider, seen, LOCALSTACK_DIMS, status=503)
       with pytest.raises(EmbeddingProviderError):
           await provider.embed(["text"], purpose="document")
       assert sleeps == [1.0, 2.0, 4.0]      # full retry envelope, no real sleeps
       assert len(seen) == 4                  # initial + 3 retries


   @pytest.mark.asyncio
   async def test_localstack_dims_mismatch_raises_without_retry(_localstack_base):
       provider = get_provider(LOCALSTACK_SLUG)
       sleeps = _record_sleeps(provider)

       def handler(request):
           body = json.loads(request.content.decode())
           return httpx.Response(200, json={"data": [
               {"index": i, "embedding": [0.0] * (LOCALSTACK_DIMS - 1)}
               for i, _ in enumerate(body["input"])
           ]})

       provider._transport = httpx.MockTransport(handler)
       with pytest.raises(EmbeddingProviderError):
           await provider.embed(["hello"], purpose="document")
       assert sleeps == []                    # guard fires after a "successful" call
   ```
   Extend `test_get_provider_class_per_registry_provider` (line 484) with:
   ```python
       assert isinstance(get_provider("qwen3-embedding-8b-local"), LocalStackProvider)
       assert isinstance(get_provider("qwen3-embedding-0.6b-local"), LocalStackProvider)
   ```

2. Run, expect FAIL — no `LocalStackProvider`.
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_providers.py -q`
   - Expected: `ImportError: cannot import name 'LocalStackProvider'` (collection error).

3. Implement the provider in `providers.py`. Add this module-level constant next to `OLLAMA_READ_TIMEOUT_S` (after line 63):
   ```python
   # llama-swap holds a request through a cross-group swap + cold 8B GGUF load
   # (healthCheckTimeout 300 on the embed member); a slow read here is the stack
   # working, exactly like OLLAMA_READ_TIMEOUT_S. Connect stays short so a dead
   # :9098 front door fails fast.
   LOCALSTACK_READ_TIMEOUT_S = 600.0
   ```
   Add the class immediately after `OllamaProvider` (after line 266, before the `_PROVIDER_CLASSES` dict):
   ```python
   class LocalStackProvider(_BaseProvider):
       """On-box embeddings via the llama-swap front door (blackbox-models.service,
       :9098). Net-new: OpenAIProvider hardcodes OPENAI_API_KEY and has no base_url,
       and OllamaProvider speaks the /api/embed (num_ctx/keep_alive) dialect — neither
       fits an OpenAI-compatible loopback llama-server. base_url is read FRESH per
       call from the resolver so re-pointing the stack needs no restart; no bearer on
       loopback. Qwen3-Embedding needs the instruct prefix on queries, so the
       query-instruction prefixing + budget accounting mirror OllamaProvider."""

       TIMEOUT = httpx.Timeout(LOCALSTACK_READ_TIMEOUT_S, connect=5.0)

       def __init__(self, slug, entry):
           super().__init__(slug, entry)
           self._transport = None  # tests inject httpx.MockTransport

       def _clamp_budget(self, purpose: str):
           # Mirror of OllamaProvider._clamp_budget: the registry query_instruction
           # is prefixed AFTER clamping (in _embed), so its tokens come out of the
           # text budget or prefix+text would overshoot what the budget promises.
           budget = super()._clamp_budget(purpose)
           if budget is None or purpose != "query":
               return budget
           instruction = self.entry.get("query_instruction")
           if not instruction:
               return budget
           return max(0, budget - tokenization.estimate_tokens(instruction, self.slug))

       async def _embed(self, texts, purpose):
           from Orchestrator import local_stack  # lazy: avoid import cycle
           instruction = self.entry.get("query_instruction")
           if purpose == "query" and instruction is not None:
               texts = [instruction + t for t in texts]
           base_url = local_stack.base_url().rstrip("/")  # fresh per call, no restart
           payload = {"model": self.model_id, "input": texts}
           async with httpx.AsyncClient(
               timeout=self.TIMEOUT, transport=self._transport
           ) as client:
               resp = await client.post(f"{base_url}/embeddings", json=payload)
               resp.raise_for_status()
               data = resp.json()["data"]
           items = sorted(data, key=lambda item: item["index"])
           return [list(item["embedding"]) for item in items]
   ```
   Register it in `_PROVIDER_CLASSES` (line 269-273):
   ```python
   _PROVIDER_CLASSES = {
       "gemini": GeminiProvider,
       "openai": OpenAIProvider,
       "ollama": OllamaProvider,
       "localstack": LocalStackProvider,
   }
   ```

4. Run, expect PASS.
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_providers.py -q`
   - Expected: `... passed`, including the eight new `test_localstack_*` cases and the extended class-dispatch test.

5. Guard the literal ratchet still passes (providers.py is scanned by `test_embeddings_guards.py`; the new class must carry no `qwen3-embedding`/`text-embedding`/`gemini-embedding` literal — `model_id` comes from the entry).
   - Run: `Orchestrator/venv/bin/python -m pytest "Orchestrator/tests/test_embeddings_guards.py::test_no_embedding_model_literals_outside_registry" -q`
   - Expected: `... passed`.

6. Commit.
   - Run: `git add Orchestrator/embeddings/providers.py Orchestrator/tests/test_embeddings_providers.py && git commit -m "feat(embeddings): LocalStackProvider — OpenAI-compatible embeds against llama-swap :9098"`

---

### Task 3.3: `local_stack` keep-warm ttl helpers (member ttl = warm/cold)

**Files:**
- Modify: `Orchestrator/local_stack.py` (add constants + `config_path`/`get_member_ttl`/`set_member_ttl`; **Read it first** — M1 may already define `config_path`)
- Create: `Orchestrator/tests/test_local_stack_keepwarm.py`
- Test: `Orchestrator/tests/test_local_stack_keepwarm.py`

1. Write the failing test file `test_local_stack_keepwarm.py`:
   ```python
   """M3: keep-warm maps to a llama-swap member ttl (0 = warm/immune to idle
   unload; >0 = cold, idle-unloads after ttl s). §6: --watch-config restarts the
   whole proxy on any config edit — these are surgical, atomic single writes."""
   import yaml
   import pytest

   from Orchestrator import local_stack

   CONFIG = {
       "healthCheckTimeout": 120,
       "models": {
           "embed-qwen3-8b": {"proxy": "http://127.0.0.1:${PORT}", "ttl": 600},
           "rerank-qwen3-0.6b": {"proxy": "http://127.0.0.1:${PORT}", "ttl": 600},
       },
       "groups": {"retrieval": {"members": ["embed-qwen3-8b", "rerank-qwen3-0.6b"]}},
   }


   @pytest.fixture
   def cfg(tmp_path, monkeypatch):
       path = tmp_path / "llama-swap-config.yaml"
       path.write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
       monkeypatch.setattr(local_stack, "config_path", lambda: path)
       return path


   def test_ttl_constants_warm_is_zero_cold_is_600():
       assert local_stack.TTL_WARM == 0
       assert local_stack.TTL_COLD == 600


   def test_get_member_ttl_reads_the_live_config(cfg):
       assert local_stack.get_member_ttl("embed-qwen3-8b") == 600


   def test_get_member_ttl_none_when_no_config(monkeypatch):
       monkeypatch.setattr(local_stack, "config_path", lambda: None)
       assert local_stack.get_member_ttl("embed-qwen3-8b") is None


   def test_get_member_ttl_none_for_absent_member(cfg):
       assert local_stack.get_member_ttl("not-a-member") is None


   def test_set_member_ttl_warm_then_cold_roundtrips(cfg):
       local_stack.set_member_ttl("embed-qwen3-8b", local_stack.TTL_WARM)
       assert local_stack.get_member_ttl("embed-qwen3-8b") == 0
       # sibling member untouched — surgical single-key edit
       on_disk = yaml.safe_load(cfg.read_text(encoding="utf-8"))
       assert on_disk["models"]["rerank-qwen3-0.6b"]["ttl"] == 600
       # ${PORT} literal preserved for llama-swap to fill
       assert on_disk["models"]["embed-qwen3-8b"]["proxy"] == "http://127.0.0.1:${PORT}"
       local_stack.set_member_ttl("embed-qwen3-8b", local_stack.TTL_COLD)
       assert local_stack.get_member_ttl("embed-qwen3-8b") == 600


   def test_set_member_ttl_absent_member_raises(cfg):
       with pytest.raises(ValueError):
           local_stack.set_member_ttl("not-a-member", 0)


   def test_set_member_ttl_no_config_raises(monkeypatch):
       monkeypatch.setattr(local_stack, "config_path", lambda: None)
       with pytest.raises(RuntimeError):
           local_stack.set_member_ttl("embed-qwen3-8b", 0)
   ```

2. Run, expect FAIL — the helpers don't exist.
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_local_stack_keepwarm.py -q`
   - Expected: `AttributeError: module 'Orchestrator.local_stack' has no attribute 'TTL_WARM'` (or `config_path`/`get_member_ttl`).

3. Read `Orchestrator/local_stack.py`. If `config_path()` is absent, add it (sourced from the `[local_models]` config; tolerant of M1's attr name via `getattr`). Then add the ttl constants + helpers. Append to `local_stack.py`:
   ```python
   import os
   from pathlib import Path

   import yaml

   # Keep-warm maps to a llama-swap member ttl (§6): 0 = immune to the 10-min idle
   # TTL (still yields to a cross-group swap); 600 = the template default (cold).
   TTL_WARM = 0
   TTL_COLD = 600


   def config_path() -> "Path | None":
       """Path to the live llama-swap config.yaml the installer (M2, Step 2f) wrote
       to ~/.blackbox/localstack/llama-swap-config.yaml (the installer's CONFIG_DEST,
       Task 2.5) — or None when that file is absent (stack not installed). Derived
       from the fixed install path rather than a config.ini key: the installer
       writes the config there unconditionally, so nothing needs to declare/write a
       [local_models] config_path key, and keep-warm resolves the REAL generated
       config in production (a getattr(config, "LOCAL_MODELS_CONFIG_PATH", ...) would
       always be None → get/set_member_ttl dead on-box). blackbox.service runs
       ProtectHome=no, so ~ is the real user's home. (If M1 already defines this,
       keep M1's and delete this copy.)"""
       p = Path(os.path.expanduser("~/.blackbox/localstack/llama-swap-config.yaml"))
       return p if p.exists() else None


   def get_member_ttl(member: str) -> "int | None":
       """The ttl (seconds) configured for a llama-swap member, or None when the
       config is absent/unreadable or the member is missing. 0 == kept warm."""
       path = config_path()
       if path is None:
           return None
       try:
           cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
       except (OSError, yaml.YAMLError):
           return None
       if not isinstance(cfg, dict):
           return None
       model = (cfg.get("models") or {}).get(member)
       if not isinstance(model, dict) or "ttl" not in model:
           return None
       try:
           return int(model["ttl"])
       except (TypeError, ValueError):
           return None


   def set_member_ttl(member: str, ttl: int) -> None:
       """Surgically set one member's ttl and atomically rewrite the config.

       WARNING (§6): the service runs with --watch-config, which auto-restarts the
       WHOLE proxy on any edit (unloads every member — there is no in-place reload,
       llama-swap #160/#547). Batch config writes; one keep-warm toggle is one
       write and one brief full-stack reload. Raises if the stack isn't installed
       (RuntimeError) or the member isn't in the config (ValueError)."""
       path = config_path()
       if path is None:
           raise RuntimeError("local stack not installed — no llama-swap config to edit")
       cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
       models = (cfg or {}).get("models") or {}
       if member not in models or not isinstance(models[member], dict):
           raise ValueError(f"llama-swap config has no member {member!r}")
       models[member]["ttl"] = int(ttl)
       tmp = path.with_name(path.name + ".tmp")
       with open(tmp, "w", encoding="utf-8") as f:
           yaml.safe_dump(cfg, f, sort_keys=False)
           f.flush()
           os.fsync(f.fileno())
       os.replace(tmp, path)
   ```
   > Note: `yaml.safe_dump` drops the template's comments — acceptable because the live config is machine-generated from `installer/templates/llama-swap-config.yaml.template` (which keeps the comments) and regenerated on reinstall. The `${PORT}` literal is preserved as a plain string.

4. Run, expect PASS.
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_local_stack_keepwarm.py -q`
   - Expected: `7 passed`.

5. Commit.
   - Run: `git add Orchestrator/local_stack.py Orchestrator/tests/test_local_stack_keepwarm.py && git commit -m "feat(local-stack): keep-warm helpers map member ttl (0=warm/600=cold) in the llama-swap config"`

---

### Task 3.4: `store` + routes — keep-warm/placement accept `localstack`

**Files:**
- Modify: `Orchestrator/embeddings/store.py` (`get_keep_alive` line 917; `set_keep_alive` line 933, guard 939; `set_placement` line 989, guard 995)
- Modify: `Orchestrator/routes/embeddings_routes.py` (`embeddings_keep_alive` guard line 512; `embeddings_placement` guard line 546)
- Modify: `Orchestrator/tests/test_local_stack_keepwarm.py` (append a store + routes section)
- Test: `Orchestrator/tests/test_local_stack_keepwarm.py`

1. Append the failing store+routes tests to `test_local_stack_keepwarm.py`:
   ```python
   # ── store keep_alive / placement localstack path ─────────────────────────────
   from fastapi import FastAPI
   from fastapi.testclient import TestClient

   from Orchestrator.embeddings import store as emb_store
   from Orchestrator.embeddings.store import (
       KEEP_ALIVE_COLD, KEEP_ALIVE_WARM, get_keep_alive, set_keep_alive, set_placement,
   )
   from Orchestrator.routes.embeddings_routes import router

   LOCALSTACK_SLUG = "qwen3-embedding-8b-local"
   LOCALSTACK_MEMBER = "embed-qwen3-8b"


   def test_set_keep_alive_warm_sets_member_ttl_zero(cfg):
       value = set_keep_alive(LOCALSTACK_SLUG, warm=True)
       assert value == KEEP_ALIVE_WARM
       assert local_stack.get_member_ttl(LOCALSTACK_MEMBER) == 0


   def test_set_keep_alive_cold_sets_member_ttl_600(cfg):
       assert set_keep_alive(LOCALSTACK_SLUG, warm=False) == KEEP_ALIVE_COLD
       assert local_stack.get_member_ttl(LOCALSTACK_MEMBER) == 600


   def test_get_keep_alive_reflects_member_ttl(cfg):
       set_keep_alive(LOCALSTACK_SLUG, warm=True)
       assert emb_store.is_warm(get_keep_alive(LOCALSTACK_SLUG)) is True
       set_keep_alive(LOCALSTACK_SLUG, warm=False)
       assert emb_store.is_warm(get_keep_alive(LOCALSTACK_SLUG)) is False


   def test_get_keep_alive_falls_back_to_registry_when_no_config(monkeypatch):
       monkeypatch.setattr(local_stack, "config_path", lambda: None)
       assert get_keep_alive(LOCALSTACK_SLUG) is None  # registry keep_alive default


   def test_set_placement_localstack_raises_install_fixed(cfg):
       with pytest.raises(ValueError, match="install"):
           set_placement(LOCALSTACK_SLUG, "cpu")


   @pytest.fixture
   def client():
       app = FastAPI()
       app.include_router(router)
       return TestClient(app)


   def test_keep_alive_route_accepts_localstack(cfg, client):
       r = client.post("/embeddings/keep_alive", json={"slug": LOCALSTACK_SLUG, "warm": True})
       assert r.status_code == 200
       assert local_stack.get_member_ttl(LOCALSTACK_MEMBER) == 0


   def test_placement_route_rejects_localstack_with_install_fixed_message(client):
       r = client.post("/embeddings/placement", json={"slug": LOCALSTACK_SLUG, "placement": "cpu"})
       assert r.status_code == 400
       assert "install" in r.json()["detail"]
   ```

2. Run, expect FAIL — store/routes still reject non-ollama.
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_local_stack_keepwarm.py -q`
   - Expected: `test_set_keep_alive_warm_sets_member_ttl_zero` → `ValueError: 'qwen3-embedding-8b-local' is not a local model; keep_alive is Ollama-only`; route test → 400.

3. Patch `store.py`. In `get_keep_alive` (line 917-930) insert a localstack branch at the very top of the body, before the override read:
   ```python
   def get_keep_alive(slug: str, base_dir=None, fallback=None) -> "str | None":
       """Effective keep_alive for slug: per-box override, else the registry
       default, else `fallback` (for synthetic entries not in the registry)."""
       entry = EMBEDDING_MODELS.get(slug)
       if entry is not None and entry.get("provider") == "localstack":
           # On-box keep-warm lives in the llama-swap member ttl (0 = warm),
           # NOT keep_alive.json. Absent/unreadable config → registry default.
           from Orchestrator import local_stack  # lazy: avoid import cycle
           ttl = local_stack.get_member_ttl(entry["model_id"])
           if ttl is None:
               return entry.get("keep_alive")
           return KEEP_ALIVE_WARM if ttl == 0 else KEEP_ALIVE_COLD
       base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
       ...  # unchanged remainder
   ```
   In `set_keep_alive` (line 933-953) replace the provider guard (lines 937-940) so localstack routes to the member ttl:
   ```python
       entry = EMBEDDING_MODELS.get(slug)
       if entry is None:
           raise ValueError(f"unknown embedding model slug {slug!r}")
       if entry["provider"] == "localstack":
           # keep-warm ⇒ member ttl 0 (immune to idle unload); cold ⇒ 600 (§6).
           from Orchestrator import local_stack  # lazy: avoid import cycle
           local_stack.set_member_ttl(
               entry["model_id"], local_stack.TTL_WARM if warm else local_stack.TTL_COLD
           )
           return KEEP_ALIVE_WARM if warm else KEEP_ALIVE_COLD
       if entry["provider"] != "ollama":
           raise ValueError(f"{slug!r} is not a local model; keep_alive is on-box/Ollama-only")
   ```
   In `set_placement` (line 989-1015) replace the provider guard (lines 992-996) so localstack gets an accurate, deliberate refusal (device is install-fixed by tier, §7 — not runtime-toggleable):
   ```python
       entry = EMBEDDING_MODELS.get(slug)
       if entry is None:
           raise ValueError(f"unknown embedding model slug {slug!r}")
       if entry["provider"] == "localstack":
           raise ValueError(
               f"{slug!r} runs on the on-box stack; its device is fixed at install "
               f"by hardware tier — no runtime placement toggle"
           )
       if entry["provider"] != "ollama":
           raise ValueError(f"{slug!r} is not a local model; placement is Ollama-only")
   ```

4. Patch `embeddings_routes.py`. In `embeddings_keep_alive` widen the guard (line 512):
   ```python
       if entry["provider"] not in ("ollama", "localstack"):
           raise HTTPException(
               status_code=400,
               detail=f"{req.slug!r} is a cloud model; keep_alive is on-box only",
           )
   ```
   In `embeddings_placement` give localstack an accurate 400 (line 546):
   ```python
       if entry["provider"] != "ollama":
           detail = (
               f"{req.slug!r} runs on the on-box stack; device is fixed at install "
               f"by hardware tier — no runtime placement toggle"
               if entry["provider"] == "localstack"
               else f"{req.slug!r} is a cloud model; placement is Ollama-only"
           )
           raise HTTPException(status_code=400, detail=detail)
   ```

5. Run, expect PASS.
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_local_stack_keepwarm.py Orchestrator/tests/test_embeddings_keep_alive.py Orchestrator/tests/test_embeddings_placement.py -q`
   - Expected: `... passed` — the new localstack cases plus the existing ollama keep_alive/placement suites (unchanged for ollama).

6. Commit.
   - Run: `git add Orchestrator/embeddings/store.py Orchestrator/routes/embeddings_routes.py Orchestrator/tests/test_local_stack_keepwarm.py && git commit -m "feat(embeddings): keep_alive toggle drives llama-swap member ttl for localstack; placement stays install-fixed"`

---

### Task 3.5: `_model_preflight` localstack blockers + status wiring

**Files:**
- Modify: `Orchestrator/routes/embeddings_routes.py` (`_model_preflight` cloud branch line 218; `embeddings_status` `is_local` line 281)
- Modify: `Orchestrator/tests/test_embeddings_routes.py` (its `client` fixture — add local_stack mocks), `Orchestrator/tests/test_embeddings_guards.py` (`client` fixture, after line 139)
- Create: `Orchestrator/tests/test_localstack_preflight.py`
- Test: `Orchestrator/tests/test_localstack_preflight.py`

1. Write the failing preflight test file `test_localstack_preflight.py`:
   ```python
   """M3 (correction [21]/[26]): localstack preflight + status wiring."""
   import pytest
   from fastapi import FastAPI
   from fastapi.testclient import TestClient

   from Orchestrator import config, fossils, local_stack
   from Orchestrator.embeddings import ollama_io
   from Orchestrator.embeddings.store import set_active_slug
   from Orchestrator.routes.embeddings_routes import router

   LOCALSTACK_SLUG = "qwen3-embedding-8b-local"


   @pytest.fixture
   def client(tmp_path, monkeypatch):
       index_path = tmp_path / "snapshot_index.json"
       index_path.write_text("{}", encoding="utf-8")
       stores_dir = tmp_path / "embeddings"
       monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", index_path)
       monkeypatch.setattr(fossils, "_index_cache", None)
       monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)
       monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(stores_dir))
       set_active_slug("gemini-embedding-001", base_dir=stores_dir)
       monkeypatch.setattr(ollama_io, "binary_installed", lambda: False)
       monkeypatch.setattr(ollama_io, "daemon_version", lambda: None)
       monkeypatch.setattr(ollama_io, "local_models", lambda: [])
       monkeypatch.setattr(ollama_io, "ram_preflight", lambda ram_gb: None)
       # localstack seams default to installed+healthy+downloaded; tests override
       monkeypatch.setattr(local_stack, "is_installed", lambda: True)
       monkeypatch.setattr(local_stack, "is_healthy", lambda: True)
       monkeypatch.setattr(local_stack, "model_downloaded", lambda mid: True)
       monkeypatch.setattr(local_stack, "get_member_ttl", lambda mid: 600)
       app = FastAPI()
       app.include_router(router)
       return TestClient(app), monkeypatch


   def _model(body, slug):
       return next(m for m in body["models"] if m["slug"] == slug)


   def test_localstack_ready_when_installed_healthy_downloaded(client):
       tc, _ = client
       m = _model(tc.get("/embeddings/status").json(), LOCALSTACK_SLUG)
       assert m["ready"] is True and m["blockers"] == []


   def test_localstack_blocker_not_installed(client):
       tc, mp = client
       mp.setattr(local_stack, "is_installed", lambda: False)
       m = _model(tc.get("/embeddings/status").json(), LOCALSTACK_SLUG)
       assert m["ready"] is False
       assert any("local stack not installed" in b for b in m["blockers"])


   def test_localstack_blocker_service_down(client):
       tc, mp = client
       mp.setattr(local_stack, "is_healthy", lambda: False)
       m = _model(tc.get("/embeddings/status").json(), LOCALSTACK_SLUG)
       assert any("blackbox-models.service" in b for b in m["blockers"])


   def test_localstack_blocker_model_not_downloaded(client):
       tc, mp = client
       mp.setattr(local_stack, "model_downloaded", lambda mid: False)
       m = _model(tc.get("/embeddings/status").json(), LOCALSTACK_SLUG)
       assert any("model not downloaded" in b for b in m["blockers"])


   def test_localstack_status_shows_keep_alive_and_no_placement(client):
       tc, mp = client
       mp.setattr(local_stack, "get_member_ttl", lambda mid: 0)  # warm
       m = _model(tc.get("/embeddings/status").json(), LOCALSTACK_SLUG)
       assert m["warm"] is True                 # is_local now privacy-based
       assert m["placement"] is None            # no runtime placement for on-box
   ```

2. Run, expect FAIL.
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_localstack_preflight.py -q`
   - Expected: `test_localstack_blocker_not_installed`/`_service_down`/`_model_not_downloaded` fail (preflight has no localstack branch → falls into the ollama path, wrong blockers); `test_localstack_status_shows_keep_alive_and_no_placement` fails (`warm` is None because `is_local` keys on provider `ollama`).

3. Patch `_model_preflight` in `embeddings_routes.py`. Immediately after the cloud branch (after line 222, before `blockers: list[str] = []` at line 224) add:
   ```python
       if provider == "localstack":
           from Orchestrator import local_stack  # lazy: avoid import cycle
           if not local_stack.is_installed():
               return False, [
                   "local stack not installed — install it from the setup wizard"
               ], None
           blockers = []
           if not local_stack.is_healthy():
               blockers.append(
                   "blackbox-models.service down — "
                   "sudo systemctl start blackbox-models.service"
               )
           elif not local_stack.model_downloaded(entry["model_id"]):
               blockers.append(
                   f"model not downloaded — download it from the setup wizard "
                   f"(≈{entry['ram_gb']:g} GB)"
               )
           return (not blockers), blockers, _recommended_placement(entry, hw)
   ```
   In `embeddings_status` widen `is_local` (line 281) so on-box models expose their keep_alive/warm state (behavior-preserving for the existing four: ollama models are exactly the local ones today):
   ```python
           # keep_alive + placement toggles are local-only; null for cloud. Keyed
           # on privacy so on-box (localstack) models expose keep_alive/warm too;
           # placement is null for localstack (device is install-fixed by tier).
           is_local = entry["privacy"] == "local"
   ```

4. Make the two existing hermetic fixtures independent of box state. In `test_embeddings_guards.py`, after line 139 (`monkeypatch.setattr(ollama_io, "ram_preflight", lambda ram_gb: None)`) add:
   ```python
       from Orchestrator import local_stack
       monkeypatch.setattr(local_stack, "is_installed", lambda: False)
       monkeypatch.setattr(local_stack, "is_healthy", lambda: False)
       monkeypatch.setattr(local_stack, "model_downloaded", lambda mid: False)
       monkeypatch.setattr(local_stack, "get_member_ttl", lambda mid: None)
   ```
   Apply the identical four `monkeypatch.setattr(local_stack, ...)` lines to the `client`/status fixture in `test_embeddings_routes.py` (locate its ollama-seam mocks and add these beside them; add `from Orchestrator import local_stack` if not imported).

5. Run, expect PASS (new file + the two touched suites).
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_localstack_preflight.py Orchestrator/tests/test_embeddings_routes.py Orchestrator/tests/test_embeddings_guards.py -q`
   - Expected: `... passed` — including `test_status_models_are_exactly_the_registry` (the two localstack slugs now render with clean blockers, not crashes).

6. Commit.
   - Run: `git add Orchestrator/routes/embeddings_routes.py Orchestrator/tests/test_localstack_preflight.py Orchestrator/tests/test_embeddings_guards.py Orchestrator/tests/test_embeddings_routes.py && git commit -m "feat(embeddings): localstack preflight blockers + on-box keep_alive/warm in /embeddings/status"`

---

### Task 3.6: Watcher — localstack catalog branch + the never-health-switch invariant

**Files:**
- Modify: `Orchestrator/embeddings/watcher.py` (`_catalog_check` else branch line 210; `_pick_migration_target` `ready()` line 271, and the function head line 264)
- Modify: `Orchestrator/tests/test_embeddings_watcher.py` (append tests + reuse existing fixtures)
- Test: `Orchestrator/tests/test_embeddings_watcher.py`

1. Append the failing tests to `test_embeddings_watcher.py` (reuse the module's existing hermetic env conventions — an isolated `EMBEDDINGS_STORES_DIR`, mocked snapshot index). Add a self-contained block:
   ```python
   # ── M3 correction [6]: on-box active model is NEVER health-switched ──────────
   import Orchestrator.embeddings.watcher as _watcher
   from Orchestrator.embeddings.store import get_store, set_active_slug


   @pytest.mark.asyncio
   async def test_localstack_active_never_auto_migrates(tmp_path, monkeypatch):
       """A broken on-box active model must NOT pick any migration target — the
       wizard re-embed cutover is the sole writer of active.json (§6). Even with a
       complete, ready cloud store on disk, target is None (stay broken →
       vector-less mints + gap-heal on recovery)."""
       stores = tmp_path / "embeddings"
       monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(stores))
       # a complete, ready gemini (3072-dim) store — the tempting cross-dim target
       monkeypatch.setattr(_watcher.config, "GOOGLE_API_KEY", "present", raising=False)
       gem = get_store("gemini-embedding-001", base_dir=stores)
       gem.append("SNAP-20260101-1", [0.1] * 3072)
       target, why = await _watcher._pick_migration_target(
           "qwen3-embedding-8b-local", successor_slug=None
       )
       assert target is None
       assert "on-box" in why or "§6" in why


   @pytest.mark.asyncio
   async def test_localstack_is_never_a_migration_target(tmp_path, monkeypatch):
       """A broken CLOUD active model must never auto-activate the on-box stack
       (opting the operator into an unchosen, possibly mid-swap on-box store)."""
       stores = tmp_path / "embeddings"
       monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(stores))
       loc = get_store("qwen3-embedding-8b-local", base_dir=stores)
       loc.append("SNAP-20260101-1", [0.1] * 4096)
       async def _no_tags():
           return []
       monkeypatch.setattr(_watcher, "_ollama_tags", _no_tags)
       target, _ = await _watcher._pick_migration_target(
           "gemini-embedding-001", successor_slug=None
       )
       assert target != "qwen3-embedding-8b-local"


   @pytest.mark.asyncio
   async def test_localstack_catalog_check_never_superseded(monkeypatch):
       """Local members have no vendor catalog and never deprecate — listed=True,
       no successor, so a healthy on-box active model can't be flagged superseded
       by an ollama-tags mismatch."""
       entry = EMBEDDING_MODELS["qwen3-embedding-8b-local"]
       listed, successor, note = await _watcher._catalog_check(entry)
       assert listed is True and successor is None
   ```
   (Add any missing imports the file doesn't already have: `from Orchestrator import config`, `from Orchestrator.embeddings.registry import EMBEDDING_MODELS`.)

2. Run, expect FAIL.
   - Run: `Orchestrator/venv/bin/python -m pytest "Orchestrator/tests/test_embeddings_watcher.py -k localstack" -q`
   - Expected: `test_localstack_active_never_auto_migrates` fails (no guard → falls through to the gemini store), `test_localstack_catalog_check_never_superseded` fails (the else-branch runs `_ollama_tags()` and reports not-listed).

3. Patch `_catalog_check` in `watcher.py`. Add an explicit localstack branch before the ollama `else` (at line 210, inside the `try`):
   ```python
           elif provider_name == "localstack":
               # On-box members have no vendor catalog and never deprecate; the
               # probe (run_health_check step 1) governs broken-ness. Present =
               # "listed", never a successor — so an ollama-tags mismatch can't
               # flag a healthy on-box model as superseded.
               return True, None, None
           else:  # ollama — local models don't deprecate; just confirm presence
               return (model_id in await _ollama_tags()), None, None
   ```
   Patch `_pick_migration_target` (line 264). Add the invariant guard as the very first statement of the body (before `try: tags = await _ollama_tags()`):
   ```python
   async def _pick_migration_target(active: str, successor_slug: "str | None") -> tuple:
       """(target_slug, why) per the broken-path precedence; (None, reasons)."""
       # §6 invariant (correction [6]): a broken ON-BOX active model is NEVER
       # health-switched. active.json is written ONLY by the wizard re-embed
       # cutover; a crash/health switch off a 4096-dim on-box store to a
       # dim-incompatible cloud/local store would fragment the corpus. Stay broken
       # (loud banner) → vector-less mints + gap-heal on recovery.
       if EMBEDDING_MODELS.get(active, {}).get("provider") == "localstack":
           return None, (
               "on-box embedding model is never auto-migrated — the wizard re-embed "
               "cutover is the sole writer of active.json (§6 invariant)"
           )
   ```
   Harden the `ready()` closure (line 271-276) so the on-box stack can never be an auto-migrate TARGET either:
   ```python
       def ready(slug: str) -> bool:
           entry = EMBEDDING_MODELS[slug]
           if entry["provider"] == "localstack":
               return False  # §6: on-box is activated ONLY by the wizard cutover
           attr = _CLOUD_KEY_ATTRS.get(entry["provider"])
           if attr is not None:
               return bool(getattr(config, attr, ""))
           return tags is not None and entry["model_id"] in tags
   ```

4. Run, expect PASS (and the full watcher suite, to prove cloud/ollama auto-migrate is unchanged).
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_watcher.py -q`
   - Expected: `... passed` — the three new localstack cases plus every pre-existing broken-path/gap-heal test.

5. Commit.
   - Run: `git add Orchestrator/embeddings/watcher.py Orchestrator/tests/test_embeddings_watcher.py && git commit -m "feat(embeddings): watcher never health-switches the on-box active model (active.json is wizard-cutover-only)"`

---

### Task 3.7: Migration sequencing — bust ToolVault caches after the re-embed cutover

**Files:**
- Modify: `Orchestrator/embeddings/migrate.py` (add `_toolvault_reload_after_cutover` near the toolvault helpers ~line 1156; call it in the cutover after line 755)
- Modify: `Orchestrator/tests/test_embeddings_reembed.py` (append a test using the reused `env`/`fake_provider` fixtures)
- Test: `Orchestrator/tests/test_embeddings_reembed.py`

> **Reconciliation of correction [27] with the real cutover (verified in `migrate.py:722-755`):** the existing cutover ALREADY re-embeds `ToolVault/embeddings.json` into the target model's dim space — it precomputes the tool vectors under the OLD active pointer, flips `active.json`, then promotes them back-to-back with no `await` gap — so there is no 4096-vs-3072 dim-mismatch window for the tool-selection cache. This repo has **no `code_embeddings.json`** (grep-verified), so that half of correction [27] is N/A here. The genuine remaining gap is the registry-derived tool-list snapshot caches (`registry.invalidate_cache` / `tool_registry.reset_cache`), which the promote path does not touch. The added hook busts exactly those. It deliberately omits `sync_embeddings` (which `/toolvault/reload` also runs): after promote it is a hash-keyed no-op, and running it could race the fire-and-forget fallback re-embed hook — so a pure cache-invalidation is the safe, correct subset.

1. Append the failing test to `test_embeddings_reembed.py`:
   ```python
   @pytest.mark.asyncio
   async def test_reembed_cutover_busts_toolvault_caches(env, fake_provider, monkeypatch):
       """Correction [27]: after the re-embed cutover flips active.json, the
       registry-derived ToolVault tool-list caches are invalidated so tool
       selection reflects the just-activated model."""
       index_path, stores_dir, volume_path = env
       _build_volume(index_path, volume_path, n=2)
       calls = {"invalidate": 0, "reset": 0}
       from Orchestrator.toolvault import registry as tv_registry
       from Orchestrator.tools import tool_registry
       monkeypatch.setattr(
           tv_registry, "invalidate_cache",
           lambda *a, **k: calls.__setitem__("invalidate", calls["invalidate"] + 1),
       )
       monkeypatch.setattr(
           tool_registry, "reset_cache",
           lambda *a, **k: calls.__setitem__("reset", calls["reset"] + 1),
       )
       await migrate.run_reembed(TARGET)
       assert get_active_slug(base_dir=stores_dir) == TARGET   # cutover happened
       assert calls["invalidate"] == 1 and calls["reset"] == 1  # caches busted once
   ```

2. Run, expect FAIL — nothing invalidates the caches.
   - Run: `Orchestrator/venv/bin/python -m pytest "Orchestrator/tests/test_embeddings_reembed.py::test_reembed_cutover_busts_toolvault_caches" -q`
   - Expected: `assert 0 == 1` on `calls["invalidate"]`.

3. Add the helper to `migrate.py` beside the other toolvault helpers (after `_toolvault_cutover_hook`, ~line 1195):
   ```python
   def _toolvault_reload_after_cutover() -> None:
       """Correction [27]: after active.json flips, clear the registry +
       tool_registry snapshot caches so registry-derived tool lists reflect the
       new active model. ToolVault/embeddings.json dim coherence is already handled
       inline by the precompute→set_active_slug→promote sequence above (and this
       box has no separate code_embeddings.json cache), so the embeddings re-sync
       that /toolvault/reload also runs is intentionally omitted here — after
       promote it is a hash-keyed no-op and would race the fallback re-embed hook.
       Imported lazily: toolvault imports migrate at load (import-cycle guard)."""
       from Orchestrator.toolvault import registry as tv_registry  # lazy
       from Orchestrator.tools import tool_registry                # lazy
       tv_registry.invalidate_cache()
       tool_registry.reset_cache()
   ```
   Call it in the cutover, immediately after the promote/fallback block and before the `raced = ...` line (insert between line 755 and line 757):
   ```python
           # Correction [27]: bust the registry-derived ToolVault tool-list caches
           # so tool selection reflects the just-activated model. Non-fatal — the
           # cutover must NEVER fail on ToolVault.
           try:
               _toolvault_reload_after_cutover()
           except Exception as e:  # noqa: BLE001 — cutover must not fail on toolvault
               print(f"[MIGRATE] post-cutover toolvault reload failed (non-fatal): {e}")
   ```

4. Run, expect PASS.
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_reembed.py -q`
   - Expected: `... passed` — the new test plus the existing re-embed/activation suite.

5. Record the wizard-step contract (for M7's `local_models` wizard step — no code here, a plan-level contract note): after `POST /embeddings/reembed {target: qwen3-embedding-8b-local}` reports `state: done`, the backend has ALREADY (a) re-embedded `ToolVault/embeddings.json` into the 4096-dim space via precompute→promote and (b) fired the cache-bust inline — so the wizard need not separately call `POST /toolvault/reload` (it may, defensively; the endpoint is idempotent). The `code_embeddings.json` rebuild named in correction [27] is not applicable on this codebase (no such cache exists).

6. Commit.
   - Run: `git add Orchestrator/embeddings/migrate.py Orchestrator/tests/test_embeddings_reembed.py && git commit -m "feat(embeddings): bust ToolVault tool-list caches after the re-embed cutover (correction [27])"`

---

### Task 3.8: Milestone regression sweep

**Files:**
- Test: the whole embeddings + toolvault-guard surface this milestone touched

1. Run the full embeddings + guard suite to prove no cross-test regressions from the new provider/registry entries and the `is_local` widening.
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_registry.py Orchestrator/tests/test_embeddings_providers.py Orchestrator/tests/test_embeddings_guards.py Orchestrator/tests/test_embeddings_routes.py Orchestrator/tests/test_embeddings_watcher.py Orchestrator/tests/test_embeddings_reembed.py Orchestrator/tests/test_embeddings_keep_alive.py Orchestrator/tests/test_embeddings_placement.py Orchestrator/tests/test_local_stack_keepwarm.py Orchestrator/tests/test_localstack_preflight.py Orchestrator/tests/test_portal_embeddings_card_parity.py Orchestrator/tests/test_android_embeddings_card_parity.py -q`
   - Expected: `... passed` (no failures, no errors).

2. Restart the service to load the new provider + registry entries live (pre-authorized).
   - Run: `sudo systemctl restart blackbox.service`
   - Expected: unit returns to `active (running)` after warm-up; `GET /embeddings/status` lists `qwen3-embedding-8b-local` / `qwen3-embedding-0.6b-local` with localstack blockers (dev box: "local stack not installed" — expected, this box stays cloud per §10).

3. No commit (verification only).


---

## Milestone 4: Reranker on localstack + G2 validation harness

**Depends on:** Milestone 1 (`Orchestrator/local_stack.py` — `base_url()` and `is_healthy()`). The llama-swap `rerank-qwen3-0.6b` member and its self-converted GGUF are provisioned by the installer milestone; this milestone's code is **inert-safe** until both land (the `localstack` provider resolves its base URL through a lazy import of `local_stack`, returning `None`/un-reranked when absent), so it never breaks the running tree.

Bring the reranker on-box: add a `qwen3-reranker-0.6b-local` entry (provider `localstack`) to `Orchestrator/rerank.py` that posts the llama.cpp `/v1/rerank` shape to the llama-swap front door and parses `results[].relevance_score` through the existing `_scatter_relevance_scores`. Wire the provider into `KNOWN_PROVIDERS`, `score()` dispatch, `reachable()`, and the one-time preflight — including the §5.2 hard rule that the legacy vLLM reranker on `:8091` may never co-run with the on-box retrieval group. Ship the GGUF self-conversion runbook and the G2 validation harness (golden query/passage pairs + a served-vs-reference check that gates on rank-order agreement and no degenerate `~1e-28` scores) before the wizard is allowed to select this model.

**Provider base URL — decided (spec §5.2 implied, not spelled out):** `_score_localstack` resolves its base URL from `local_stack.base_url()` (the `[local_models]` front door, `http://127.0.0.1:9098/v1`), **not** from `[rerank] base_url` (which defaults to the legacy vLLM `:8091` seam). This keeps the on-box reranker pointed at llama-swap regardless of the vLLM config and matches the canonical M1 resolver.

---

### Task 4.1: RERANK_MODELS `qwen3-reranker-0.6b-local` entry + register the `localstack` provider

**Files:**
- Modify: `Orchestrator/rerank.py` (registry table ends line 297; `KNOWN_PROVIDERS` line 304; `CLOUD_PROVIDERS` line 310)
- Test: `Orchestrator/tests/test_rerank.py` (guard tests: `test_rerank_models_table_shape` line 150; `test_known_providers_set` line 387)

1. Update the two existing guard tests to expect the new model + provider (they pin exact sets, so they fail until the entry lands). In `Orchestrator/tests/test_rerank.py`, edit `test_rerank_models_table_shape`'s expected set:

   Replace:
   ```python
    assert set(rerank.RERANK_MODELS) == {
        "qwen3-reranker-0.6b", "qwen3-reranker-4b", "qwen3-reranker-0.6b-cpu",
        "llm-rerank-gemini-flash", "llm-rerank-gpt-mini",
        "llm-rerank-claude-haiku", "llm-rerank-grok",
        "voyage-rerank-2.5", "cohere-rerank-4", "vertex-semantic-ranker"}
   ```
   with:
   ```python
    assert set(rerank.RERANK_MODELS) == {
        "qwen3-reranker-0.6b", "qwen3-reranker-4b", "qwen3-reranker-0.6b-cpu",
        "qwen3-reranker-0.6b-local",
        "llm-rerank-gemini-flash", "llm-rerank-gpt-mini",
        "llm-rerank-claude-haiku", "llm-rerank-grok",
        "voyage-rerank-2.5", "cohere-rerank-4", "vertex-semantic-ranker"}
   ```

2. Edit `test_known_providers_set` in the same file:

   Replace:
   ```python
    assert rerank.KNOWN_PROVIDERS == {
        "null", "vllm", "cpu", "voyage", "cohere", "vertex", "llm"}
   ```
   with:
   ```python
    assert rerank.KNOWN_PROVIDERS == {
        "null", "vllm", "cpu", "voyage", "cohere", "vertex", "llm",
        "localstack"}
   ```

3. Run the guards, expect FAIL (registry/set not yet updated):

   Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_rerank.py -q -k "table_shape or known_providers_set"`
   Expected: `2 failed` — `AssertionError` on the set comparisons (extra expected member/provider not present).

4. Add the registry entry in `Orchestrator/rerank.py`. Anchor on the CPU entry's comment at line 146 and insert the new entry immediately BEFORE it.

   Replace:
   ```python
    # MID-tier opt-in (M5): the SAME Qwen 0.6B weights served IN-PROCESS on CPU
   ```
   with:
   ```python
    # ── On-box localstack reranker (Milestone 4): Qwen3-Reranker-0.6B served by
    # llama-server (--reranking --pooling rank) behind the llama-swap front door,
    # exposed as /v1/rerank. Provider "localstack" — the on-box retrieval-group
    # member, NOT the vLLM :8091 seam (retained as the FP16 fallback, which may
    # NEVER co-run with this group; see preflight()). model_id is the llama-swap
    # MEMBER name ("rerank-qwen3-0.6b" in the config template) — llama-swap routes
    # /v1/rerank to that member by the body `model`. The GGUF is SELF-CONVERTED from
    # a pinned post-#16407 llama.cpp build (community GGUFs are broken → ~1e-28
    # scores); G2 (eval/rerank_g2.py) gates score validity + rank order before the
    # wizard flips the sidecar to this model. query_instruction is the SAME
    # mandatory Qwen instruct prefix as the vllm/cpu entries — the ranker inverts
    # without it (measured 2026-07-03). Keyless loopback (auth_kind none).
    "qwen3-reranker-0.6b-local": {
        "provider": "localstack",
        "model_id": "rerank-qwen3-0.6b",
        "label": "Qwen3 Reranker 0.6B (on-box, llama-swap)",
        "vram_gb": 1.4,  # f16 0.6B resident in the retrieval group (§5.2 ~1.3–1.9)
        "max_input_tokens": 8192,  # matches the member's -c 8192 (config template)
        "query_instruction": "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: ",
        "quality_note": "On-box default reranker; pairs with the on-box qwen3 embedding store. Self-converted GGUF — G2-gated for score validity.",
        "auth_kind": "none",
        "key_env": None,
        "cost_note": "On-box GPU/CPU — free, private, unlimited (runs on your box via llama-swap; nothing leaves it)",
        "privacy": "local",
        "tiers": ["MID", "HIGH"],
        "preflight_ceiling_ms": 500,  # G2 target (40-passage rerank inside ceiling)
        "preflight_passage_n": 1,     # llama.cpp /v1/rerank batches all docs in one call
    },
    # MID-tier opt-in (M5): the SAME Qwen 0.6B weights served IN-PROCESS on CPU
   ```

5. Register the provider in `KNOWN_PROVIDERS` (line 304).

   Replace:
   ```python
   KNOWN_PROVIDERS = {"null", "vllm", "cpu", "voyage", "cohere", "vertex", "llm"}
   ```
   with:
   ```python
   KNOWN_PROVIDERS = {"null", "vllm", "cpu", "voyage", "cohere", "vertex", "llm",
                      "localstack"}
   ```

6. Add the TTL-recoverable set right after `CLOUD_PROVIDERS` (line 310). This is a **decision the spec left open**: under llama-swap a cross-group swap can leave the retrieval group transiently cold, so a first probe queues behind a ~6–10s group load and reads over-ceiling — that transient must NOT disable rerank for the process lifetime the way a genuinely-dead local reranker does. localstack therefore gets the cloud-style TTL recovery for its preflight cache **only** (its reachability stays `is_healthy()`-based, never key-present).

   Replace:
   ```python
   CLOUD_PROVIDERS = {"voyage", "cohere", "vertex", "llm"}
   ```
   with:
   ```python
   CLOUD_PROVIDERS = {"voyage", "cohere", "vertex", "llm"}

   # Providers whose FAILED preflight recovers after a TTL rather than sticking for
   # the process lifetime (M3.2). localstack (Milestone 4) joins here: under
   # llama-swap a cross-group swap can leave the retrieval group transiently cold
   # (a first probe queues behind a ~6–10s group load → over-ceiling), and that
   # transient must NOT disable rerank until a restart. NB this is ONLY the cache
   # policy — localstack reachability stays is_healthy()-based (loopback, keyless),
   # never the CLOUD_PROVIDERS key-present path.
   _PREFLIGHT_TTL_RECOVERABLE = CLOUD_PROVIDERS | {"localstack"}
   ```

7. Run the guards, expect PASS:

   Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_rerank.py -q -k "table_shape or known_providers_set or every_rerank_model_declares"`
   Expected: `... passed` (the parametrized `test_every_rerank_model_declares_new_schema_keys` now also covers the new entry and passes — it declares every tiering-schema key).

8. Commit.

   Run: `git add Orchestrator/rerank.py Orchestrator/tests/test_rerank.py && git commit -m "feat(rerank): register qwen3-reranker-0.6b-local (localstack provider) in the registry"`
   Expected: one commit created.

---

### Task 4.2: `_score_localstack` scorer + reachability, wired into `score()` dispatch

**Files:**
- Modify: `Orchestrator/rerank.py` (helpers after `reachable()` ends line 429 / before the comment at line 432; `_score_localstack` before the Vertex header at line 779; `reachable()` cpu branch line 413; `score()` dispatch dict line 1095)
- Test: `Orchestrator/tests/test_rerank.py`

1. Write the failing tests. Append this block to the end of `Orchestrator/tests/test_rerank.py`:

   ```python
   # ── Milestone 4: on-box localstack reranker (llama.cpp /v1/rerank) ────────────
   # The localstack reranker posts to the llama-swap front door resolved by
   # Orchestrator/local_stack.py (M1). local_stack may not be landed in CI, so the
   # scorer resolves the base URL through rerank._localstack_base_url() — patched
   # here to a fixed URL so these tests are fully hermetic (HTTP mocked).

   def _pin_localstack_base(monkeypatch, base="http://127.0.0.1:9098/v1"):
       monkeypatch.setattr(rerank, "_localstack_base_url", lambda: base)


   def test_localstack_in_known_providers_and_registry():
       assert "localstack" in rerank.KNOWN_PROVIDERS
       e = rerank.RERANK_MODELS["qwen3-reranker-0.6b-local"]
       assert e["provider"] == "localstack"
       assert e["model_id"] == "rerank-qwen3-0.6b"
       assert e["auth_kind"] == "none" and e["key_env"] is None
       assert e["preflight_ceiling_ms"] == 500 and e["preflight_passage_n"] == 1
       # the mandatory Qwen3-Reranker instruct prefix, verbatim from the vllm entry
       assert e["query_instruction"] == \
           rerank.RERANK_MODELS["qwen3-reranker-0.6b"]["query_instruction"]


   def test_score_localstack_posts_rerank_shape_and_parses_results(monkeypatch):
       calls = {}

       def fake_post(url, json=None, timeout=None):
           calls["url"], calls["json"], calls["timeout"] = url, json, timeout
           # llama.cpp /v1/rerank shape: results[].relevance_score, out of order
           return FakeResp(200, {"results": [
               {"index": 1, "relevance_score": 0.9},
               {"index": 0, "relevance_score": 0.1}]})

       monkeypatch.setattr(rerank.requests, "post", fake_post)
       _pin_localstack_base(monkeypatch)
       with pin_cfg("rerank", provider="localstack",
                    model="qwen3-reranker-0.6b-local", timeout_s="9"):
           got = rerank.score("the query", ["pass A", "pass B"])
       assert got == [0.1, 0.9]                       # scattered back by index
       assert calls["url"] == "http://127.0.0.1:9098/v1/rerank"
       assert calls["json"]["model"] == "rerank-qwen3-0.6b"
       assert calls["json"]["documents"] == ["pass A", "pass B"]
       # the query carries the mandatory Qwen3-Reranker instruct prefix
       assert calls["json"]["query"].startswith("Instruct:")
       assert calls["json"]["query"].endswith("\nQuery: the query")
       assert calls["timeout"] == 9.0


   def test_score_localstack_stack_absent_returns_none(monkeypatch):
       """base_url None (M1 resolver not landed / stack not installed) → inert,
       and the scorer must NOT even attempt the POST."""
       monkeypatch.setattr(rerank, "_localstack_base_url", lambda: None)

       def boom(*a, **k):
           raise AssertionError("must not POST when the stack base_url is absent")

       monkeypatch.setattr(rerank.requests, "post", boom)
       with pin_cfg("rerank", provider="localstack",
                    model="qwen3-reranker-0.6b-local"):
           assert rerank.score("q", ["p"]) is None


   def test_score_localstack_empty_base_url_returns_none(monkeypatch):
       monkeypatch.setattr(rerank, "_localstack_base_url", lambda: "")
       with pin_cfg("rerank", provider="localstack",
                    model="qwen3-reranker-0.6b-local"):
           assert rerank.score("q", ["p"]) is None


   @pytest.mark.parametrize("resp", [
       FakeResp(500, {}),                                              # HTTP error
       FakeResp(200, {"results": [{"index": 0, "relevance_score": 1.0}]}),  # wrong count
       FakeResp(200, {"results": "garbage"}),                         # malformed
       FakeResp(200, {}),                                             # no results key
   ])
   def test_score_localstack_bad_response_returns_none(monkeypatch, resp):
       monkeypatch.setattr(rerank.requests, "post", lambda *a, **k: resp)
       _pin_localstack_base(monkeypatch)
       with pin_cfg("rerank", provider="localstack",
                    model="qwen3-reranker-0.6b-local"):
           assert rerank.score("q", ["a", "b"]) is None


   def test_score_localstack_transport_exception_returns_none(monkeypatch):
       def boom(*a, **k):
           raise ConnectionError("refused")
       monkeypatch.setattr(rerank.requests, "post", boom)
       _pin_localstack_base(monkeypatch)
       with pin_cfg("rerank", provider="localstack",
                    model="qwen3-reranker-0.6b-local"):
           assert rerank.score("q", ["a"]) is None    # dispatcher never-raise (A9)


   def test_reachable_localstack_uses_is_healthy(monkeypatch):
       """localstack reachability is the on-box stack health (loopback, keyless),
       NOT a key-present check and NOT the [rerank] :8091 localhost probe."""
       def no_net(*a, **k):
           raise AssertionError("localstack reachability must not hit :8091")
       monkeypatch.setattr(rerank.requests, "get", no_net)
       monkeypatch.setattr(rerank, "_localstack_healthy", lambda: True)
       with pin_cfg("rerank", provider="localstack",
                    model="qwen3-reranker-0.6b-local"):
           assert rerank.reachable() is True
       monkeypatch.setattr(rerank, "_localstack_healthy", lambda: False)
       with pin_cfg("rerank", provider="localstack",
                    model="qwen3-reranker-0.6b-local"):
           assert rerank.reachable() is False
   ```

2. Run, expect FAIL (helpers + dispatch not implemented — `_localstack_base_url`/`_localstack_healthy` do not exist, dispatch has no `localstack` key):

   Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_rerank.py -q -k localstack`
   Expected: multiple `failed`/`error` — `AttributeError: module 'Orchestrator.rerank' has no attribute '_localstack_base_url'` and `assert None == [0.1, 0.9]` (dispatch returns None).

3. Add the localstack resolver helpers. Anchor on the comment at line 432 and insert the helpers BEFORE it.

   Replace:
   ```python
   # Malformed-config resilience (M3.1 fold-in from the M2 review). M2 moved
   ```
   with:
   ```python
   # ── On-box localstack reranker helpers (Milestone 4) ──────────────────────────
   # The localstack reranker posts to the llama-swap front door (/v1/rerank),
   # resolved from the [local_models] base_url via Orchestrator/local_stack.py — NOT
   # the [rerank] base_url (which defaults to the legacy vLLM :8091 seam). local_stack
   # is imported LAZILY (cycle-proof, mirroring _load_sidecar's rationale: local_stack
   # may import rerank for capability routing, so a top-level import risks a cycle).
   # Absent/uninstalled resolver → None/False so the provider stays inert, never raises.

   def _localstack_base_url() -> "str | None":
       """The llama-swap front-door base_url ([local_models]) for the on-box
       reranker, or None when the resolver is unavailable (M1 not landed / stack not
       installed). Never raises."""
       try:
           from Orchestrator import local_stack
           return local_stack.base_url()
       except Exception:  # noqa: BLE001 - resolver absent → inert
           return None


   def _localstack_healthy() -> bool:
       """Whether the on-box stack is installed + configured + llama-swap is live
       (local_stack.is_healthy(), which keys on install/config/process-liveness, NOT
       live per-member VRAM residency — a group swap never flips this). Never raises."""
       try:
           from Orchestrator import local_stack
           return bool(local_stack.is_healthy())
       except Exception:  # noqa: BLE001 - resolver absent → not healthy
           return False


   def _vllm_reranker_running(timeout_s: float = 1.0) -> bool:
       """Is the legacy vllm-reranker.service answering on its :8091 port? A direct
       ~1s-capped GET of DEFAULT_BASE_URL/v1/models (NOT the shared _probe_localhost
       cache — this is a distinct, safety-critical probe for the §5.2 hard rule).
       Never raises."""
       try:
           return requests.get(
               DEFAULT_BASE_URL + "/v1/models", timeout=timeout_s
           ).status_code == 200
       except Exception:  # noqa: BLE001 - never-raise
           return False


   # Malformed-config resilience (M3.1 fold-in from the M2 review). M2 moved
   ```

4. Add the `_score_localstack` scorer. Anchor on the Vertex section header at line 779 and insert BEFORE it.

   Replace:
   ```python
   # ── Google Vertex semantic-ranker (M7.2) ──────────────────────────────────────
   ```
   with:
   ```python
   # ── On-box localstack reranker (Milestone 4) ──────────────────────────────────
   # llama.cpp's /v1/rerank (--reranking --pooling rank) behind llama-swap. Wire shape
   # {model, query, documents} → {results:[{index, relevance_score}]} — the SAME row
   # shape the cloud rerankers return, so _scatter_relevance_scores parses it verbatim
   # (no bespoke parser). The mandatory Qwen3-Reranker instruct prefix is prepended to
   # the query (the ranker inverts without it). Base URL is the [local_models] front
   # door (loopback, keyless), NOT the [rerank] :8091 vLLM seam. None on absent stack /
   # non-200 / malformed / count-or-index anomaly; transport blow-ups are backstopped
   # by the dispatcher's never-raise (audit A9).

   def _score_localstack(query: str, passages: list[str],
                         settings: dict) -> list[float] | None:
       """On-box Qwen3-Reranker-0.6B via llama-swap /v1/rerank (the retrieval-group
       member). Posts {model, query: instruction+query, documents} and scatters
       {index, relevance_score} back onto passage positions."""
       if not passages:
           return None
       base = _localstack_base_url()
       if not base:
           return None
       resp = requests.post(
           base.rstrip("/") + "/rerank",
           json={"model": settings["model_id"],
                 "query": settings.get("query_instruction", "") + query,
                 "documents": list(passages)},
           timeout=settings["timeout_s"],
       )
       if resp.status_code != 200:
           return None
       return _scatter_relevance_scores(resp.json(), len(passages))


   # ── Google Vertex semantic-ranker (M7.2) ──────────────────────────────────────
   ```

5. Add the `reachable()` localstack branch. Anchor on the cpu branch at line 413.

   Replace:
   ```python
       if p == "cpu":
           return _cpu_reachable()
       if p in CLOUD_PROVIDERS:
   ```
   with:
   ```python
       if p == "cpu":
           return _cpu_reachable()
       if p == "localstack":
           return _localstack_healthy()
       if p in CLOUD_PROVIDERS:
   ```

6. Wire `_score_localstack` into the `score()` dispatch dict (line 1095).

   Replace:
   ```python
       fn = {"vllm": _score_vllm, "cpu": _score_cpu, "voyage": _score_voyage,
             "cohere": _score_cohere, "vertex": _score_vertex,
             "llm": _score_llm}.get(p)
   ```
   with:
   ```python
       fn = {"vllm": _score_vllm, "cpu": _score_cpu, "voyage": _score_voyage,
             "cohere": _score_cohere, "vertex": _score_vertex,
             "llm": _score_llm, "localstack": _score_localstack}.get(p)
   ```

7. Run the localstack tests, expect PASS:

   Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_rerank.py -q -k localstack`
   Expected: `... passed` (all Task-4.2 tests green).

8. Run the whole rerank suite to prove no regression:

   Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_rerank.py -q`
   Expected: `... passed` (existing tests unaffected — the dispatch/reachable additions are purely additive).

9. Commit.

   Run: `git add Orchestrator/rerank.py Orchestrator/tests/test_rerank.py && git commit -m "feat(rerank): _score_localstack posts llama.cpp /v1/rerank + is_healthy reachability"`
   Expected: one commit created.

---

### Task 4.3: §5.2 hard-rule preflight assertion (vLLM `:8091` must be down) + TTL-recoverable localstack preflight

**Files:**
- Modify: `Orchestrator/rerank.py` (`preflight()` — the skip block ends line 1142; the cache-policy branch line 1174)
- Test: `Orchestrator/tests/test_rerank.py`

1. Write the failing tests. Append to `Orchestrator/tests/test_rerank.py`:

   ```python
   # ── Milestone 4: §5.2 hard rule + TTL-recoverable localstack preflight ────────

   def test_localstack_preflight_refuses_when_vllm_reranker_up(monkeypatch):
       """The vLLM reranker (:8091) and the on-box retrieval group cannot co-run
       (both pre-allocate VRAM → OOM). preflight() REFUSES localstack while :8091
       answers — and does NOT cache the refusal, so stopping the service resolves
       it live on the next probe (no restart)."""
       # something answering on :8091 = vllm-reranker.service still up
       monkeypatch.setattr(rerank.requests, "get",
                           lambda url, timeout=None: FakeResp(200, {"data": []}))
       _pin_localstack_base(monkeypatch)
       # a WORKING scorer — proves the refusal is the conflict, not a scoring fail
       monkeypatch.setattr(rerank, "score", lambda q, p: [1.0])
       with pin_cfg("rerank", provider="localstack",
                    model="qwen3-reranker-0.6b-local", preflight_ceiling_ms="5000"):
           pf = rerank.preflight()
           assert pf["state"] == "failed"
           assert ":8091" in pf["reason"] and "vllm" in pf["reason"].lower()
           # NOT cached: stop the service (get now refuses) → next preflight re-checks
           def refused(*a, **k):
               raise ConnectionError("service stopped")
           monkeypatch.setattr(rerank.requests, "get", refused)
           assert rerank.preflight()["state"] == "ok"


   def test_localstack_failed_preflight_recovers_after_ttl(monkeypatch):
       """A localstack preflight failure (stack cold / transiently down) is
       TTL-recoverable — unlike a vllm/cpu failure, it does NOT stick for the
       process lifetime (a group swap must not disable rerank until restart)."""
       clock = Clock(1000.0)
       monkeypatch.setattr(rerank.time, "monotonic", clock)
       _pin_localstack_base(monkeypatch)
       # autouse _no_network_reach refuses .get → no :8091 conflict
       monkeypatch.setattr(rerank, "score", lambda q, p: None)   # stack cold/down
       with pin_cfg("rerank", provider="localstack",
                    model="qwen3-reranker-0.6b-local", preflight_ceiling_ms="5000"):
           assert rerank.preflight()["state"] == "failed"
           # within the TTL the failure is cached (not re-probed)
           def reprobed(q, p):
               raise AssertionError("localstack preflight re-probed within its TTL")
           monkeypatch.setattr(rerank, "score", reprobed)
           assert rerank.preflight()["state"] == "failed"
           # past the TTL, the retrieval group warmed → re-probe → ok
           clock.t += rerank._PREFLIGHT_FAIL_TTL_S + 1
           monkeypatch.setattr(rerank, "score", lambda q, p: [1.0])
           assert rerank.preflight()["state"] == "ok"
   ```

2. Run, expect FAIL (`preflight()` has no conflict guard yet, and localstack failures still stick process-lifetime):

   Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_rerank.py -q -k "refuses_when_vllm or localstack_failed_preflight_recovers"`
   Expected: `2 failed` — the refusal test sees `state == "ok"` (no guard), the TTL test sees the second probe still `failed`.

3. Add the §5.2 conflict guard to `preflight()`. Anchor on the end of the "skipped" block at line 1142.

   Replace:
   ```python
                       "reason": "no reranker provider configured"}
           passages = ["preflight probe passage"] * max(1, passage_n)
   ```
   with:
   ```python
                       "reason": "no reranker provider configured"}
           # §5.2 hard rule (Milestone 4): the legacy vLLM reranker (:8091) and the
           # on-box localstack retrieval group CANNOT co-run — vLLM pre-allocates
           # ~90% of VRAM via gpu_memory_utilization and is invisible to llama-swap's
           # budgeting, so both resident → guaranteed OOM. Refuse to activate while
           # it answers. NOT cached (stopping vllm-reranker.service resolves it live,
           # no restart) — same non-cached posture as "skipped".
           if provider == "localstack" and _vllm_reranker_running():
               return {"state": "failed", "latency_ms": None, "measured_ms": None,
                       "ceiling_ms": ceiling, "passage_n": passage_n,
                       "reason": ("vLLM reranker still answering on :8091 — stop "
                                  "vllm-reranker.service before enabling the on-box "
                                  "retrieval group (both pre-allocate VRAM → OOM)")}
           passages = ["preflight probe passage"] * max(1, passage_n)
   ```

4. Make the localstack preflight failure TTL-recoverable. Anchor on the cache-policy branch at line 1174.

   Replace:
   ```python
           if result["state"] == "failed" and provider in CLOUD_PROVIDERS:
               _preflight_expiry = time.monotonic() + _PREFLIGHT_FAIL_TTL_S
               disabled = (f" — rerank disabled for {int(_PREFLIGHT_FAIL_TTL_S)}s"
                           f" (cloud TTL, then re-probes)")
   ```
   with:
   ```python
           if result["state"] == "failed" and provider in _PREFLIGHT_TTL_RECOVERABLE:
               _preflight_expiry = time.monotonic() + _PREFLIGHT_FAIL_TTL_S
               disabled = (f" — rerank disabled for {int(_PREFLIGHT_FAIL_TTL_S)}s"
                           f" (retry TTL, then re-probes)")
   ```

5. Run the two new tests, expect PASS:

   Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_rerank.py -q -k "refuses_when_vllm or localstack_failed_preflight_recovers"`
   Expected: `2 passed`.

6. Run the full rerank suite — prove the vllm process-lifetime path is unchanged (the TTL set only added localstack):

   Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_rerank.py -q`
   Expected: `... passed` (incl. `test_local_failed_preflight_stays_process_lifetime` for vllm and `test_preflight_over_ceiling_disables_for_process`).

7. Commit.

   Run: `git add Orchestrator/rerank.py Orchestrator/tests/test_rerank.py && git commit -m "feat(rerank): §5.2 preflight refuses localstack while vLLM :8091 is up; localstack failures are TTL-recoverable"`
   Expected: one commit created.

---

### Task 4.4: GGUF self-conversion runbook (pinned llama.cpp build)

**Files:**
- Create: `LocalModels/reranker/CONVERT-QWEN3-RERANKER-GGUF.md`

No automated test (operator runbook, executed on MS02). The commands are exact and the output filename MUST match the config template's member path (`${LOCALSTACK_MODELS}/Qwen3-Reranker-0.6B-f16.gguf`).

1. Create `LocalModels/reranker/CONVERT-QWEN3-RERANKER-GGUF.md` with this content:

   ```markdown
   # Qwen3-Reranker-0.6B → GGUF self-conversion runbook (Milestone 4)

   Community reranker GGUFs are frequently broken: a conversion that drops
   `cls.output.weight` yields degenerate `~1e-28` relevance scores (the ranker is
   effectively random). We convert ourselves from a **pinned llama.cpp build that
   post-dates the Qwen3-Reranker `convert_hf_to_gguf.py` fix** (llama.cpp #16407,
   resolved: detects Qwen3-Reranker, extracts `cls.output.weight`, sets
   `pooling_type=RANK` + the yes/no classifier labels). The output is validated by
   the G2 gate (`eval/rerank_g2.py`) before the wizard is allowed to select the
   `qwen3-reranker-0.6b-local` model.

   Run this on the GPU box (MS02). `${LOCALSTACK_MODELS}` is the llama-swap
   models-dir the installer substitutes into
   `installer/templates/llama-swap-config.yaml.template` (the reranker member reads
   `${models-dir}/Qwen3-Reranker-0.6B-f16.gguf`), e.g. `~/.blackbox/localstack/models`.

   ## 1. Clone + pin llama.cpp (post-#16407)

   ```bash
   git clone https://github.com/ggml-org/llama.cpp
   cd llama.cpp
   # Pin to a tagged release that INCLUDES PR #16407. Resolve the exact tag once,
   # then hard-pin it here (record it in the commit message). As of 2026-07 use the
   # latest b-tag and VERIFY the fix is present before trusting the build:
   git checkout <PINNED_LLAMACPP_TAG>   # e.g. b6600+ — must post-date #16407
   ```

   Verify the convert script actually handles Qwen3-Reranker (abort if these do not
   match — the pin is too old and WILL produce ~1e-28 GGUFs):

   ```bash
   grep -nE "Qwen3.*[Rr]erank|cls\.output\.weight|pooling_type.*RANK|classifier" \
     convert_hf_to_gguf.py
   ```
   Expected: at least one hit referencing the Qwen3-Reranker head / `cls.output.weight`.

   ## 2. Conversion venv

   ```bash
   python3 -m venv .conv-venv
   . .conv-venv/bin/activate
   pip install -r requirements/requirements-convert_hf_to_gguf.txt
   pip install -U "huggingface_hub[cli]"
   ```

   ## 3. Download the HF weights (CausalLM checkpoint)

   ```bash
   huggingface-cli download Qwen/Qwen3-Reranker-0.6B \
     --local-dir ./Qwen3-Reranker-0.6B
   ```

   ## 4. Convert to f16 GGUF (output name is load-bearing)

   ```bash
   python3 convert_hf_to_gguf.py ./Qwen3-Reranker-0.6B \
     --outfile "${LOCALSTACK_MODELS}/Qwen3-Reranker-0.6B-f16.gguf" \
     --outtype f16
   ```

   The filename MUST be exactly `Qwen3-Reranker-0.6B-f16.gguf` — the llama-swap
   member `rerank-qwen3-0.6b` loads it by that path. f16 is fine for a 0.6B
   (~1.3–1.5GB). If VRAM is tight, optionally quantize (and update the config
   template's `--model` path to match):

   ```bash
   # optional: cmake --build build --target llama-quantize   (if not already built)
   # ./build/bin/llama-quantize \
   #   "${LOCALSTACK_MODELS}/Qwen3-Reranker-0.6B-f16.gguf" \
   #   "${LOCALSTACK_MODELS}/Qwen3-Reranker-0.6B-Q8_0.gguf" Q8_0
   ```

   ## 5. Quick metadata pre-check (NOT the authoritative gate)

   ```bash
   ./build/bin/llama-gguf "${LOCALSTACK_MODELS}/Qwen3-Reranker-0.6B-f16.gguf" \
     | grep -iE "pooling|cls\.output|classifier|rank"
   ```
   Expected: a `pooling_type` = RANK / `cls.output.weight` tensor is present. A
   missing `cls.output.weight` here means a broken conversion — go back to step 1
   and use a newer pin.

   ## 6. Authoritative validity gate — G2

   Serve the member (llama-swap on `:9098`, or a standalone `llama-server
   --model … --reranking --pooling rank -c 8192`) and run the G2 harness:

   ```bash
   Orchestrator/venv/bin/python eval/rerank_g2.py            # served-vs-golden gate
   Orchestrator/venv/bin/python eval/rerank_g2.py --hf-reference \
     --hf-model-dir ./llama.cpp/Qwen3-Reranker-0.6B         # + HF cross-check
   ```
   The GGUF is only trustworthy once G2 exits 0 (rank-order agreement + no
   degenerate `~1e-28` scores). Only then does the wizard flip the sidecar to
   `qwen3-reranker-0.6b-local`.
   ```

2. Commit.

   Run: `git add LocalModels/reranker/CONVERT-QWEN3-RERANKER-GGUF.md && git commit -m "docs(reranker): GGUF self-conversion runbook (pinned post-#16407 llama.cpp)"`
   Expected: one commit created.

---

### Task 4.5: G2 golden set + validation harness + pure-logic pytest

**Files:**
- Create: `eval/rerank_golden.jsonl` (golden query/passage pairs)
- Create: `eval/rerank_g2.py` (validation harness)
- Test: `Orchestrator/tests/test_rerank_g2_harness.py`

1. Create the golden set `eval/rerank_golden.jsonl`. Each row is a query plus `relevant` passages (must score high) and `hard_negative` passages (must score low); the harness builds `documents = relevant + hard_negative` so `separation_ok` can assert `min(relevant) > max(negative)`. Write these 8 rows (one JSON object per line, no trailing blank line):

   ```jsonl
   {"id": "rr-voice-clone-consent", "query": "how do I clone a voice with the person's explicit consent", "relevant": ["To create a voice clone you must first capture explicit consent from the voice owner; the clone endpoint returns 422 unless the consent flag is set, mirroring the ElevenLabs gate.", "Voice cloning requires a ~3 second reference sample plus a recorded consent record stored in the voice profile before synthesis is allowed."], "hard_negative": ["The grocery store demo app lists produce prices and lets you add bananas and apples to a shopping cart.", "Cron jobs are scheduled with a five-field expression and run in the box's local timezone."]}
   {"id": "rr-cron-schedule", "query": "create a recurring weekly scheduled task", "relevant": ["The scheduler accepts a cron expression; a weekly job uses the day-of-week field and fires at the configured hour in local time.", "Recurring tasks are persisted so they survive a restart and re-arm on the next matching minute."], "hard_negative": ["Qwen3-TTS exposes nine preset CustomVoice voices including Vivian and Serena.", "Tailscale provides the network security perimeter for remote access to the box."]}
   {"id": "rr-reembed-migration", "query": "migrate the whole corpus to a new embedding model", "relevant": ["A re-embed migration builds a fresh chunked store for the target model and atomically swaps it in once every snapshot has a vector.", "The wizard drives the re-embed with a progress UI; after cutover the tool-selection and code-embedding caches must be rebuilt to avoid a dimension mismatch."], "hard_negative": ["The live view panel streams the computer-use virtual display over noVNC in an iframe.", "Background music should be mixed at fifteen percent while narration plays at full volume."]}
   {"id": "rr-tailscale-remote", "query": "how does remote access to the box work", "relevant": ["External access is over Tailscale; the tailnet plus LAN is the trust boundary by design and there is no app-layer auth on the loopback services.", "The MCP tool server is exposed over a Tailscale Funnel with bearer and OAuth, isolated per operator."], "hard_negative": ["Reranker GGUFs that drop cls.output.weight produce degenerate scores near 1e-28.", "The onboarding wizard shows the hardware tier and disk headroom before downloading weights."]}
   {"id": "rr-reranker-instruct", "query": "why does the reranker need a query instruction prefix", "relevant": ["Qwen3-Reranker inverts its ranking without the instruct prefix: it scores well-formedness instead of relevance, so a relevant passage can lose to an off-topic one.", "The mandatory instruct prefix is prepended to the query before scoring on every Qwen reranker path (vLLM, CPU, and the on-box llama.cpp member)."], "hard_negative": ["Xvfb renders the computer-use virtual display on the CPU with llvmpipe and never touches the GPU.", "Speaches holds each whisper model warm under its own model TTL inside the process."]}
   {"id": "rr-tts-streaming", "query": "stream text to speech audio as it is generated", "relevant": ["Streaming TTS yields audio chunks as they are generated via a StreamingResponse; a base-clone stream buffers about three seconds first to avoid drift.", "The Qwen TTS server reads the output sample rate from the model at runtime rather than hardcoding it, then resamples for browser playback."], "hard_negative": ["Snapshots are minted through the chat save path which is far cheaper than a full LLM round-trip.", "The two GPU groups are mutually exclusive and swap with a ten minute idle TTL."]}
   {"id": "rr-snapshot-search", "query": "search past sessions for how a bug was fixed", "relevant": ["Semantic search runs embed then rerank back-to-back over the snapshot corpus, both members co-resident in the retrieval group.", "Every development session is minted as a searchable snapshot with an embedding so past bug fixes can be recalled later."], "hard_negative": ["A voice conversation is duplex: whisper listens while the TTS model speaks, both in the audio group.", "The installer downloads the llama-swap binary sha256-pinned like the zellij release."]}
   {"id": "rr-app-register", "query": "register a new web app with the portal", "relevant": ["A new app is created under the Apps directory, started on a port in the 8060 to 8099 range, and registered with the portal using the system operator.", "Registering an app posts its name, port, and directory so it appears in the portal app list and can be reverse-proxied."], "hard_negative": ["Keep-warm maps to a ttl of zero which is immune to the idle timeout but still yields to a cross-group swap.", "The Vertex semantic ranker mints an OAuth token from the ambient service account credentials."]}
   ```

2. Write the failing pure-logic test. Create `Orchestrator/tests/test_rerank_g2_harness.py`:

   ```python
   """Pure-logic unit tests for the G2 reranker-validity harness (eval/rerank_g2.py).

   The harness itself runs on the GPU box (MS02) against a live llama-server; these
   tests cover only its dependency-free scoring logic — degenerate-score detection,
   relevant-vs-negative separation, and the pure-Python Spearman — so CI (no torch,
   no GPU, no network) still guards the pass/fail math.
   """
   import sys
   from pathlib import Path

   import pytest

   REPO = Path(__file__).resolve().parents[2]
   sys.path.insert(0, str(REPO / "eval"))
   import rerank_g2  # noqa: E402


   def test_is_degenerate_flags_1e28_scores():
       # the exact broken-GGUF signature: all scores ~1e-28
       assert rerank_g2.is_degenerate([1e-28, 2e-28, 1.5e-28]) is True
       assert rerank_g2.is_degenerate([]) is True
       assert rerank_g2.is_degenerate([0.5, 0.5, 0.5]) is True   # no spread
       assert rerank_g2.is_degenerate([0.9, 0.1, 0.5]) is False


   def test_separation_ok_requires_min_relevant_over_max_negative():
       # documents = relevant(2) + hard_negative(2)
       assert rerank_g2.separation_ok([0.9, 0.8, 0.2, 0.1], 2) is True
       assert rerank_g2.separation_ok([0.9, 0.1, 0.8, 0.2], 2) is False  # a neg beats a rel
       # not enough info to judge → not a failure
       assert rerank_g2.separation_ok([0.5, 0.4], 0) is True
       assert rerank_g2.separation_ok([0.5, 0.4], 2) is True


   def test_spearman_perfect_inverse_and_tie():
       assert rerank_g2.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
       assert rerank_g2.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
       # a constant series has zero variance → nan (guarded, never a ZeroDivision)
       import math
       assert math.isnan(rerank_g2.spearman([1, 1, 1], [1, 2, 3]))
   ```

3. Run, expect FAIL (harness module does not exist yet):

   Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_rerank_g2_harness.py -q`
   Expected: `ModuleNotFoundError: No module named 'rerank_g2'` (collection error).

4. Create the harness `eval/rerank_g2.py`:

   ```python
   #!/usr/bin/env python3
   """G2 gate: Qwen3-Reranker-0.6B GGUF validity + rank-order agreement.

   Broken reranker GGUFs (missing cls.output.weight) return degenerate ~1e-28
   scores. This harness scores each golden query's passages through the SERVED
   llama.cpp /v1/rerank (the same wire path Orchestrator/rerank.py:_score_localstack
   uses — it reuses rerank._scatter_relevance_scores to parse), and gates on:

     * no degenerate scores (max |score| and the spread must clear a floor), AND
     * relevant passages rank above the hard-negatives on the served scores
       (min(relevant) > max(negative)), AND
     * (optional --hf-reference) per-query Spearman rank agreement between the
       served scores and a HuggingFace transformers reference >= --rank-agreement-min.

   The primary gate (degenerate + separation) needs only `requests`; --hf-reference
   additionally needs torch + transformers (present on the GPU box's reranker venv).

   Run (from the repo root, on the GPU box after serving the member):
       Orchestrator/venv/bin/python eval/rerank_g2.py
       Orchestrator/venv/bin/python eval/rerank_g2.py --base-url http://127.0.0.1:9098/v1
       Orchestrator/venv/bin/python eval/rerank_g2.py --hf-reference \
           --hf-model-dir ./llama.cpp/Qwen3-Reranker-0.6B

   Writes eval/results/{date}-rerank-g2.{md,json} and exits non-zero on ANY failure
   (a failed gate = STOP: do not let the wizard select the on-box reranker).
   """
   from __future__ import annotations

   import argparse
   import json
   import sys
   from datetime import date
   from pathlib import Path

   import requests

   REPO = Path(__file__).resolve().parents[1]
   if str(REPO) not in sys.path:
       sys.path.insert(0, str(REPO))

   from Orchestrator import rerank  # noqa: E402

   GOLDEN = REPO / "eval" / "rerank_golden.jsonl"
   RESULTS_DIR = REPO / "eval" / "results"
   SLUG = "qwen3-reranker-0.6b-local"

   # A working reranker separates relevant from off-topic by far more than this; a
   # broken GGUF collapses everything to ~1e-28. Both the magnitude and the spread
   # must clear the floor.
   _DEGENERATE_FLOOR = 1e-6


   # ── pure logic (unit-tested in Orchestrator/tests/test_rerank_g2_harness.py) ──

   def is_degenerate(scores: list[float]) -> bool:
       """True if the scores look like a broken conversion: empty, all near zero,
       or with no meaningful spread (the ~1e-28 signature)."""
       if not scores:
           return True
       mag = max(abs(s) for s in scores)
       spread = max(scores) - min(scores)
       return mag < _DEGENERATE_FLOOR or spread < _DEGENERATE_FLOOR


   def separation_ok(scores: list[float], n_relevant: int) -> bool:
       """documents were built as relevant(n_relevant) + hard_negative(rest); a
       valid reranker scores every relevant passage above every negative. Not
       enough info to judge (no relevants, or all-relevant) is not a failure."""
       if n_relevant <= 0 or n_relevant >= len(scores):
           return True
       rel = scores[:n_relevant]
       neg = scores[n_relevant:]
       return min(rel) > max(neg)


   def _rank(xs: list[float]) -> list[float]:
       """Average-rank of each element (ties share their mean rank)."""
       order = sorted(range(len(xs)), key=lambda i: xs[i])
       ranks = [0.0] * len(xs)
       i = 0
       while i < len(order):
           j = i
           while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
               j += 1
           avg = (i + j) / 2.0
           for k in range(i, j + 1):
               ranks[order[k]] = avg
           i = j + 1
       return ranks


   def spearman(a: list[float], b: list[float]) -> float:
       """Spearman rank correlation, pure Python (no scipy). nan on a length
       mismatch, <2 points, or a zero-variance (constant) series."""
       if len(a) != len(b) or len(a) < 2:
           return float("nan")
       ra, rb = _rank(a), _rank(b)
       n = len(a)
       ma, mb = sum(ra) / n, sum(rb) / n
       cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
       va = sum((ra[i] - ma) ** 2 for i in range(n))
       vb = sum((rb[i] - mb) ** 2 for i in range(n))
       if va == 0 or vb == 0:
           return float("nan")
       return cov / (va ** 0.5 * vb ** 0.5)


   # ── I/O (exercised as the G2 gate on the GPU box, not in CI) ──────────────────

   def load_golden() -> list[dict]:
       rows = []
       for line in GOLDEN.read_text().splitlines():
           line = line.strip()
           if line:
               rows.append(json.loads(line))
       return rows


   def score_via_endpoint(base_url: str, model_id: str, instruction: str,
                          query: str, documents: list[str],
                          timeout_s: float = 30.0) -> "list[float] | None":
       """POST the llama.cpp /v1/rerank shape and parse via the SAME scatter the
       production path uses (rerank._scatter_relevance_scores). None on any anomaly."""
       resp = requests.post(
           base_url.rstrip("/") + "/rerank",
           json={"model": model_id, "query": instruction + query,
                 "documents": list(documents)},
           timeout=timeout_s,
       )
       if resp.status_code != 200:
           return None
       return rerank._scatter_relevance_scores(resp.json(), len(documents))


   def score_via_hf(model_dir: str, instruction: str, query: str,
                    documents: list[str]) -> list[float]:
       """HuggingFace transformers reference scores for Qwen3-Reranker (the yes/no
       logit recipe from the model card). Heavy (torch); imported lazily and only
       under --hf-reference. Returns P(yes) per document."""
       import torch
       from transformers import AutoModelForCausalLM, AutoTokenizer

       tok = AutoTokenizer.from_pretrained(model_dir, padding_side="left")
       model = AutoModelForCausalLM.from_pretrained(model_dir).eval()
       tok_yes = tok.convert_tokens_to_ids("yes")
       tok_no = tok.convert_tokens_to_ids("no")
       prefix = ("<|im_start|>system\nJudge whether the Document meets the "
                 "requirements based on the Query and the Instruct provided. Note "
                 'that the answer can only be "yes" or "no".<|im_end|>\n'
                 "<|im_start|>user\n")
       suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
       out: list[float] = []
       with torch.no_grad():
           for doc in documents:
               body = (f"<Instruct>: {instruction}\n<Query>: {query}\n"
                       f"<Document>: {doc}")
               enc = tok(prefix + body + suffix, return_tensors="pt")
               logits = model(**enc).logits[0, -1]
               yn = torch.softmax(
                   torch.stack([logits[tok_no], logits[tok_yes]]), dim=0)
               out.append(float(yn[1]))
       return out


   def main(argv=None) -> int:
       import os
       os.chdir(REPO)  # results path + any config reads are repo-relative
       ap = argparse.ArgumentParser()
       ap.add_argument("--base-url", default=None,
                       help="llama-swap /v1 front door (default: local_stack, else :9098/v1)")
       ap.add_argument("--hf-reference", action="store_true",
                       help="also compute the HuggingFace reference + Spearman agreement")
       ap.add_argument("--hf-model-dir", default=None,
                       help="path to the Qwen3-Reranker-0.6B HF checkpoint (for --hf-reference)")
       ap.add_argument("--rank-agreement-min", type=float, default=0.9,
                       help="min mean per-query Spearman vs HF reference (with --hf-reference)")
       ap.add_argument("--out-date", default=date.today().isoformat())
       args = ap.parse_args(argv)

       entry = rerank.RERANK_MODELS[SLUG]
       instruction = entry["query_instruction"]
       model_id = entry["model_id"]
       base_url = args.base_url or rerank._localstack_base_url() or "http://127.0.0.1:9098/v1"

       rows = load_golden()
       per_query = []
       all_pass = True
       spearmans = []
       for row in rows:
           documents = list(row["relevant"]) + list(row["hard_negative"])
           n_rel = len(row["relevant"])
           served = score_via_endpoint(base_url, model_id, instruction,
                                       row["query"], documents)
           rec = {"id": row["id"], "n_relevant": n_rel,
                  "served_scores": served}
           if served is None:
               rec["state"] = "no-response"
               all_pass = False
           else:
               degen = is_degenerate(served)
               sep = separation_ok(served, n_rel)
               rec["degenerate"] = degen
               rec["separation_ok"] = sep
               ok = (not degen) and sep
               if args.hf_reference:
                   if not args.hf_model_dir:
                       print("--hf-reference requires --hf-model-dir", file=sys.stderr)
                       return 2
                   ref = score_via_hf(args.hf_model_dir, instruction,
                                      row["query"], documents)
                   rho = spearman(served, ref)
                   rec["hf_scores"] = ref
                   rec["spearman"] = rho
                   spearmans.append(rho)
                   ok = ok and (rho == rho and rho >= args.rank_agreement_min)  # rho==rho excludes nan
               rec["state"] = "pass" if ok else "fail"
               all_pass = all_pass and ok
           per_query.append(rec)

       mean_rho = (sum(spearmans) / len(spearmans)) if spearmans else None
       report = {
           "date": args.out_date, "slug": SLUG, "model_id": model_id,
           "base_url": base_url, "hf_reference": args.hf_reference,
           "rank_agreement_min": args.rank_agreement_min,
           "mean_spearman": mean_rho,
           "pass": all_pass, "queries": per_query,
       }

       RESULTS_DIR.mkdir(parents=True, exist_ok=True)
       json_path = RESULTS_DIR / f"{args.out_date}-rerank-g2.json"
       json_path.write_text(json.dumps(report, indent=2))

       lines = [f"# G2 reranker validity — {args.out_date}", "",
                f"- model: `{model_id}` @ `{base_url}`",
                f"- HF reference: {args.hf_reference}"
                + (f" (mean Spearman {mean_rho:.3f}, min {args.rank_agreement_min})"
                   if mean_rho is not None else ""),
                f"- **overall: {'PASS' if all_pass else 'FAIL'}**", "",
                "| query | state | degenerate | separation | spearman |",
                "|---|---|---|---|---|"]
       for r in per_query:
           lines.append(
               f"| {r['id']} | {r.get('state')} | {r.get('degenerate', '-')} "
               f"| {r.get('separation_ok', '-')} | "
               f"{r.get('spearman', '-') if 'spearman' in r else '-'} |")
       md_path = RESULTS_DIR / f"{args.out_date}-rerank-g2.md"
       md_path.write_text("\n".join(lines) + "\n")

       print(f"G2 {'PASS' if all_pass else 'FAIL'} — wrote {md_path} / {json_path}")
       return 0 if all_pass else 1


   if __name__ == "__main__":
       raise SystemExit(main())
   ```

5. Run the pure-logic tests, expect PASS:

   Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_rerank_g2_harness.py -q`
   Expected: `3 passed`.

6. Sanity-check the harness imports and its argparse wire without a live server (must not crash at import; `--help` exits 0):

   Run: `Orchestrator/venv/bin/python eval/rerank_g2.py --help`
   Expected: the argparse usage text, exit 0 (proves `from Orchestrator import rerank` and module-level code are import-safe with no chdir side effect).

7. Commit.

   Run: `git add eval/rerank_golden.jsonl eval/rerank_g2.py Orchestrator/tests/test_rerank_g2_harness.py && git commit -m "feat(eval): G2 reranker-validity harness + golden set (rank order + no ~1e-28 scores)"`
   Expected: one commit created.

---

**Milestone 4 done-check:** run the full reranker suite once more —

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_rerank.py Orchestrator/tests/test_rerank_g2_harness.py -q`
Expected: `... passed` (no failures, no errors). The `localstack` provider is now selectable through the existing `POST /rerank/select` (no route change: `localstack` ∈ `KNOWN_PROVIDERS`, the model's `provider` matches, and its `tiers` include HIGH/MID); the wizard flips the sidecar to `qwen3-reranker-0.6b-local` only after `eval/rerank_g2.py` exits 0 on MS02.


---

## Milestone 5: STT On-Box + D12 Orchestrator Serialization + D10 Loading Affordance

**Depends on:** Milestone 1 (`Orchestrator/local_stack.py` exposing `is_healthy()`, `enabled(capability)`, `base_url()` → `http://127.0.0.1:9098/v1`; the llama-swap front door `:9098`; the `[local_models]` config section; and the llama-swap template that pins the Speaches member to static port `9099`). Milestone 3 (the `localstack` embeddings provider in `Orchestrator/embeddings/providers.py`) and Milestone 4 (the `localstack` rerank scoring path in `Orchestrator/rerank.py`) for Tasks 5.6 / 5.7 only — Tasks 5.1–5.5 and 5.8 depend on M1 alone.

Bring on-box streaming + batch STT (the pinned Speaches member) into the resolver, the batch `file_transcribe` path, and the streaming `/ws/stt` bridge, using a **distinct `onbox` token ranked above the custom-server `local` token** (§5.3, corrections [5][7]). Ship the **D12 serialization primitive** (`voice_session()` / `retrieval_gate()` in `local_stack.py`) so a direct-to-port voice stream never gets swap-evicted mid-utterance, and the **D10 "loading models…" affordance** (an `stt_status` frame + ~30s honest-error ceiling, never a silent provider switch) so cross-group warm-up is honest. Streaming stays WebSocket end-to-end (client ↔ `/ws/stt` ↔ direct Speaches `:9099/v1/realtime`); all existing bridge mechanics (24 kHz resample, trailing-silence stop, hallucination filter, `stt_done`) carry over unchanged.

---

### Task 5.1: On-box STT availability + resolver ordering

Add an on-box availability signal independent of the custom-server registry and rank the new `onbox` token **above** `local` in the ordered avail dict, without touching the existing `local`/cloud tie-break. An explicit wizard pick (D9) still wins whenever its capability is available.

**Files:**
- Modify: `Orchestrator/stt/resolve.py` (add `onbox_stt_available()`; thread `onbox_ok` through `resolve_stt_provider` at lines 69, 82-94)
- Test: `Orchestrator/tests/test_stt_resolve.py` (append cases)

**Steps:**

1. Append a failing test to `Orchestrator/tests/test_stt_resolve.py`:
```python
def test_onbox_ranked_above_local_no_explicit_pick():
    # No explicit pick, only onbox + local available -> onbox wins (ranked above
    # the custom-server local token). Cloud absent.
    assert resolve_stt_provider(
        "", openai_ok=False, google_ok=False, elevenlabs_ok=False,
        onbox_ok=True, local_ok=True) == "onbox"

def test_explicit_onbox_wins_when_available():
    assert resolve_stt_provider(
        "onbox", openai_ok=True, google_ok=False, onbox_ok=True) == "onbox"

def test_explicit_elevenlabs_still_wins_over_onbox(monkeypatch):
    # D9: an explicit credentialed pick is NEVER overridden by the on-box default.
    assert resolve_stt_provider(
        "elevenlabs", openai_ok=False, google_ok=False,
        elevenlabs_ok=True, onbox_ok=True) == "elevenlabs"

def test_onbox_availability_keys_on_local_stack(monkeypatch):
    # onbox availability = is_healthy() AND enabled('stt'), NOT the custom-server
    # registry (which is what local_stt_available() reads).
    import Orchestrator.stt.resolve as r
    calls = {}
    class _LS:
        @staticmethod
        def is_healthy(): calls["healthy"] = True; return True
        @staticmethod
        def enabled(cap): calls["cap"] = cap; return True
    monkeypatch.setitem(__import__("sys").modules, "Orchestrator.local_stack", _LS)
    assert r.onbox_stt_available() is True
    assert calls == {"healthy": True, "cap": "stt"}
```

2. Run it, expect FAIL (`onbox_stt_available` undefined; `resolve_stt_provider` has no `onbox_ok`):
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_stt_resolve.py -v`
   - Expected: `AttributeError`/`TypeError` — `onbox_stt_available` missing and `resolve_stt_provider() got an unexpected keyword argument 'onbox_ok'`.

3. Add `onbox_stt_available()` after `local_streaming_stt_available()` (after line 52) in `Orchestrator/stt/resolve.py`:
```python
def onbox_stt_available() -> bool:
    """True iff the on-box local stack is installed+healthy AND STT is enabled in
    [local_models]. Independent of the custom-server registry (local_stt_available)
    so on a fresh on-box-only box the on-box Speaches member is reachable. Lazy
    import + fail-soft: a missing/broken local_stack never breaks STT resolution."""
    try:
        from Orchestrator import local_stack
        return bool(local_stack.is_healthy() and local_stack.enabled("stt"))
    except Exception:
        return False
```

4. Change the `resolve_stt_provider` signature (line 69) to add `onbox_ok`:
```python
def resolve_stt_provider(provided=None, *, openai_ok=None, google_ok=None, elevenlabs_ok=None, local_ok=None, onbox_ok=None):
```

5. In the runtime auto-fill block (after line 91, alongside `local_ok`), add:
```python
        onbox_ok = onbox_stt_available() if onbox_ok is None else onbox_ok
```

6. Replace the `avail` dict (lines 92-94) so `onbox` is inserted **above** `local` in insertion order:
```python
    # local STT is file-only + a fallback, so it sits LAST in the tie-break order;
    # the on-box stack (onbox) is ranked ABOVE it but BELOW cloud tie-breaks — the
    # wizard seeds an explicit "onbox" pick (D2) rather than relying on this order.
    avail = {"openai": openai_ok, "google": google_ok,
             "elevenlabs": bool(elevenlabs_ok), "onbox": bool(onbox_ok), "local": bool(local_ok)}
```

7. Run, expect PASS:
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_stt_resolve.py -v`
   - Expected: all cases pass, including the pre-existing ones (their `**kw` absorbs the new kwarg).

8. Commit:
```bash
git add Orchestrator/stt/resolve.py Orchestrator/tests/test_stt_resolve.py
git commit -m "feat(stt): add onbox availability + rank onbox above local in resolver"
```

---

### Task 5.2: On-box STT model ids + Design-B Speaches locator (local_stack helpers)

Add the STT model ids, the pinned Speaches static port, and the two URL helpers the batch + streaming paths need. Additive to the M1 module.

**Files:**
- Modify: `Orchestrator/local_stack.py` (append the M5 STT-locator block)
- Test: `Orchestrator/tests/test_local_stack_stt_locator.py` (Create)

**Steps:**

1. Create failing test `Orchestrator/tests/test_local_stack_stt_locator.py`:
```python
from Orchestrator import local_stack


def test_speaches_static_port_is_9099():
    assert local_stack.SPEACHES_STATIC_PORT == 9099


def test_front_door_strips_v1(monkeypatch):
    monkeypatch.setattr(local_stack, "base_url", lambda: "http://127.0.0.1:9098/v1")
    assert local_stack.front_door() == "http://127.0.0.1:9098"


def test_warm_url_hits_upstream_speaches_health(monkeypatch):
    monkeypatch.setattr(local_stack, "base_url", lambda: "http://127.0.0.1:9098/v1")
    assert local_stack.speaches_warm_url() == "http://127.0.0.1:9098/upstream/speaches/health"


def test_realtime_ws_url_is_direct_to_9099():
    url = local_stack.speaches_realtime_ws_url("deepdml/faster-whisper-large-v3-turbo-ct2")
    assert url.startswith("ws://127.0.0.1:9099/v1/realtime?")
    assert "model=deepdml%2Ffaster-whisper-large-v3-turbo-ct2" in url
    assert "intent=transcription" in url


def test_stt_model_getters():
    assert local_stack.stt_stream_model() == "deepdml/faster-whisper-large-v3-turbo-ct2"
    assert local_stack.stt_batch_model() == "Systran/faster-whisper-large-v3"
```

2. Run, expect FAIL (`AttributeError: module 'Orchestrator.local_stack' has no attribute 'SPEACHES_STATIC_PORT'`):
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_local_stack_stt_locator.py -v`
   - Expected: FAIL — helpers not defined yet.

3. Append this block to `Orchestrator/local_stack.py`:
```python
# ─────────────────────────────────────────────────────────────────────────────
# M5 — on-box STT model ids + Design-B direct-WS Speaches locator.
# (base_url()/is_healthy()/enabled() come from the M1 module.)
# ─────────────────────────────────────────────────────────────────────────────
from urllib.parse import quote as _quote

# llama-swap pins the Speaches member to this STATIC loopback port in the
# generated config (installer/templates/llama-swap-config.yaml.template) so the
# Design-B streaming STT bridge can open a DIRECT WebSocket to it — llama-swap
# WebSocket proxying is a known-missing feature (mostlygeek/llama-swap#754).
SPEACHES_STATIC_PORT = 9099

# On-box whisper model ids served by the Speaches member (§5.3 / §8 template).
# Streaming defaults to the turbo ct2 build (parity with today's gemma-box path);
# batch uses full large-v3 for quality. Wizard-overridable later (Q3/Q6).
ONBOX_STT_STREAM_MODEL = "deepdml/faster-whisper-large-v3-turbo-ct2"
ONBOX_STT_BATCH_MODEL = "Systran/faster-whisper-large-v3"


def stt_stream_model() -> str:
    return ONBOX_STT_STREAM_MODEL


def stt_batch_model() -> str:
    return ONBOX_STT_BATCH_MODEL


def front_door() -> str:
    """llama-swap front-door root (base_url() without the trailing /v1)."""
    return base_url().rsplit("/v1", 1)[0]


def speaches_warm_url() -> str:
    """llama-swap /upstream passthrough that LOADS the audio group and proxies
    Speaches /health — GET it until 200 to warm the group before a direct-WS
    stream (Design B). Going through :9098 is what triggers the load/evict."""
    return f"{front_door()}/upstream/speaches/health"


def speaches_realtime_ws_url(model: str, *, intent: str = "transcription") -> str:
    """DIRECT ws:// URL to the pinned Speaches member's /v1/realtime endpoint
    (Design B — bypasses the llama-swap proxy, which cannot proxy WebSockets)."""
    return (f"ws://127.0.0.1:{SPEACHES_STATIC_PORT}/v1/realtime"
            f"?model={_quote(model)}&intent={intent}")
```

4. Run, expect PASS:
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_local_stack_stt_locator.py -v`
   - Expected: 5 passed.

5. Commit:
```bash
git add Orchestrator/local_stack.py Orchestrator/tests/test_local_stack_stt_locator.py
git commit -m "feat(local-stack): on-box STT model ids + Design-B Speaches :9099 locator"
```

---

### Task 5.3: D12 serialization primitive — `voice_session()` / `retrieval_gate()`

The core D12 primitive: a module-level voice-session depth counter and a gate that any retrieval-group (localstack embeddings/rerank) caller wraps its `:9098` dispatch in. A bounded timeout lets auto-mint embeds degrade to vector-less rather than deadlock behind a long voice call.

**Files:**
- Modify: `Orchestrator/local_stack.py` (append the M5 serialization block)
- Test: `Orchestrator/tests/test_local_stack_serialization.py` (Create)

**Steps:**

1. Create failing test `Orchestrator/tests/test_local_stack_serialization.py`:
```python
import asyncio
import time

from Orchestrator import local_stack


def test_gate_open_when_no_voice_session():
    async def scenario():
        assert local_stack.is_voice_active() is False
        async with local_stack.retrieval_gate(timeout=1.0):
            return "ran"
    assert asyncio.run(scenario()) == "ran"


def test_voice_session_blocks_then_releases_gate():
    async def scenario():
        order = []

        async def retriever():
            async with local_stack.retrieval_gate(timeout=5.0):
                order.append("retrieval")

        async def voice():
            async with local_stack.voice_session():
                assert local_stack.is_voice_active() is True
                order.append("voice-start")
                await asyncio.sleep(0.2)   # gate must wait through this
                order.append("voice-end")
        await asyncio.gather(voice(), retriever())
        return order
    order = asyncio.run(scenario())
    # retrieval ran only AFTER the voice session closed.
    assert order == ["voice-start", "voice-end", "retrieval"], order
    assert local_stack.is_voice_active() is False


def test_bounded_gate_times_out_under_a_held_session():
    async def scenario():
        async with local_stack.voice_session():
            t0 = time.monotonic()
            try:
                async with local_stack.retrieval_gate(timeout=0.1):
                    return "ran"      # must NOT happen
            except asyncio.TimeoutError:
                return round(time.monotonic() - t0, 2)
    elapsed = asyncio.run(scenario())
    assert isinstance(elapsed, float) and elapsed >= 0.1


def test_reentrant_depth_counter():
    async def scenario():
        async with local_stack.voice_session():
            async with local_stack.voice_session():
                assert local_stack.is_voice_active() is True
            assert local_stack.is_voice_active() is True   # still one open
        assert local_stack.is_voice_active() is False
    asyncio.run(scenario())
```

2. Run, expect FAIL (`AttributeError: ... has no attribute 'voice_session'`):
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_local_stack_serialization.py -v`
   - Expected: FAIL — primitive not defined.

3. Append this block to `Orchestrator/local_stack.py`:
```python
# ─────────────────────────────────────────────────────────────────────────────
# M5 — D12 Orchestrator-level voice/retrieval serialization primitive.
#
# A direct-to-port on-box voice stream (Design-B STT WS to :9099, or streaming
# Qwen TTS) is INVISIBLE to llama-swap's in-flight drain counter, so llama-swap
# would evict the audio group mid-utterance to serve a retrieval-group request.
# We serialize at the Orchestrator instead: while ANY on-box voice stream is
# open, retrieval-group dispatch (localstack embeddings/rerank -> :9098) WAITS
# behind it, sequencing retrieval into the STT-finalize -> retrieve -> TTS-speak
# gap of a voice turn (§6).
#
# Deliberately poll-based on a plain int, NOT an asyncio.Event: voice_session()
# runs on the FastAPI loop while auto-mint embeds run via search._run_async
# (which may drive a coroutine on a DIFFERENT loop/thread). A module-level
# asyncio.Event would raise "bound to a different event loop"; a GIL-atomic int
# read is loop- and thread-agnostic. Single active user (personal box), so this
# is cooperative sequencing, not a hard mutex.
# ─────────────────────────────────────────────────────────────────────────────
import asyncio as _asyncio
import time as _time
from contextlib import asynccontextmanager as _asynccontextmanager

_voice_depth = 0

# Bounded wait a retrieval caller tolerates before giving up on the gate. The
# auto-mint embed path converts a timeout into a vector-less mint; a query embed
# falls back to keyword search; rerank falls through to un-reranked retrieval.
RETRIEVAL_GATE_TIMEOUT_S = 8.0
_GATE_POLL_S = 0.05


def is_voice_active() -> bool:
    """True while >=1 on-box voice stream (STT bridge or streaming TTS) is open."""
    return _voice_depth > 0


@_asynccontextmanager
async def voice_session():
    """Hold for the FULL duration of an on-box voice stream. While held,
    retrieval_gate() callers wait. Re-entrant via a depth counter (a duplex voice
    turn may briefly overlap listen+speak)."""
    global _voice_depth
    _voice_depth += 1
    try:
        yield
    finally:
        _voice_depth -= 1
        if _voice_depth < 0:
            _voice_depth = 0


@_asynccontextmanager
async def retrieval_gate(*, timeout: float | None = RETRIEVAL_GATE_TIMEOUT_S):
    """Await until no on-box voice stream is open, then yield. Wrap every
    localstack retrieval-group dispatch (:9098 embeddings/rerank) in this.

    timeout=None  -> wait indefinitely.
    timeout=<s>   -> raise asyncio.TimeoutError once the ceiling passes, so the
                     caller degrades (vector-less mint / keyword fallback /
                     un-reranked) rather than deadlock behind a long voice call.
    """
    deadline = None if timeout is None else _time.monotonic() + timeout
    while is_voice_active():
        if deadline is not None and _time.monotonic() >= deadline:
            raise _asyncio.TimeoutError(
                "on-box voice session held the retrieval group past the gate timeout")
        await _asyncio.sleep(_GATE_POLL_S)
    yield
```

4. Run, expect PASS:
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_local_stack_serialization.py -v`
   - Expected: 4 passed.

5. Commit:
```bash
git add Orchestrator/local_stack.py Orchestrator/tests/test_local_stack_serialization.py
git commit -m "feat(local-stack): D12 voice_session/retrieval_gate serialization primitive"
```

---

### Task 5.4: On-box batch transcribe (`_onbox_transcribe`) + 429 retry-with-backoff

Batch `file_transcribe` gains an on-box branch posting to the **proxied** llama-swap `/v1/audio/transcriptions` (`:9098`, so drain/TTL accounting sees it), body `model` = the Speaches batch model id. Per-capability 429 contract (correction [28]): batch translates a llama-swap 429 into retry-with-backoff.

**Files:**
- Modify: `Orchestrator/stt/file_transcribe.py` (add `import time`; add `onbox` dispatch at line 33-34; add `_onbox_transcribe`)
- Test: `Orchestrator/tests/test_stt_file_transcribe.py` (append cases)

**Steps:**

1. Append a failing test to `Orchestrator/tests/test_stt_file_transcribe.py`:
```python
def test_onbox_transcribe_posts_to_9098_with_model(monkeypatch):
    from Orchestrator.stt import file_transcribe as ft
    from Orchestrator import local_stack
    monkeypatch.setattr(local_stack, "base_url", lambda: "http://127.0.0.1:9098/v1")
    monkeypatch.setattr(local_stack, "stt_batch_model", lambda: "Systran/faster-whisper-large-v3")
    captured = {}

    class _Resp:
        status_code = 200
        def json(self): return {"text": " hello "}

    def _post(url, **kw):
        captured["url"] = url
        captured["model"] = kw["data"]["model"]
        return _Resp()

    monkeypatch.setattr(ft.requests, "post", _post)
    out = ft.transcribe_bytes(b"RIFF...", "audio/wav", provider="onbox", filename="a.wav")
    assert out == "hello"
    assert captured["url"] == "http://127.0.0.1:9098/v1/audio/transcriptions"
    assert captured["model"] == "Systran/faster-whisper-large-v3"


def test_onbox_transcribe_retries_on_429(monkeypatch):
    from Orchestrator.stt import file_transcribe as ft
    from Orchestrator import local_stack
    monkeypatch.setattr(local_stack, "base_url", lambda: "http://127.0.0.1:9098/v1")
    monkeypatch.setattr(local_stack, "stt_batch_model", lambda: "m")
    monkeypatch.setattr(ft.time, "sleep", lambda *_: None)  # no real backoff wait
    calls = {"n": 0}

    class _Resp:
        def __init__(self, code, text=""):
            self.status_code = code
            self._t = text
        def json(self): return {"text": self._t}

    def _post(url, **kw):
        calls["n"] += 1
        return _Resp(429) if calls["n"] < 3 else _Resp(200, "done")

    monkeypatch.setattr(ft.requests, "post", _post)
    assert ft.transcribe_bytes(b"x", "audio/wav", provider="onbox") == "done"
    assert calls["n"] == 3   # two 429s then success


def test_onbox_transcribe_raises_after_429_exhaustion(monkeypatch):
    import pytest
    from Orchestrator.stt import file_transcribe as ft
    from Orchestrator import local_stack
    monkeypatch.setattr(local_stack, "base_url", lambda: "http://127.0.0.1:9098/v1")
    monkeypatch.setattr(local_stack, "stt_batch_model", lambda: "m")
    monkeypatch.setattr(ft.time, "sleep", lambda *_: None)

    class _Resp:
        status_code = 429
        text = "busy"
        def json(self): return {"error": "busy"}

    monkeypatch.setattr(ft.requests, "post", lambda url, **kw: _Resp())
    with pytest.raises(RuntimeError):
        ft.transcribe_bytes(b"x", "audio/wav", provider="onbox")
```

2. Run, expect FAIL (`onbox` unrouted → falls to `_openai_transcribe` and raises "OPENAI_API_KEY not configured"; `ft.time` missing):
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_stt_file_transcribe.py -v -k onbox`
   - Expected: FAIL — `AttributeError: module 'Orchestrator.stt.file_transcribe' has no attribute 'time'` / wrong routing.

3. In `Orchestrator/stt/file_transcribe.py`, add `import time` under `import json` (after line 15) and module-level 429 constants after the imports (after line 20):
```python
import time
```
```python
# Batch 429 contract (correction [28]): a llama-swap concurrency 429 becomes
# retry-with-backoff (capped) for the non-realtime path.
_ONBOX_429_RETRIES = 3
_ONBOX_429_BACKOFF_BASE = 0.5
_ONBOX_429_BACKOFF_MAX = 4.0
```

4. Add the `onbox` dispatch inside `transcribe_bytes` — insert immediately before the `if provider == "local":` line (line 33):
```python
    if provider == "onbox":
        return _onbox_transcribe(audio_bytes, content_type, filename)
```

5. Add `_onbox_transcribe` after `_local_transcribe` (after line 105):
```python
def _onbox_transcribe(audio_bytes: bytes, content_type: str, filename: str) -> str:
    """Transcribe via the on-box Speaches member through the llama-swap front door
    (:9098 /v1/audio/transcriptions). PROXIED (not direct-to-:9099) so llama-swap's
    drain/TTL accounting sees the batch request. body model = the on-box batch STT
    model id. A llama-swap 429 (concurrency limit) is retried with capped backoff."""
    from Orchestrator import local_stack
    base = local_stack.base_url()          # http://127.0.0.1:9098/v1
    model = local_stack.stt_batch_model()
    attempts = 0
    while True:
        files = {"file": (filename, audio_bytes, content_type)}
        r = requests.post(f"{base}/audio/transcriptions",
                          data={"model": model}, files=files, timeout=120)
        if r.status_code == 429 and attempts < _ONBOX_429_RETRIES:
            attempts += 1
            time.sleep(min(_ONBOX_429_BACKOFF_BASE * (2 ** (attempts - 1)), _ONBOX_429_BACKOFF_MAX))
            continue
        break
    if r.status_code != 200:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"On-box STT error ({r.status_code}): {detail}")
    try:
        return (r.json().get("text") or "").strip()
    except Exception:
        return r.text.strip()
```

6. Run, expect PASS:
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_stt_file_transcribe.py -v -k onbox`
   - Expected: 3 passed.

7. Commit:
```bash
git add Orchestrator/stt/file_transcribe.py Orchestrator/tests/test_stt_file_transcribe.py
git commit -m "feat(stt): on-box batch transcribe via :9098 proxy with 429 backoff"
```

---

### Task 5.5: Streaming `_onbox_bridge` + D10 loading affordance + `voice_session` hold

`/ws/stt` gains an `onbox` dispatch → `_onbox_bridge`, cloned from `_local_bridge` but connecting **direct to the pinned Speaches `:9099/v1/realtime`** (Design B). Before connecting it **warms the audio group** via the `:9098` `/upstream/speaches/health` passthrough, emitting a D10 `stt_status {state:"loading_models"}` frame with a ~30s ceiling → honest `stt_error` (never a silent provider switch). The whole stream runs inside `local_stack.voice_session()` so retrieval-group dispatch serializes behind it (D12). All existing mechanics carry over (24 kHz resample, trailing-silence stop, hallucination filter, `stt_done`). Realtime 429 contract (correction [28]): a busy/unready warm probe keeps the affordance up to the ceiling, then an honest error — never a raw 429 to the client, never a cloud switch.

**Files:**
- Modify: `Orchestrator/routes/stt_ws_routes.py` (import `onbox_stt_available` at line 68; pass `onbox_ok` at line 127-128; add `onbox` dispatch in `run_stt_bridge` at line 170-171; add `_warm_audio_group`, `_probe_speaches_health`, `_relay_realtime`, `_onbox_bridge`; document the new `stt_status` frame in the endpoint docstring)
- Test: `Orchestrator/tests/test_onbox_stt_bridge.py` (Create)

**Steps:**

1. Create failing test `Orchestrator/tests/test_onbox_stt_bridge.py` (mirrors `test_stt_ws_reconnect.py`'s fake-WS idiom):
```python
"""Unit tests for the on-box (Design-B) streaming STT bridge.

Drives _onbox_bridge directly with a scripted fake client WebSocket and a fake
Speaches upstream. No network, no real sleeps: the warm probe and websockets
module are monkeypatched. Mirrors test_stt_ws_reconnect.py's asyncio.run idiom.
"""
import asyncio
import json

from fastapi import WebSocketDisconnect

from Orchestrator.routes import stt_ws_routes


class _FakeUpstream:
    def __init__(self, frames):
        self._frames = [json.dumps(f) for f in frames]
        self.sent = []
        self.closed = 0
    def __aiter__(self):
        return self
    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)
    async def send(self, data):
        self.sent.append(data)
    async def close(self, *a, **k):
        self.closed += 1


class _FakeWsModule:
    def __init__(self, sock):
        self._sock = sock
        self.connect_calls = []
    async def connect(self, url, **kwargs):
        self.connect_calls.append(url)
        return self._sock


class _FakeClientWS:
    def __init__(self, frames):
        self._frames = list(frames)
        self._i = 0
        self.sent = []
        self.closed = 0
    async def accept(self):
        pass
    async def receive_json(self):
        if self._i >= len(self._frames):
            raise WebSocketDisconnect()
        f = self._frames[self._i]; self._i += 1
        return f
    async def send_json(self, obj):
        self.sent.append(obj)
    async def close(self, *a, **k):
        self.closed += 1


def _patch_localstack(monkeypatch):
    from Orchestrator import local_stack
    monkeypatch.setattr(local_stack, "stt_stream_model", lambda: "turbo")
    monkeypatch.setattr(local_stack, "speaches_realtime_ws_url",
                        lambda m, **k: "ws://127.0.0.1:9099/v1/realtime?model=turbo")
    monkeypatch.setattr(local_stack, "speaches_warm_url",
                        lambda: "http://127.0.0.1:9098/upstream/speaches/health")


def test_onbox_bridge_emits_loading_affordance_then_relays_final(monkeypatch):
    async def scenario():
        _patch_localstack(monkeypatch)
        # Warm succeeds immediately.
        async def _warm_ok(url):
            return True
        monkeypatch.setattr(stt_ws_routes, "_probe_speaches_health", _warm_ok)
        upstream = _FakeUpstream([
            {"type": "conversation.item.input_audio_transcription.completed",
             "transcript": "hello on box"},
        ])
        monkeypatch.setattr(stt_ws_routes, "websockets", _FakeWsModule(upstream))
        client = _FakeClientWS([
            {"type": "stt_audio", "pcm": "AAAA"},
            {"type": "stt_stop"},
        ])
        await asyncio.wait_for(
            stt_ws_routes._onbox_bridge(client, target="prompt", lang="en", sample_rate=24000),
            timeout=10.0)
        return client, upstream
    client, upstream = asyncio.run(scenario())
    types = [m.get("type") for m in client.sent]
    assert types[0] == "stt_status" and client.sent[0]["state"] == "loading_models"
    finals = [m for m in client.sent if m.get("type") == "stt_final"]
    assert finals and finals[-1]["text"] == "hello on box"
    assert finals[-1]["target"] == "prompt"


def test_onbox_bridge_ceiling_yields_honest_error_no_switch(monkeypatch):
    async def scenario():
        _patch_localstack(monkeypatch)
        monkeypatch.setattr(stt_ws_routes, "_ONBOX_WARM_CEILING_S", 0.1)
        monkeypatch.setattr(stt_ws_routes, "_ONBOX_WARM_POLL_S", 0.02)
        async def _never(url):
            return False
        monkeypatch.setattr(stt_ws_routes, "_probe_speaches_health", _never)
        # If the bridge tried to connect anyway, this would blow up the test.
        class _Boom:
            async def connect(self, *a, **k):
                raise AssertionError("must NOT connect after warm ceiling")
        monkeypatch.setattr(stt_ws_routes, "websockets", _Boom())
        client = _FakeClientWS([{"type": "stt_audio", "pcm": "AAAA"}])
        await asyncio.wait_for(
            stt_ws_routes._onbox_bridge(client, target="prompt", lang="en", sample_rate=24000),
            timeout=10.0)
        return client
    client = asyncio.run(scenario())
    types = [m.get("type") for m in client.sent]
    assert "stt_status" in types                 # affordance was shown
    assert types[-1] == "stt_error"              # honest error, no cloud switch
    # never emitted a provider-switch / non-onbox final
    assert not any(m.get("type") == "stt_final" for m in client.sent)


def test_onbox_bridge_holds_voice_session_during_relay(monkeypatch):
    async def scenario():
        _patch_localstack(monkeypatch)
        from Orchestrator import local_stack
        seen = {"active": False}
        async def _warm_ok(url):
            return True
        monkeypatch.setattr(stt_ws_routes, "_probe_speaches_health", _warm_ok)

        class _Probe(_FakeUpstream):
            async def send(self, data):
                # while the bridge is streaming to Speaches, the voice session
                # must be held (retrieval_gate would block).
                if local_stack.is_voice_active():
                    seen["active"] = True
                await super().send(data)
        upstream = _Probe([
            {"type": "conversation.item.input_audio_transcription.completed",
             "transcript": "x"},
        ])
        monkeypatch.setattr(stt_ws_routes, "websockets", _FakeWsModule(upstream))
        client = _FakeClientWS([{"type": "stt_audio", "pcm": "AAAA"}, {"type": "stt_stop"}])
        await asyncio.wait_for(
            stt_ws_routes._onbox_bridge(client, target="prompt", lang="en", sample_rate=24000),
            timeout=10.0)
        return seen
    seen = asyncio.run(scenario())
    assert seen["active"] is True
```

2. Run, expect FAIL (`AttributeError: module ... has no attribute '_onbox_bridge'`):
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_onbox_stt_bridge.py -v`
   - Expected: FAIL — bridge + helpers not defined.

3. Extend the import at line 68 to include `onbox_stt_available`:
```python
from Orchestrator.stt.resolve import resolve_stt_provider, local_streaming_stt_available, onbox_stt_available
```

4. In `ws_stt`, pass `onbox_ok` into the resolver (replace lines 127-128):
```python
        provider = resolve_stt_provider(start.get("provider"),
                                        local_ok=local_streaming_stt_available(),
                                        onbox_ok=onbox_stt_available())
```

5. Add the `onbox` dispatch in `run_stt_bridge` — insert before the `elif provider == "local":` branch (line 170):
```python
    elif provider == "onbox":
        await _onbox_bridge(websocket, target=target, lang=lang, sample_rate=sample_rate)
```

6. Document the new downstream frame in the endpoint module docstring — after the `DOWN:` block (after line 15), add:
```python
#           {"type":"stt_status","state":"loading_models"}   (onbox warm affordance, additive)
```

7. Add the warm helpers + shared relay + `_onbox_bridge` immediately after `_local_bridge` (after line 516). Paste verbatim:
```python
# =============================================================================
# On-box (Design B) streaming STT — direct-to-:9099 Speaches with a D10 warm
# affordance and D12 voice-session serialization.
# =============================================================================

# D10: generous ceiling on warming the audio group before an honest error;
# NEVER a silent provider switch (§5.3 / D10).
_ONBOX_WARM_CEILING_S = 30.0
_ONBOX_WARM_POLL_S = 1.0


def _probe_speaches_health_sync(url: str) -> bool:
    """Blocking one-shot health GET (run in an executor). 200 = ready; a 429
    (concurrency), 5xx, or connection error = not ready yet. requests imported
    lazily so this module stays websockets-only at import time."""
    try:
        import requests
        r = requests.get(url, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


async def _probe_speaches_health(url: str) -> bool:
    """Async wrapper so tests can monkeypatch a single awaitable; runs the
    blocking GET off the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _probe_speaches_health_sync, url)


async def _warm_audio_group(websocket: WebSocket) -> bool:
    """D10 affordance: emit stt_status {state:'loading_models'} then warm the
    on-box audio group by GETting llama-swap's /upstream/speaches/health (:9098)
    until healthy. Returns True once warm; False if the ~30s ceiling passes (the
    caller then sends an honest stt_error — NEVER a silent provider switch). A
    busy (429) or not-yet-ready probe just keeps the affordance up until the
    ceiling (realtime 429 contract, correction [28])."""
    from Orchestrator import local_stack
    await websocket.send_json({"type": "stt_status", "state": "loading_models"})
    url = local_stack.speaches_warm_url()
    deadline = time.monotonic() + _ONBOX_WARM_CEILING_S
    while time.monotonic() < deadline:
        if await _probe_speaches_health(url):
            return True
        await asyncio.sleep(_ONBOX_WARM_POLL_S)
    print(f"[STT/WS] onbox warm ceiling ({_ONBOX_WARM_CEILING_S}s) exceeded — honest stt_error")
    return False


async def _relay_realtime(websocket: WebSocket, upstream_ws, *, target, sample_rate, label):
    """Shared Speaches /v1/realtime relay body (cloned from _local_bridge): client
    PCM -> resample-to-24k -> upstream; per-utterance finals -> client; ~0.7s
    trailing-silence stop; hallucination filter; drain-for-final with a 5s
    backstop. `label` tags the log/error lines ('onbox')."""
    stop_evt = asyncio.Event()
    stop_ts = {"v": None}

    async def client_to_upstream():
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            if mtype == "stt_audio":
                pcm_b64 = msg.get("pcm", "")
                if pcm_b64:
                    raw = _resample_pcm16(base64.b64decode(pcm_b64), sample_rate, 24000)
                    await upstream_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(raw).decode(),
                    }))
            elif mtype == "stt_stop":
                # Server-VAD auto-commits on a pause; feed ~0.7s trailing silence
                # to trigger the final-utterance cut (an explicit commit races the
                # socket close, per _local_bridge).
                silence = base64.b64encode(b"\x00\x00" * int(24000 * 0.7)).decode()
                await upstream_ws.send(json.dumps({
                    "type": "input_audio_buffer.append", "audio": silence}))
                stop_ts["v"] = time.monotonic()
                stop_evt.set()
                return

    async def upstream_to_client():
        try:
            async for raw in upstream_ws:
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                etype = event.get("type", "")
                if "error" in etype:
                    detail = (event.get("error") or {}).get("message") or json.dumps(event)[:300]
                    print(f"[STT/WS] {label} ERROR event: {json.dumps(event)[:500]}")
                    try:
                        await websocket.send_json({"type": "stt_error", "message": f"{label}: {detail}"})
                    except Exception:
                        pass
                    continue
                if etype != "conversation.item.input_audio_transcription.completed":
                    continue  # lifecycle events -- server emits finals only
                text = (event.get("transcript") or "").strip()
                if is_whisper_hallucination(text):
                    if stop_evt.is_set():
                        await _send_final(websocket, label,
                                          {"type": "stt_final", "text": "", "target": target}, stop_ts)
                        return
                    continue
                await _send_final(websocket, label,
                                  {"type": "stt_final", "text": text, "target": target}, stop_ts)
                if stop_evt.is_set():
                    return
        except websockets.ConnectionClosed:
            return

    pump = asyncio.ensure_future(client_to_upstream())
    relay = asyncio.ensure_future(upstream_to_client())
    try:
        done, _pending = await asyncio.wait({pump, relay}, return_when=asyncio.FIRST_COMPLETED)
        if pump in done and relay not in done:
            try:
                await asyncio.wait_for(relay, timeout=5.0)
            except asyncio.TimeoutError:
                relay.cancel()
                try:
                    await relay
                except (asyncio.CancelledError, WebSocketDisconnect):
                    pass
                except Exception:
                    pass
        elif relay in done:
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, WebSocketDisconnect):
                pass
            except Exception:
                pass
        for t in (pump, relay):
            if t.done() and not t.cancelled():
                exc = t.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                    raise exc
    finally:
        for t in (pump, relay):
            if not t.done():
                t.cancel()


async def _onbox_bridge(websocket: WebSocket, *, target, lang, sample_rate):
    """On-box Design-B streaming STT bridge. Warms the audio group through the
    llama-swap :9098 /upstream passthrough (with the D10 loading affordance),
    then connects DIRECT to the pinned Speaches :9099/v1/realtime for near-real-
    time latency. The whole stream runs inside local_stack.voice_session() so
    retrieval-group dispatch serializes behind it (D12). NEVER falls back to a
    cloud provider — an unmet warm ceiling is an honest stt_error."""
    from Orchestrator import local_stack
    async with local_stack.voice_session():
        if not await _warm_audio_group(websocket):
            await websocket.send_json({"type": "stt_error",
                                       "message": "on-box STT models still loading — please retry"})
            return
        model = local_stack.stt_stream_model()
        ws_url = local_stack.speaches_realtime_ws_url(model)
        upstream_ws = await websockets.connect(
            ws_url, open_timeout=10, ping_interval=20, ping_timeout=30,
            close_timeout=10, max_size=None,
        )
        try:
            print(f"[STT/WS] onbox connected model={model} rate={sample_rate}->24000 url={ws_url}")
            await _relay_realtime(websocket, upstream_ws,
                                  target=target, sample_rate=sample_rate, label="onbox")
        finally:
            try:
                await upstream_ws.close()
            except Exception:
                pass
```

8. Run, expect PASS:
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_onbox_stt_bridge.py -v`
   - Expected: 3 passed.

9. Run the existing STT WS suite to confirm no regression (the new `onbox_ok` kwarg is absorbed by the tests' `**kw`):
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_stt_ws.py Orchestrator/tests/test_stt_ws_reconnect.py Orchestrator/tests/test_stt_done.py -v`
   - Expected: all pass.

10. Commit:
```bash
git add Orchestrator/routes/stt_ws_routes.py Orchestrator/tests/test_onbox_stt_bridge.py
git commit -m "feat(stt): Design-B on-box streaming bridge with D10 warm affordance + D12 voice hold"
```

---

### Task 5.6: Gate the localstack embeddings provider (auto-mint degrades to vector-less)

Wrap the M3 `localstack` embeddings provider's `:9098` dispatch in `retrieval_gate()` with the bounded timeout, converting a timeout into `EmbeddingProviderError` — which the existing mint path (`embed_snapshot_for_index`'s `except Exception → {}`; `generate_embedding_sync`'s `except Exception → None`, verified in `Orchestrator/embeddings/search.py:212-221, 257-294`) already turns into a **vector-less mint** + watcher gap-heal. No new degradation path is invented; we only feed the existing one.

**Files:**
- Modify: `Orchestrator/embeddings/providers.py` (wrap the `localstack` provider's embed dispatch — the net-new provider class M3 added; confirm its exact method name against M3's landed code, typically `embed()` / `_embed_batch()`)
- Test: `Orchestrator/tests/test_embeddings_localstack_gate.py` (Create)

**Steps:**

1. Create failing test `Orchestrator/tests/test_embeddings_localstack_gate.py`:
```python
"""The localstack embeddings provider must serialize behind an open on-box voice
session (D12) and, if the bounded gate times out, raise EmbeddingProviderError so
the mint completes vector-less rather than deadlocking."""
import asyncio

import pytest

from Orchestrator import local_stack
from Orchestrator.embeddings.providers import EmbeddingProviderError, get_provider


def _localstack_provider():
    # M3 registers the on-box embedding slug; adjust the slug if M3's registry
    # entry differs. get_provider returns the localstack-backed provider.
    return get_provider("qwen3-embedding-8b-local")


def test_embed_raises_provider_error_when_gate_times_out(monkeypatch):
    async def scenario():
        monkeypatch.setattr(local_stack, "RETRIEVAL_GATE_TIMEOUT_S", 0.1)
        prov = _localstack_provider()
        async with local_stack.voice_session():          # gate can never open
            with pytest.raises(EmbeddingProviderError):
                await prov.embed(["hello"], "document")
    asyncio.run(scenario())


def test_embed_proceeds_when_no_voice_session(monkeypatch):
    # With no voice session the gate is open; stub the network so the test is
    # hermetic and asserts the gate does not block the happy path.
    async def scenario():
        prov = _localstack_provider()

        async def _fake_post(texts, purpose):        # replaces the real HTTP call
            return [[0.1, 0.2, 0.3] for _ in texts]
        # Adjust to M3's actual inner dispatch name (e.g. _post_embeddings).
        monkeypatch.setattr(prov, "_post_embeddings", _fake_post, raising=False)
        assert local_stack.is_voice_active() is False
    asyncio.run(scenario())
```

2. Run, expect FAIL (no gate around the dispatch → `embed()` does not raise under a held session; likely hangs then times out via the outer `asyncio.wait_for`-free call, or returns):
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_localstack_gate.py -v`
   - Expected: FAIL — `DID NOT RAISE EmbeddingProviderError`.

3. In `Orchestrator/embeddings/providers.py`, wrap the `localstack` provider's async embed dispatch (the HTTP `POST {base_url}/embeddings` M3 added). Insert the gate as the outermost `async with` around the network call, and convert a gate timeout into the provider's own error type:
```python
        # D12: serialize behind any open on-box voice stream. A bounded wait so a
        # long voice call degrades auto-mint to a vector-less mint (the caller's
        # except-EmbeddingProviderError -> {} path) instead of deadlocking.
        from Orchestrator import local_stack
        try:
            async with local_stack.retrieval_gate(timeout=local_stack.RETRIEVAL_GATE_TIMEOUT_S):
                # ... the existing localstack POST {base_url}/embeddings call ...
                return await self._post_embeddings(texts, purpose)
        except asyncio.TimeoutError:
            raise EmbeddingProviderError(
                f"{self.slug}: retrieval gate held by an on-box voice session")
```
   (Keep the real dispatch that M3 wrote inside the `async with`; the snippet shows the wrapper shape only. `import asyncio` at module top if not already present.)

4. Run, expect PASS:
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_localstack_gate.py -v`
   - Expected: 2 passed.

5. Run the mint suite to confirm the vector-less path still holds:
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_mint.py Orchestrator/tests/test_embeddings_mint_v2.py -v`
   - Expected: all pass.

6. Commit:
```bash
git add Orchestrator/embeddings/providers.py Orchestrator/tests/test_embeddings_localstack_gate.py
git commit -m "feat(embeddings): gate localstack embed behind voice session; timeout -> vector-less mint"
```

---

### Task 5.7: Gate the localstack rerank score path (timeout → un-reranked)

Wrap the M4 `localstack` rerank dispatch (`:9098 /v1/rerank`) in `retrieval_gate()`. On a gate timeout, return `None` — the `score()` contract already returns `None` on any failure and never raises (verified: `Orchestrator/rerank.py` `_scatter_relevance_scores` region ~697-724 and `score()`), so a voice-held reranker costs latency, never recall (§6 invariant).

**Files:**
- Modify: `Orchestrator/rerank.py` (wrap the `localstack` scoring branch M4 added — likely `_score_localstack` or the `score()` dispatch entry)
- Test: `Orchestrator/tests/test_rerank_localstack_gate.py` (Create)

**Steps:**

1. Create failing test `Orchestrator/tests/test_rerank_localstack_gate.py`:
```python
"""localstack rerank must serialize behind an open on-box voice session (D12) and,
if the bounded gate times out, return None (un-reranked) rather than raise."""
import asyncio

from Orchestrator import local_stack
from Orchestrator import rerank


def test_localstack_score_returns_none_when_gate_times_out(monkeypatch):
    async def scenario():
        monkeypatch.setattr(local_stack, "RETRIEVAL_GATE_TIMEOUT_S", 0.1)
        async with local_stack.voice_session():
            # Call the localstack scorer directly. Adjust the symbol to M4's
            # landed name (_score_localstack); it must be reachable + async.
            out = await rerank._score_localstack("q", ["doc a", "doc b"])
            return out
    assert asyncio.run(scenario()) is None
```

2. Run, expect FAIL (`AttributeError` if unwrapped, or the scorer hangs/returns a value instead of `None`):
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_rerank_localstack_gate.py -v`
   - Expected: FAIL.

3. In `Orchestrator/rerank.py`, wrap the localstack scoring dispatch with the gate and return `None` on timeout:
```python
    # D12: serialize behind any open on-box voice stream; a gate timeout means
    # un-reranked retrieval (score None), never an error (a dead reranker costs
    # latency, never recall — §6).
    from Orchestrator import local_stack
    try:
        async with local_stack.retrieval_gate(timeout=local_stack.RETRIEVAL_GATE_TIMEOUT_S):
            # ... the existing localstack POST {base_url}/rerank + _scatter_relevance_scores ...
            return await self._do_localstack_rerank(query, documents)
    except asyncio.TimeoutError:
        return None
```
   (Keep M4's real dispatch inside the `async with`; ensure `import asyncio` at module top.)

4. Run, expect PASS:
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_rerank_localstack_gate.py -v`
   - Expected: 1 passed.

5. Run the rerank suite for no regression:
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_rerank.py Orchestrator/tests/test_rerank_sidecar.py -v`
   - Expected: all pass.

6. Commit:
```bash
git add Orchestrator/rerank.py Orchestrator/tests/test_rerank_localstack_gate.py
git commit -m "feat(rerank): gate localstack score behind voice session; timeout -> un-reranked"
```

---

### Task 5.8: `speech_to_text` ToolVault enum — verify M0 Task 0.2 already landed it (NO-OP)

**The `local` + `onbox` provider-enum change is landed in M0 Task 0.2** (which runs FIRST in the execution order and creates `Orchestrator/toolvault/tests/test_speech_to_text_provider_enum.py`). This task does NOT re-edit `schema.json` — a second edit would be a no-op (the tokens are already present) and its commit would stage an unchanged file (empty commit). It exists only to VERIFY the enum is in place before the STT resolver (Tasks 5.1–5.5) starts routing the `onbox` token.

**Files:** none (the schema edit + its test belong to M0 Task 0.2).

**Steps:**

1. Verify the enum already carries both tokens (M0 landed it):
   - Run: `python -c "import json; e=json.load(open('ToolVault/tools/speech_to_text/schema.json'))['parameters']['properties']['provider']['enum']; assert {'local','onbox'} <= set(e), e; print('OK: local+onbox present', e)"`
   - Expected: `OK: local+onbox present [...]`. If it FAILS, M0 Task 0.2 was skipped — go land it there (do NOT edit the schema from M5).
2. Re-run M0's enum test to confirm it still passes:
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/toolvault/tests/test_speech_to_text_provider_enum.py -v`
   - Expected: passed.
3. No schema edit, no commit here (M0 Task 0.2 owns the change).

---

### Milestone 5 — closeout verification

Run the full set of files this milestone touched (fast, hermetic — no live service):
```bash
Orchestrator/venv/bin/python -m pytest \
  Orchestrator/tests/test_stt_resolve.py \
  Orchestrator/tests/test_local_stack_stt_locator.py \
  Orchestrator/tests/test_local_stack_serialization.py \
  Orchestrator/tests/test_stt_file_transcribe.py \
  Orchestrator/tests/test_onbox_stt_bridge.py \
  Orchestrator/tests/test_stt_ws.py \
  Orchestrator/tests/test_stt_ws_reconnect.py \
  Orchestrator/tests/test_stt_done.py \
  Orchestrator/tests/test_embeddings_localstack_gate.py \
  Orchestrator/tests/test_rerank_localstack_gate.py \
  Orchestrator/toolvault/tests/test_speech_to_text_provider_enum.py \
  -v
```
Expected: all pass. Then restart the service so the new routes/module are live (pre-authorized): `sudo systemctl restart blackbox.service`. `GET /local-models/status` (M1) should show STT wiring present; a live end-to-end voice-loop pass is a Phase-2 MS02 gate (G4/G6), not a dev-box step (this box stays cloud, §10).


---

## Milestone 6: The qwen-tts server (`LocalModels/qwen_tts_server/`)

**Depends on:** nothing (the server is self-contained). It has NO code dependency on other milestones; its runtime lifecycle (lean venv creation, `${QWEN_TTS_VENV}`, the `qwen-tts` llama-swap member, weights download) is provided by the installer/wizard milestones and consumes the env-var + `/upstream/qwen-tts/...` path contract this milestone documents (Task 6.8). M7 (Orchestrator `qwen:` routing + catalog + Voice Lab) consumes the HTTP surface built here.

**Goal.** Build the in-repo FastAPI server that exposes the three Qwen3-TTS 1.7B variants — Base (3s zero-shot clones), CustomVoice (9 presets, the hot path), VoiceDesign (text-described voices) — behind an OpenAI-compatible audio surface plus consent-gated cloning and 2-step voice design, per spec §5.4 and corrections [8]/[11]/[18]/[23]. It runs in its OWN lean venv and MUST NOT import `Orchestrator` (the MCP lean-venv lesson). A single in-process variant manager lazy-loads exactly one variant at a time under a FREE-BEFORE-LOAD mandate (drop refs → `gc.collect()` → `torch.cuda.empty_cache()` → verify free VRAM) and serializes all synthesis behind one `asyncio.Lock`. The entire API layer is CPU-testable with the model mocked; a separate manual GPU smoke (Task 6.9) validates the real model on MS02 for gate G3.

**Canonical facts this milestone locks in (do not deviate):** package `qwen_tts_server` under `LocalModels/qwen_tts_server/`; uvicorn entrypoint `qwen_tts_server.app:app` (matches the llama-swap template §8); catalog group id `qwen`; voice ids `qwen:<Voice>`; profiles at `Manifest/voices/qwen/{slug}/profile.json`; the 9 presets are `Vivian, Serena, Uncle_Fu, Dylan, Eric, Ryan, Aiden, Ono_Anna, Sohee`; sample rate is READ FROM THE MODEL OUTPUT, never hardcoded 24kHz (correction [23]); `stream:true` ships the StreamingResponse-over-full-generation fallback with true chunked streaming behind a default-OFF G3 feature flag (correction [8]); clone/design are non-OpenAI paths the Orchestrator reaches via `/upstream/qwen-tts/...` (correction [18]); the clone consent gate mirrors `elevenlabs_routes.py:112` exactly (422 unless `consent == "true"`, correction [11]).

**Test convention (verified).** Backend tests live under `Orchestrator/tests/` and run via `Orchestrator/venv/bin/python -m pytest` (repo `pytest.ini`: `testpaths = Orchestrator/tests`, root `conftest.py` puts the repo root on `sys.path`). The Orchestrator venv already has `fastapi 0.118.0`, `httpx 0.28.1`, `python-multipart 0.0.9`, and `pytest` — everything the mocked API-layer tests need. Because `qwen_tts_server` lives under `LocalModels/` (not on the default path), each qwen test file inserts `LocalModels/` onto `sys.path` at its top. The server's own heavy deps (`torch`, `transformers`, `soundfile`, `numpy`, the streaming fork) are imported LAZILY inside the variant manager, so the API/control tests never touch CUDA and run on this no-GPU dev box. **Decision I made (spec left the test location open):** API-layer tests live in `Orchestrator/tests/` (the repo's discoverable pytest convention) rather than a separate per-venv suite; the server code stays lean (the test imports the server, never the reverse).

---

### Task 6.1: Package skeleton — settings, profile store, requirements

**Files:**
- Create: `LocalModels/qwen_tts_server/__init__.py`
- Create: `LocalModels/qwen_tts_server/settings.py`
- Create: `LocalModels/qwen_tts_server/profile_store.py`
- Create: `LocalModels/qwen_tts_server/requirements.txt`
- Test: `Orchestrator/tests/test_qwen_tts_profile_store.py`

1. Create `LocalModels/qwen_tts_server/__init__.py`:

```python
"""qwen-tts — in-repo Qwen3-TTS server (§5.4). STANDALONE: never import Orchestrator."""
__version__ = "1.0"
```

2. Create `LocalModels/qwen_tts_server/settings.py`:

```python
"""Static config + env-driven paths for the qwen-tts server.

STANDALONE: this package MUST NOT import Orchestrator (own lean venv — the MCP
lean-venv lesson). Every cross-process wire (venv, model dir, voices dir, the
G3 streaming flag) arrives via environment variables set on the llama-swap
member's process environment (see README, Task 6.8, for the installer contract).
"""
import os
from pathlib import Path

# The 9 Qwen3-TTS CustomVoice presets (design spec §5.4 / §14 — verified).
PRESET_VOICES = (
    "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric",
    "Ryan", "Aiden", "Ono_Anna", "Sohee",
)

# In-process variant identifiers.
VARIANT_CUSTOM_VOICE = "custom_voice"   # 9 presets — the hot path
VARIANT_BASE = "base"                   # 3-second zero-shot clones
VARIANT_VOICE_DESIGN = "voice_design"   # text-described voices
VARIANTS = (VARIANT_CUSTOM_VOICE, VARIANT_BASE, VARIANT_VOICE_DESIGN)

MIN_CLONE_SECONDS = 3.0   # Base zero-shot cloning reference minimum (§5.4)


def _root() -> Path:
    # BLACKBOX_ROOT set by the unit; else infer the repo root from this file.
    env = os.environ.get("BLACKBOX_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[2]


def voices_dir() -> Path:
    env = os.environ.get("QWEN_TTS_VOICES_DIR")
    return Path(env) if env else _root() / "Manifest" / "voices" / "qwen"


def model_dir() -> Path:
    env = os.environ.get("QWEN_TTS_MODEL_DIR")
    return Path(env) if env else _root() / "LocalModels" / "weights" / "qwen3-tts"


def streaming_enabled() -> bool:
    """G3-gated TRUE chunked streaming (§5.4). Default OFF — ships the
    StreamingResponse-over-full-generation fallback (correction [8])."""
    return os.environ.get("QWEN_TTS_STREAMING", "0").strip().lower() in ("1", "true", "yes", "on")


def min_free_vram_mb() -> int:
    """Free-VRAM floor asserted before allocating the next variant (§5.4)."""
    try:
        return int(os.environ.get("QWEN_TTS_MIN_FREE_MB", "5000"))
    except ValueError:
        return 5000
```

3. Create `LocalModels/qwen_tts_server/profile_store.py`:

```python
"""Voice-profile persistence for the qwen-tts server.

Layout (§5.4): Manifest/voices/qwen/{slug}/
    profile.json     — name, slug, variant, operator, consent record, created,
                       sample_rate, ref_audio filename (clone) / design params
    reference.<ext>  — the cloning reference audio (Base variant only)

Atomic writes (tmp file + os.replace + fsync). Never in git (Manifest/ is
gitignored). STANDALONE — stdlib only, no Orchestrator import.
"""
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import settings

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def sanitize_slug(name: str) -> str:
    """Path-safe slug from a display name: lowercase, non-alnum runs -> '-',
    collapse, trim, clamp 64. Raises ValueError on an empty result. Any path
    separator or '..' collapses to '-' so traversal is impossible."""
    base = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    base = base[:64].strip("-")
    if not base:
        raise ValueError("name did not yield a usable slug")
    return base


def unique_slug(name: str) -> str:
    """sanitize_slug + numeric suffix on directory collision (-2, -3, ...)."""
    base = sanitize_slug(name)
    root = settings.voices_dir()
    slug, n = base, 2
    while (root / slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _atomic_write(path: Path, write_fn) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            write_fn(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_json(path: Path, data: dict) -> None:
    _atomic_write(path, lambda f: f.write(json.dumps(data, indent=2).encode("utf-8")))


def save_clone_profile(slug, name, operator, consent, ref_bytes, ref_filename, sample_rate=None):
    d = settings.voices_dir() / slug
    d.mkdir(parents=True, exist_ok=True)
    ext = os.path.splitext(ref_filename or "")[1] or ".wav"
    ref_name = f"reference{ext}"
    _atomic_write(d / ref_name, lambda f: f.write(ref_bytes))
    profile = {
        "slug": slug, "name": name, "variant": settings.VARIANT_BASE,
        "operator": operator, "consent": bool(consent),
        "consent_recorded_at": datetime.now(timezone.utc).isoformat(),
        "created": datetime.now(timezone.utc).isoformat(),
        "ref_audio": ref_name, "sample_rate": sample_rate,
    }
    _write_json(d / "profile.json", profile)
    return profile


def save_design_profile(slug, name, operator, description, design_params):
    d = settings.voices_dir() / slug
    d.mkdir(parents=True, exist_ok=True)
    profile = {
        "slug": slug, "name": name, "variant": settings.VARIANT_VOICE_DESIGN,
        "operator": operator, "description": description, "design": design_params,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(d / "profile.json", profile)
    return profile


def get_profile(slug):
    p = settings.voices_dir() / slug / "profile.json"
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def list_profiles():
    root = settings.voices_dir()
    if not root.exists():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if child.is_dir():
            prof = get_profile(child.name)
            if prof:
                out.append(prof)
    return out


def ref_audio_path(slug):
    prof = get_profile(slug)
    if not prof or not prof.get("ref_audio"):
        return None
    p = settings.voices_dir() / slug / prof["ref_audio"]
    return str(p) if p.exists() else None


def delete_profile(slug) -> bool:
    d = settings.voices_dir() / slug
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False
```

4. Create `LocalModels/qwen_tts_server/requirements.txt` (the lean venv's deps — installed by the installer milestone; torch is the CUDA build there, unpinned here):

```
fastapi
uvicorn[standard]
python-multipart
soundfile
numpy
torch
transformers
# Qwen3-TTS streaming fork (kunzite-app/Qwen3-TTS-streaming) providing
# stream_generate_pcm() (§5.4). Pinned to a specific commit at install time;
# the exact call signatures are confirmed on MS02 during G3 (Task 6.9).
```

5. Write the failing test `Orchestrator/tests/test_qwen_tts_profile_store.py`:

```python
"""Unit tests for the qwen-tts profile store — pure stdlib, no FastAPI, no model.

Isolation: QWEN_TTS_VOICES_DIR points at a tmp dir per test (never the real
Manifest/voices/qwen). Same isolation recipe as the embeddings-route tests.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "LocalModels"))

import json

import pytest

from qwen_tts_server import profile_store


@pytest.fixture
def voices(tmp_path, monkeypatch):
    d = tmp_path / "qwen"
    monkeypatch.setenv("QWEN_TTS_VOICES_DIR", str(d))
    return d


def test_sanitize_slug_basic():
    assert profile_store.sanitize_slug("My Cool Voice") == "my-cool-voice"


def test_sanitize_slug_strips_traversal():
    slug = profile_store.sanitize_slug("../../etc/passwd")
    assert "/" not in slug and ".." not in slug
    assert slug == "etc-passwd"


def test_sanitize_slug_empty_raises():
    with pytest.raises(ValueError):
        profile_store.sanitize_slug("///")


def test_unique_slug_suffixes_on_collision(voices):
    (voices / "brandon").mkdir(parents=True)
    assert profile_store.unique_slug("Brandon") == "brandon-2"


def test_save_clone_profile_persists(voices):
    prof = profile_store.save_clone_profile(
        "brandon", "Brandon", "system", True, b"RIFFfake", "ref.wav", sample_rate=22050
    )
    assert prof["variant"] == "base"
    on_disk = json.loads((voices / "brandon" / "profile.json").read_text())
    assert on_disk["consent"] is True
    assert on_disk["sample_rate"] == 22050
    assert (voices / "brandon" / "reference.wav").read_bytes() == b"RIFFfake"


def test_atomic_write_leaves_no_tmp(voices):
    profile_store.save_design_profile("d1", "D1", "system", "warm", {"seed": 1})
    assert list((voices / "d1").glob(".*tmp")) == []


def test_list_and_get_profiles(voices):
    profile_store.save_design_profile("d1", "D1", "system", "warm", {"seed": 1})
    assert [p["slug"] for p in profile_store.list_profiles()] == ["d1"]
    assert profile_store.get_profile("d1")["name"] == "D1"
    assert profile_store.get_profile("nope") is None


def test_ref_audio_path_and_delete(voices):
    profile_store.save_clone_profile("b", "B", "system", True, b"aud", "r.wav")
    assert profile_store.ref_audio_path("b").endswith("/b/reference.wav")
    assert profile_store.delete_profile("b") is True
    assert profile_store.get_profile("b") is None
```

6. Run the test, expect FAIL (package not importable yet if step order slips, else collection error):

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_qwen_tts_profile_store.py -q`
   Expected: FAIL — the very first run before the modules exist errors with `ModuleNotFoundError: No module named 'qwen_tts_server'` (or an assertion failure if run out of order).

7. With steps 1–4 in place, run again, expect PASS:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_qwen_tts_profile_store.py -q`
   Expected: `8 passed` (all `test_*` functions green).

8. Commit:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && git add LocalModels/qwen_tts_server/__init__.py LocalModels/qwen_tts_server/settings.py LocalModels/qwen_tts_server/profile_store.py LocalModels/qwen_tts_server/requirements.txt Orchestrator/tests/test_qwen_tts_profile_store.py && git commit -m "feat(qwen-tts): package skeleton + atomic voice-profile store"`
   Expected: one commit, five files created.

---

### Task 6.2: Variant manager — lazy load, FREE-BEFORE-LOAD, single-flight lock

**Files:**
- Create: `LocalModels/qwen_tts_server/variant_manager.py`
- Test: `Orchestrator/tests/test_qwen_tts_variant_manager.py`

1. Create `LocalModels/qwen_tts_server/variant_manager.py`:

```python
"""In-process manager for the three Qwen3-TTS 1.7B variants (§5.4).

ONE process, three variants, exactly ONE resident at a time. FREE-BEFORE-LOAD is
mandatory: drop refs -> gc.collect() -> empty CUDA cache -> VERIFY free VRAM
before allocating the next variant. llama-swap budgets at the PROCESS level and
cannot see an intra-process old+new balloon (~5-6GB each at the mid-swap peak),
so a naive load-then-drop would OOM the 16,380 MiB card. ALL synthesis + swaps
serialize behind one asyncio.Lock so two variants never load concurrently.

The heavy model ops live behind a `backend` object. The default TorchQwenBackend
imports torch + the streaming fork LAZILY (only when a variant is loaded), so
this module imports cleanly and the API/control tests run on a no-GPU box. Tests
inject a fake backend; the real model NEVER loads in CI.
"""
import asyncio
import gc
import logging

from . import settings

log = logging.getLogger("qwen_tts.variant_manager")


class VramError(RuntimeError):
    """Free VRAM is below the safety floor after a free-before-load."""


class VariantManager:
    def __init__(self, backend=None, min_free_mb=None):
        self._backend = backend if backend is not None else TorchQwenBackend()
        self._min_free_mb = settings.min_free_vram_mb() if min_free_mb is None else min_free_mb
        self._current = None    # resident variant name
        self._handle = None     # backend model handle
        self._lock = asyncio.Lock()

    # -- FREE-BEFORE-LOAD -------------------------------------------------
    def _free_before_load(self):
        """Reclaim the resident variant's VRAM BEFORE the next allocation."""
        if self._handle is not None:
            self._backend.free(self._handle)
        self._handle = None
        self._current = None
        gc.collect()
        self._backend.empty_cache()
        free = self._backend.free_vram_mb()
        if free is not None and free < self._min_free_mb:
            raise VramError(
                f"insufficient free VRAM after unload: {free}MB < {self._min_free_mb}MB floor"
            )

    def _ensure_locked(self, variant):
        """Make `variant` resident. Caller MUST hold self._lock."""
        if variant not in settings.VARIANTS:
            raise ValueError(f"unknown variant {variant!r}")
        if self._current == variant and self._handle is not None:
            return self._handle
        self._free_before_load()               # reclaim old FIRST — never old+new
        self._handle = self._backend.load(variant, settings.model_dir())
        self._current = variant
        log.info("qwen-tts: loaded variant %s", variant)
        return self._handle

    # -- public API -------------------------------------------------------
    async def synthesize_full(self, variant, text, *, preset=None, ref_audio=None, design_params=None):
        """Full (non-chunked) generation -> (pcm_s16le_bytes, sample_rate).
        sample_rate is READ FROM THE MODEL OUTPUT (correction [23])."""
        async with self._lock:
            handle = self._ensure_locked(variant)
            return await asyncio.to_thread(
                self._backend.synth, handle, text,
                preset=preset, ref_audio=ref_audio, design_params=design_params,
            )

    async def stream_true(self, variant, text, *, preset=None, ref_audio=None, design_params=None):
        """G3-gated TRUE chunked yield. Returns (sample_rate, async_iter[bytes]).
        The lock is held for the FULL stream duration (released in the async
        generator's finally — also on Starlette client-disconnect aclose)."""
        await self._lock.acquire()
        try:
            handle = self._ensure_locked(variant)
            sr = self._backend.sample_rate(handle)
        except BaseException:
            self._lock.release()
            raise

        async def _gen():
            try:
                for chunk in self._backend.synth_stream(
                    handle, text, preset=preset, ref_audio=ref_audio, design_params=design_params
                ):
                    yield chunk
            finally:
                self._lock.release()

        return sr, _gen()

    async def design_preview(self, description, text):
        """VoiceDesign preview -> list[{generated_voice_id, pcm, sr, params}]."""
        async with self._lock:
            handle = self._ensure_locked(settings.VARIANT_VOICE_DESIGN)
            return await asyncio.to_thread(self._backend.design_preview, handle, description, text)

    @property
    def current_variant(self):
        return self._current


def _float_to_pcm16(wavs) -> bytes:
    import numpy as np
    arr = np.asarray(wavs, dtype="float32").reshape(-1)
    arr = np.clip(arr, -1.0, 1.0)
    return (arr * 32767.0).astype("<i2").tobytes()


class TorchQwenBackend:
    """Real GPU backend. torch + the streaming fork are imported LAZILY inside
    load()/synth()/design_preview() so the module (and CPU-box tests) never need
    CUDA.

    NB: the exact fork call signatures (kunzite-app Qwen3-TTS-streaming
    stream_generate_pcm(), etc.) are CONFIRMED on MS02 in G3 (Task 6.9); the
    shapes below follow the documented fork API. The real model NEVER loads in
    CI — the API/control tests inject a fake backend."""

    def free_vram_mb(self):
        try:
            import torch
            if not torch.cuda.is_available():
                return None
            free, _total = torch.cuda.mem_get_info()
            return free // (1024 * 1024)
        except Exception:
            return None

    def empty_cache(self):
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def load(self, variant, model_dir):
        import torch  # noqa: F401  (ensures CUDA context is up)
        from qwen3_tts_streaming import load_variant  # fork API — confirm in G3
        return load_variant(str(model_dir), variant)

    def free(self, handle):
        try:
            handle.to("cpu")   # release VRAM; the caller drops the ref
        except Exception:
            pass

    def sample_rate(self, handle):
        return int(getattr(handle, "sample_rate", 0)) or None

    def synth(self, handle, text, *, preset=None, ref_audio=None, design_params=None):
        # READ sr FROM THE MODEL OUTPUT — never hardcode 24kHz (correction [23]).
        wavs, sr = handle.generate(text, preset=preset, ref_audio=ref_audio, design=design_params)
        return _float_to_pcm16(wavs), int(sr)

    def synth_stream(self, handle, text, *, preset=None, ref_audio=None, design_params=None):
        # kunzite-app stream_generate_pcm()-style KV-cache streamer (fork).
        # Yields pcm_s16le byte chunks (~3s initial buffer for Base clones, §5.4).
        for pcm_chunk in handle.stream_generate_pcm(
            text, preset=preset, ref_audio=ref_audio, design=design_params
        ):
            yield pcm_chunk

    def design_preview(self, handle, description, text):
        import uuid
        previews = []
        for wavs, sr, params in handle.design_previews(description, text):
            previews.append({
                "generated_voice_id": uuid.uuid4().hex,
                "pcm": _float_to_pcm16(wavs), "sr": int(sr), "params": params,
            })
        return previews
```

2. Write the failing test `Orchestrator/tests/test_qwen_tts_variant_manager.py`:

```python
"""Control-logic tests for the variant manager — a fake backend records call
order (free/empty_cache/load/synth) so we prove FREE-BEFORE-LOAD and the
single-flight lock WITHOUT torch/CUDA. The real model never loads."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "LocalModels"))

import asyncio

import pytest

from qwen_tts_server.variant_manager import VariantManager, VramError


class FakeBackend:
    def __init__(self, free_mb=8000):
        self.events = []
        self.free_mb = free_mb

    def load(self, variant, model_dir):
        self.events.append(("load", variant))
        return {"variant": variant, "sr": 22050}

    def free(self, handle):
        self.events.append(("free", handle["variant"]))

    def empty_cache(self):
        self.events.append(("empty_cache", None))

    def free_vram_mb(self):
        return self.free_mb

    def sample_rate(self, handle):
        return handle["sr"]

    def synth(self, handle, text, *, preset=None, ref_audio=None, design_params=None):
        self.events.append(("synth", handle["variant"]))
        return (b"\x00\x01" * 100, handle["sr"])


def _mgr(be, tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_TTS_MODEL_DIR", str(tmp_path))
    return VariantManager(backend=be, min_free_mb=5000)


def test_first_load_reclaims_then_loads(tmp_path, monkeypatch):
    be = FakeBackend()
    mgr = _mgr(be, tmp_path, monkeypatch)
    pcm, sr = asyncio.run(mgr.synthesize_full("custom_voice", "hi", preset="Vivian"))
    assert sr == 22050 and pcm
    assert be.events == [("empty_cache", None), ("load", "custom_voice"), ("synth", "custom_voice")]


def test_variant_transition_frees_before_load(tmp_path, monkeypatch):
    be = FakeBackend()
    mgr = _mgr(be, tmp_path, monkeypatch)

    async def scenario():
        await mgr.synthesize_full("custom_voice", "hi", preset="Vivian")
        be.events.clear()
        await mgr.synthesize_full("base", "hi", ref_audio="/x.wav")

    asyncio.run(scenario())
    # free(old) MUST precede load(new) — the whole point of FREE-BEFORE-LOAD.
    assert be.events == [
        ("free", "custom_voice"), ("empty_cache", None),
        ("load", "base"), ("synth", "base"),
    ]


def test_same_variant_no_reload(tmp_path, monkeypatch):
    be = FakeBackend()
    mgr = _mgr(be, tmp_path, monkeypatch)

    async def scenario():
        await mgr.synthesize_full("custom_voice", "a", preset="Vivian")
        be.events.clear()
        await mgr.synthesize_full("custom_voice", "b", preset="Serena")

    asyncio.run(scenario())
    assert be.events == [("synth", "custom_voice")]  # no free/load


def test_low_vram_raises(tmp_path, monkeypatch):
    be = FakeBackend(free_mb=1000)  # below the 5000 floor
    mgr = _mgr(be, tmp_path, monkeypatch)
    with pytest.raises(VramError):
        asyncio.run(mgr.synthesize_full("custom_voice", "hi", preset="Vivian"))


def test_unknown_variant_raises(tmp_path, monkeypatch):
    be = FakeBackend()
    mgr = _mgr(be, tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(mgr.synthesize_full("nope", "hi"))


def test_lock_serializes_concurrent_synths(tmp_path, monkeypatch):
    be = FakeBackend()
    mgr = _mgr(be, tmp_path, monkeypatch)

    async def scenario():
        await asyncio.gather(
            mgr.synthesize_full("custom_voice", "a", preset="Vivian"),
            mgr.synthesize_full("base", "b", ref_audio="/x.wav"),
        )

    asyncio.run(scenario())
    # Serialized: every ("load", X) is immediately followed by ("synth", X) with
    # no interleaved load/free from the other task.
    for i, ev in enumerate(be.events):
        if ev[0] == "load":
            assert be.events[i + 1] == ("synth", ev[1])
```

3. Run the test, expect FAIL first (module missing), then PASS after step 1:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_qwen_tts_variant_manager.py -q`
   Expected before impl: FAIL (`ModuleNotFoundError: No module named 'qwen_tts_server.variant_manager'`). After step 1: `6 passed`.

4. Commit:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && git add LocalModels/qwen_tts_server/variant_manager.py Orchestrator/tests/test_qwen_tts_variant_manager.py && git commit -m "feat(qwen-tts): variant manager with FREE-BEFORE-LOAD + single-flight lock"`
   Expected: one commit, two files.

---

### Task 6.3: FastAPI app shell — lifespan, `/health`, manager dependency

**Files:**
- Create: `LocalModels/qwen_tts_server/app.py`
- Test: `Orchestrator/tests/test_qwen_tts_server.py`

1. Create `LocalModels/qwen_tts_server/app.py`:

```python
"""qwen-tts — standalone FastAPI server exposing the three Qwen3-TTS 1.7B
variants behind an OpenAI-compatible audio surface, plus consent-gated cloning
and 2-step voice design (§5.4). Runs as the `qwen-tts` member of
blackbox-models.service's llama-swap front door. STANDALONE: no Orchestrator
import (own lean venv — the MCP lean-venv lesson).

Orchestrator (M7) path contract:
  * /health, /v1/audio/speech, /v1/audio/voices are OpenAI-shaped paths that
    llama-swap body-`model` auto-routes — the Orchestrator calls them at
    http://127.0.0.1:9098/v1/... (front door).
  * /v1/voices/clone, /v1/voices/design, /v1/voices/design/save are NON-OpenAI
    paths llama-swap does NOT auto-route (it extracts `model` only from known
    endpoints, open #245) — the Orchestrator MUST call them through
    /upstream/qwen-tts/v1/voices/... so the member auto-loads and group
    swap/exclusivity are honored (correction [18]). See README (Task 6.8).
"""
import base64
import io
import wave
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse

from . import profile_store, settings
from .variant_manager import VariantManager, VramError


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # torch-free construction — variants load lazily on first synth.
    app.state.manager = VariantManager()
    app.state.design_cache = {}   # generated_voice_id -> {description, params}
    yield


app = FastAPI(title="qwen-tts", version="1.0", lifespan=_lifespan)


def get_manager() -> VariantManager:
    mgr = getattr(app.state, "manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="qwen-tts manager not initialized")
    return mgr


@app.get("/health")
def health():
    """llama-swap checkEndpoint — STARTUP readiness ONLY (§6). Cheap; never
    loads a model (health is a one-time startup gate, never re-probed)."""
    return {"status": "ok"}
```

2. Write the failing test file `Orchestrator/tests/test_qwen_tts_server.py` (grows across Tasks 6.3–6.7; start with health + the shared fixtures/fakes):

```python
"""API-layer tests for the qwen-tts server. The variant manager is REPLACED by a
FakeManager via dependency_overrides so no torch/CUDA is touched — the real model
never loads. Voice profiles land in a tmp QWEN_TTS_VOICES_DIR."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "LocalModels"))

import io
import json
import wave

import pytest
from fastapi.testclient import TestClient

from qwen_tts_server.app import app, get_manager

# DISTINCTIVE non-24k rate — proves sample rate is read from the model output,
# not hardcoded 24000 (correction [23]).
SR = 16000


class FakeManager:
    def __init__(self):
        self.calls = []

    async def synthesize_full(self, variant, text, *, preset=None, ref_audio=None, design_params=None):
        self.calls.append(("synthesize_full", variant, preset, ref_audio, design_params))
        return (b"\x11\x22" * 50, SR)

    async def stream_true(self, variant, text, *, preset=None, ref_audio=None, design_params=None):
        self.calls.append(("stream_true", variant))

        async def _g():
            yield b"\x00\x00"

        return SR, _g()

    async def design_preview(self, description, text):
        self.calls.append(("design_preview", description, text))
        return [{"generated_voice_id": "gvid-1", "pcm": b"\x33\x44" * 10, "sr": SR, "params": {"seed": 7}}]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_TTS_VOICES_DIR", str(tmp_path / "qwen"))
    monkeypatch.delenv("QWEN_TTS_STREAMING", raising=False)  # G3 flag default OFF
    fake = FakeManager()
    app.dependency_overrides[get_manager] = lambda: fake
    with TestClient(app) as c:
        c.fake = fake
        c.voices_dir = tmp_path / "qwen"
        yield c
    app.dependency_overrides.clear()


def _wav_bytes(seconds, sr=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * int(sr * seconds))
    return buf.getvalue()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
```

3. Run, expect FAIL then PASS:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_qwen_tts_server.py -q`
   Expected before impl: FAIL (`ModuleNotFoundError: No module named 'qwen_tts_server.app'`). After step 1: `1 passed`.

4. Commit:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && git add LocalModels/qwen_tts_server/app.py Orchestrator/tests/test_qwen_tts_server.py && git commit -m "feat(qwen-tts): FastAPI app shell + /health checkEndpoint"`
   Expected: one commit, two files.

---

### Task 6.4: `POST /v1/audio/speech` — batch synthesis (wav/pcm) + voice resolution

**Files:**
- Modify: `LocalModels/qwen_tts_server/app.py` (append helpers + endpoint after `health()`, currently ending ~line 55)
- Modify: `Orchestrator/tests/test_qwen_tts_server.py` (append test cases)

1. Append the shared helpers + the batch endpoint to `app.py` (after the `health()` function):

```python
# ---- helpers ---------------------------------------------------------------
def _resolve_voice(voice: str):
    """(variant, synth_kwargs, resolved_id). Accepts a bare preset name,
    'qwen:<Preset>', or a saved profile slug. 422 if missing; 404 if unknown."""
    if not voice:
        raise HTTPException(status_code=422, detail="voice is required")
    v = voice.split(":", 1)[1] if voice.startswith("qwen:") else voice
    if v in settings.PRESET_VOICES:
        return settings.VARIANT_CUSTOM_VOICE, {"preset": v}, v
    prof = profile_store.get_profile(v)
    if prof is None:
        raise HTTPException(status_code=404, detail=f"unknown voice {voice!r}")
    variant = prof.get("variant")
    if variant == settings.VARIANT_BASE:
        ref = profile_store.ref_audio_path(v)
        if not ref:
            raise HTTPException(status_code=422, detail=f"voice {v!r} has no reference audio")
        return settings.VARIANT_BASE, {"ref_audio": ref}, v
    if variant == settings.VARIANT_VOICE_DESIGN:
        return settings.VARIANT_VOICE_DESIGN, {"design_params": prof.get("design")}, v
    raise HTTPException(status_code=422, detail=f"voice {v!r} has an unknown variant")


def _pcm_to_wav(pcm: bytes, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)   # sr FROM MODEL OUTPUT — never hardcoded (correction [23])
        w.writeframes(pcm)
    return buf.getvalue()


def _frame_iter(pcm: bytes, sr: int):
    """Chunk full PCM into 12Hz frames (tokenizer is Qwen3-TTS-Tokenizer-12Hz,
    §5.4) for the StreamingResponse-over-full-generation fallback (correction [8])."""
    samples_per_frame = max(1, sr // 12)
    step = samples_per_frame * 2   # int16
    for i in range(0, len(pcm), step):
        yield pcm[i:i + step]


# ---- endpoints -------------------------------------------------------------
@app.post("/v1/audio/speech")
async def audio_speech(body: dict = Body(...), mgr: VariantManager = Depends(get_manager)):
    """OpenAI-shaped {model, input, voice, response_format, stream}. `model` is
    consumed by llama-swap for routing; we synthesize `input` in `voice`.
    (The Orchestrator applies sanitize_for_speech BEFORE calling — §5.4 — so
    this server trusts `input`.) sr is read from the model output."""
    text = (body or {}).get("input")
    if not text or not str(text).strip():
        raise HTTPException(status_code=422, detail="input is required")
    response_format = (body or {}).get("response_format") or "wav"
    if response_format not in ("wav", "pcm"):
        raise HTTPException(status_code=400, detail="response_format must be 'wav' or 'pcm'")
    stream = bool((body or {}).get("stream"))
    variant, kwargs, _id = _resolve_voice((body or {}).get("voice"))

    if stream and settings.streaming_enabled():
        # G3-gated TRUE chunked streaming (OFF by default) — Task 6.5.
        try:
            sr, aiter = await mgr.stream_true(variant, str(text), **kwargs)
        except VramError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        return StreamingResponse(
            aiter, media_type="application/octet-stream",
            headers={"X-Sample-Rate": str(sr), "X-Audio-Format": "pcm_s16le"},
        )

    try:
        pcm, sr = await mgr.synthesize_full(variant, str(text), **kwargs)
    except VramError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    if stream:
        # Default shipping path: StreamingResponse OVER a full generation (correction [8]).
        return StreamingResponse(
            _frame_iter(pcm, sr), media_type="application/octet-stream",
            headers={"X-Sample-Rate": str(sr), "X-Audio-Format": "pcm_s16le"},
        )
    if response_format == "pcm":
        return Response(
            content=pcm, media_type="application/octet-stream",
            headers={"X-Sample-Rate": str(sr), "X-Audio-Format": "pcm_s16le"},
        )
    return Response(content=_pcm_to_wav(pcm, sr), media_type="audio/wav")
```

2. Append the failing test cases to `Orchestrator/tests/test_qwen_tts_server.py`:

```python
def test_speech_preset_wav_uses_model_sample_rate(client):
    r = client.post("/v1/audio/speech", json={"input": "hello", "voice": "qwen:Vivian"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    with wave.open(io.BytesIO(r.content), "rb") as w:
        assert w.getframerate() == SR   # NOT 24000 — read from the model output
    assert client.fake.calls[0][:3] == ("synthesize_full", "custom_voice", "Vivian")


def test_speech_bare_preset_name_ok(client):
    assert client.post("/v1/audio/speech", json={"input": "hi", "voice": "Serena"}).status_code == 200


def test_speech_pcm_format_sets_headers(client):
    r = client.post("/v1/audio/speech", json={"input": "hi", "voice": "qwen:Vivian", "response_format": "pcm"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["x-sample-rate"] == str(SR)
    assert r.headers["x-audio-format"] == "pcm_s16le"


def test_speech_missing_input_422(client):
    assert client.post("/v1/audio/speech", json={"voice": "qwen:Vivian"}).status_code == 422


def test_speech_missing_voice_422(client):
    assert client.post("/v1/audio/speech", json={"input": "hi"}).status_code == 422


def test_speech_unknown_voice_404(client):
    assert client.post("/v1/audio/speech", json={"input": "x", "voice": "qwen:Nope"}).status_code == 404


def test_speech_bad_format_400(client):
    r = client.post("/v1/audio/speech", json={"input": "hi", "voice": "qwen:Vivian", "response_format": "mp3"})
    assert r.status_code == 400
```

3. Run, expect FAIL first (endpoint missing → 404/405 on the new cases), then PASS:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_qwen_tts_server.py -q`
   Expected before impl: FAIL (new `test_speech_*` cases get 404/405). After step 1: `8 passed`.

4. Commit:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && git add LocalModels/qwen_tts_server/app.py Orchestrator/tests/test_qwen_tts_server.py && git commit -m "feat(qwen-tts): POST /v1/audio/speech batch synthesis + voice resolution"`
   Expected: one commit, two files.

---

### Task 6.5: `POST /v1/audio/speech` streaming — default fallback + G3 true-stream gate

**Depends on:** Task 6.4 (the streaming branch is already written in `audio_speech`; this task proves both the default-OFF fallback and the flag-ON path).

**Files:**
- Modify: `Orchestrator/tests/test_qwen_tts_server.py` (append streaming test cases — no `app.py` change; the branch shipped in 6.4)

1. Append the streaming test cases:

```python
def test_stream_true_flag_off_uses_full_generation_fallback(client):
    # stream:true with the G3 flag OFF -> StreamingResponse OVER a full
    # generation (correction [8]): synthesize_full runs, stream_true does NOT.
    r = client.post("/v1/audio/speech", json={"input": "hello", "voice": "qwen:Vivian", "stream": True})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["x-sample-rate"] == str(SR)
    assert r.headers["x-audio-format"] == "pcm_s16le"
    assert any(c[0] == "synthesize_full" for c in client.fake.calls)
    assert all(c[0] != "stream_true" for c in client.fake.calls)


def test_stream_fallback_frames_reassemble_to_full_pcm(client):
    # The framed body must equal the full PCM the manager produced (b"\x11\x22"*50).
    r = client.post("/v1/audio/speech", json={"input": "hi", "voice": "qwen:Vivian", "stream": True})
    assert r.content == b"\x11\x22" * 50


def test_stream_true_flag_on_uses_stream_true(client, monkeypatch):
    monkeypatch.setenv("QWEN_TTS_STREAMING", "1")   # G3 gate ON
    r = client.post("/v1/audio/speech", json={"input": "hello", "voice": "qwen:Vivian", "stream": True})
    assert r.status_code == 200
    assert r.headers["x-sample-rate"] == str(SR)
    assert any(c[0] == "stream_true" for c in client.fake.calls)
    assert all(c[0] != "synthesize_full" for c in client.fake.calls)
```

2. Run, expect PASS (the branch already exists; these lock the behavior):

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_qwen_tts_server.py -q -k stream`
   Expected: `3 passed` (and the full file: `11 passed`).

   Note on `settings.streaming_enabled()`: it reads the env FRESH per request, so `monkeypatch.setenv` inside a test flips the gate for that request without a restart — this is the intended runtime behavior (the installer sets `QWEN_TTS_STREAMING` on the member env only after G3 passes on MS02).

3. Commit:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && git add Orchestrator/tests/test_qwen_tts_server.py && git commit -m "test(qwen-tts): pin streaming fallback (default) + G3 true-stream gate"`
   Expected: one commit, one file.

---

### Task 6.6: `GET /v1/audio/voices` + `POST /v1/voices/clone` (consent gate + min-duration)

**Files:**
- Modify: `LocalModels/qwen_tts_server/app.py` (append the two endpoints + the duration helper)
- Modify: `Orchestrator/tests/test_qwen_tts_server.py` (append test cases)

1. Append to `app.py`:

```python
@app.get("/v1/audio/voices")
def audio_voices():
    """9 CustomVoice presets + saved clone/design profiles. Present only when the
    stack is healthy; the Orchestrator catalog (M7) fail-opens when it is not."""
    voices = [
        {"id": p, "name": p, "type": "preset", "variant": settings.VARIANT_CUSTOM_VOICE}
        for p in settings.PRESET_VOICES
    ]
    for prof in profile_store.list_profiles():
        voices.append({
            "id": prof.get("slug"), "name": prof.get("name"),
            "type": "clone" if prof.get("variant") == settings.VARIANT_BASE else "design",
            "variant": prof.get("variant"), "created": prof.get("created"),
        })
    return {"voices": voices}


def _audio_duration_seconds(data: bytes, filename):
    """Best-effort duration probe. WAV via stdlib wave; other containers via a
    lazy soundfile import. None if undeterminable — fail-open (a legit upload
    whose container we cannot parse is accepted, not rejected)."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        pass
    try:
        import soundfile as sf   # lazy; present in the qwen venv
        info = sf.info(io.BytesIO(data))
        return float(info.frames) / float(info.samplerate or 1)
    except Exception:
        return None


@app.post("/v1/voices/clone")
async def voices_clone(
    name: str = Form(...),
    file: UploadFile = File(...),
    consent: str = Form(...),
    operator: str = Form("system"),
):
    """Base zero-shot clone: persist the ~3s reference + name as a profile (no
    synthesis here — Base conditions on the stored reference at SPEAK time).
    CONSENT GATE mirrors elevenlabs_routes.py:112 EXACTLY — 422 without the
    literal "true", no work done (correction [11]). Reached by the Orchestrator
    via /upstream/qwen-tts/v1/voices/clone (correction [18])."""
    if consent != "true":
        raise HTTPException(status_code=422, detail="Voice cloning requires consent confirmation")
    data = await file.read()
    dur = _audio_duration_seconds(data, file.filename)
    if dur is not None and dur < settings.MIN_CLONE_SECONDS:
        raise HTTPException(
            status_code=422,
            detail=f"reference audio must be at least {settings.MIN_CLONE_SECONDS:g}s (got {dur:.1f}s)",
        )
    try:
        slug = profile_store.unique_slug(name)
    except ValueError:
        raise HTTPException(status_code=400, detail="name did not yield a usable slug")
    profile_store.save_clone_profile(
        slug, name, operator, True, data, file.filename or "reference.wav"
    )
    return {"voice_id": slug, "name": name}
```

2. Append the failing test cases:

```python
def test_voices_lists_nine_presets(client):
    voices = client.get("/v1/audio/voices").json()["voices"]
    ids = [v["id"] for v in voices]
    assert "Vivian" in ids and "Sohee" in ids
    assert len([v for v in voices if v["type"] == "preset"]) == 9


def test_clone_without_consent_422_no_profile(client):
    r = client.post(
        "/v1/voices/clone",
        data={"name": "Brandon", "consent": "false"},
        files={"file": ("ref.wav", _wav_bytes(4.0), "audio/wav")},
    )
    assert r.status_code == 422
    assert not (client.voices_dir / "brandon").exists()


def test_clone_too_short_422(client):
    r = client.post(
        "/v1/voices/clone",
        data={"name": "Brandon", "consent": "true"},
        files={"file": ("ref.wav", _wav_bytes(1.0), "audio/wav")},
    )
    assert r.status_code == 422


def test_clone_ok_persists_base_profile(client):
    r = client.post(
        "/v1/voices/clone",
        data={"name": "Brandon", "consent": "true", "operator": "Brandon"},
        files={"file": ("ref.wav", _wav_bytes(4.0), "audio/wav")},
    )
    assert r.status_code == 200 and r.json()["voice_id"] == "brandon"
    prof = json.loads((client.voices_dir / "brandon" / "profile.json").read_text())
    assert prof["variant"] == "base" and prof["consent"] is True and prof["operator"] == "Brandon"
    # the cloned voice now appears in the voices list as a clone
    listed = {v["id"]: v for v in client.get("/v1/audio/voices").json()["voices"]}
    assert listed["brandon"]["type"] == "clone"


def test_clone_name_traversal_sanitized(client):
    r = client.post(
        "/v1/voices/clone",
        data={"name": "../../etc/passwd", "consent": "true"},
        files={"file": ("ref.wav", _wav_bytes(4.0), "audio/wav")},
    )
    assert r.status_code == 200
    slug = r.json()["voice_id"]
    assert "/" not in slug and ".." not in slug
    assert (client.voices_dir / slug / "profile.json").exists()


def test_speech_with_cloned_voice_resolves_base(client):
    client.post(
        "/v1/voices/clone",
        data={"name": "Brandon", "consent": "true"},
        files={"file": ("ref.wav", _wav_bytes(4.0), "audio/wav")},
    )
    r = client.post("/v1/audio/speech", json={"input": "hi", "voice": "brandon"})
    assert r.status_code == 200
    call = client.fake.calls[-1]              # (kind, variant, preset, ref_audio, design)
    assert call[1] == "base" and call[3] is not None
```

3. Run, expect FAIL first (endpoints missing), then PASS:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_qwen_tts_server.py -q`
   Expected before impl: FAIL (new voices/clone cases 404). After step 1: `17 passed`.

4. Commit:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && git add LocalModels/qwen_tts_server/app.py Orchestrator/tests/test_qwen_tts_server.py && git commit -m "feat(qwen-tts): GET /v1/audio/voices + consent-gated /v1/voices/clone"`
   Expected: one commit, two files.

---

### Task 6.7: `POST /v1/voices/design` + `/v1/voices/design/save` (2-step preview→save)

**Files:**
- Modify: `LocalModels/qwen_tts_server/app.py` (append the two endpoints)
- Modify: `Orchestrator/tests/test_qwen_tts_server.py` (append test cases)

1. Append to `app.py`:

```python
@app.post("/v1/voices/design")
async def voices_design(body: dict = Body(...), mgr: VariantManager = Depends(get_manager)):
    """VoiceDesign step 1 — preview voices from a text description, mirroring the
    ElevenLabs design UX. No profile is created yet; the chosen preview is saved
    via .../design/save. Reached via /upstream/qwen-tts/v1/voices/design
    (correction [18]). Design params are cached in-process keyed by
    generated_voice_id so save can persist them."""
    description = (body or {}).get("voice_description")
    if not description:
        raise HTTPException(status_code=400, detail="voice_description is required")
    text = (body or {}).get("text") or "The quick brown fox jumps over the lazy dog."
    try:
        previews = await mgr.design_preview(str(description), str(text))
    except VramError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    out = []
    for p in previews:
        gid = p["generated_voice_id"]
        app.state.design_cache[gid] = {"description": description, "params": p.get("params")}
        out.append({
            "generated_voice_id": gid,
            "audio_b64": base64.b64encode(_pcm_to_wav(p["pcm"], p["sr"])).decode("ascii"),
            "sample_rate": p["sr"],
        })
    return {"previews": out}


@app.post("/v1/voices/design/save")
async def voices_design_save(body: dict = Body(...)):
    """VoiceDesign step 2 — persist a chosen preview as a real profile. Missing
    generated_voice_id or name -> 400 (mirrors elevenlabs_routes.py:186);
    unknown/expired generated_voice_id -> 404. Reached via
    /upstream/qwen-tts/v1/voices/design/save (correction [18])."""
    gid = (body or {}).get("generated_voice_id")
    name = (body or {}).get("name")
    if not gid or not name:
        raise HTTPException(status_code=400, detail="generated_voice_id and name are required")
    cached = app.state.design_cache.get(gid)
    if cached is None:
        raise HTTPException(status_code=404, detail="unknown or expired generated_voice_id")
    operator = (body or {}).get("operator") or "system"
    try:
        slug = profile_store.unique_slug(name)
    except ValueError:
        raise HTTPException(status_code=400, detail="name did not yield a usable slug")
    profile_store.save_design_profile(slug, name, operator, cached.get("description"), cached.get("params"))
    app.state.design_cache.pop(gid, None)
    return {"voice_id": slug}
```

2. Append the failing test cases:

```python
def test_design_missing_description_400(client):
    assert client.post("/v1/voices/design", json={}).status_code == 400


def test_design_preview_then_save_persists_design_profile(client):
    pr = client.post("/v1/voices/design", json={"voice_description": "warm calm narrator"})
    assert pr.status_code == 200
    preview = pr.json()["previews"][0]
    gid = preview["generated_voice_id"]
    assert preview["audio_b64"] and preview["sample_rate"] == SR
    assert client.fake.calls[-1][0] == "design_preview"

    sv = client.post("/v1/voices/design/save", json={"generated_voice_id": gid, "name": "Narrator"})
    assert sv.status_code == 200 and sv.json()["voice_id"] == "narrator"
    prof = json.loads((client.voices_dir / "narrator" / "profile.json").read_text())
    assert prof["variant"] == "voice_design" and prof["design"] == {"seed": 7}
    # design voice surfaces in the list with type "design"
    listed = {v["id"]: v for v in client.get("/v1/audio/voices").json()["voices"]}
    assert listed["narrator"]["type"] == "design"


def test_design_save_missing_name_400(client):
    assert client.post("/v1/voices/design/save", json={"generated_voice_id": "x"}).status_code == 400


def test_design_save_unknown_gid_404(client):
    r = client.post("/v1/voices/design/save", json={"generated_voice_id": "nope", "name": "X"})
    assert r.status_code == 404
```

3. Run, expect FAIL first, then PASS the WHOLE file:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_qwen_tts_server.py -q`
   Expected before impl: FAIL (design cases 404). After step 1: `21 passed`.

4. Run the full qwen suite to confirm nothing regressed across all three files:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_qwen_tts_profile_store.py Orchestrator/tests/test_qwen_tts_variant_manager.py Orchestrator/tests/test_qwen_tts_server.py -q`
   Expected: `35 passed` (8 profile-store + 6 variant-manager + 21 server).

5. Commit:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && git add LocalModels/qwen_tts_server/app.py Orchestrator/tests/test_qwen_tts_server.py && git commit -m "feat(qwen-tts): 2-step voice design (preview + save)"`
   Expected: one commit, two files.

---

### Task 6.8: README — M7 path contract + installer env-var contract

**Files:**
- Create: `LocalModels/qwen_tts_server/README.md`

1. Create `LocalModels/qwen_tts_server/README.md` (the cross-milestone contract; no code, no test — a documentation deliverable other milestones read):

```markdown
# qwen-tts server (`qwen_tts_server`)

Standalone FastAPI server for the three Qwen3-TTS 1.7B variants (design spec
§5.4). Runs as the `qwen-tts` llama-swap member of `blackbox-models.service`
behind the front door on `127.0.0.1:9098`. STANDALONE — it never imports
`Orchestrator` (own lean venv, per the MCP lean-venv lesson).

## Run

    ${QWEN_TTS_VENV}/bin/uvicorn qwen_tts_server.app:app --host 127.0.0.1 --port ${PORT}

(cwd = `LocalModels/`, or install the package editable into the venv so
`qwen_tts_server` resolves.) Matches the `qwen-tts` member cmd in
`installer/templates/llama-swap-config.yaml.template`.

## Environment contract (set by the installer on the member's process env)

| Var | Meaning | Default |
|---|---|---|
| `BLACKBOX_ROOT` | repo root (anchors the two dirs below) | inferred from the package path |
| `QWEN_TTS_MODEL_DIR` | dir holding the three variant checkpoints | `${BLACKBOX_ROOT}/LocalModels/weights/qwen3-tts` |
| `QWEN_TTS_VOICES_DIR` | clone/design profile store | `${BLACKBOX_ROOT}/Manifest/voices/qwen` |
| `QWEN_TTS_STREAMING` | G3 flag: `1` enables TRUE chunked streaming | `0` (ships the full-gen fallback) |
| `QWEN_TTS_MIN_FREE_MB` | free-VRAM floor asserted before loading the next variant | `5000` |

## HTTP surface + Orchestrator (M7) routing contract

OpenAI-shaped paths — llama-swap body-`model` auto-routes; the Orchestrator
calls them at `http://127.0.0.1:9098/v1/...`:

- `GET  /health` — llama-swap `checkEndpoint`; startup readiness only (never loads a model).
- `POST /v1/audio/speech` — `{model, input, voice, response_format, stream}`.
  - `voice`: a preset (`Vivian`…`Sohee`, optionally `qwen:`-prefixed) or a saved profile slug.
  - `response_format`: `wav` (default, `audio/wav`) or `pcm` (`application/octet-stream` + `X-Sample-Rate`/`X-Audio-Format: pcm_s16le`).
  - `stream:true`: streams `pcm_s16le` 12Hz frames (`X-Sample-Rate` header). Default = StreamingResponse OVER a full generation; TRUE chunked streaming only when `QWEN_TTS_STREAMING=1` (post-G3).
  - **Sample rate is read from the model output — never assume 24 kHz.** The Orchestrator applies `sanitize_for_speech` BEFORE calling; this server trusts `input`.
- `GET  /v1/audio/voices` — `{voices:[{id,name,type,variant,created?}]}` (9 presets + saved profiles).

Non-OpenAI paths llama-swap does NOT auto-route (open #245) — the Orchestrator
MUST call these through **`/upstream/qwen-tts/...`** so the member auto-loads
and group swap/exclusivity are honored:

- `POST /upstream/qwen-tts/v1/voices/clone` — multipart `{name, file, consent, operator?}`.
  **Consent gate:** 422 unless `consent == "true"` (mirrors `elevenlabs_routes.py:112`). Ref audio must be ≥ 3 s. Returns `{voice_id, name}`.
- `POST /upstream/qwen-tts/v1/voices/design` — `{voice_description, text?}` → `{previews:[{generated_voice_id, audio_b64, sample_rate}]}`.
- `POST /upstream/qwen-tts/v1/voices/design/save` — `{generated_voice_id, name, operator?}` → `{voice_id}` (400 missing field, 404 unknown/expired preview).

Catalog group id `qwen`; voice ids `qwen:<Voice>`; profiles at
`Manifest/voices/qwen/{slug}/`.

## Tests

    Orchestrator/venv/bin/python -m pytest \
      Orchestrator/tests/test_qwen_tts_profile_store.py \
      Orchestrator/tests/test_qwen_tts_variant_manager.py \
      Orchestrator/tests/test_qwen_tts_server.py

API/control tests run on CPU with the model mocked. GPU validation (gate G3) is
the manual `smoke_gpu.py` on MS02 (see that file).
```

2. Sanity-check the app still imports cleanly (guards against a stray edit):

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/LocalModels && ../Orchestrator/venv/bin/python -c "import qwen_tts_server.app as a; print(sorted(r.path for r in a.app.routes if getattr(r,'path','').startswith('/')))"`
   Expected: prints a list containing `/health`, `/v1/audio/speech`, `/v1/audio/voices`, `/v1/voices/clone`, `/v1/voices/design`, `/v1/voices/design/save`.

3. Commit:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && git add LocalModels/qwen_tts_server/README.md && git commit -m "docs(qwen-tts): M7 path contract + installer env-var contract"`
   Expected: one commit, one file.

---

### Task 6.9: Manual GPU smoke for MS02 (gate G3)

This is the MS02-only real-model validation feeding gate G3 (Qwen3-TTS RTF + first-packet latency + FREE-BEFORE-LOAD verification). It is NOT a CI test — it loads real checkpoints and needs the RTX 2000 Ada. It runs after the installer milestone has created the qwen venv + downloaded the three variant checkpoints on MS02.

**Files:**
- Create: `LocalModels/qwen_tts_server/smoke_gpu.py`

1. Create `LocalModels/qwen_tts_server/smoke_gpu.py`:

```python
#!/usr/bin/env python3
"""MANUAL GPU smoke for the qwen-tts variant manager — MS02 only, gate G3.

Loads each of the three real variants in turn through the SAME VariantManager
the server uses, synthesizes a short clip, and reports RTF + wall time + the
model's output sample rate. It exercises FREE-BEFORE-LOAD across the two
transitions and prints nvidia-smi free VRAM before/after each load so a reviewer
can confirm the old variant's VRAM was reclaimed before the next allocation.

NOT a pytest test (real checkpoints + CUDA). Run on MS02 with the qwen venv:

    QWEN_TTS_MODEL_DIR=/path/to/qwen3-tts \\
      ${QWEN_TTS_VENV}/bin/python -m qwen_tts_server.smoke_gpu

Feeds G3: RTF < ~0.9 -> 1.7B streams; else 0.6B streaming / 1.7B batch split.
Planning-time expectation (§5.4): 1.7B streaming near-certainly FAILS <0.9 on the
2000 Ada, so the streaming default is 0.6B-CustomVoice, 1.7B is the batch tier.
"""
import asyncio
import subprocess
import time

from qwen_tts_server import settings
from qwen_tts_server.variant_manager import VariantManager

TEXT = "The quick brown fox jumps over the lazy dog, twice, for a real timing sample."


def _free_mb():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            text=True,
        )
        return int(out.strip().splitlines()[0])
    except Exception as exc:
        return f"n/a ({exc})"


async def _run():
    mgr = VariantManager()
    plan = [
        (settings.VARIANT_CUSTOM_VOICE, {"preset": settings.PRESET_VOICES[0]}),
        (settings.VARIANT_BASE, {"ref_audio": None}),           # supply a real ~3s ref path on MS02
        (settings.VARIANT_VOICE_DESIGN, {"design_params": None}),  # supply a real design on MS02
    ]
    for variant, kwargs in plan:
        print(f"\n=== {variant} ===")
        print(f"free VRAM before load: {_free_mb()} MiB")
        t0 = time.perf_counter()
        try:
            pcm, sr = await mgr.synthesize_full(variant, TEXT, **kwargs)
        except Exception as exc:
            print(f"  synth failed (expected until real refs/design supplied): {exc}")
            print(f"  free VRAM after:  {_free_mb()} MiB (variant resident)")
            continue
        wall = time.perf_counter() - t0
        seconds_audio = (len(pcm) / 2) / float(sr or 1)
        rtf = wall / seconds_audio if seconds_audio else float("inf")
        print(f"  sample_rate (from model): {sr} Hz  (MUST NOT be assumed 24000)")
        print(f"  audio: {seconds_audio:.2f}s  wall: {wall:.2f}s  RTF: {rtf:.2f}")
        print(f"  free VRAM after load: {_free_mb()} MiB (variant resident)")
    print("\nG3 note: RTF < ~0.9 => that variant streams; else batch tier (§5.4).")


if __name__ == "__main__":
    asyncio.run(_run())
```

2. There is no automated Run/Expected on the dev box (no GPU). Manual MS02 procedure (documented for the Phase-2 gate runner):

   - Prereqs: qwen venv built, three variant checkpoints under `QWEN_TTS_MODEL_DIR`, and the streaming-fork import (`qwen3_tts_streaming.load_variant`) resolvable — confirm the real signatures and edit `TorchQwenBackend.load/synth/synth_stream/design_preview` in `variant_manager.py` if the fork's API differs (this is the deliberate G3 seam noted in that file).
   - Run: `ssh bbx@192.168.1.153` then `QWEN_TTS_MODEL_DIR=<weights> <qwen-venv>/bin/python -m qwen_tts_server.smoke_gpu`.
   - Expected: three variant blocks print a `sample_rate` that is the model's real rate (record it — it is NOT assumed 24 kHz), an RTF, and a `free VRAM after load` that returns to roughly the same level across variants (proving FREE-BEFORE-LOAD reclaimed the prior variant). Record RTF + first-packet numbers into `eval/results/` per the G3 gate; if 1.7B RTF ≥ ~0.9 the wizard/installer defaults 1.7B to the batch tier and 0.6B-CustomVoice to the streaming default (§5.4 / §7).

3. Commit:

   Run: `cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && git add LocalModels/qwen_tts_server/smoke_gpu.py && git commit -m "test(qwen-tts): manual GPU smoke for MS02 gate G3"`
   Expected: one commit, one file.

---

### Milestone 6 — done when

- `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_qwen_tts_profile_store.py Orchestrator/tests/test_qwen_tts_variant_manager.py Orchestrator/tests/test_qwen_tts_server.py -q` → `35 passed` on this no-GPU dev box (the real model never loads).
- `LocalModels/qwen_tts_server/` is a self-contained package (`__init__.py`, `settings.py`, `profile_store.py`, `variant_manager.py`, `app.py`, `requirements.txt`, `README.md`, `smoke_gpu.py`) with ZERO `Orchestrator` imports.
- The HTTP surface + `/upstream/qwen-tts/...` routing contract + the `QWEN_TTS_*` env contract are documented in the README for M7 and the installer milestone.
- FREE-BEFORE-LOAD, the single-flight `asyncio.Lock`, the consent gate (422), min-duration (422), sample-rate-from-output, and the default-OFF streaming fallback are all test-locked.
- G3's manual GPU smoke is ready to run on MS02 once the installer has provisioned the venv + weights.


---

## Milestone 7: TTS integration across the three surfaces (Qwen3-TTS on-box)

**Depends on:** Milestone 1 (`Orchestrator/local_stack.py` resolver — `is_healthy()`, `enabled('tts')`, `base_url()` — and `GET /local-models/status`); Milestone 6 (the `LocalModels/qwen_tts_server/` FastAPI member exposing `/v1/audio/speech`, `/v1/voices/clone`, `/v1/voices/design`, `/v1/voices/design/save`, and persisting profiles under `Manifest/voices/qwen/{slug}/`); Milestone 0 (Android generic-provider fix — `TtsRepository.generateTts` gains a `provider` parameter).

Wire the on-box Qwen3-TTS member into the three surfaces as an **additive** provider beside OpenAI/Gemini/ElevenLabs/local (D1): a fail-open `qwen` group in `GET /tts/catalog`, `qwen:`-prefix branches in `POST /tts` and `POST /tts/batch` that hit the llama-swap `/v1/audio/speech` proxy, Orchestrator proxy endpoints for clone/design/manage through `/upstream/qwen-tts/…`, a Voice Lab Qwen tab, and the D10 "loading models…" slow-first-byte affordance on Portal + Android. All backend additions are inert on a box without the local stack (the dev box, tier LOW) — the qwen group is simply absent and every existing path is untouched, so the working tree stays runnable at every commit.

> **Topology note (spec §10):** the dev box (this machine, no GPU) stays cloud for all four capabilities, so on it `local_stack.is_healthy()` is False and the qwen group never appears. The Portal/Android **manual** steps below are therefore split: on the **dev box** they are *no-regression* checks (qwen absent, picker still works, no console/logcat errors); the **full qwen voice validation** is a Phase-2 step on MS02 (tier HIGH), called out in each manual step.

---

### Task 7.1: Qwen3-TTS Orchestrator integration module

The single seam between the TTS routes / Voice Lab and the on-box `qwen-tts` llama-swap member. Pure helpers (no FastAPI): the 9 CustomVoice presets, saved-profile listing (read from disk — never wakes the GPU), the dynamic catalog group, the synthesis call against the `/v1/audio/speech` proxy, and the `/upstream/qwen-tts/…` URL builder for the NON-OpenAI clone/design paths.

**Files:**
- Create: `Orchestrator/qwen_tts.py`
- Test: `Orchestrator/tests/test_qwen_tts_module.py`

**Steps:**

1. Write the failing test `Orchestrator/tests/test_qwen_tts_module.py`:
   ```python
   """Orchestrator-side Qwen3-TTS integration helpers (M7 Task 7.1).

   Pure-helper contracts (no FastAPI): the catalog group is fail-open on the
   on-box TTS availability seam (_tts_available), the preset list matches the
   spec §5.4/§14 verified 9 CustomVoice voices, saved profiles are read from
   Manifest/voices/qwen/ (never wakes the GPU), synthesize() POSTs the
   llama-swap /v1/audio/speech proxy with model="qwen-tts", and upstream_url()
   strips the /v1 suffix to build the /upstream/qwen-tts passthrough.
   """
   from unittest.mock import patch

   from Orchestrator import qwen_tts


   def test_preset_voices_are_the_nine_customvoice_presets():
       names = [n for n, _desc in qwen_tts.QWEN_PRESET_VOICES]
       assert names == [
           "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric",
           "Ryan", "Aiden", "Ono_Anna", "Sohee",
       ]


   def test_catalog_group_absent_when_tts_unavailable():
       with patch("Orchestrator.qwen_tts._tts_available", return_value=False):
           assert qwen_tts.catalog_group() is None


   def test_catalog_group_presets_only_when_no_profiles():
       with patch("Orchestrator.qwen_tts._tts_available", return_value=True), \
            patch("Orchestrator.qwen_tts.list_profiles", return_value=[]):
           g = qwen_tts.catalog_group()
       assert g["id"] == "qwen"
       assert g["label"] == "Qwen3-TTS (On-Box)"
       assert g["dynamic"] is True
       assert len(g["voices"]) == 9
       assert g["voices"][0]["id"] == "qwen:Vivian"
       # underscores humanized for display only; the id keeps the raw voice token
       fu = next(v for v in g["voices"] if v["id"] == "qwen:Uncle_Fu")
       assert fu["name"] == "Uncle Fu"


   def test_catalog_group_appends_saved_profiles_star_prefixed():
       prof = [{"slug": "brandon-clone", "name": "Brandon", "variant": "base"}]
       with patch("Orchestrator.qwen_tts._tts_available", return_value=True), \
            patch("Orchestrator.qwen_tts.list_profiles", return_value=prof):
           g = qwen_tts.catalog_group()
       last = g["voices"][-1]
       assert last["id"] == "qwen:brandon-clone"
       assert last["name"] == "⭐ Brandon"        # star-prefixed like ElevenLabs My Voices


   def test_synthesize_posts_speech_proxy_with_member_model():
       captured = {}

       class _Resp:
           status_code = 200
           content = b"WAVDATA"
           text = ""

       def _fake_post(url, json=None, timeout=None):
           captured["url"] = url
           captured["json"] = json
           return _Resp()

       with patch("Orchestrator.qwen_tts._base_url", return_value="http://127.0.0.1:9098/v1"), \
            patch("Orchestrator.qwen_tts.requests.post", side_effect=_fake_post):
           r = qwen_tts.synthesize("Vivian", "hello", response_format="mp3")
       assert r.content == b"WAVDATA"
       assert captured["url"] == "http://127.0.0.1:9098/v1/audio/speech"
       assert captured["json"]["model"] == "qwen-tts"
       assert captured["json"]["voice"] == "Vivian"
       assert captured["json"]["input"] == "hello"
       assert captured["json"]["response_format"] == "mp3"


   def test_upstream_url_strips_v1_and_targets_member():
       with patch("Orchestrator.qwen_tts._base_url", return_value="http://127.0.0.1:9098/v1"):
           assert qwen_tts.upstream_url("/v1/voices/clone") == \
               "http://127.0.0.1:9098/upstream/qwen-tts/v1/voices/clone"
   ```

2. Run it, expect collection/attribute failure (module absent):
   - **Run:** `python -m pytest Orchestrator/tests/test_qwen_tts_module.py -v`
   - **Expected:** `ModuleNotFoundError: No module named 'Orchestrator.qwen_tts'` (collection error, 0 passed).

3. Create `Orchestrator/qwen_tts.py` with the full implementation:
   ```python
   #!/usr/bin/env python3
   """Orchestrator-side integration for the on-box Qwen3-TTS llama-swap member.

   The SINGLE seam between the TTS routes / Voice Lab and the on-box Qwen3-TTS
   server (LocalModels/qwen_tts_server, run as the `qwen-tts` llama-swap member
   on :9098). It provides:
     - the 9 CustomVoice preset voices (static) + saved clone/design profiles,
     - the dynamic `qwen` catalog group (present only when the stack is healthy
       AND TTS is enabled — fail-open, like the ElevenLabs/local dynamic groups),
     - synthesis via the llama-swap /v1/audio/speech proxy (body-`model`
       auto-routed to the qwen-tts member),
     - the /upstream/qwen-tts/… URL builder for clone/design/save — NON-OpenAI
       paths llama-swap does NOT auto-route by body-model (spec §5.4, open #245).

   Everything fails soft: on a box without the local stack, _tts_available() is
   False and the catalog group is simply absent (the cloud groups remain).
   Profile listing reads Manifest/voices/qwen/ from disk so building the catalog
   NEVER wakes the audio group (no cross-group swap just to render the picker).
   """
   from __future__ import annotations

   import json
   from typing import Any, Dict, List, Optional, Tuple

   import requests

   # llama-swap member id — the value put in the request `model` field so
   # llama-swap auto-routes /v1/audio/speech to our qwen-tts server.
   QWEN_TTS_MODEL = "qwen-tts"

   # Generous timeout: a cold audio-group swap (~5-8s, §5.2) PLUS slow 1.7B batch
   # synthesis on the RTX 2000 Ada (RTF ~2.4-4x, §5.4) can run long; llama-swap
   # holds the request through the swap. The client shows the D10 "loading models…"
   # affordance during the wait. Streaming (G3) will cut first-byte latency later.
   QWEN_TTS_TIMEOUT = 300  # seconds

   # The 9 CustomVoice presets shipped with Qwen3-TTS (spec §5.4, §14-verified).
   # (raw_voice_token, short_description). Descriptions are ours (the model card
   # ships none) and only populate the picker's "name - description" line.
   QWEN_PRESET_VOICES: List[Tuple[str, str]] = [
       ("Vivian", "Warm, expressive"),
       ("Serena", "Calm, measured"),
       ("Uncle_Fu", "Deep, avuncular"),
       ("Dylan", "Bright, youthful"),
       ("Eric", "Neutral, clear"),
       ("Ryan", "Confident, direct"),
       ("Aiden", "Friendly, relaxed"),
       ("Ono_Anna", "Soft, gentle"),
       ("Sohee", "Light, melodic"),
   ]

   # Streaming-tier expectation-setting copy (correction [25] / §5.4). ONE
   # canonical string reused by the Voice Lab tab and (later) the wizard
   # local_models step so the UI never over-promises 1.7B streaming on the 2000 Ada.
   QWEN_STREAM_TIER_NOTE = (
       "On-box streaming uses the 0.6B voice tier for low latency; the 1.7B "
       "voices are used for batch/file quality. (Streaming size is finalized by "
       "the G3 benchmark on your GPU.)"
   )


   # --------------------------------------------------------------------------
   # local_stack seams (isolated so the catalog/synth code has ONE place to reach
   # the resolver, and tests patch exactly here — no need for local_stack to be
   # importable in a unit test).
   # --------------------------------------------------------------------------
   def _tts_available() -> bool:
       """True when the on-box stack is healthy AND TTS is enabled."""
       try:
           from Orchestrator import local_stack
           return bool(local_stack.is_healthy() and local_stack.enabled("tts"))
       except Exception:
           return False


   def _base_url() -> str:
       """llama-swap front-door base, e.g. http://127.0.0.1:9098/v1 (no trailing /)."""
       from Orchestrator import local_stack
       return local_stack.base_url().rstrip("/")


   def upstream_url(path: str) -> str:
       """Build a /upstream/qwen-tts/<path> URL (auto-loads the member, honors
       group swap/exclusivity) for NON-OpenAI paths llama-swap won't auto-route."""
       base = _base_url()                                   # …:9098/v1
       root = base[:-3] if base.endswith("/v1") else base   # …:9098
       return f"{root}/upstream/{QWEN_TTS_MODEL}{path}"


   # --------------------------------------------------------------------------
   # Voice profiles (clones + saved designs) — persisted by the qwen-tts server
   # under Manifest/voices/qwen/{slug}/profile.json (spec §5.4). Read from disk.
   # --------------------------------------------------------------------------
   def _profiles_root():
       from Orchestrator.utils.paths import manifest_dir
       return manifest_dir() / "voices" / "qwen"


   def list_profiles() -> List[Dict[str, Any]]:
       """Return saved clone/design profiles, newest first. Each dict carries at
       least {slug, name, variant}. Fail-soft: any error -> []."""
       out: List[Dict[str, Any]] = []
       try:
           root = _profiles_root()
           if not root.is_dir():
               return []
           for d in sorted(root.iterdir(), key=lambda p: p.name):
               if not d.is_dir():
                   continue
               pf = d / "profile.json"
               if not pf.is_file():
                   continue
               try:
                   meta = json.loads(pf.read_text(encoding="utf-8"))
               except Exception:
                   continue
               out.append({
                   "slug": d.name,
                   "name": meta.get("name") or d.name,
                   "variant": meta.get("variant", "custom"),
                   "operator": meta.get("operator", ""),
                   "created": meta.get("created", ""),
               })
       except Exception:
           return []
       # newest first when a created timestamp exists, else stable name order
       out.sort(key=lambda m: m.get("created", ""), reverse=True)
       return out


   def delete_profile(slug: str) -> bool:
       """Remove a saved profile directory. Delete is a pure filesystem op (no
       server round-trip): voices are lazy-loaded from disk per request, so a
       removed dir simply won't be found next time. Returns True if it existed."""
       import shutil
       # slug is a directory name; refuse path traversal.
       if not slug or "/" in slug or "\\" in slug or slug in (".", ".."):
           return False
       d = _profiles_root() / slug
       if not d.is_dir():
           return False
       shutil.rmtree(d, ignore_errors=True)
       return True


   # --------------------------------------------------------------------------
   # Catalog group + synthesis
   # --------------------------------------------------------------------------
   def catalog_group() -> Optional[Dict[str, Any]]:
       """Return the dynamic 'qwen' TTS catalog group, or None when the on-box
       TTS capability is unavailable (fail-open — the picker keeps its cloud
       groups). Voice ids are `qwen:<voice-or-slug>`; saved profiles are
       star-prefixed like ElevenLabs My Voices."""
       if not _tts_available():
           return None
       voices: List[Dict[str, str]] = [
           {"id": f"qwen:{tok}", "name": tok.replace("_", " "), "description": desc}
           for tok, desc in QWEN_PRESET_VOICES
       ]
       for p in list_profiles():
           voices.append({
               "id": f"qwen:{p['slug']}",
               "name": f"⭐ {p['name']}",
               "description": p.get("variant", "custom"),
           })
       if not voices:
           return None
       return {"id": "qwen", "label": "Qwen3-TTS (On-Box)",
               "dynamic": True, "voices": voices}


   def synthesize(voice: str, text: str, response_format: str = "wav",
                  stream: bool = False) -> "requests.Response":
       """POST the llama-swap /v1/audio/speech proxy (body-`model` auto-routed to
       the qwen-tts member). `voice` is the BARE token (preset name or profile
       slug — the caller strips any `qwen:` prefix). Returns the raw Response so
       the route decides stream-vs-file. Raises on transport error.

       NB: the M6 server emits WAV/PCM only — its /v1/audio/speech 400s any
       response_format not in ('wav','pcm') (Task 6.4, `test_speech_bad_format_400`).
       Default 'wav' (a proper RIFF container the browser plays); callers pass
       'wav'/'pcm', never 'mp3'/'opus' (there is no mp3/opus encoder in the server)."""
       req = {
           "model": QWEN_TTS_MODEL,
           "input": text,
           "voice": voice,
           "response_format": response_format,
           "stream": stream,
       }
       return requests.post(f"{_base_url()}/audio/speech", json=req,
                            timeout=QWEN_TTS_TIMEOUT)
   ```

4. Run the test, expect PASS:
   - **Run:** `python -m pytest Orchestrator/tests/test_qwen_tts_module.py -v`
   - **Expected:** `6 passed`.

5. Commit:
   - **Run:** `git add Orchestrator/qwen_tts.py Orchestrator/tests/test_qwen_tts_module.py && git commit -m "feat(tts): Orchestrator-side Qwen3-TTS integration module (presets, profiles, catalog group, synth proxy)"`

---

### Task 7.2: `qwen` dynamic group in `GET /tts/catalog`

Append the on-box Qwen group after the ElevenLabs and local groups, fail-open (present only when `local_stack.is_healthy()` and `enabled('tts')`, encoded inside `qwen_tts.catalog_group()`).

**Files:**
- Modify: `Orchestrator/routes/tts_routes.py` (the `tts_catalog()` local-group block ends at line 1063 `pass  # fail-open`; insert before `return {"groups": groups}` at line 1064)
- Test: `Orchestrator/tests/test_qwen_tts_routes.py` (new file — grows across 7.2/7.3/7.4)

**Steps:**

1. Write the failing test file `Orchestrator/tests/test_qwen_tts_routes.py`:
   ```python
   """Qwen3-TTS route integration — catalog group + /tts and /tts/batch branch
   routing (M7 Tasks 7.2-7.4). local_stack/qwen_tts are mocked so the suite runs
   with no on-box stack (the dev-box / CI state). sync_embeddings is mocked
   before app construction (mirrors test_tts_routes_elevenlabs_synth)."""
   from unittest.mock import patch

   import pytest
   from fastapi.testclient import TestClient


   @pytest.fixture
   def client():
       with patch("Orchestrator.toolvault.embeddings.sync_embeddings") as m_emb:
           m_emb.return_value = {"x": {"vector": [0.1]}}
           from Orchestrator.app import app
           with TestClient(app) as c:
               yield c


   _FAKE_MP3 = b"ID3\x03\x00\x00\x00fake-qwen-mp3"


   class _Resp:
       def __init__(self, content=_FAKE_MP3, status=200):
           self.content = content
           self.status_code = status
           self.text = ""


   # ── 7.2 catalog ──────────────────────────────────────────────────────────
   def test_catalog_appends_qwen_group_when_available(client):
       fake_group = {"id": "qwen", "label": "Qwen3-TTS (On-Box)", "dynamic": True,
                     "voices": [{"id": "qwen:Vivian", "name": "Vivian",
                                 "description": "Warm, expressive"}]}
       with patch("Orchestrator.qwen_tts.catalog_group", return_value=fake_group):
           resp = client.get("/tts/catalog")
       assert resp.status_code == 200
       groups = resp.json()["groups"]
       ids = [g["id"] for g in groups]
       assert "qwen" in ids
       qwen = next(g for g in groups if g["id"] == "qwen")
       assert qwen["voices"][0]["id"] == "qwen:Vivian"


   def test_catalog_omits_qwen_group_when_unavailable(client):
       with patch("Orchestrator.qwen_tts.catalog_group", return_value=None):
           resp = client.get("/tts/catalog")
       assert resp.status_code == 200
       ids = [g["id"] for g in resp.json()["groups"]]
       assert "qwen" not in ids   # fail-open: cloud groups still returned


   def test_catalog_survives_qwen_helper_raising(client):
       """A raising qwen_tts must never 500 the catalog (fail-open like the
       ElevenLabs/local blocks)."""
       with patch("Orchestrator.qwen_tts.catalog_group", side_effect=RuntimeError("boom")):
           resp = client.get("/tts/catalog")
       assert resp.status_code == 200
       assert "qwen" not in [g["id"] for g in resp.json()["groups"]]
   ```

2. Run, expect FAIL (qwen never appended):
   - **Run:** `python -m pytest Orchestrator/tests/test_qwen_tts_routes.py -v`
   - **Expected:** `test_catalog_appends_qwen_group_when_available` FAILS with `assert 'qwen' in ids` (the two negative tests pass because the group is never added).

3. In `Orchestrator/routes/tts_routes.py`, insert the qwen block into `tts_catalog()` between the local-group `try/except` (which ends at line 1063 `pass  # fail-open`) and `return {"groups": groups}` (line 1064):
   ```python
       # On-box Qwen3-TTS group — present only when the local stack is healthy and
       # TTS is enabled (fail-open, same as the ElevenLabs/local groups above).
       try:
           from Orchestrator import qwen_tts
           _qg = qwen_tts.catalog_group()
           if _qg:
               groups.append(_qg)
       except Exception:
           pass  # fail-open: qwen group simply absent if the helper errors
       return {"groups": groups}
   ```
   (Replace the bare `return {"groups": groups}` at line 1064 with the block above so the insert is a single contiguous edit.)

4. Run, expect PASS:
   - **Run:** `python -m pytest Orchestrator/tests/test_qwen_tts_routes.py -v`
   - **Expected:** `3 passed`.

5. Commit:
   - **Run:** `git add Orchestrator/routes/tts_routes.py Orchestrator/tests/test_qwen_tts_routes.py && git commit -m "feat(tts): append on-box qwen group to /tts/catalog (fail-open)"`

---

### Task 7.3: `qwen:` branch in `POST /tts`

Route a `qwen:`-prefixed voice (or `provider == "qwen"`) to the llama-swap `/v1/audio/speech` proxy — a browser audio stream by default, or a saved-file JSON when `return_json`. `sanitize_for_speech` is already applied up front (line 140), matching the ElevenLabs/local branches. Placed AFTER the local branch and BEFORE the OpenAI fallthrough.

**Files:**
- Modify: `Orchestrator/routes/tts_routes.py` (insert after the local `POST /tts` branch, which ends at line 236, before `if AUDIO_ENGINE == "browser"` at line 238)
- Test: `Orchestrator/tests/test_qwen_tts_routes.py` (append)

**Steps:**

1. Append to `Orchestrator/tests/test_qwen_tts_routes.py`:
   ```python
   # ── 7.3 POST /tts ────────────────────────────────────────────────────────
   def test_tts_qwen_voice_streams_audio(client):
       with patch("Orchestrator.qwen_tts.synthesize", return_value=_Resp()) as m_syn:
           resp = client.post("/tts", json={"text": "Hello there.", "voice": "qwen:Vivian"})
       assert resp.status_code == 200
       # On-box Qwen emits WAV/PCM only — the branch always serves audio/wav.
       assert resp.headers["content-type"].startswith("audio/wav")
       assert resp.content == _FAKE_MP3
       m_syn.assert_called_once()
       args, kwargs = m_syn.call_args
       assert args[0] == "Vivian"          # prefix stripped -> bare token
       assert args[1] == "Hello there."
       # The member is asked for 'wav', never 'mp3' (which the M6 server 400s).
       assert kwargs.get("response_format", args[2] if len(args) > 2 else None) == "wav"


   def test_tts_qwen_return_json_shape(client):
       with patch("Orchestrator.qwen_tts.synthesize", return_value=_Resp()):
           resp = client.post("/tts", json={"text": "Hi.", "voice": "qwen:Serena",
                                            "return_json": True})
       assert resp.status_code == 200
       body = resp.json()
       assert body["status"] == "success"
       assert body["audio_url"].startswith("/ui/uploads/")
       assert body["voice"] == "Serena"
       assert body["model"] == "qwen-tts"
       assert body["size_bytes"] == len(_FAKE_MP3)


   def test_tts_qwen_provider_bare_voice(client):
       """provider='qwen' with a BARE voice (the Android /tts shape) also routes."""
       with patch("Orchestrator.qwen_tts.synthesize", return_value=_Resp()) as m_syn:
           resp = client.post("/tts", json={"text": "Yo.", "voice": "Ryan",
                                            "provider": "qwen"})
       assert resp.status_code == 200
       m_syn.assert_called_once()
       assert m_syn.call_args[0][0] == "Ryan"


   def test_tts_qwen_upstream_error_is_fallback(client):
       with patch("Orchestrator.qwen_tts.synthesize", return_value=_Resp(status=503)):
           resp = client.post("/tts", json={"text": "Hi.", "voice": "qwen:Vivian",
                                            "return_json": True})
       assert resp.status_code == 200
       assert resp.json()["status"] == "fallback"
   ```

2. Run, expect FAIL (qwen voice falls through to the OpenAI path → fallback/no synth call):
   - **Run:** `python -m pytest Orchestrator/tests/test_qwen_tts_routes.py -k qwen_voice_streams -v`
   - **Expected:** FAIL — `synthesize` not called / content-type not audio/wav (the request hit the OpenAI branch).

3. In `Orchestrator/routes/tts_routes.py`, insert the qwen branch immediately after the local `POST /tts` branch's final `return` (line 236) and before `if AUDIO_ENGINE == "browser": return {"status": "fallback"}` (line 238):
   ```python
       # --- On-box Qwen3-TTS branch: route to the llama-swap /v1/audio/speech
       # proxy (body-model auto-routed to the qwen-tts member). Triggered by a
       # 'qwen:' voice prefix or provider=='qwen'. Checked BEFORE the OpenAI path
       # (which rejects Qwen voice tokens with an OpenAI-voice-enum 400). Mirrors
       # the local branch but targets the on-box stack (:9098). sanitize_for_speech
       # already applied up front (line 140).
       if (body.get("voice") or "").startswith("qwen:") or body.get("provider") == "qwen":
           from Orchestrator import qwen_tts
           _text = (body.get("text") or "").strip()
           if not _text:
               raise HTTPException(400, "No text provided")
           _voice = (body.get("voice") or "").strip()
           _bare = _voice.split(":", 1)[1] if _voice.startswith("qwen:") else _voice
           # The on-box Qwen member emits WAV/PCM only (M6 /v1/audio/speech 400s any
           # other response_format — there is no mp3/opus encoder). Always request
           # 'wav' (a proper RIFF container the browser plays directly); ignore any
           # client `format` hint since Qwen cannot honor mp3/opus.
           _fmt = "wav"
           try:
               _r = qwen_tts.synthesize(_bare, _text, response_format=_fmt)
           except Exception as e:
               return {"status": "fallback", "detail": str(e)}
           if _r.status_code != 200:
               return {"status": "fallback", "api_status": _r.status_code,
                       "api_body": _r.text[:400]}
           _mime = "audio/wav"
           if not body.get("return_json"):
               return StreamingResponse(iter([_r.content]), media_type=_mime)
           _filename = f"{uuid.uuid4()}_tts.wav"
           (UPLOADS_DIR / _filename).write_bytes(_r.content)
           return {"status": "success", "audio_url": f"/ui/uploads/{_filename}",
                   "voice": _bare, "model": qwen_tts.QWEN_TTS_MODEL, "format": _fmt,
                   "size_bytes": len(_r.content)}

   ```

4. Run, expect PASS:
   - **Run:** `python -m pytest Orchestrator/tests/test_qwen_tts_routes.py -v`
   - **Expected:** `7 passed` (3 catalog + 4 /tts).

5. Commit:
   - **Run:** `git add Orchestrator/routes/tts_routes.py Orchestrator/tests/test_qwen_tts_routes.py && git commit -m "feat(tts): qwen: branch in POST /tts (llama-swap /v1/audio/speech proxy)"`

---

### Task 7.4: `qwen` provider branch in `POST /tts/batch`

Add the `qwen` provider (the Android auto-TTS/manual-speak path posts `/tts/batch` with `provider="qwen"` and a bare voice, via `buildTtsBatchBody`). Synthesize chunks **sequentially** (the single GPU serializes them anyway, and it avoids the qwen-tts member's `concurrencyLimit: 2` → HTTP 429 that a parallel fan-out would trigger). Add the `qwen:`-prefix→provider override for parity with `local:`.

**Files:**
- Modify: `Orchestrator/routes/tts_routes.py` (local-prefix override at lines 314-315; add qwen branch before the `else:` at line 430; update the else message at line 431)
- Test: `Orchestrator/tests/test_qwen_tts_routes.py` (append)

**Steps:**

1. Append to `Orchestrator/tests/test_qwen_tts_routes.py`:
   ```python
   # ── 7.4 POST /tts/batch ──────────────────────────────────────────────────
   def test_tts_batch_qwen_provider(client):
       with patch("Orchestrator.qwen_tts.synthesize", return_value=_Resp()) as m_syn:
           resp = client.post("/tts/batch",
                              json={"text": "Short batch.", "provider": "qwen",
                                    "voice": "Vivian"})
       assert resp.status_code == 200
       # Qwen emits WAV — the batch stitches WAV and serves audio/wav (a single
       # chunk passes through stitch_wav_chunks unchanged, so content is preserved).
       assert resp.headers["content-type"].startswith("audio/wav")
       assert resp.content == _FAKE_MP3
       m_syn.assert_called_once()
       assert m_syn.call_args[0][0] == "Vivian"
       # The member is asked for 'wav', never mp3 (which the M6 server 400s).
       assert m_syn.call_args.kwargs.get("response_format") == "wav"


   def test_tts_batch_qwen_prefix_forces_provider(client):
       """A 'qwen:'-prefixed voice forces provider=qwen even if provider omitted
       (parity with the local: override), and the bare token reaches synthesize."""
       with patch("Orchestrator.qwen_tts.synthesize", return_value=_Resp()) as m_syn:
           resp = client.post("/tts/batch",
                              json={"text": "Hi.", "voice": "qwen:Serena"})
       assert resp.status_code == 200
       m_syn.assert_called_once()
       assert m_syn.call_args[0][0] == "Serena"


   def test_tts_batch_qwen_upstream_error_502(client):
       with patch("Orchestrator.qwen_tts.synthesize", return_value=_Resp(status=500)):
           resp = client.post("/tts/batch",
                              json={"text": "Hi.", "provider": "qwen", "voice": "Vivian"})
       assert resp.status_code == 502
   ```

2. Run, expect FAIL (`Unknown provider: qwen`):
   - **Run:** `python -m pytest Orchestrator/tests/test_qwen_tts_routes.py -k batch_qwen -v`
   - **Expected:** FAIL — `assert 400 == 200` (the else-branch raises `Unknown provider: qwen`).

3. In `Orchestrator/routes/tts_routes.py`, add the qwen-prefix override right after the local override (lines 314-315 `if voice.startswith("local:"): provider = "local"`):
   ```python
       # A qwen:-prefixed voice forces the qwen provider (parity with local:), so a
       # caller that passes the prefixed voice but omits provider still routes on-box.
       if voice.startswith("qwen:"):
           provider = "qwen"
   ```

4. Add the `qwen` provider branch immediately before the `else:` at line 430 (`else: raise HTTPException(400, f"Unknown provider…`):
   ```python
       # --- On-box Qwen3-TTS provider: llama-swap /v1/audio/speech, sequential ---
       elif provider == "qwen":
           from Orchestrator import qwen_tts
           _bare = voice.split(":", 1)[1] if voice.startswith("qwen:") else voice

           def _qwen_all() -> List[bytes]:
               # Sequential on purpose: one GPU serializes synthesis anyway, and
               # the qwen-tts member's concurrencyLimit:2 would 429 a parallel
               # fan-out. Any non-200 raises -> the endpoint returns 502.
               # Request WAV: the M6 server emits WAV/PCM only (it 400s mp3/opus —
               # there is no encoder), so audio_format cannot flow through here.
               out: List[bytes] = []
               for ch in chunks:
                   r = qwen_tts.synthesize(_bare, ch, response_format="wav")
                   if r.status_code != 200:
                       raise HTTPException(502, f"Qwen TTS failed (HTTP {r.status_code}): {r.text[:200]}")
                   out.append(r.content)
               return out

           results = await loop.run_in_executor(_tts_executor, _qwen_all)
           # Qwen returns WAV — drive the WAV stitcher + audio/wav mime below
           # (mirrors the Gemini branch's `audio_format = "wav"`).
           audio_format = "wav"

   ```

5. Update the `else` error message (line 431) to list qwen:
   ```python
       else:
           raise HTTPException(400, f"Unknown provider: {provider}. Use 'openai', 'gemini-pro', 'gemini-flash', 'elevenlabs', 'local', or 'qwen'.")
   ```

6. Run, expect PASS:
   - **Run:** `python -m pytest Orchestrator/tests/test_qwen_tts_routes.py -v`
   - **Expected:** `10 passed`.

7. Regression-check the sibling ElevenLabs route suite is untouched:
   - **Run:** `python -m pytest Orchestrator/tests/test_tts_routes_elevenlabs_synth.py -v`
   - **Expected:** all pass (no regressions).

8. Commit:
   - **Run:** `git add Orchestrator/routes/tts_routes.py Orchestrator/tests/test_qwen_tts_routes.py && git commit -m "feat(tts): qwen provider branch in POST /tts/batch (sequential, 429-safe)"`

---

### Task 7.5: Qwen voice-management proxy endpoints (clone / design / save / list / delete)

Orchestrator endpoints backing the Voice Lab Qwen tab. Clone/design/save proxy through `/upstream/qwen-tts/v1/voices/…` (NON-OpenAI paths llama-swap won't auto-route by body-model, spec §5.4). List/delete are pure filesystem ops over `Manifest/voices/qwen/` (no GPU wake). Clone enforces the literal consent flag (422 without it — mirrors the ElevenLabs gate exactly).

> **M6 server contract** (built in Milestone 6 — see `LocalModels/qwen_tts_server/README.md`, Task 6.8): `POST /v1/voices/clone` accepts multipart `name`, **`file`** (singular `UploadFile`, Task 6.6), `consent`, optional `operator` and returns `{voice_id: <slug>, name}`; `POST /v1/voices/design` accepts JSON `{voice_description, text?}` and returns `{previews:[{generated_voice_id, audio_b64, sample_rate}]}` — **the preview audio is a base64-encoded WAV in `audio_b64` (with its `sample_rate`), NOT a `data:` URL/`audio_url`** (correction: the Voice Lab builds `data:audio/wav;base64,${audio_b64}` itself, Task 7.6); `POST /v1/voices/design/save` accepts `{generated_voice_id, name, operator?}` → `{voice_id: <slug>}` and persists `Manifest/voices/qwen/{slug}/profile.json`. The Orchestrator proxy (below) MUST forward the clone reference audio under the singular field name **`file`** to match this endpoint.

**Files:**
- Modify: `Orchestrator/routes/tts_routes.py` (add 5 routes after `tts_catalog()`, before `@app.get("/stt/catalog")` at line 1066)
- Test: `Orchestrator/tests/test_qwen_voices_proxy.py`

**Steps:**

1. Write the failing test `Orchestrator/tests/test_qwen_voices_proxy.py`:
   ```python
   """Qwen voice-management proxy endpoints (M7 Task 7.5): list/delete are
   filesystem ops over qwen_tts.{list_profiles,delete_profile}; clone/design/save
   proxy /upstream/qwen-tts/… Clone enforces the consent flag (422 without it)."""
   from unittest.mock import patch

   import pytest
   from fastapi.testclient import TestClient


   @pytest.fixture
   def client():
       with patch("Orchestrator.toolvault.embeddings.sync_embeddings") as m_emb:
           m_emb.return_value = {"x": {"vector": [0.1]}}
           from Orchestrator.app import app
           with TestClient(app) as c:
               yield c


   class _JResp:
       def __init__(self, payload, status=200):
           self._p = payload
           self.status_code = status
           self.text = str(payload)

       def json(self):
           return self._p


   def test_qwen_voices_list(client):
       prof = [{"slug": "brandon", "name": "Brandon", "variant": "base"}]
       with patch("Orchestrator.qwen_tts.list_profiles", return_value=prof):
           resp = client.get("/qwen/voices")
       assert resp.status_code == 200
       body = resp.json()
       assert body["voices"][0]["slug"] == "brandon"


   def test_qwen_clone_requires_consent(client):
       # No consent -> 422, and the upstream is never called.
       with patch("Orchestrator.routes.tts_routes.requests.post") as m_post:
           resp = client.post(
               "/qwen/voices/clone",
               data={"name": "Test"},
               files={"files": ("clip.wav", b"RIFFxxxx", "audio/wav")},
           )
       assert resp.status_code == 422
       m_post.assert_not_called()


   def test_qwen_clone_proxies_upstream_with_consent(client):
       with patch("Orchestrator.qwen_tts.upstream_url",
                  return_value="http://127.0.0.1:9098/upstream/qwen-tts/v1/voices/clone") as m_url, \
            patch("Orchestrator.routes.tts_routes.requests.post",
                  return_value=_JResp({"voice_id": "test-slug"})) as m_post:
           resp = client.post(
               "/qwen/voices/clone",
               data={"name": "Test", "consent": "true"},
               files={"files": ("clip.wav", b"RIFFxxxx", "audio/wav")},
           )
       assert resp.status_code == 200
       assert resp.json()["voice_id"] == "test-slug"
       m_url.assert_called_once_with("/v1/voices/clone")
       m_post.assert_called_once()
       # The reference audio MUST be forwarded under the singular field name 'file'
       # to match the M6 server (`file: UploadFile = File(...)`); 'files' would 422.
       fwd = m_post.call_args.kwargs["files"]
       assert [part[0] for part in fwd] == ["file"]


   def test_qwen_design_proxies_upstream(client):
       with patch("Orchestrator.qwen_tts.upstream_url",
                  return_value="http://x/v1/voices/design"), \
            patch("Orchestrator.routes.tts_routes.requests.post",
                  return_value=_JResp({"text": "sample", "previews": []})):
           resp = client.post("/qwen/voices/design",
                              json={"voice_description": "a warm narrator"})
       assert resp.status_code == 200
       assert "previews" in resp.json()


   def test_qwen_design_save_proxies_upstream(client):
       with patch("Orchestrator.qwen_tts.upstream_url",
                  return_value="http://x/v1/voices/design/save"), \
            patch("Orchestrator.routes.tts_routes.requests.post",
                  return_value=_JResp({"voice_id": "design-slug"})):
           resp = client.post("/qwen/voices/design/save",
                              json={"generated_voice_id": "g1", "name": "Narrator"})
       assert resp.status_code == 200
       assert resp.json()["voice_id"] == "design-slug"


   def test_qwen_delete_profile(client):
       with patch("Orchestrator.qwen_tts.delete_profile", return_value=True) as m_del:
           resp = client.delete("/qwen/voices/brandon")
       assert resp.status_code == 200
       assert resp.json()["ok"] is True
       m_del.assert_called_once_with("brandon")


   def test_qwen_delete_missing_profile(client):
       with patch("Orchestrator.qwen_tts.delete_profile", return_value=False):
           resp = client.delete("/qwen/voices/nope")
       assert resp.status_code == 404
   ```

2. Run, expect FAIL (routes 404):
   - **Run:** `python -m pytest Orchestrator/tests/test_qwen_voices_proxy.py -v`
   - **Expected:** FAIL — `assert 404 == 200` on the list test (no route registered).

3. In `Orchestrator/routes/tts_routes.py`, insert the 5 routes after `tts_catalog()` (line 1064's block) and before `@app.get("/stt/catalog")` (line 1066):
   ```python
   # =============================================================================
   # Qwen3-TTS voice management (backs the Voice Lab Qwen tab). Clone/design/save
   # proxy /upstream/qwen-tts/v1/voices/… (NON-OpenAI paths llama-swap won't
   # auto-route, §5.4); list/delete are filesystem ops over Manifest/voices/qwen/.
   # =============================================================================
   @app.get("/qwen/voices")
   async def qwen_voices_list():
       """Saved clone/design profiles for the manage zone. No GPU wake (disk read)."""
       from Orchestrator import qwen_tts
       return {"voices": qwen_tts.list_profiles()}


   @app.post("/qwen/voices/clone")
   async def qwen_voices_clone(
       name: str = Form(...),
       consent: str = Form(None),
       description: str = Form(None),
       files: List[UploadFile] = File(...),
   ):
       """Clone a voice from reference audio (~3s min). Requires the literal
       consent flag — 422 without it, mirroring the ElevenLabs gate. Forwards the
       multipart to the qwen-tts server via /upstream/qwen-tts/v1/voices/clone."""
       if (consent or "").strip().lower() != "true":
           raise HTTPException(422, "consent required to clone a voice")
       from Orchestrator import qwen_tts
       # Forward under the SINGULAR field name 'file' — the M6 server declares
       # `file: UploadFile = File(...)` (Task 6.6), so forwarding 'files' (plural)
       # would 422 the member (missing required 'file'). The proxy still accepts a
       # list from the browser (a single reference clip is the norm) and forwards
       # each part as 'file'.
       fwd_files = []
       for f in files:
           fwd_files.append(("file", (f.filename or "clip.wav", await f.read(),
                                      f.content_type or "application/octet-stream")))
       data = {"name": name, "consent": "true"}
       if description:
           data["description"] = description
       try:
           r = requests.post(qwen_tts.upstream_url("/v1/voices/clone"),
                             data=data, files=fwd_files,
                             timeout=qwen_tts.QWEN_TTS_TIMEOUT)
       except Exception as e:
           raise HTTPException(502, f"qwen-tts clone unreachable: {e}")
       if r.status_code != 200:
           raise HTTPException(r.status_code, r.text[:400])
       return r.json()


   @app.post("/qwen/voices/design")
   async def qwen_voices_design(body: dict = Body(...)):
       """Design→preview: {voice_description, text?} -> {previews:[{generated_voice_id,
       audio_b64, sample_rate}]} (passed through verbatim; the Voice Lab builds the
       data: URL from audio_b64). Proxied through /upstream/qwen-tts/v1/voices/design."""
       from Orchestrator import qwen_tts
       try:
           r = requests.post(qwen_tts.upstream_url("/v1/voices/design"),
                             json=body, timeout=qwen_tts.QWEN_TTS_TIMEOUT)
       except Exception as e:
           raise HTTPException(502, f"qwen-tts design unreachable: {e}")
       if r.status_code != 200:
           raise HTTPException(r.status_code, r.text[:400])
       return r.json()


   @app.post("/qwen/voices/design/save")
   async def qwen_voices_design_save(body: dict = Body(...)):
       """Save a chosen design preview: {generated_voice_id, name, description?}
       -> {voice_id}. Persists Manifest/voices/qwen/{slug}/ server-side."""
       from Orchestrator import qwen_tts
       try:
           r = requests.post(qwen_tts.upstream_url("/v1/voices/design/save"),
                             json=body, timeout=qwen_tts.QWEN_TTS_TIMEOUT)
       except Exception as e:
           raise HTTPException(502, f"qwen-tts design/save unreachable: {e}")
       if r.status_code != 200:
           raise HTTPException(r.status_code, r.text[:400])
       return r.json()


   @app.delete("/qwen/voices/{slug}")
   async def qwen_voices_delete(slug: str):
       """Delete a saved profile (filesystem op; voices are lazy-loaded from disk,
       so no server round-trip is needed)."""
       from Orchestrator import qwen_tts
       if qwen_tts.delete_profile(slug):
           return {"ok": True, "slug": slug}
       raise HTTPException(404, f"no such qwen voice profile: {slug}")

   ```

4. Run, expect PASS:
   - **Run:** `python -m pytest Orchestrator/tests/test_qwen_voices_proxy.py -v`
   - **Expected:** `7 passed`.

5. Commit:
   - **Run:** `git add Orchestrator/routes/tts_routes.py Orchestrator/tests/test_qwen_voices_proxy.py && git commit -m "feat(tts): Qwen voice-management proxy endpoints (clone/design/save/list/delete)"`

---

### Task 7.6: Portal Voice Lab — Qwen tab

Add a fifth zone to the Voice Lab modal: clone (consent gate), design (preview→save), and manage/delete Qwen profiles, gated on `GET /local-models/status` healthy (instead of an API key). Mirrors the ElevenLabs zones exactly; after any mutation it re-runs `populateVoiceCatalog()` so the picker stays in sync. Also hide the three ElevenLabs zones when ElevenLabs is not configured (so a Qwen-only box opens a clean modal).

> **Assumed M1 contract** (flag for the M1 executor): `GET /local-models/status` returns JSON with a top-level truthy `healthy` **or** `capabilities.tts.enabled`. The gate below accepts either shape defensively; if M1 uses a different field, adjust the one-liner in `qwenTabAvailable()`.

**Files:**
- Modify: `Portal/voice-lab.js`

**Steps:**

1. In `Portal/voice-lab.js`, add the Qwen zone to the modal `innerHTML` in `ensureModal()`, immediately after the xAI zone `</section>` (line 192) and before the `<p class="vlab-foot-hint">` (line 194):
   ```html
               <!-- ── Zone 5: Qwen3-TTS (On-Box) — hidden until /local-models/status healthy ── -->
               <section class="vlab-zone" id="vlabQwenZone" hidden>
                 <h4 class="vlab-zone-title">Qwen3-TTS (On-Box)</h4>
                 <p class="vlab-zone-hint">Clone a voice from ~3s of clear speech, or design one from a
                   description — all on your box, no API key. On-box streaming uses the 0.6B voice tier for
                   low latency; the 1.7B voices are used for batch/file quality.</p>

                 <!-- Clone -->
                 <div class="vlab-method-label">Clone a voice</div>
                 <input id="vlabQwenFile" class="vlab-file-input" type="file"
                        accept="audio/wav,audio/mpeg,audio/mp3,audio/x-m4a,audio/mp4,audio/webm,.wav,.mp3,.m4a,.webm" />
                 <input id="vlabQwenCloneName" class="vlab-input" type="text" placeholder="Name this voice" autocomplete="off" />
                 <label class="vlab-consent">
                   <input id="vlabQwenConsent" type="checkbox" />
                   <span>I confirm I own this voice or have permission to clone it.</span>
                 </label>
                 <div class="vlab-row vlab-row-end">
                   <button id="vlabQwenCloneBtn" class="vlab-btn vlab-btn-accent" type="button" disabled>Clone voice</button>
                 </div>

                 <!-- Design -->
                 <div class="vlab-method-label">Design a voice from text</div>
                 <textarea id="vlabQwenDesignDesc" class="vlab-input vlab-textarea"
                           placeholder="e.g. a gravelly old sea captain, weathered and warm" rows="2"></textarea>
                 <div class="vlab-row vlab-row-end">
                   <button id="vlabQwenDesignGenBtn" class="vlab-btn vlab-btn-accent" type="button">Generate previews</button>
                 </div>
                 <div id="vlabQwenDesignPreviews" class="vlab-previews"></div>
                 <div id="vlabQwenDesignSaveRow" class="vlab-save-row" hidden>
                   <input id="vlabQwenDesignName" class="vlab-input" type="text" placeholder="Name this voice" autocomplete="off" />
                   <button id="vlabQwenDesignSaveBtn" class="vlab-btn vlab-btn-accent" type="button">Save voice</button>
                 </div>

                 <!-- Manage -->
                 <div class="vlab-method-label">My on-box voices</div>
                 <div id="vlabQwenStatus" class="vlab-status"></div>
                 <div id="vlabQwenList" class="vlab-my-list"></div>
               </section>
   ```

2. Wire the static Qwen buttons inside `ensureModal()`, right after the xAI wiring block (line 213 `modal.querySelector('#vlabXaiCloneBtn').addEventListener('click', submitXaiClone);`):
   ```javascript
       // ── Qwen (on-box) zone wiring (static; gate/list refreshed per-open) ──
       modal.querySelector('#vlabQwenFile').addEventListener('change', refreshQwenCloneButton);
       modal.querySelector('#vlabQwenCloneName').addEventListener('input', refreshQwenCloneButton);
       modal.querySelector('#vlabQwenConsent').addEventListener('change', refreshQwenCloneButton);
       modal.querySelector('#vlabQwenCloneBtn').addEventListener('click', submitQwenClone);
       modal.querySelector('#vlabQwenDesignGenBtn').addEventListener('click', runQwenDesign);
       modal.querySelector('#vlabQwenDesignSaveBtn').addEventListener('click', saveQwenDesign);
   ```

3. Add the Qwen zone module functions. Insert immediately before the `// Open / close` section header (line 779 `// ====…`):
   ```javascript
   // =============================================================================
   // Zone 5 — Qwen3-TTS (On-Box): clone (consent) / design (preview→save) / manage.
   // Gated on GET /local-models/status healthy (no API key). Mirrors the ElevenLabs
   // zones; refreshes populateVoiceCatalog() after every mutation.
   // =============================================================================
   let qwenSelectedPreviewId = null;

   /** Is the on-box TTS capability available? (defensive across M1 shapes.) */
   async function qwenTabAvailable() {
       try {
           const res = await fetch('/local-models/status');
           if (!res.ok) return false;
           const s = await res.json();
           return !!(s && (s.healthy === true || s.status === 'healthy'
                    || (s.capabilities && s.capabilities.tts && s.capabilities.tts.enabled)));
       } catch { return false; }
   }

   function refreshQwenCloneButton() {
       const btn = document.getElementById('vlabQwenCloneBtn');
       if (!btn) return;
       const name = (document.getElementById('vlabQwenCloneName')?.value || '').trim();
       const consent = !!document.getElementById('vlabQwenConsent')?.checked;
       const hasFile = !!(document.getElementById('vlabQwenFile')?.files || []).length;
       btn.disabled = !(name && consent && hasFile);
   }

   async function submitQwenClone() {
       const btn = document.getElementById('vlabQwenCloneBtn');
       if (!btn || btn.disabled) return;
       const name = (document.getElementById('vlabQwenCloneName').value || '').trim();
       const file = document.getElementById('vlabQwenFile').files[0];
       const fd = new FormData();
       fd.append('name', name);
       fd.append('consent', 'true');
       fd.append('files', file, file.name);
       btn.disabled = true;
       const orig = btn.textContent;
       btn.textContent = 'Cloning…';
       try {
           const res = await fetch('/qwen/voices/clone', { method: 'POST', body: fd });
           const data = await res.json().catch(() => ({}));
           if (!res.ok) {
               toastError(`Clone failed: ${(data && data.detail) ? data.detail : 'HTTP ' + res.status}`);
               btn.disabled = false; btn.textContent = orig; return;
           }
           toastSuccess(`Cloned "${name}"`);
           document.getElementById('vlabQwenCloneName').value = '';
           document.getElementById('vlabQwenFile').value = '';
           document.getElementById('vlabQwenConsent').checked = false;
           btn.textContent = orig;
           const newId = data.voice_id ? `qwen:${data.voice_id}` : null;
           await Promise.all([loadQwenVoices(), populateVoiceCatalog(newId)]);
       } catch (err) {
           toastError(`Clone failed: ${err.message}`);
           btn.disabled = false; btn.textContent = orig;
       }
   }

   async function runQwenDesign() {
       const desc = (document.getElementById('vlabQwenDesignDesc').value || '').trim();
       if (!desc) { toastError('Describe the voice you want first'); return; }
       const genBtn = document.getElementById('vlabQwenDesignGenBtn');
       const previewsEl = document.getElementById('vlabQwenDesignPreviews');
       const saveRow = document.getElementById('vlabQwenDesignSaveRow');
       stopPreview();
       qwenSelectedPreviewId = null;
       saveRow.hidden = true;
       previewsEl.innerHTML = '<div class="vlab-status">Generating previews…</div>';
       genBtn.disabled = true;
       const genOrig = genBtn.textContent;
       genBtn.textContent = 'Generating…';
       try {
           const res = await fetch('/qwen/voices/design', {
               method: 'POST',
               headers: { 'Content-Type': 'application/json' },
               body: JSON.stringify({ voice_description: desc }),
           });
           const data = await res.json().catch(() => ({}));
           if (!res.ok) {
               previewsEl.innerHTML = `<div class="vlab-status">Design failed: ${escapeHtml((data && data.detail) || ('HTTP ' + res.status))}</div>`;
               return;
           }
           renderQwenDesignPreviews(data.previews || [], previewsEl);
       } catch (err) {
           previewsEl.innerHTML = `<div class="vlab-status">Design failed: ${escapeHtml(err.message)}</div>`;
       } finally {
           genBtn.disabled = false;
           genBtn.textContent = genOrig;
       }
   }

   function renderQwenDesignPreviews(previews, container) {
       if (!previews.length) {
           container.innerHTML = '<div class="vlab-status">No previews returned. Try a different description.</div>';
           return;
       }
       container.innerHTML = '';
       previews.forEach((p, i) => {
           // M6 /v1/voices/design returns previews as {generated_voice_id, audio_b64,
           // sample_rate} — build the playable data: URL from the base64 WAV here (the
           // member is loopback-only, so a data: URL is what the browser can play).
           // `audio_url` kept as a defensive fallback if the contract ever emits one.
           const audioUrl = p.audio_url
               || (p.audio_b64 ? `data:audio/wav;base64,${p.audio_b64}` : null);
           const card = document.createElement('div');
           card.className = 'vlab-preview-card';
           card.innerHTML = `
               <button class="vlab-preview-btn" type="button" title="Play preview" ${audioUrl ? '' : 'disabled'}>▶</button>
               <div class="vlab-preview-meta"><div class="vlab-preview-name">Option ${i + 1}</div></div>
               <button class="vlab-btn vlab-use-btn" type="button">Use this one</button>`;
           const playBtn = card.querySelector('.vlab-preview-btn');
           const useBtn = card.querySelector('.vlab-use-btn');
           if (audioUrl) playBtn.addEventListener('click', () => togglePreview(audioUrl, playBtn));
           useBtn.addEventListener('click', () => {
               qwenSelectedPreviewId = p.generated_voice_id;
               container.querySelectorAll('.vlab-preview-card').forEach(c => c.classList.remove('selected'));
               card.classList.add('selected');
               container.querySelectorAll('.vlab-use-btn').forEach(b => { b.textContent = 'Use this one'; });
               useBtn.textContent = '✓ Selected';
               const saveRow = document.getElementById('vlabQwenDesignSaveRow');
               saveRow.hidden = false;
               const nameInput = document.getElementById('vlabQwenDesignName');
               if (nameInput && !nameInput.value.trim()) {
                   nameInput.value = (document.getElementById('vlabQwenDesignDesc').value || '').trim().slice(0, 40);
               }
               nameInput?.focus();
           });
           container.appendChild(card);
       });
   }

   async function saveQwenDesign() {
       if (!qwenSelectedPreviewId) { toastError('Pick a preview with "Use this one" first'); return; }
       const name = (document.getElementById('vlabQwenDesignName').value || '').trim();
       if (!name) { toastError('Name the voice before saving'); return; }
       const btn = document.getElementById('vlabQwenDesignSaveBtn');
       btn.disabled = true;
       const orig = btn.textContent;
       btn.textContent = 'Saving…';
       try {
           const res = await fetch('/qwen/voices/design/save', {
               method: 'POST',
               headers: { 'Content-Type': 'application/json' },
               body: JSON.stringify({ generated_voice_id: qwenSelectedPreviewId, name }),
           });
           const data = await res.json().catch(() => ({}));
           if (!res.ok) {
               toastError(`Save failed: ${(data && data.detail) ? data.detail : 'HTTP ' + res.status}`);
               btn.disabled = false; btn.textContent = orig; return;
           }
           toastSuccess(`Saved "${name}"`);
           stopPreview();
           qwenSelectedPreviewId = null;
           document.getElementById('vlabQwenDesignSaveRow').hidden = true;
           document.getElementById('vlabQwenDesignPreviews').innerHTML = '';
           document.getElementById('vlabQwenDesignDesc').value = '';
           document.getElementById('vlabQwenDesignName').value = '';
           btn.disabled = false; btn.textContent = orig;
           const newId = data.voice_id ? `qwen:${data.voice_id}` : null;
           await Promise.all([loadQwenVoices(), populateVoiceCatalog(newId)]);
       } catch (err) {
           toastError(`Save failed: ${err.message}`);
           btn.disabled = false; btn.textContent = orig;
       }
   }

   async function loadQwenVoices() {
       const listEl = document.getElementById('vlabQwenList');
       const statusEl = document.getElementById('vlabQwenStatus');
       if (!listEl) return;
       stopPreview();
       if (statusEl) statusEl.textContent = 'Loading…';
       listEl.innerHTML = '';
       try {
           const res = await fetch('/qwen/voices');
           if (!res.ok) throw new Error(`HTTP ${res.status}`);
           const data = await res.json();
           const mine = data.voices || [];
           if (statusEl) statusEl.textContent = mine.length ? `${mine.length} on-box voice${mine.length === 1 ? '' : 's'}` : '';
           if (!mine.length) {
               listEl.innerHTML = '<div class="vlab-empty">No on-box voices yet. Clone or design one above.</div>';
               return;
           }
           for (const v of mine) {
               const row = document.createElement('div');
               row.className = 'vlab-voice-row';
               row.innerHTML = `
                   <div class="vlab-voice-main">
                     <div class="vlab-voice-name">${escapeHtml(v.name || v.slug)}</div>
                     <div class="vlab-voice-desc">${escapeHtml(v.variant || '')}</div>
                   </div>
                   <button class="vlab-btn vlab-delete-btn" type="button">Delete</button>`;
               row.querySelector('.vlab-delete-btn').addEventListener('click',
                   (e) => deleteQwenVoice(v.slug, v.name, e.currentTarget));
               listEl.appendChild(row);
           }
       } catch (err) {
           if (statusEl) statusEl.textContent = '';
           listEl.innerHTML = `<div class="vlab-empty">Failed to load voices: ${escapeHtml(err.message)}</div>`;
       }
   }

   async function deleteQwenVoice(slug, name, btn) {
       if (btn.disabled) return;
       if (!window.confirm(`Delete on-box voice "${name || slug}"? This cannot be undone.`)) return;
       btn.disabled = true;
       try {
           const res = await fetch(`/qwen/voices/${encodeURIComponent(slug)}`, { method: 'DELETE' });
           const data = await res.json().catch(() => ({}));
           if (!res.ok || !data.ok) {
               toastError(`Delete failed: ${(data && data.detail) ? data.detail : 'HTTP ' + res.status}`);
               btn.disabled = false; return;
           }
           toastSuccess(`Deleted "${name || slug}"`);
           await Promise.all([loadQwenVoices(), populateVoiceCatalog()]);
       } catch (err) {
           toastError(`Delete failed: ${err.message}`);
           btn.disabled = false;
       }
   }
   ```

4. Update `openVoiceLab()` (line 784) to gate the ElevenLabs zones on the ElevenLabs key and the Qwen zone on the local stack. Replace the body from the `renderCloneZone(canClone);` line through the `loadXaiVoices();` line (lines 799-805) with:
   ```javascript
       // ElevenLabs zones (clone/design/manage) only make sense with a key —
       // hide all three on a Qwen-only box so the modal isn't full of 400s.
       const elevenOk = canClone || false;   // canClone already implies a working key
       const elevenConfigured = await (async () => {
           try {
               const r = await fetch('/elevenlabs/status');
               return r.ok ? !!(await r.json()).configured : false;
           } catch { return false; }
       })();
       document.getElementById('vlabCloneZone').hidden = !elevenConfigured;
       document.getElementById('vlabDesignZone').hidden = !elevenConfigured;
       document.getElementById('vlabManageZone').hidden = !elevenConfigured;
       if (elevenConfigured) {
           renderCloneZone(canClone);
           loadMyVoices();
       }

       // Gate + load the Grok (xAI) zone — hidden when no XAI key.
       loadXaiVoices();

       // Gate + load the on-box Qwen zone — hidden unless the local stack is healthy.
       const qwenOk = await qwenTabAvailable();
       document.getElementById('vlabQwenZone').hidden = !qwenOk;
       if (qwenOk) loadQwenVoices();
   ```
   (The `elevenOk` line is inert scaffolding kept only if the executor prefers; it may be dropped. `canClone` is still computed above at line 791-799 unchanged.)

5. Manual verification (house rule — browser step; the version bump happens in Task 7.7):
   - Restart the service: `sudo systemctl restart blackbox.service` (pre-authorized), wait ~90s.
   - **Dev box (no-regression):** open the Portal, open Voice Lab. Expect the Qwen zone **hidden** (no local stack), and — if an ElevenLabs key is set — the ElevenLabs zones still render and work; if no key, the modal opens without ElevenLabs 400 spam. No console errors.
   - **MS02 Phase 2 (full):** with the stack healthy, the Qwen zone shows; clone a ~3s clip with consent → appears in "My on-box voices" and the picker (star-prefixed); design → preview (data: URLs play) → save → appears in both; delete → drops from both.

6. Commit:
   - **Run:** `git add Portal/voice-lab.js && git commit -m "feat(portal): Voice Lab Qwen (on-box) tab — clone/design/manage, gated on local-stack health"`

---

### Task 7.7: Portal picker routing + D10 slow-first-byte affordance + version bump

Route a `qwen:` voice through the single-call `POST /tts` path (the picker already lists the group via `populateVoiceCatalog`, no change needed there — it is catalog-driven). Add the D10 "loading models…" affordance for on-box TTS (a delayed toast if the first byte is slow — the group may be swapping in). Reveal the Voice Lab trigger when the local stack is healthy (so Qwen-only boxes can reach the tab). Bump the cache version.

**Files:**
- Modify: `Portal/modules/tts-stt.js` (`generateTTSAudioWithVoice` at lines 925-956; `setupVoiceLabTrigger` at lines 1953-1979)
- Modify: `Portal/index.html` (version string, lines 11 and 21)

**Steps:**

1. In `Portal/modules/tts-stt.js`, extend `generateTTSAudioWithVoice` (line 925) to include `qwen` in the single-call `/tts` branch and add the slow-first-byte affordance. Replace the block from line 930 (`if (voiceConfig.provider === "openai"…`) through line 956 (`return null;` closing that `if`) with:
   ```javascript
       // OpenAI, ElevenLabs, local (custom-server Kokoro), AND on-box Qwen are
       // single-call /tts providers (audio stream). Gemini falls through below.
       if (voiceConfig.provider === "openai" || voiceConfig.provider === "elevenlabs"
           || voiceConfig.provider === "local" || voiceConfig.provider === "qwen") {
           const isEleven = voiceConfig.provider === "elevenlabs";
           const isLocal = voiceConfig.provider === "local";
           const isQwen = voiceConfig.provider === "qwen";
           // D10 affordance: an on-box request may be queued behind a GPU group
           // swap (~6-10s cold). If the first byte is slow, tell the user we're
           // loading models rather than leaving a dead button. Cleared on return.
           let slowTimer = null;
           if (isQwen) slowTimer = setTimeout(() => toast("Loading on-box voice models…"), 1500);
           try {
               const r = await fetch("/tts", {
                   method: "POST",
                   headers: { "Content-Type": "application/json" },
                   body: JSON.stringify({
                       text: text,
                       // ElevenLabs/local/qwen: no OpenAI model; backend resolves it.
                       ...(isEleven || isLocal || isQwen ? {} : { model: TTS_MODEL }),
                       provider: voiceConfig.provider,
                       // Preserve the provider prefix so the backend detects the
                       // provider (elevenlabs:/local:/qwen: each trigger their branch).
                       voice: isEleven ? `elevenlabs:${voiceConfig.voice}`
                            : isLocal ? `local:${voiceConfig.voice}`
                            : isQwen ? `qwen:${voiceConfig.voice}`
                            : voiceConfig.voice,
                       format: TTS_FMT
                   })
               });
               if (r.ok) {
                   const blob = await r.blob();
                   return URL.createObjectURL(blob);
               }
               console.error(`${voiceConfig.provider} TTS failed:`, r.status, await r.text());
               return null;
           } finally {
               if (slowTimer) clearTimeout(slowTimer);
           }
       } else {
   ```
   (The existing `else {` that begins the Gemini branch at line 957 is now the `} else {` above — keep the Gemini branch body unchanged.)

2. In `setupVoiceLabTrigger` (line 1953), also reveal the button when the local stack is healthy. Replace the final reveal block (lines 1974-1978, the `fetch("/elevenlabs/status")…` chain) with:
   ```javascript
       // Reveal if EITHER an ElevenLabs key is configured OR the on-box stack is
       // healthy (so Qwen-only boxes can reach the Voice Lab / Qwen tab).
       Promise.allSettled([
           fetch("/elevenlabs/status").then(r => r.ok ? r.json() : null).then(s => !!(s && s.configured)),
           fetch("/local-models/status").then(r => r.ok ? r.json() : null)
               .then(s => !!(s && (s.healthy === true || s.status === "healthy"
                    || (s.capabilities && s.capabilities.tts && s.capabilities.tts.enabled)))),
       ]).then(results => {
           const show = results.some(x => x.status === "fulfilled" && x.value);
           if (show) btn.style.display = "";
       });
   ```

3. In `Portal/index.html`, bump the cache version `genui318` → `genui319` on line 11 and line 21:
   - Line 11: `<link rel="stylesheet" href="/ui/styles/main.css?v=genui319"/> <!-- v319: on-box Qwen3-TTS voices -->`
   - Line 21: `<script type="module" src="/ui/app-modular.js?v=genui319"></script> <!-- v319: on-box Qwen3-TTS voices -->`

4. Manual verification (house rule):
   - Restart: `sudo systemctl restart blackbox.service`, wait ~90s. Hard-refresh the Portal (the `?v=genui319` busts the cache).
   - **Dev box (no-regression):** the voice picker still populates; the "Qwen3-TTS (On-Box)" group is **absent** (no local stack); selecting any existing OpenAI/Gemini/ElevenLabs voice still speaks; no console errors.
   - **MS02 Phase 2 (full):** the picker shows "Qwen3-TTS (On-Box)" with the 9 presets (+ any saved profiles). Select `qwen:Vivian`, trigger auto-TTS/▶ speak → audio plays. On the first voice turn after a search (cross-group swap), the "Loading on-box voice models…" toast appears, then audio.

5. Commit:
   - **Run:** `git add Portal/modules/tts-stt.js Portal/index.html && git commit -m "feat(portal): route qwen: voices through /tts + D10 loading-models affordance + reveal Voice Lab on local stack (v319)"`

---

### Task 7.8: Android — Qwen routing correctness + preview fix + offline-fallback decision

The chat auto-TTS / manual-speak paths in `NativeMainActivity.kt` already pass `config.provider` generically via `buildTtsBatchBody` → `POST /tts/batch`, so a `qwen:` voice already routes correctly there (verified in recon). The gap is the **settings preview** button (`SettingsViewModel.previewVoice`), whose `when(cfg.provider)` sends non-OpenAI/ElevenLabs providers to the Gemini poll path — a `qwen:`/`local:` preview would fail. Fix it to route synchronous on-box/OpenAI providers through the generic `/tts/batch` call (using the `provider` parameter M0 added to `generateTts`).

> **Decision — do NOT add Qwen to the compiled-in `TTS_VOICE_GROUPS`.** That constant is the *offline fallback*, and by the existing convention it holds only the always-available cloud groups (OpenAI + Gemini). ElevenLabs and `local:` are dynamic-only (fetched live from `/tts/catalog`, absent from the fallback). Qwen is likewise on-box-only, so compiling it in would falsely advertise unavailable voices on a box without the stack. Qwen reaches Android exclusively via the live `fetchCatalog()`. (This is a deliberate no-op on `TTS_VOICE_GROUPS`, matching ElevenLabs/local.)

> **Depends on M0:** this task assumes `TtsRepository.generateTts` gained a `provider: String = "openai"` parameter (the M0 generic-provider fix). If M0 named it differently, adjust the two `generateTts(...)` call sites below.

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsViewModel.kt` (`previewVoice` `when`, lines 97-104)
- Create: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/QwenVoiceRoutingTest.kt`

**Steps:**

1. Write the failing unit test `…/app/src/test/java/com/aiblackbox/portal/QwenVoiceRoutingTest.kt`:
   ```kotlin
   package com.aiblackbox.portal

   import com.aiblackbox.portal.data.repository.TtsRepository
   import com.aiblackbox.portal.data.repository.TTS_VOICE_GROUPS
   import org.junit.Assert.assertEquals
   import org.junit.Assert.assertFalse
   import org.junit.Test

   /**
    * M7 Task 7.8 — Qwen voice routing key + offline-fallback convention.
    *   1. parseVoice("qwen:Vivian") yields provider=qwen / voice=Vivian, which is
    *      the routing key buildTtsBatchBody consumes (POST /tts/batch provider=qwen).
    *   2. Underscored preset tokens survive the split intact (voice=Uncle_Fu).
    *   3. Qwen is NOT in the compiled-in offline fallback (dynamic-only, like
    *      ElevenLabs/local) — it must come only from the live /tts/catalog.
    */
   class QwenVoiceRoutingTest {

       @Test
       fun parseVoice_qwenPreset_splitsProviderAndVoice() {
           val cfg = TtsRepository.parseVoice("qwen:Vivian")
           assertEquals("qwen", cfg.provider)
           assertEquals("Vivian", cfg.voice)
       }

       @Test
       fun parseVoice_qwenUnderscoreToken_preserved() {
           val cfg = TtsRepository.parseVoice("qwen:Uncle_Fu")
           assertEquals("qwen", cfg.provider)
           assertEquals("Uncle_Fu", cfg.voice)
       }

       @Test
       fun offlineFallback_hasNoQwenGroup() {
           val labels = TTS_VOICE_GROUPS.map { it.label }
           assertFalse(labels.any { it.contains("Qwen", ignoreCase = true) })
       }
   }
   ```

2. Run the offline unit gate, expect PASS on tests 1-3 (parseVoice already splits generically, and Qwen is not yet in the fallback — so this test *documents and locks* the correct behavior; it passes immediately and guards against regressions):
   - **Run:** `cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --offline --tests "com.aiblackbox.portal.QwenVoiceRoutingTest"`
   - **Expected:** `BUILD SUCCESSFUL`, 3 tests passed. (This is a characterization test; it confirms the routing key is already correct and pins the fallback convention.)

3. Fix the preview path in `SettingsViewModel.kt` — replace the `when (cfg.provider)` block (lines 97-104) with an explicit split that routes on-box/OpenAI synchronous providers generically:
   ```kotlin
                   val url = when {
                       cfg.provider == "elevenlabs" ->
                           repo.generateElevenLabsTts(text, cfg.voice).audio_url
                       cfg.provider == "gemini-pro" || cfg.provider == "gemini-flash" -> {
                           val sub = repo.generateGeminiTts(text, cfg.voice, cfg.model)
                           repo.pollGeminiTaskForUrl(sub.task_id)
                       }
                       // openai / local / qwen — synchronous /tts/batch, provider passed
                       // through (M0 generic-provider fix). Without this a qwen/local
                       // preview fell into the Gemini poll and failed.
                       else ->
                           repo.generateTts(text, cfg.voice, cfg.model, provider = cfg.provider).audio_url
                   }
   ```

4. Re-run the full offline unit gate to confirm no compile/test regressions:
   - **Run:** `cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --offline`
   - **Expected:** `BUILD SUCCESSFUL` (~35s), all tests pass, no compile errors.

5. Commit:
   - **Run:** `git add "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsViewModel.kt" "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/QwenVoiceRoutingTest.kt" && git commit -m "fix(android): route qwen/local voice preview through generic /tts/batch; lock dynamic-only fallback convention"`

---

### Task 7.9: Android — D10 "loading models…" slow-first-byte affordance

Surface the D10 affordance on the two Android TTS entry points that already show a spinner: the settings **preview** (`SettingsViewModel.previewVoice`) and the chat **manual-speak** path (`NativeMainActivity.onSpeakWithId`, which already toasts "Generating speech..."). For an on-box (`qwen`) voice, if the request is slow (first byte behind a GPU group swap), show "Loading on-box voice models…". HTTP TTS has no mid-request signal, so this is a client-side delayed timer (per correction — "for HTTP TTS use a slow-first-byte spinner state").

**Files:**
- Modify: `…/ui/settings/SettingsViewModel.kt` (`previewVoice`, add a slow flag)
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/NativeMainActivity.kt` (`onSpeakWithId` scope.launch, lines 535-560)

**Steps:**

1. In `SettingsViewModel.kt`, add a slow-state flow next to `_previewing` (after line 82's `previewError` declaration):
   ```kotlin
       // D10: flips true when a slow on-box preview is likely waiting on a GPU
       // group swap ("Loading models…"), so the UI can distinguish "generating"
       // from "loading the model in".
       private val _previewSlow = MutableStateFlow(false)
       val previewSlow: StateFlow<Boolean> = _previewSlow.asStateFlow()
   ```

2. In `previewVoice` (line 87), start a slow-timer for on-box voices and clear it in `finally`. Insert right after `_previewError.value = null` (line 91):
   ```kotlin
           _previewSlow.value = false
           val cfgForSlow = com.aiblackbox.portal.data.repository.TtsRepository.parseVoice(voiceId)
           val slowJob = if (cfgForSlow.provider == "qwen") viewModelScope.launch {
               kotlinx.coroutines.delay(1500)
               if (_previewing.value) _previewSlow.value = true
           } else null
   ```
   And in the `finally` block (currently line 109-111), add the cleanup:
   ```kotlin
               } finally {
                   slowJob?.cancel()
                   _previewSlow.value = false
                   _previewing.value = false
               }
   ```

3. In `NativeMainActivity.kt` `onSpeakWithId` (the `scope.launch` at line 535), after resolving `config` (line 543) and before building the request, start a delayed "loading models…" toast for on-box voices; cancel it once the response returns. Insert after line 543 (`val config = …parseVoice(voiceValue)`):
   ```kotlin
                                   // D10: on-box voices may wait on a GPU group swap.
                                   // Show "loading models…" only if the first byte is slow.
                                   val slowToastJob = if (config.provider == "qwen") scope.launch {
                                       kotlinx.coroutines.delay(1500)
                                       Toast.makeText(applicationContext, "Loading on-box voice models…", Toast.LENGTH_SHORT).show()
                                   } else null
   ```
   Then cancel it once the network call returns — insert immediately after the `response = withContext(Dispatchers.IO) { … }` block (after line 551):
   ```kotlin
                                   slowToastJob?.cancel()
   ```

4. Run the offline unit gate (no new unit tests — affordance is timing/UI; the gate confirms compilation):
   - **Run:** `cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --offline`
   - **Expected:** `BUILD SUCCESSFUL`, all tests pass (compilation of the new flow + jobs clean).

5. Manual Fold device validation (house rule — UI):
   - **Dev box backend (no-regression):** build/install the debug APK, point it at the dev box. Preview and speak an OpenAI/Gemini voice → works; no stray "Loading models…" toast (only fires for `qwen`). No logcat errors.
   - **MS02 Phase 2 (full):** point the APK at MS02, select `qwen:Vivian`. Trigger a speak right after a search (forces a cross-group swap) → the "Loading on-box voice models…" toast appears within ~1.5s, then audio plays. A fast (warm audio group) synthesis shows no toast.

6. Wire the `previewSlow` flow into the settings screen's preview spinner label (manual Fold step — locate the composable observing `viewModel.previewing` and show "Loading models…" when `previewSlow` is true; the exact composable is in the settings screen file, unchanged here beyond the ViewModel state it reads).

7. Commit:
   - **Run:** `git add "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsViewModel.kt" "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/NativeMainActivity.kt" && git commit -m "feat(android): D10 loading-models slow-first-byte affordance for on-box (qwen) TTS"`

---

### Milestone 7 — completion checklist

- Backend: `qwen` catalog group (fail-open), `POST /tts` + `POST /tts/batch` qwen branches, and 5 `/qwen/voices/*` proxy endpoints — all covered by `test_qwen_tts_module.py`, `test_qwen_tts_routes.py`, `test_qwen_voices_proxy.py`.
  - **Run:** `python -m pytest Orchestrator/tests/test_qwen_tts_module.py Orchestrator/tests/test_qwen_tts_routes.py Orchestrator/tests/test_qwen_voices_proxy.py Orchestrator/tests/test_tts_routes_elevenlabs_synth.py -v`
  - **Expected:** all pass (qwen suites + the ElevenLabs regression guard).
- Portal: Voice Lab Qwen tab + catalog-driven picker + D10 affordance + `?v=genui319`. Manual browser step done (dev-box no-regression; full validation deferred to MS02 Phase 2).
- Android: preview routing fixed, Qwen kept out of the offline fallback (locked by `QwenVoiceRoutingTest`), D10 affordance added. `./gradlew :app:testDebugUnitTest --offline` green; manual Fold step done.
- Mint a `/snapshot-dev` after the milestone lands.


---

## Milestone 8: Onboarding wizard "local_models" step + Updates panel status

**Depends on:** M1 (`Orchestrator/local_stack.py` resolver; `[local_models]` config.ini section written by the installer; `GET /local-models/status`; `POST /local-models/download`; the `localstack` embedding registry slugs `qwen3-embedding-8b-local` / `qwen3-embedding-0.6b-local`), M2 (`localstack` reranker registered in `rerank.KNOWN_PROVIDERS` + the `qwen3-reranker-0.6b-local` `RERANK_MODELS` slug), and the STT milestone (`resolve_stt_provider` accepts the `onbox` token; `/stt/catalog` exposes an `onbox` entry). The backend registration (Task 8.1–8.3) is self-contained and testable **without** those milestones landed; the wizard step (Task 8.4–8.5) consumes their endpoints **fail-open** (every read is defensive) so it renders meaningfully even before they are wired.

Add a dedicated `local_models` onboarding step so a customer can see their hardware tier + disk headroom, download the on-box weights with progress, and deliberately activate STT / TTS / embeddings / reranking on-box per capability — never implicitly (D2, §8). The transcription step also gains an "On-box (local)" provider option (`STT_PROVIDER=onbox`), and the Updates panel gets a **status-only** card reading `/local-models/status` (panels are status-only; selection lives in the wizard). Every activation flip reuses an existing endpoint (`/embeddings/reembed`, `/rerank/select`, `/onboarding/save`) plus one thin M08 endpoint for the `[local_models]` stt/tts flags; the embeddings cutover is gated by an `nvidia-smi` near-idle blocking check (Phase-2 Step-0, §10) and followed by the `/toolvault/reload` sequencing (§5.1).

---

### Task 8.1: Register the `local_models` step across all five parity surfaces

The wizard couples FIVE lists that a source-text parity test suite (`test_onboarding_steps_parity.py`) guards: `state.ALL_STEPS` ↔ `onboarding.js` `STEPS` ↔ `onboarding.js` `STEP_LABELS` ↔ `status.js` `SECTIONS` ↔ `status_rollup.SECTIONS`, plus a `steps/<step>.js` module must exist for every step (or the dynamic `import()` throws and the user is stuck). Land all five atomically with a minimal-but-functional placeholder module so the tree stays runnable and the whole parity suite stays green. Position: **immediately after `embeddings`**, group **`keys`** (the group label is literally "Keys & Models", and this step is on-box *models*).

**Files:**
- Modify: `Orchestrator/onboarding/state.py` (`StepName` Literal :30-44; `ALL_STEPS` :46-52)
- Modify: `Portal/onboarding/onboarding.js` (`STEPS` :5-8; `STEP_LABELS` :15-29)
- Modify: `Portal/onboarding/status.js` (`SECTIONS` :20-32)
- Modify: `Orchestrator/onboarding/status_rollup.py` (`SECTIONS` :33-45)
- Create: `Portal/onboarding/steps/local_models.js` (minimal placeholder; Task 8.4 replaces it wholesale)
- Create (Test): `Orchestrator/tests/test_local_models_onboarding_step.py`

1. Add the registration test. Create `Orchestrator/tests/test_local_models_onboarding_step.py`:
   ```python
   """Onboarding registration for the on-box 'local_models' wizard step (M8).

   Mirrors test_stt_onboarding_step.py: the step must be a first-class member of
   ALL_STEPS (so /onboarding/step/complete|skip don't 500 with a ValueError) and
   must sit immediately after 'embeddings' — matching the frontend STEPS order.
   Hermetic: OnboardingState persists to a module-level STATE_FILE we redirect to
   a tmp file, so the real .onboarding_state.json is never touched.
   """
   import pytest

   from Orchestrator.onboarding import state as st


   def test_local_models_in_all_steps_after_embeddings():
       assert "local_models" in st.ALL_STEPS
       i = st.ALL_STEPS.index("local_models")
       assert st.ALL_STEPS[i - 1] == "embeddings"
       assert st.ALL_STEPS[i + 1] == "optional_integrations"


   def test_step_complete_accepts_local_models(tmp_path, monkeypatch):
       monkeypatch.setattr(st, "STATE_FILE", tmp_path / ".onboarding_state.json")
       monkeypatch.setattr(st, "COMPLETE_SENTINEL", tmp_path / ".onboarding_complete")
       s = st.OnboardingState()
       # None of these validate-gated calls may raise for the new step.
       s.set_current("local_models")
       s.mark_step_complete("local_models")
       s.mark_step_skipped("local_models")
       snap = s.snapshot()
       assert "local_models" in snap["all_steps"]
       assert "local_models" in snap["skipped_steps"]
       assert "local_models" not in snap["completed_steps"]  # skip removed it


   def test_unknown_step_still_rejected(tmp_path, monkeypatch):
       monkeypatch.setattr(st, "STATE_FILE", tmp_path / ".onboarding_state.json")
       monkeypatch.setattr(st, "COMPLETE_SENTINEL", tmp_path / ".onboarding_complete")
       s = st.OnboardingState()
       with pytest.raises(ValueError):
           s.mark_step_complete("not_a_real_step")
   ```
2. **Run:** `python -m pytest Orchestrator/tests/test_local_models_onboarding_step.py Orchestrator/tests/test_onboarding_steps_parity.py -q`
   **Expected:** FAIL — `test_local_models_in_all_steps_after_embeddings` (KeyError/AssertionError: not in `ALL_STEPS`) plus the parity suite (`test_frontend_steps_match_backend_all_steps`, `test_every_dynamically_imported_step_has_a_module_file`, `test_status_rollup_sections_match_all_steps_minus_welcome_done`, `test_status_sections_match_steps_minus_welcome_done`, `test_frontend_step_labels_cover_every_step`) once the next steps begin — right now only the registration test fails because nothing is added yet.
3. Add the step to `state.py`. In the `StepName` Literal (after `"embeddings",` on :34) insert `    "local_models",`; in `ALL_STEPS` change the first list line (:47) from `    "welcome", "tailscale", "api_keys", "embeddings",` to `    "welcome", "tailscale", "api_keys", "embeddings", "local_models",`.
4. Add the step to `Portal/onboarding/onboarding.js`. In `STEPS` (:6) change `    "welcome", "tailscale", "api_keys", "embeddings",` to `    "welcome", "tailscale", "api_keys", "embeddings", "local_models",`. In `STEP_LABELS` add after the `embeddings:` line (:19) a new line `    local_models: "ON-BOX MODELS",`.
5. Add the section to `Portal/onboarding/status.js` `SECTIONS`. After the `embeddings` row (:23) insert:
   ```javascript
       { key: "local_models",           group: "keys",         label: "On-Box Models", required: false },
   ```
6. Add the section to `Orchestrator/onboarding/status_rollup.py` `SECTIONS`. After the `embeddings` row (:36) insert:
   ```python
       {"key": "local_models",           "group": "keys",         "label": "On-Box Models", "required": False},
   ```
7. Create the placeholder `Portal/onboarding/steps/local_models.js` (Task 8.4 replaces this in full):
   ```javascript
   // On-box local model stack step (M8) — PLACEHOLDER. Full UI lands in Task 8.4.
   // Registered now so the parity guard (a steps/<step>.js module must exist for
   // every STEPS entry) is satisfied and the wizard can advance past this step.
   export async function render(container, { next, back, skip, sigil }) {
       const s = sigil || { num: "05", backLabel: "memory & search" };
       container.innerHTML = `
           <section class="ob-step ob-local-models">
               <aside class="ob-step-sigil" aria-hidden="true">
                   <div class="ob-step-sigil-num"><em>${s.num}</em></div>
                   <div class="ob-step-sigil-rule"></div>
                   <div class="ob-step-sigil-label">ON-BOX</div>
               </aside>
               <div class="ob-step-body">
                   <div class="ob-step-eyebrow">
                       <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                       On-box model stack
                   </div>
                   <h1 class="ob-step-title">Run models <em>on the box</em>.</h1>
                   <p class="ob-step-lede">Setup for on-box speech, memory, and
                       reranking is being prepared. You can skip for now.</p>
                   <nav class="ob-step-nav" aria-label="Step navigation">
                       <button type="button" class="ob-back" id="ob-lm-back">
                           <span aria-hidden="true">&larr;</span> Back to ${s.backLabel ? s.backLabel.toLowerCase() : "memory & search"}
                       </button>
                       <button type="button" class="ob-cta" id="ob-lm-continue">
                           Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                       </button>
                       <button type="button" class="ob-skip" id="ob-lm-skip">
                           Skip &mdash; set up later <span aria-hidden="true">&rarr;</span>
                       </button>
                   </nav>
               </div>
           </section>`;
       document.getElementById("ob-lm-back").addEventListener("click", back);
       document.getElementById("ob-lm-continue").addEventListener("click", next);
       document.getElementById("ob-lm-skip").addEventListener("click", skip);
   }
   ```
8. **Run:** `python -m pytest Orchestrator/tests/test_local_models_onboarding_step.py Orchestrator/tests/test_onboarding_steps_parity.py Orchestrator/tests/test_onboarding_status.py -q`
   **Expected:** PASS — all parity + registration tests green (the `_derive_default` fallback handles the new section until Task 8.2 adds a richer deriver).
9. **Commit** (explicit paths): `git add Orchestrator/onboarding/state.py Portal/onboarding/onboarding.js Portal/onboarding/status.js Orchestrator/onboarding/status_rollup.py Portal/onboarding/steps/local_models.js Orchestrator/tests/test_local_models_onboarding_step.py && git commit -m "feat(onboarding): register local_models wizard step across parity surfaces"`

---

### Task 8.2: Hub status rollup deriver for `local_models`

Give the hub section a real (probe-free) summary instead of the generic `_derive_default`: read the persisted `[local_models]` per-capability flags + the `STT_PROVIDER` env, and report how many capabilities resolve on-box. The rollup contract is PURE — no subprocess/HTTP probe (the live llama-swap health check belongs to the wizard step's own `/local-models/status` fetch). `required: False`, so absence is always OPTIONAL, never ATTENTION.

**Files:**
- Modify: `Orchestrator/onboarding/status_rollup.py` (add `_derive_local_models`; dispatch in `build_status` :275-296; add `local_models` kwarg :268-270)
- Modify: `Orchestrator/routes/onboarding_routes.py` (`_collect_status_inputs` :782-892 — add the cheap `local_models` snapshot; `onboarding_status` already forwards `**_collect_status_inputs()`)
- Create (Test): `Orchestrator/tests/test_local_models_status_rollup.py`

1. Add the deriver test. Create `Orchestrator/tests/test_local_models_status_rollup.py`:
   ```python
   """status_rollup._derive_local_models (M8): probe-free hub summary for the
   on-box stack, driven purely by the persisted [local_models] flags + the
   STT_PROVIDER env snapshot passed in by _collect_status_inputs."""
   from Orchestrator.onboarding import status_rollup as sr


   def _lm(**kw):
       base = {"enabled": False,
               "capabilities": {"stt": False, "tts": False,
                                "embeddings": False, "rerank": False},
               "stt_provider": ""}
       base.update(kw)
       return base


   def test_disabled_is_optional():
       st, summary, items, atts = sr._derive_local_models(_lm())
       assert st == sr.OPTIONAL
       assert atts == []


   def test_stt_onbox_via_env_counts_even_when_flag_missing():
       st, summary, items, atts = sr._derive_local_models(_lm(stt_provider="onbox"))
       assert st == sr.READY
       assert "1" in summary


   def test_multiple_capabilities_ready():
       lm = _lm(enabled=True,
                capabilities={"stt": True, "tts": True,
                              "embeddings": True, "rerank": False},
                stt_provider="onbox")
       st, summary, items, atts = sr._derive_local_models(lm)
       assert st == sr.READY
       assert "3" in summary  # stt + tts + embeddings (stt not double-counted)


   def test_build_status_accepts_local_models_kwarg():
       # build_status must thread the new kwarg (regression guard against a
       # missing dispatch branch falling back to _derive_default silently).
       out = sr.build_status(
           env={}, state={"completed_steps": [], "skipped_steps": [], "validated_at": {}},
           embeddings={"active": None, "health": {"state": "ok"}, "stores": [], "models": []},
           cli={"providers": {}}, web_search={"enabled": [], "providers": {}, "default": ""},
           image={"enabled": [], "providers": {}, "default": ""}, paired=[], operators=["A"],
           restart={"needs_restart": False}, local_models=_lm(enabled=True,
               capabilities={"stt": True, "tts": False, "embeddings": False, "rerank": False}),
       )
       sec = next(s for s in out["sections"] if s["key"] == "local_models")
       assert sec["state"] == sr.READY
   ```
2. **Run:** `python -m pytest Orchestrator/tests/test_local_models_status_rollup.py -q`
   **Expected:** FAIL — `AttributeError: module ... has no attribute '_derive_local_models'` and `build_status()` rejects the unexpected `local_models` kwarg.
3. Add `_derive_local_models` to `status_rollup.py` (place it beside `_derive_embeddings`, after :192):
   ```python
   def _derive_local_models(local_models):
       """On-box stack hub summary — PERSISTED flags only, no probe. The live
       llama-swap health check is the wizard step's own /local-models/status
       fetch; this section stays fast + probe-free like the rest of the rollup.
       required:False → absence is OPTIONAL, never ATTENTION."""
       lm = local_models or {}
       caps = lm.get("capabilities") or {}
       on = {c for c in ("stt", "tts", "embeddings", "rerank") if caps.get(c)}
       # STT can be pinned on-box purely via STT_PROVIDER=onbox (the router
       # preference) even when the [local_models].stt seed flag is unset — count it.
       if (lm.get("stt_provider") or "").strip().lower() == "onbox":
           on.add("stt")
       items = [{"key": c, "label": c, "configured": c in on, "validated_at": None}
                for c in ("stt", "tts", "embeddings", "rerank")]
       if not lm.get("enabled") and not on:
           return OPTIONAL, "Not set up", items, []
       n = len(on)
       word = "capability" if n == 1 else "capabilities"
       return READY, f"{n} {word} on-box", items, []
   ```
4. Add the `local_models` parameter + dispatch to `build_status`. In the signature (:268-270) add `local_models=None` (keyword-only, after `custom_servers=None`):
   ```python
   def build_status(*, env, state, embeddings, cli, web_search, image,
                    paired, operators, restart, mcp=None, rerank=None,
                    custom_servers=None, local_models=None, is_complete=False):
   ```
   In the per-section `if/elif` chain (after the `embeddings` branch, :283-284) add:
   ```python
           elif key == "local_models":
               st, summary, items, atts = _derive_local_models(local_models)
   ```
5. Add the cheap snapshot to `_collect_status_inputs` in `onboarding_routes.py`. Immediately before the final `return dict(...)` (:887) insert:
   ```python
       # local_models (M8) — probe-free: read the [local_models] flags FRESH from
       # config.ini (the resolver's source of truth; a few-ms stdlib read, no
       # subprocess/HTTP) + STT_PROVIDER from the already-read .env snapshot.
       local_models = _read_local_models_snapshot(env)
   ```
   Add the `local_models=local_models,` entry inside the `return dict(...)` call (alongside `rerank=rerank_block,`). Then add the helper just above `_collect_status_inputs` (after the `from Orchestrator.onboarding import status_rollup` line :779):
   ```python
   def _read_local_models_snapshot(env: dict) -> dict:
       """FAST persisted snapshot of the on-box stack for the hub rollup. Reads
       config.ini [local_models] fresh via stdlib configparser (the section is
       written by the installer/M1 + the M8 capability endpoint; the resolver
       reads it fresh too). Fail-soft to all-off. No probe."""
       import configparser
       from Orchestrator.utils.paths import resolve
       enabled = False
       caps = {"stt": False, "tts": False, "embeddings": False, "rerank": False}
       try:
           cp = configparser.ConfigParser()
           cp.read(resolve("config.ini"))
           if cp.has_section("local_models"):
               enabled = cp.getboolean("local_models", "enabled", fallback=False)
               for c in caps:
                   caps[c] = cp.getboolean("local_models", c, fallback=False)
       except Exception:
           logger.exception("status rollup: local_models config read failed")
       return {"enabled": enabled, "capabilities": caps,
               "stt_provider": (env.get("STT_PROVIDER") or "").strip().lower()}
   ```
6. **Run:** `python -m pytest Orchestrator/tests/test_local_models_status_rollup.py Orchestrator/tests/test_onboarding_status.py -q`
   **Expected:** PASS.
7. **Commit:** `git add Orchestrator/onboarding/status_rollup.py Orchestrator/routes/onboarding_routes.py Orchestrator/tests/test_local_models_status_rollup.py && git commit -m "feat(onboarding): probe-free local_models hub status deriver"`

---

### Task 8.3: Backend — `[local_models]` stt/tts flag endpoint + `nvidia-smi` GPU preflight

Two thin M08 endpoints under the `/local-models/*` namespace (M1 owns `/local-models/status` + `/download` under the same prefix — FastAPI serves multiple routers sharing a prefix as long as paths differ). `POST /local-models/capability` flips the `[local_models]` stt/tts seed flag (config.ini, fresh read-modify-write, NOT the import-time `CFG`; M1's resolver reads the section fresh per request) and, for `stt`, mirrors `STT_PROVIDER` in `.env` (the actual STT router preference — enable→`onbox`, disable→cleared). `GET /local-models/gpu-preflight` shells `nvidia-smi` to surface the Phase-2 Step-0 near-idle precondition (§10) as a blocking check the wizard gates the embeddings cutover on; fail-open on a GPU-less/`nvidia-smi`-absent box (no contention possible).

**Files:**
- Create: `Orchestrator/routes/local_stack_routes.py`
- Modify: `Orchestrator/app.py` (mount the router next to the other `include_router` calls, :126-133)
- Create (Test): `Orchestrator/tests/test_local_stack_routes.py`

1. Write the endpoint tests. Create `Orchestrator/tests/test_local_stack_routes.py`:
   ```python
   """M8 on-box wizard-activation endpoints: POST /local-models/capability
   (writes [local_models] stt/tts flags + mirrors STT_PROVIDER) and
   GET /local-models/gpu-preflight (nvidia-smi near-idle blocking check).

   Hermetic: config.ini path + .env writer path are redirected to tmp; the GPU
   probe helper is monkeypatched so no real nvidia-smi runs."""
   import configparser

   import pytest
   from fastapi.testclient import TestClient


   @pytest.fixture
   def client(tmp_path, monkeypatch):
       from Orchestrator.onboarding import secrets_writer
       from Orchestrator.routes import local_stack_routes as lsr
       cfg = tmp_path / "config.ini"
       cfg.write_text("[users]\nlist = A\n")
       env = tmp_path / ".env"
       env.write_text("")
       monkeypatch.setattr(lsr, "CONFIG_INI", cfg)
       monkeypatch.setattr(secrets_writer, "ENV_FILE", env)
       from Orchestrator.app import app
       with TestClient(app) as c:
           c._cfg, c._env = cfg, env
           yield c


   def _flags(cfg):
       cp = configparser.ConfigParser()
       cp.read(cfg)
       return cp


   def test_enable_tts_writes_config_flag(client):
       r = client.post("/local-models/capability",
                       json={"capability": "tts", "enabled": True})
       assert r.status_code == 200 and r.json()["enabled"] is True
       cp = _flags(client._cfg)
       assert cp.getboolean("local_models", "tts") is True
       # tts must NOT touch STT_PROVIDER
       assert "STT_PROVIDER" not in client._env.read_text()


   def test_enable_stt_writes_flag_and_mirrors_provider(client):
       r = client.post("/local-models/capability",
                       json={"capability": "stt", "enabled": True})
       assert r.status_code == 200
       cp = _flags(client._cfg)
       assert cp.getboolean("local_models", "stt") is True
       assert "STT_PROVIDER=onbox" in client._env.read_text()


   def test_disable_stt_clears_provider(client):
       client.post("/local-models/capability", json={"capability": "stt", "enabled": True})
       client.post("/local-models/capability", json={"capability": "stt", "enabled": False})
       cp = _flags(client._cfg)
       assert cp.getboolean("local_models", "stt") is False
       # STT_PROVIDER cleared to "" (auto) — the on-box pin is removed.
       txt = client._env.read_text()
       assert "STT_PROVIDER=onbox" not in txt


   def test_rejects_unknown_capability(client):
       r = client.post("/local-models/capability",
                       json={"capability": "embeddings", "enabled": True})
       assert r.status_code == 400  # embeddings/rerank activate via their own endpoints


   def test_gpu_preflight_idle_ok(client, monkeypatch):
       from Orchestrator.routes import local_stack_routes as lsr
       monkeypatch.setattr(lsr, "_probe_gpu_usage",
                           lambda: {"present": True, "used_mib": 300,
                                    "total_mib": 16380, "processes": []})
       r = client.get("/local-models/gpu-preflight")
       assert r.status_code == 200 and r.json()["ok"] is True


   def test_gpu_preflight_busy_blocks(client, monkeypatch):
       from Orchestrator.routes import local_stack_routes as lsr
       monkeypatch.setattr(lsr, "_probe_gpu_usage",
                           lambda: {"present": True, "used_mib": 7100, "total_mib": 16380,
                                    "processes": [{"pid": 42, "name": "ollama", "used_mib": 6994}]})
       r = client.get("/local-models/gpu-preflight")
       assert r.status_code == 200 and r.json()["ok"] is False
       assert r.json()["used_mib"] == 7100


   def test_gpu_preflight_no_gpu_is_ok(client, monkeypatch):
       from Orchestrator.routes import local_stack_routes as lsr
       monkeypatch.setattr(lsr, "_probe_gpu_usage",
                           lambda: {"present": False, "used_mib": None,
                                    "total_mib": None, "processes": []})
       r = client.get("/local-models/gpu-preflight")
       assert r.status_code == 200 and r.json()["ok"] is True  # CPU box: no contention
   ```
2. **Run:** `python -m pytest Orchestrator/tests/test_local_stack_routes.py -q`
   **Expected:** FAIL — `ModuleNotFoundError: Orchestrator.routes.local_stack_routes`.
3. Create `Orchestrator/routes/local_stack_routes.py`:
   ```python
   """On-box local-model-stack wizard-activation endpoints (M8).

   Shares the /local-models/* prefix with M1's status/download router (FastAPI
   serves multiple routers under one prefix as long as paths differ).

     POST /local-models/capability   — flip the [local_models] stt|tts seed flag
                                        (+ mirror STT_PROVIDER for stt).
     GET  /local-models/gpu-preflight — nvidia-smi near-idle blocking check the
                                        wizard gates the embeddings cutover on
                                        (Phase-2 Step-0, §10). Fail-open on CPU.

   Config writes use a FRESH configparser read-modify-write of config.ini (NOT
   the import-time Orchestrator.config.CFG); M1's local_stack resolver reads the
   [local_models] section fresh per request, so the flip takes effect with no
   restart. config.ini is a gitignored per-box file — never committed.
   """
   from __future__ import annotations

   import configparser
   import logging
   import os
   import subprocess

   from fastapi import APIRouter, HTTPException
   from pydantic import BaseModel

   from Orchestrator.onboarding.secrets_writer import update_env
   from Orchestrator.utils.paths import resolve

   logger = logging.getLogger(__name__)

   router = APIRouter(prefix="/local-models", tags=["local-models"])

   # Monkeypatched in tests to a tmp config.ini. Resolved once at import; the
   # helpers below always read/write THIS path so a test redirect is honored.
   CONFIG_INI = resolve("config.ini")

   # Only stt/tts activate via the [local_models] seed flag here. embeddings
   # activate via POST /embeddings/reembed (the corpus cutover) and rerank via
   # POST /rerank/select — each has its own persistence + validation ladder.
   _FLAG_CAPS = {"stt", "tts"}

   # GPU is "near-idle" enough to safely lazy-load the retrieval group when the
   # resident footprint is below this. The pinned pair the Phase-2 reset retires
   # is ~10GB (Ollama 8B ~7GB + vLLM ~3.3GB), so a 2GB ceiling reliably catches
   # "the old embedder/reranker is still resident" without tripping on the small
   # CU/Xvfb llvmpipe footprint (CU renders on CPU, never VRAM).
   _GPU_IDLE_CEIL_MIB = 2048


   class CapabilityRequest(BaseModel):
       capability: str
       enabled: bool


   def _set_local_flag(capability: str, enabled: bool) -> None:
       """Atomic fresh read-modify-write of config.ini [local_models].<cap>."""
       cp = configparser.ConfigParser()
       cp.read(CONFIG_INI)
       if not cp.has_section("local_models"):
           cp.add_section("local_models")
       cp.set("local_models", capability, "true" if enabled else "false")
       tmp = str(CONFIG_INI) + ".tmp"
       with open(tmp, "w") as f:
           cp.write(f)
       os.replace(tmp, CONFIG_INI)


   @router.post("/capability")
   def set_capability(req: CapabilityRequest) -> dict:
       cap = (req.capability or "").strip().lower()
       if cap not in _FLAG_CAPS:
           raise HTTPException(
               status_code=400,
               detail=(f"capability must be one of {sorted(_FLAG_CAPS)}; "
                       "embeddings activate via /embeddings/reembed and rerank "
                       "via /rerank/select"),
           )
       _set_local_flag(cap, req.enabled)
       # STT routing mirror: the on-box token is the actual resolver preference
       # (resolve_stt_provider). Enable → pin 'onbox'; disable → clear to auto.
       # TTS has no global provider env (the qwen catalog is voice-pick-driven),
       # so the [local_models].tts seed flag above is its whole activation.
       if cap == "stt":
           update_env({"STT_PROVIDER": "onbox" if req.enabled else ""})
       return {"ok": True, "capability": cap, "enabled": req.enabled}


   def _probe_gpu_usage() -> dict:
       """Return {present, used_mib, total_mib, processes[]} via nvidia-smi.
       present=False (no GPU / nvidia-smi absent / any error) → callers treat as
       'no contention'. Monkeypatched in tests."""
       try:
           out = subprocess.run(
               ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits"],
               capture_output=True, text=True, timeout=5,
           )
           if out.returncode != 0 or not out.stdout.strip():
               return {"present": False, "used_mib": None, "total_mib": None, "processes": []}
           used_s, _, total_s = out.stdout.strip().splitlines()[0].partition(",")
           used_mib, total_mib = int(used_s.strip()), int(total_s.strip())
           procs = []
           papp = subprocess.run(
               ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits"],
               capture_output=True, text=True, timeout=5,
           )
           if papp.returncode == 0:
               for line in papp.stdout.strip().splitlines():
                   if not line.strip():
                       continue
                   parts = [p.strip() for p in line.split(",")]
                   if len(parts) >= 3:
                       procs.append({"pid": parts[0], "name": parts[1],
                                     "used_mib": int(parts[2]) if parts[2].isdigit() else None})
           return {"present": True, "used_mib": used_mib, "total_mib": total_mib, "processes": procs}
       except Exception:
           logger.info("gpu-preflight: nvidia-smi probe failed (treating as no GPU)", exc_info=True)
           return {"present": False, "used_mib": None, "total_mib": None, "processes": []}


   @router.get("/gpu-preflight")
   def gpu_preflight() -> dict:
       """Phase-2 Step-0 near-idle precondition (§10). The wizard gates the
       on-box embeddings cutover on ok=true: the retrieval group lazy-loads
       ~11.5-13GB on the first re-embed and would CUDA-OOM if the old pinned
       Ollama 8B / vLLM reranker were still resident. Fail-open on a GPU-less
       box (no VRAM contention is possible)."""
       g = _probe_gpu_usage()
       if not g["present"]:
           return {"ok": True, "present": False, "used_mib": None, "total_mib": None,
                   "processes": [], "detail": "No NVIDIA GPU detected — no VRAM contention."}
       ok = (g["used_mib"] or 0) <= _GPU_IDLE_CEIL_MIB
       detail = ("GPU near-idle — safe to load the on-box retrieval group."
                 if ok else
                 f"GPU holds {g['used_mib']} MiB (ceiling {_GPU_IDLE_CEIL_MIB} MiB). "
                 "Free it first — stop the old embedder/reranker "
                 "(vllm-reranker.service and the pinned Ollama 8B) — then retry.")
       return {"ok": ok, "present": True, "used_mib": g["used_mib"],
               "total_mib": g["total_mib"], "processes": g["processes"],
               "ceiling_mib": _GPU_IDLE_CEIL_MIB, "detail": detail}
   ```
4. Mount the router in `Orchestrator/app.py`. After the rerank mount (:132-133) add:
   ```python
   from Orchestrator.routes.local_stack_routes import router as local_stack_router
   app.include_router(local_stack_router)
   ```
5. **Run:** `python -m pytest Orchestrator/tests/test_local_stack_routes.py -q`
   **Expected:** PASS (all 7 tests).
6. **Commit:** `git add Orchestrator/routes/local_stack_routes.py Orchestrator/app.py Orchestrator/tests/test_local_stack_routes.py && git commit -m "feat(local-models): stt/tts capability flag + nvidia-smi GPU preflight endpoints"`

---

### Task 8.4: Build the full `local_models` wizard step UI

Replace the placeholder with the real step: fetch `GET /local-models/status`, render hardware tier + disk headroom, a per-capability recommendation table (§7, tier-aware with a static fallback so it renders before M1 enriches status), one-click downloads with NDJSON progress (cloned from the embeddings-pull pattern), and deliberate per-capability activation. Embeddings activation runs the GPU-idle preflight as a **blocking gate**, fires `POST /embeddings/reembed`, then on job completion fires `POST /toolvault/reload` (the §5.1 cache-coherence sequencing). Honest CPU-tier warnings + the D10 "first use after idle takes a few seconds" note + the D9 "~6-10s the first time you switch between voice and search" cross-group note are always shown. All reads are fail-open.

**Files:**
- Modify (replace): `Portal/onboarding/steps/local_models.js`
- Modify: `Portal/onboarding/onboarding.css` (append the small `.ob-lm-*` block below)
- Test: manual browser walk (house rule — no JS test infra) + backend parity suite still green

1. Replace `Portal/onboarding/steps/local_models.js` entirely with:
   ```javascript
   // On-box local model stack step (M8). Reads GET /local-models/status (M1) and
   // lets the user download weights + deliberately activate STT/TTS/embeddings/
   // reranking on-box, per capability (D2: nothing activates implicitly). Every
   // status read is fail-open — the step renders even if the stack isn't
   // installed yet. Activation flips reuse existing endpoints:
   //   embeddings → GPU-idle preflight (blocking) → /embeddings/reembed → on done
   //                /toolvault/reload (§5.1 cache coherence)
   //   rerank     → /rerank/select {provider:'localstack'}
   //   stt / tts  → /local-models/capability (config seed flag; stt also pins
   //                STT_PROVIDER=onbox)
   import { stepSigilContext } from "../onboarding.js";

   let status = null;         // last GET /local-models/status
   let busy = {};             // per-capability activation in-flight guard
   let downloading = {};      // model key -> {completed,total,statusText}
   let pollTimer = null;      // /embeddings/status poll during the reembed cutover

   // Static §7 fallback so the table is meaningful before M1's status carries
   // per-tier recommendations. Keyed by a coarse tier: 'gpu' | 'cpu'.
   const REC_FALLBACK = {
       gpu: {
           embeddings: { label: "Qwen3-Embedding-8B (Q8_0, 4096-dim)", size: "~8 GB", note: "" },
           rerank: { label: "Qwen3-Reranker-0.6B", size: "~1.3 GB", note: "Validated by benchmark before selection." },
           stt: { label: "whisper large-v3-turbo (stream) + large-v3 (files)", size: "~5 GB", note: "" },
           tts: { label: "Qwen3-TTS 0.6B-CustomVoice (streaming) · 1.7B (files)", size: "~9 GB", note: "" },
       },
       cpu: {
           embeddings: { label: "Qwen3-Embedding-0.6B (1024-dim)", size: "~0.6 GB", note: "Fast on CPU." },
           rerank: { label: "Qwen3-Reranker-0.6B (CPU)", size: "~1.3 GB", note: "Latency-gated; may fall back to cloud." },
           stt: { label: "whisper large-v3-turbo (int8)", size: "~1.6 GB", note: "Near-realtime for files; streaming may lag." },
           tts: { label: "Cloud recommended", size: "—", note: "On-box TTS is far slower than realtime on CPU — offered as experimental only." },
       },
   };

   const CAPS = [
       { id: "embeddings", label: "Memory (embeddings)" },
       { id: "rerank", label: "Search reranking" },
       { id: "stt", label: "Speech-to-text" },
       { id: "tts", label: "Text-to-speech" },
   ];

   export async function render(container, { next, back, skip, sigil }) {
       const sig = sigil || stepSigilContext("local_models");
       container.innerHTML = `
           <section class="ob-step ob-local-models">
               <aside class="ob-step-sigil" aria-hidden="true">
                   <div class="ob-step-sigil-num"><em>${sig.num}</em></div>
                   <div class="ob-step-sigil-rule"></div>
                   <div class="ob-step-sigil-label">ON-BOX</div>
               </aside>
               <div class="ob-step-body">
                   <div class="ob-step-eyebrow">
                       <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                       On-box model stack
                   </div>
                   <h1 class="ob-step-title">Run speech, memory &amp; search <em>on the box</em>.</h1>
                   <p class="ob-step-lede">
                       When your hardware allows, the BlackBox runs transcription,
                       voice, memory embeddings, and search reranking locally — the
                       only thing that leaves the box is the chat model itself.
                       Everything here is optional and turned on one capability at a
                       time. An explicit provider you've already chosen (e.g. your
                       ElevenLabs key) is never overridden.
                   </p>
                   <div id="ob-lm-body"><div class="ob-loading">Checking your hardware&hellip;</div></div>
                   <nav class="ob-step-nav" aria-label="Step navigation">
                       <button type="button" class="ob-back" id="ob-lm-back">
                           <span aria-hidden="true">&larr;</span> Back to ${sig.backLabel ? sig.backLabel.toLowerCase() : "memory & search"}
                       </button>
                       <button type="button" class="ob-cta" id="ob-lm-continue">
                           Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                       </button>
                       <button type="button" class="ob-skip" id="ob-lm-skip">
                           Skip &mdash; set up later <span aria-hidden="true">&rarr;</span>
                       </button>
                   </nav>
               </div>
           </section>`;
       document.getElementById("ob-lm-back").addEventListener("click", () => { stopPoll(); back(); });
       document.getElementById("ob-lm-continue").addEventListener("click", () => { stopPoll(); next(); });
       document.getElementById("ob-lm-skip").addEventListener("click", () => { stopPoll(); skip(); });

       status = await fetchJson("/local-models/status");
       renderBody(container);
   }

   function tierKey() {
       // 'gpu' when a usable GPU is present (status.gpu / tier high), else 'cpu'.
       const t = (status && (status.tier || "")).toLowerCase();
       const hasGpu = !!(status && status.gpu && (status.gpu.vram_mb || status.gpu.name));
       return (hasGpu || t === "high") ? "gpu" : "cpu";
   }

   function rec(capId) {
       const fromStatus = status && status.recommendations && status.recommendations[capId];
       if (fromStatus && (fromStatus.label || fromStatus.model)) {
           return { label: fromStatus.label || fromStatus.model,
                    size: fromStatus.size_gb ? `~${fromStatus.size_gb} GB` : (fromStatus.size || ""),
                    note: fromStatus.note || "", slug: fromStatus.slug || fromStatus.model || "" };
       }
       return REC_FALLBACK[tierKey()][capId] || { label: "—", size: "", note: "" };
   }

   function isActive(capId) {
       const routing = (status && status.routing) || {};
       const caps = (status && status.capabilities) || {};
       if (capId === "stt") return (routing.stt || "").toLowerCase() === "onbox" || !!caps.stt;
       if (capId === "tts") return (routing.tts || "").toLowerCase() === "onbox" || !!caps.tts;
       if (capId === "embeddings") {
           // active when the resolved embedding slug is a localstack slug.
           return /-local$/.test(String(routing.embeddings || ""));
       }
       if (capId === "rerank") return /localstack/i.test(String(routing.rerank || ""));
       return false;
   }

   function modelForCap(capId) {
       // The downloadable weight entry backing this capability, if status lists it.
       return ((status && status.models) || []).find((m) => m.capability === capId) || null;
   }

   function renderBody(container) {
       const body = container.querySelector("#ob-lm-body");
       if (!body) return;
       const installed = !!(status && status.installed);
       const healthy = !!(status && status.healthy);
       const gpu = status && status.gpu;
       const disk = (status && status.disk) || {};
       const tier = tierKey();

       const hwLine = gpu && (gpu.name || gpu.vram_mb)
           ? `GPU: <strong>${escapeHtml(gpu.name || "NVIDIA")}</strong>${gpu.vram_mb ? ` · ${Math.round(gpu.vram_mb / 1024)} GB VRAM` : ""}`
           : `No GPU detected — <strong>CPU tier</strong>`;
       const diskLine = (disk.free_gb != null)
           ? `Disk free: <strong>${escapeHtml(String(disk.free_gb))} GB</strong>${disk.required_gb ? ` (needs ~${escapeHtml(String(disk.required_gb))} GB)` : ""}`
           : "";
       const diskWarn = (disk.ok === false)
           ? `<p class="ob-lm-warn">Not enough free disk for the full on-box weight set. Free up space before downloading.</p>` : "";
       const cpuWarn = (tier === "cpu")
           ? `<p class="ob-lm-warn">On a CPU box the local models run <strong>much slower than realtime</strong>. Embeddings + files are fine; live voice is best left on a cloud provider. Nothing here is turned on by default.</p>` : "";
       const notInstalled = !installed
           ? `<p class="ob-lm-warn">The on-box stack isn't installed yet. Re-run <code>install.sh</code> (Step 2f) to add it, then return here to download models.</p>` : "";
       const swapNote = `<p class="ob-lm-note">Voice and search share one GPU and take turns. The <strong>first</strong> interaction after you switch between talking and searching takes about <strong>6–10 seconds</strong> while models swap; after an idle spell the first use also takes a few seconds to warm up. Everything in between is fast.</p>`;

       body.innerHTML = `
           <div class="ob-lm-hw">
               <div class="ob-lm-hw-line">${hwLine}${diskLine ? ` &nbsp;·&nbsp; ${diskLine}` : ""}</div>
               ${installed ? `<div class="ob-lm-hw-badge ${healthy ? "ok" : "warn"}">${healthy ? "Stack healthy" : "Stack installed"}</div>` : ""}
           </div>
           ${notInstalled}${diskWarn}${cpuWarn}
           <div class="ob-lm-caps">${CAPS.map(renderCapRow).join("")}</div>
           ${swapNote}
           <p id="ob-lm-hint" class="ob-lm-hint" hidden></p>`;

       CAPS.forEach((c) => wireCapRow(container, c.id));
   }

   function renderCapRow(cap) {
       const r = rec(cap.id);
       const active = isActive(cap.id);
       const m = modelForCap(cap.id);
       const dl = downloading[m && m.key];
       const downloaded = !m || m.downloaded === true;

       let control;
       if (dl) {
           const pct = dl.total ? Math.min(100, Math.floor((dl.completed / dl.total) * 100)) : 0;
           control = `<div class="ob-lm-progress"><div class="ob-lm-progress-track"><div class="ob-lm-progress-fill" style="width:${pct}%"></div></div><span class="ob-lm-progress-text">${pct}% ${escapeHtml(dl.statusText || "downloading")}</span></div>`;
       } else if (!downloaded) {
           control = `<button type="button" class="ob-lm-btn" data-dl="${escapeHtml(m.key)}">Download ${escapeHtml(r.size || "")}</button>`;
       } else if (active) {
           control = `<button type="button" class="ob-lm-btn ob-lm-btn-on" data-off="${cap.id}">On-box active — turn off</button>`;
       } else {
           control = `<button type="button" class="ob-lm-btn ob-lm-btn-activate" data-on="${cap.id}">Use on-box</button>`;
       }

       return `
           <div class="ob-lm-cap" data-cap="${cap.id}">
               <div class="ob-lm-cap-head">
                   <span class="ob-lm-cap-name">${escapeHtml(cap.label)}${active ? ' <span class="ob-lm-dot" title="On-box active">●</span>' : ""}</span>
                   <span class="ob-lm-cap-model">${escapeHtml(r.label)}${r.size ? ` · ${escapeHtml(r.size)}` : ""}</span>
               </div>
               ${r.note ? `<p class="ob-lm-cap-note">${escapeHtml(r.note)}</p>` : ""}
               <div class="ob-lm-cap-action">${control}</div>
           </div>`;
   }

   function wireCapRow(container, capId) {
       const row = container.querySelector(`.ob-lm-cap[data-cap="${capId}"]`);
       if (!row) return;
       const dlBtn = row.querySelector("[data-dl]");
       if (dlBtn) dlBtn.addEventListener("click", () => startDownload(container, dlBtn.getAttribute("data-dl")));
       const onBtn = row.querySelector("[data-on]");
       if (onBtn) onBtn.addEventListener("click", () => activate(container, capId, true));
       const offBtn = row.querySelector("[data-off]");
       if (offBtn) offBtn.addEventListener("click", () => activate(container, capId, false));
   }

   // ── Downloads (NDJSON progress, cloned from the embeddings-pull pattern) ──
   async function startDownload(container, key) {
       if (downloading[key]) return;
       downloading[key] = { completed: 0, total: 0, statusText: "starting" };
       renderBody(container);
       try {
           const r = await fetch("/local-models/download", {
               method: "POST", headers: { "Content-Type": "application/json" },
               body: JSON.stringify({ model: key }),
           });
           if (!r.ok && r.status !== 409) throw new Error(`download returned ${r.status}`);
           // Stream NDJSON lines: {model,status,completed,total}. If the body
           // isn't streamable (409 already-running / older backend), fall back
           // to a status refresh.
           if (r.body && r.body.getReader) {
               const reader = r.body.getReader();
               const dec = new TextDecoder();
               let buf = "";
               for (;;) {
                   const { done, value } = await reader.read();
                   if (done) break;
                   buf += dec.decode(value, { stream: true });
                   let nl;
                   while ((nl = buf.indexOf("\n")) >= 0) {
                       const line = buf.slice(0, nl).trim();
                       buf = buf.slice(nl + 1);
                       if (!line) continue;
                       try {
                           const p = JSON.parse(line);
                           downloading[key] = { completed: Number(p.completed) || downloading[key].completed,
                                                total: Number(p.total) || downloading[key].total,
                                                statusText: p.status || "downloading" };
                           updateDownloadBar(container, key);
                       } catch (_) { /* skip malformed line */ }
                   }
               }
           }
       } catch (e) {
           showHint(container, `Couldn't download: ${e.message}. Try again.`, true);
       }
       delete downloading[key];
       status = await fetchJson("/local-models/status");  // reflect downloaded=true
       renderBody(container);
   }

   function updateDownloadBar(container, key) {
       const dl = downloading[key];
       const row = [...container.querySelectorAll(".ob-lm-cap")]
           .find((el) => (modelForCap(el.getAttribute("data-cap")) || {}).key === key);
       const fill = row && row.querySelector(".ob-lm-progress-fill");
       const text = row && row.querySelector(".ob-lm-progress-text");
       if (!fill || !text || !dl) { renderBody(container); return; }
       const pct = dl.total ? Math.min(100, Math.floor((dl.completed / dl.total) * 100)) : 0;
       fill.style.width = pct + "%";
       text.textContent = `${pct}% ${dl.statusText || "downloading"}`;
   }

   // ── Per-capability activation ────────────────────────────────────────────
   async function activate(container, capId, on) {
       if (busy[capId]) return;
       busy[capId] = true;
       try {
           if (capId === "embeddings") return await activateEmbeddings(container, on);
           if (capId === "rerank") return await activateRerank(container, on);
           // stt / tts → the seed-flag endpoint (stt also pins STT_PROVIDER).
           const r = await fetch("/local-models/capability", {
               method: "POST", headers: { "Content-Type": "application/json" },
               body: JSON.stringify({ capability: capId, enabled: on }),
           });
           if (!r.ok) throw new Error(await safeDetail(r));
           status = await fetchJson("/local-models/status");
           renderBody(container);
       } catch (e) {
           showHint(container, `Couldn't change ${capId}: ${e.message}`, true);
       } finally {
           busy[capId] = false;
       }
   }

   async function activateRerank(container, on) {
       const r = await fetch("/rerank/select", {
           method: "POST", headers: { "Content-Type": "application/json" },
           body: JSON.stringify({
               provider: "localstack",
               model: rec("rerank").slug || "qwen3-reranker-0.6b-local",
               enabled: on,
           }),
       });
       if (!r.ok) throw new Error(await safeDetail(r));
       status = await fetchJson("/local-models/status");
       renderBody(container);
   }

   async function activateEmbeddings(container, on) {
       if (!on) {
           showHint(container, "To move memory back to cloud, pick a cloud model in the Memory step — the on-box corpus stays searchable meanwhile.", false);
           return;
       }
       // BLOCKING GPU-idle precondition (Phase-2 Step-0): the retrieval group
       // lazy-loads ~11.5-13GB on the first re-embed and OOMs if the old pinned
       // embedder/reranker is still resident.
       const pf = await fetchJson("/local-models/gpu-preflight");
       if (pf && pf.ok === false) {
           showHint(container, pf.detail || "Free the GPU before moving memory on-box, then retry.", true);
           return;
       }
       const target = rec("embeddings").slug || (tierKey() === "gpu" ? "qwen3-embedding-8b-local" : "qwen3-embedding-0.6b-local");
       const r = await fetch("/embeddings/reembed", {
           method: "POST", headers: { "Content-Type": "application/json" },
           body: JSON.stringify({ target }),
       });
       if (r.status === 409) { showHint(container, "A memory rebuild is already running — see the Memory step for progress.", false); startEmbedPoll(container, target); return; }
       if (!r.ok) throw new Error(await safeDetail(r));
       showHint(container, "Rebuilding your memory index on-box. Voice features may be slow until it finishes — track detailed progress in the Memory step.", false);
       startEmbedPoll(container, target);
   }

   // Poll /embeddings/status; when the cutover job is done, fire /toolvault/reload
   // (§5.1 cache-coherence: ToolVault + code embeddings must re-embed at the new
   // dimension or the first hot query mixes dims).
   function startEmbedPoll(container, target) {
       stopPoll();
       pollTimer = setInterval(async () => {
           const es = await fetchJson("/embeddings/status");
           const job = es && es.job;
           if (job && job.state === "running") {
               const pct = job.total ? Math.floor((job.done / job.total) * 100) : 0;
               showHint(container, `Rebuilding memory on-box: ${job.done || 0}/${job.total || "?"} (${pct}%)…`, false);
               return;
           }
           stopPoll();
           if (es && (es.active || "").endsWith("-local")) {
               await fetch("/toolvault/reload", { method: "POST" }).catch(() => {});
               showHint(container, "On-box memory active. Tool + code search caches refreshed.", false);
           }
           status = await fetchJson("/local-models/status");
           renderBody(container);
       }, 3000);
   }

   function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

   // ── helpers ──────────────────────────────────────────────────────────────
   function showHint(container, msg, isError) {
       const hint = container.querySelector("#ob-lm-hint");
       if (!hint) return;
       hint.className = "ob-lm-hint" + (isError ? " ob-lm-hint-error" : "");
       hint.textContent = msg;
       hint.hidden = false;
   }
   async function fetchJson(url) {
       try { const r = await fetch(url, { cache: "no-store" }); if (!r.ok) return null; return await r.json(); }
       catch (_) { return null; }
   }
   async function safeDetail(r) {
       try { const j = await r.json(); return j.detail || `HTTP ${r.status}`; } catch (_) { return `HTTP ${r.status}`; }
   }
   function escapeHtml(s) {
       if (s == null) return "";
       return String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;")
           .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
   }
   ```
2. Append the scoped styles to `Portal/onboarding/onboarding.css` (end of file):
   ```css
   /* ── On-box local model stack step (M8) ───────────────────────────── */
   .ob-lm-hw { display: flex; align-items: center; justify-content: space-between;
       gap: 12px; padding: 12px 14px; border: 1px solid var(--ob-border, #2a2a33);
       border-radius: 8px; margin: 4px 0 14px; }
   .ob-lm-hw-line { font-size: 0.92rem; opacity: 0.9; }
   .ob-lm-hw-badge { font-size: 0.72rem; text-transform: uppercase; letter-spacing: .04em;
       padding: 3px 8px; border-radius: 999px; white-space: nowrap; }
   .ob-lm-hw-badge.ok { background: rgba(60,180,120,.18); color: #6fdca0; }
   .ob-lm-hw-badge.warn { background: rgba(220,170,60,.18); color: #e6c56b; }
   .ob-lm-warn { color: #e6b45b; font-size: 0.86rem; margin: 6px 0; }
   .ob-lm-note { color: var(--ob-muted, #9aa0ac); font-size: 0.82rem; margin: 12px 0 0; }
   .ob-lm-caps { display: flex; flex-direction: column; gap: 10px; margin: 8px 0; }
   .ob-lm-cap { border: 1px solid var(--ob-border, #2a2a33); border-radius: 8px; padding: 12px 14px; }
   .ob-lm-cap-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
   .ob-lm-cap-name { font-weight: 600; }
   .ob-lm-cap-model { font-size: 0.82rem; opacity: 0.8; }
   .ob-lm-cap-note { font-size: 0.78rem; opacity: 0.7; margin: 4px 0 0; }
   .ob-lm-dot { color: #6fdca0; font-size: 0.7rem; }
   .ob-lm-cap-action { margin-top: 10px; }
   .ob-lm-btn { font: inherit; font-size: 0.86rem; padding: 7px 14px; border-radius: 6px;
       border: 1px solid var(--ob-accent, #6f8cff); background: transparent;
       color: var(--ob-accent, #6f8cff); cursor: pointer; }
   .ob-lm-btn:hover { background: rgba(111,140,255,.12); }
   .ob-lm-btn-on { border-color: #6fdca0; color: #6fdca0; }
   .ob-lm-progress { display: flex; align-items: center; gap: 10px; }
   .ob-lm-progress-track { flex: 1; height: 6px; border-radius: 3px; background: rgba(255,255,255,.1); overflow: hidden; }
   .ob-lm-progress-fill { height: 100%; background: var(--ob-accent, #6f8cff); transition: width .2s; }
   .ob-lm-progress-text { font-size: 0.78rem; opacity: 0.8; white-space: nowrap; }
   .ob-lm-hint { font-size: 0.84rem; margin-top: 12px; color: var(--ob-muted, #9aa0ac); }
   .ob-lm-hint-error { color: #e6675b; }
   ```
3. **Run (backend regression only — no JS test infra):** `python -m pytest Orchestrator/tests/test_onboarding_steps_parity.py -q`
   **Expected:** PASS (module still exists; the full rewrite keeps the `render` export + sigil-derived number).
4. **Manual Fold + browser walk (house rule):**
   - Restart the service: `sudo systemctl restart blackbox.service` (wait ~60-90s).
   - Open `http://localhost:9091/onboarding/?step=local_models` in a hard-refreshed browser (Cmd/Ctrl-Shift-R — the onboarding modules are un-versioned; a hard refresh is required to pick up the rewrite).
   - Verify: hardware/tier + disk line renders; the four capability rows show tier-appropriate recommendations; on a GPU box the embeddings "Use on-box" button runs the GPU preflight (block message when the GPU is busy) then starts the reembed with a progress hint; on the dev box (CPU/LOW) the CPU warning shows and downloads/activation degrade gracefully with a fail-open empty status. Confirm Back/Continue/Skip navigate.
   - Repeat the render check on the Fold (Android WebView renders the same page).
5. **Commit:** `git add Portal/onboarding/steps/local_models.js Portal/onboarding/onboarding.css && git commit -m "feat(onboarding): full on-box local_models wizard step (downloads + per-capability activation)"`

---

### Task 8.5: Transcription step gains an "On-box (local)" STT provider option

The `transcription` step already persists a provider pick via `POST /onboarding/save {secrets:{STT_PROVIDER:id}}` and renders a card per entry in its `PROVIDERS` array. Add an `onbox` entry so a user who set up the on-box stack in the previous step can pin STT to it here too. The card's availability comes from `/stt/catalog` (the STT milestone adds an `onbox` catalog entry); until then it renders as an informational "Needs setup" card that points back to the On-Box Models step — never blocking.

**Files:**
- Modify: `Portal/onboarding/steps/transcription.js` (`PROVIDERS` array :32-53)
- Create (Test): `Orchestrator/tests/test_transcription_onbox_option.py`

1. Add a source-text guard test. Create `Orchestrator/tests/test_transcription_onbox_option.py`:
   ```python
   """The transcription step must offer the on-box STT option (M8): an 'onbox'
   PROVIDERS entry so a user can pin STT_PROVIDER=onbox. Source-text test — the
   wizard has no JS test infra (mirrors test_onboarding_steps_parity.py)."""
   import re
   from pathlib import Path

   TRANSCRIPTION_JS = (
       Path(__file__).resolve().parents[2]
       / "Portal" / "onboarding" / "steps" / "transcription.js"
   )


   def test_transcription_offers_onbox_provider():
       src = TRANSCRIPTION_JS.read_text(encoding="utf-8")
       m = re.search(r"const PROVIDERS\s*=\s*\[(.*?)\];", src, re.DOTALL)
       assert m, "could not find `const PROVIDERS = [...]` in transcription.js"
       ids = re.findall(r'id:\s*"([a-z0-9_]+)"', m.group(1))
       assert "onbox" in ids, (
           "transcription.js PROVIDERS must include an 'onbox' (on-box local STT) "
           f"option; found {ids}"
       )
       # The distinct on-box token must not be conflated with the custom-server
       # 'local' token (spec §5.3 — they route to different backends).
       assert "local" in ids and "onbox" in ids
   ```
2. **Run:** `python -m pytest Orchestrator/tests/test_transcription_onbox_option.py -q`
   **Expected:** FAIL — `assert 'onbox' in ids` (only openai/google/elevenlabs/local today).
3. Add the `onbox` entry to the `PROVIDERS` array in `transcription.js`. Insert before the closing `];` of the array (after the `local` entry, :52):
   ```javascript
       {
           id: "onbox",
           vendor: "On-box (local)",
           needsHint: "Set up the on-box model stack in the On-Box Models step (whisper runs locally — no cloud STT).",
       },
   ```
4. **Run:** `python -m pytest Orchestrator/tests/test_transcription_onbox_option.py -q`
   **Expected:** PASS.
5. **Manual browser walk (house rule):** hard-refresh `http://localhost:9091/onboarding/?step=transcription`; confirm five provider cards render, the "On-box (local)" card selects and persists (`GET /onboarding/current-config` shows `stt.provider == "onbox"`), and it shows "Needs setup" until `/stt/catalog` reports the `onbox` entry available.
6. **Commit:** `git add Portal/onboarding/steps/transcription.js Orchestrator/tests/test_transcription_onbox_option.py && git commit -m "feat(onboarding): on-box (local) STT provider option in the transcription step"`

---

### Task 8.6: Updates panel — status-only on-box stack card

Add a read-only card to the Updates panel that reflects `GET /local-models/status` with a `[Manage]` deep-link into the wizard (`/onboarding/?step=local_models`) — panels are status-only; all selection lives in the wizard (house rule). Model it on the existing read-only reranker status line: fail-soft (hidden when the endpoint is unreachable / older backend), never able to break the panel.

**Files:**
- Modify: `Portal/index.html` (add a `localModelsCard` container after `embeddingsCard` :673; bump `?v=genui318` → `genui319` on the two asset links :11, :21)
- Modify: `Portal/modules/updates-manager.js` (`initUpdatesPanel` :52-61 — fire a new refresher; add the renderer)
- Test: manual browser step (house rule)

1. Add the card container to `Portal/index.html`. After line 673 (`<div id="embeddingsCard" ...>`) insert:
   ```html
             <!-- On-box local model stack status (M8, read-only; selection lives in the wizard) -->
             <div id="localModelsCard" class="embeddings-card-container hide"></div>
   ```
2. Bump the cache version so the panel changes ship (house rule). In `Portal/index.html`: line 11 change `main.css?v=genui318` → `main.css?v=genui319` (and the trailing comment to `v319: on-box local model stack updates card`); line 21 change `app-modular.js?v=genui318` → `app-modular.js?v=genui319` (same comment).
3. Fire the refresher from `initUpdatesPanel` in `updates-manager.js`. After the `_refreshEmbeddingsCard();` line (:59) add:
   ```javascript
       // On-box local model stack status (M8): fire-and-forget, fail-soft — it
       // must never delay or break the updates panel itself (same contract as
       // the embeddings card).
       _refreshLocalModelsCard();
   ```
4. Add the renderer to `updates-manager.js` (append near the reranker status helpers, after `_rerankStatusLineHtml`, ~:456):
   ```javascript
   // ── On-box local model stack card (M8, read-only) ─────────────────────
   // Reflects GET /local-models/status. Status-only: no activation here — the
   // [Manage] button deep-links to the wizard step. Fail-soft: any error hides
   // the card (never breaks the panel), mirroring the embeddings card contract.
   let _lmWarnedOnce = false;

   async function _refreshLocalModelsCard() {
       const container = document.getElementById("localModelsCard");
       if (!container) return;  // panel not in DOM (menu modal not built yet)
       const status = await _fetchJsonSoft("/local-models/status");
       if (!status) {  // unreachable / older backend / not installed → hide
           container.classList.add("hide");
           container.innerHTML = "";
           if (!_lmWarnedOnce) { _lmWarnedOnce = true; console.warn("[updates] local-models status unavailable"); }
           return;
       }
       _renderLocalModelsCard(container, status);
   }

   function _renderLocalModelsCard(container, status) {
       const routing = status.routing || {};
       const onbox = [];
       if ((routing.stt || "").toLowerCase() === "onbox") onbox.push("Speech");
       if ((routing.tts || "").toLowerCase() === "onbox") onbox.push("Voice");
       if (String(routing.embeddings || "").endsWith("-local")) onbox.push("Memory");
       if (/localstack/i.test(String(routing.rerank || ""))) onbox.push("Reranking");

       let line;
       if (!status.installed) line = "On-box models: not installed";
       else if (!status.healthy) line = "On-box models: installed — stack not running";
       else if (onbox.length) line = `On-box models: ${onbox.join(", ")}`;
       else line = "On-box models: ready — none active";

       container.innerHTML = `
           <div class="embeddings-card embeddings-rerank-status">
               <div class="embeddings-card-title">${_esc(line)}</div>
               <div class="embeddings-card-actions">
                   <button class="btn local-models-manage-btn">Manage</button>
               </div>
           </div>`;
       container.classList.remove("hide");
       container.querySelectorAll(".local-models-manage-btn").forEach((btn) => {
           btn.addEventListener("click", () => { window.location.href = "/onboarding/?step=local_models"; });
       });
   }
   ```
5. **Manual browser walk (house rule):**
   - `sudo systemctl restart blackbox.service` (wait ~60-90s).
   - Hard-refresh the Portal, open the menu/Updates panel. On the dev box (no on-box stack) confirm the card either hides (status null) or reads "not installed" and the panel is otherwise unaffected. Confirm `[Manage]` opens `/onboarding/?step=local_models`.
   - Confirm the `?v=genui319` bump loaded (Network tab shows the new query string).
6. **Commit:** `git add Portal/index.html Portal/modules/updates-manager.js && git commit -m "feat(portal): read-only on-box model stack card in the Updates panel (genui319)"`

---

### Milestone 8 — done check

- `python -m pytest Orchestrator/tests/test_local_models_onboarding_step.py Orchestrator/tests/test_local_models_status_rollup.py Orchestrator/tests/test_local_stack_routes.py Orchestrator/tests/test_transcription_onbox_option.py Orchestrator/tests/test_onboarding_steps_parity.py Orchestrator/tests/test_onboarding_status.py -q` — all green.
- The wizard shows a `local_models` step (after Memory & Search); it downloads weights with progress and activates each capability deliberately; embeddings activation is GPU-idle-gated and fires `/toolvault/reload` on cutover; the transcription step offers On-box (local); the Updates panel shows a status-only on-box card. Every `/local-models/*` read is fail-open so nothing here breaks a box that never installs the stack.


---

## Milestone 9: CU per-session virtual displays + live view + in-use flag

**Depends on:** nothing (this milestone is self-contained — it touches the `Orchestrator/browser/*` CU stack + Portal/Android, none of which the model-stack milestones M1–M8 modify; it may land in parallel). Cross-milestone note: Task 9.8 edits `Scripts/onboarding/system-packages.txt`, which the **install milestone M2 Task 2.1 (spec §8 step 7)** already owns — NOT M8 (M8 is the wizard milestone and never touches system-packages.txt). M2 lands before M9 in the execution order, so **M2 Task 2.1 is the single owner of the `xvfb`/`websockify`/`novnc` allowlist lines and Task 9.8 is an explicit no-op** (it verifies the lines are present rather than adding duplicates).

Goal: give every computer-use session its own private Xvfb screen at the model's native resolution so the agent opens its own windows without ever touching the user's desktop, watchable through a live-view panel in the Portal and Android. This rewrites `browser/display.py`'s singleton `VirtualDisplay` into a per-session `DisplayAllocator` (tracked by PID, no global `pkill`/`pgrep`, concurrency-capped, boot+TTL orphan reaping), makes virtual the default for `use_computer`/`/browser/run`/scheduler and the three chat CU launch sites (native becomes an explicit per-session opt-in still guarded by `display_arbiter`), and adds a noVNC/websockify live view plus a D11 "CU in use" indicator visible to all users.

**Spec anchors verified in code (2026-07-20):**
- `browser/display.py` `VirtualDisplay`: global `pkill -f "Xvfb …"` (line 51), `pkill -f "openbox.*DISPLAY=…"` (line 96 — the spec-noted DEAD no-op: `DISPLAY` is passed via env at line 93/234, never argv), `pkill -f x11vnc` (line 128, kills ALL sessions), `pgrep -f x11vnc`/`pgrep -f openbox` (lines 160/170, True on ANY session), hardcoded `-rfbport 5900` (line 139). Singleton at lines 254-268.
- Per-backend resolution constants: Anthropic/OpenAI `CU_DISPLAY_WIDTH=1280`/`CU_DISPLAY_HEIGHT=720` (`browser/config.py:72-73`), `OPENAI_CU_WIDTH/HEIGHT=1280/720` (`openai_cu/config.py:25-26`), Gemini `GEMINI_CU_WIDTH/HEIGHT=1440/900` (`gemini_cu/config.py:35-36`, its `RECOMMENDED_RESOLUTION` at :31).
- `browser/actions.py`: `to_native` returns unscaled `int(x),int(y)` when not native (line 232-233 — confirms "sandbox coords already unscaled"); `_run_xdotool(*args, display_number=ACTIVE_DISPLAY)` else-branch uses the SINGLETON `get_display().get_env()` (line 147); `ActionExecutor.__init__` (line 213) stores `display_number` but the action methods call `_run_xdotool(...)` WITHOUT passing it (lines 265/274/282/290/297/303/313/321/330/424) — a per-session correctness gap.
- `browser/screenshot.py`: `capture_screenshot_display(display_number)` builds env `{"DISPLAY": f":{display_number}", …}` from the arg (line 107) and returns raw (no resize); `capture_screenshot(display_number)` branches on GLOBAL `NATIVE_MODE` (line 178).
- `browser/session_manager.py`: `ComputerUseSession.__init__` (line 62-91) builds `self.actions = ActionExecutor()` (line 68); `ensure_browser` (184-195) calls `ensure_display_running()`; `is_alive` (170-174); `destroy` (197-204).
- `browser/chrome.py`: `ChromeInstance.start` cmd `--window-size={DISPLAY_WIDTH},{DISPLAY_HEIGHT}` (line 40) with `env = get_display().get_env()` (line 35).
- Driver capture seams (all have `session` in scope): `driver_anthropic.py` `_capture_ss` `return capture_screenshot()` (line 45) + `fresh_png = capture_screenshot()` (line 538); `openai_cu/agent_loop.py:220`; `gemini_cu/agent_loop.py` `_capture_screenshot(session)` → `capture_screenshot_display(ACTIVE_DISPLAY)` (line 192); `headless.py:665`; `chat_routes.py:4224`.
- Launch sites: `headless.run_cu_task` display setup (`headless.py:583-592`, singleton `get_display()`); `_run_gemini_cu_task` arbiter (`headless.py:457-461`); `chat_routes.stream_computer_use` (4074, display setup 4166-4188, `try_claim` 4293), `stream_gemini_computer_use` (4368, `try_claim` 4518), `stream_openai_computer_use` (4657, `try_claim` 4775).
- WS reverse-proxy pattern to mirror: `agent_routes.py` `app_proxy_websocket` (1482-1600) — `websockets.connect` + two pump tasks.
- Routes register via `from Orchestrator.checkpoint import app` then `@app.get`/`@app.websocket` (`browser_routes.py:10`); `/cu/preflight` at `browser_routes.py:203`. Static mounted in `app.py:209`.
- `display_arbiter.py` `try_claim`/`release_claim`/`claim_local_display` — pure mutex, STAYS for native mode (unchanged this milestone).
- Tests: `Orchestrator/tests/test_*.py`, `python -m pytest` (pytest.ini: `testpaths=Orchestrator/tests`, `pythonpath=.`). Arbiter-test autouse-clean-fixture convention in `test_cu_display_arbiter.py`.

**Decisions this milestone makes that the spec left open (FLAGGED):**
- **noVNC assets**: served from the system apt path `/usr/share/novnc` (the `novnc` package added in 9.8), mounted read-only and conditionally; NOT vendored into the repo (avoids committing a multi-MB JS blob; DRY, and the fresh-box gate is covered by system-packages.txt). If `/usr/share/novnc` is absent the live-view page degrades to an "install novnc" notice.
- **Port/display ranges** (spec fixes only `:100+n` and `rfbport 5901+n`): display `:100+slot`, x11vnc `5901+slot`, websockify `6101+slot`, slots `0..2` (cap 3). All loopback-only.
- **WS path split**: HTML viewer at `GET /cu/view/{session_id}`, socket at `WS /cu/view/{session_id}/ws` (the spec wrote one `/cu/view/{session_id}` WS route; split to avoid HTTP/WS path ambiguity — Starlette dispatches by scope type but a distinct `/ws` suffix keeps the viewer HTML and its socket unambiguous).
- **Live view is view-only** (`rfb.viewOnly = true`) — resolves Q5 (interactivity) to the D11 "watch" reading; click-to-takeover is out of scope.
- **Orphan-reaper TTL** = 1800s idle (`VIRTUAL_DISPLAY_TTL`); boot sweep targets OUR specific slot displays/ports (targeted pid kill, never a blanket process-name kill).
- **Native default flip**: launch sites default `native_mode=False` (virtual). This is the intended §9 flip ("native becomes opt-in"); a box previously running `CU_NATIVE_MODE=True` now needs the explicit per-session opt-in to act on the real desktop.

---

### Task 9.1: DisplayAllocator core — per-session Xvfb/openbox/x11vnc/websockify quartet, tracked by PID

**Files:**
- Modify: `Orchestrator/browser/display.py` (REWRITE the module — replace the `VirtualDisplay` singleton with a `DisplayAllocator`; keep a thin `get_display()`/`ensure_display_running()` shim so existing importers — `chrome.py:13`, `session_manager.py:23`, `browser_routes.py:68/85`, `headless.py:584`, `chat_routes.py:4175` — keep resolving until Task 9.4 rewires them; the shim delegates to a reserved default slot and contains NO global pkill/pgrep).
- Test: `Orchestrator/tests/test_cu_display_allocator.py` (new)

1. Write the failing test file. Spawn is fully mocked — `subprocess.Popen` is monkeypatched to a fake returning an object with an incrementing `.pid` and `.poll()==None`; the scrot health probe is stubbed True.

```python
"""M9: per-session DisplayAllocator — allocate/spawn (mocked)/teardown by pid,
per-backend native resolution, concurrency cap. No global pkill/pgrep anywhere.

Spawn is mocked (no real Xvfb), so these run headless in CI. Behavior, not mocks:
the allocator's own bookkeeping (slots, ports, resolution, pid tracking) is real.
"""
import itertools
import pytest

from Orchestrator.browser import display as disp


class _FakePopen:
    _ids = itertools.count(1000)

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.pid = next(self._ids)
        self._alive = True
        self.args = cmd

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def kill(self):
        self._alive = False


@pytest.fixture
def alloc(monkeypatch):
    # Mock every spawn + the readiness probe so no real X server is touched, and
    # stub the startup sleeps so the suite stays fast.
    monkeypatch.setattr(disp.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(disp.time, "sleep", lambda *_: None)
    monkeypatch.setattr(disp, "_xvfb_ready", lambda display_num: True)
    monkeypatch.setattr(disp, "_live_view_available", lambda: True)
    a = disp.DisplayAllocator()
    yield a
    a.shutdown_all()


def test_allocate_assigns_slot0_ports_and_anthropic_resolution(alloc):
    h = alloc.allocate("sess-a", backend="anthropic", operator="Brandon")
    assert h.slot == 0
    assert h.display == ":100"
    assert h.vnc_port == 5901
    assert h.ws_port == 6101
    assert (h.width, h.height) == (1280, 720)
    assert set(h.pids) >= {"xvfb", "openbox", "x11vnc"}  # websockify too when live view available
    assert h.pids["websockify"]


def test_gemini_backend_gets_1440x900(alloc):
    h = alloc.allocate("sess-g", backend="google", operator="op")
    assert (h.width, h.height) == (1440, 900)


def test_openai_backend_gets_1280x720(alloc):
    h = alloc.allocate("sess-o", backend="openai", operator="op")
    assert (h.width, h.height) == (1280, 720)


def test_second_session_gets_slot1_distinct_ports_and_display(alloc):
    a = alloc.allocate("s1", backend="anthropic", operator="op")
    b = alloc.allocate("s2", backend="google", operator="op")
    assert (b.slot, b.display, b.vnc_port, b.ws_port) == (1, ":101", 5902, 6102)
    assert a.display != b.display


def test_allocate_is_idempotent_per_session(alloc):
    a = alloc.allocate("dup", backend="anthropic", operator="op")
    b = alloc.allocate("dup", backend="anthropic", operator="op")
    assert a is b  # same handle, no second quartet


def test_release_terminates_all_tracked_pids_and_frees_slot(alloc):
    h = alloc.allocate("sess-r", backend="anthropic", operator="op")
    procs = list(alloc._procs["sess-r"].values())
    alloc.release("sess-r")
    assert all(p.poll() is not None for p in procs)   # every child terminated by pid
    assert "sess-r" not in alloc._sessions
    # slot 0 is now free — next allocate reuses it
    h2 = alloc.allocate("sess-r2", backend="anthropic", operator="op")
    assert h2.slot == 0


def test_no_global_process_kill_in_source():
    """Guard: the rewritten module must never shell out to a blanket pkill/pgrep."""
    import inspect
    src = inspect.getsource(disp)
    assert "pkill" not in src
    assert "pgrep -f x11vnc" not in src and "pgrep -f openbox" not in src
```

2. Run it, expect failure (module has no `DisplayAllocator`).
   Run: `python -m pytest Orchestrator/tests/test_cu_display_allocator.py -q`
   Expected: `ImportError`/`AttributeError: module 'Orchestrator.browser.display' has no attribute 'DisplayAllocator'` — collection/tests FAIL.

3. Rewrite `Orchestrator/browser/display.py` with the allocator. Replace the ENTIRE file:

```python
"""Per-session virtual-display allocation for computer use (M9).

Each CU session gets a private Xvfb screen (at the model's native resolution),
an openbox WM, an x11vnc server bound to loopback, and — when live-view assets
are present — a websockify WS bridge. Everything is tracked BY PID; teardown and
liveness are per-pid. There is NO global pkill/pgrep (the singleton VirtualDisplay
this replaces used pkill -f x11vnc / pgrep -f openbox, which broke multi-session
correctness). display_arbiter.py still owns native-mode mutual exclusion; this
module owns virtual-session lifecycle only.
"""
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from Orchestrator.browser.config import DISPLAY_DEPTH, ACTIVE_DISPLAY

# ── Slot ranges (loopback-only) ──
DISPLAY_BASE = 100            # Xvfb :100, :101, :102
VNC_BASE_PORT = 5901         # x11vnc RFB per session
WEBSOCKIFY_BASE_PORT = 6101  # websockify WS bridge per session
MAX_VIRTUAL_SESSIONS = 3     # concurrency cap (§9)
VIRTUAL_DISPLAY_TTL = 1800.0 # idle seconds before the TTL reaper tears a session down
_STARTUP_WAIT = 1.0          # seconds to let Xvfb come up before openbox/x11vnc
_NOVNC_DIR = "/usr/share/novnc"


def resolution_for_backend(backend: str) -> tuple:
    """Native CU resolution per backend (§9 / D6). ONE source per backend."""
    b = (backend or "anthropic").lower()
    if b in ("google", "gemini"):
        from Orchestrator.gemini_cu.config import GEMINI_CU_WIDTH, GEMINI_CU_HEIGHT
        return GEMINI_CU_WIDTH, GEMINI_CU_HEIGHT       # 1440x900
    from Orchestrator.browser.config import CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT
    return CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT           # 1280x720 (anthropic + openai)


def _live_view_available() -> bool:
    """websockify binary + noVNC assets both present -> live view can run."""
    import shutil
    return bool(shutil.which("websockify")) and os.path.isdir(_NOVNC_DIR)


def _xvfb_ready(display_num: int) -> bool:
    """One-shot readiness probe: xdpyinfo / scrot against :N succeeds."""
    env = {"DISPLAY": f":{display_num}", "PATH": "/usr/bin:/usr/local/bin:/bin"}
    try:
        r = subprocess.run(["scrot", "--overwrite", f"/tmp/xvfb_ready_{display_num}.png"],
                           env=env, capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _terminate_proc(p) -> None:
    """Tear down a TRACKED child we own (a Popen): terminate -> wait -> kill.
    Reaps the zombie via wait(). Used by release() (we hold the Popen)."""
    try:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=2)
    except Exception:
        pass


def _terminate_pid(pid: int) -> None:
    """SIGTERM then SIGKILL a single pid we do NOT hold as a Popen (a restart-
    survivor orphan). Never a process-name match. Used by reap_orphans()."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        time.sleep(0.2)
        try:
            os.kill(pid, 0)  # still alive?
        except OSError:
            return


def _pids_matching(pattern: str) -> List[int]:
    """Targeted pgrep for a SPECIFIC slot identifier (e.g. 'Xvfb :100',
    'rfbport 5901'). Used ONLY by the boot reaper to find restart-survivors on
    OUR OWN slots — never a blanket process-name match like 'x11vnc'/'openbox'."""
    try:
        r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
        return [int(x) for x in r.stdout.split()]
    except Exception:
        return []


@dataclass
class DisplayHandle:
    session_id: str
    slot: int
    backend: str
    operator: str
    width: int
    height: int
    display_num: int
    vnc_port: int
    ws_port: int
    live_view: bool = False
    pids: Dict[str, int] = field(default_factory=dict)  # role -> pid (introspection)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)

    @property
    def display(self) -> str:
        return f":{self.display_num}"

    def get_env(self) -> dict:
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        return env

    def touch(self) -> None:
        self.last_activity = time.time()

    def to_public(self) -> dict:
        return {
            "session_id": self.session_id,
            "operator": self.operator,
            "backend": self.backend,
            "width": self.width,
            "height": self.height,
            "display": self.display,
            "live_view": self.live_view,
            "view_url": f"/cu/view/{self.session_id}",
            "started_at": self.created_at,
        }


class DisplayAllocator:
    """Thread-safe per-session virtual-display lifecycle. Contenders are OS
    threads (tasks.py ThreadPoolExecutor), so a process-wide RLock guards the
    slot table."""

    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: Dict[str, DisplayHandle] = {}       # session_id -> handle
        self._slots: Dict[int, str] = {}                    # slot -> session_id
        self._procs: Dict[str, Dict[str, subprocess.Popen]] = {}  # session_id -> role -> Popen

    def _free_slot(self) -> int:
        for slot in range(MAX_VIRTUAL_SESSIONS):
            if slot not in self._slots:
                return slot
        raise RuntimeError(
            f"CU virtual-display cap reached ({MAX_VIRTUAL_SESSIONS} concurrent sessions)")

    def allocate(self, session_id: str, backend: str = "anthropic",
                 operator: str = "system") -> DisplayHandle:
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                existing.touch()
                return existing
            slot = self._free_slot()  # raises when capped
            width, height = resolution_for_backend(backend)
            h = DisplayHandle(
                session_id=session_id, slot=slot, backend=backend, operator=operator,
                width=width, height=height, display_num=DISPLAY_BASE + slot,
                vnc_port=VNC_BASE_PORT + slot, ws_port=WEBSOCKIFY_BASE_PORT + slot,
            )
            self._start_quartet(h)
            self._sessions[session_id] = h
            self._slots[slot] = session_id
            return h

    def _start_quartet(self, h: DisplayHandle) -> None:
        procs: Dict[str, subprocess.Popen] = {}
        # 1. Xvfb at the backend's native resolution — scale 1.0, no LANCZOS.
        procs["xvfb"] = subprocess.Popen(
            ["Xvfb", h.display, "-screen", "0", f"{h.width}x{h.height}x{DISPLAY_DEPTH}",
             "-nolisten", "tcp", "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(_STARTUP_WAIT)
        if not _xvfb_ready(h.display_num):
            for p in procs.values():
                _terminate_proc(p)
            raise RuntimeError(f"Xvfb {h.display} failed to become ready")
        env = h.get_env()
        # 2. openbox WM (DISPLAY via env — the singleton's pkill of 'openbox' by
        #    argv was a dead no-op precisely because DISPLAY is env, not argv).
        procs["openbox"] = subprocess.Popen(
            ["openbox", "--config-file", "/dev/null"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.3)
        # 3. x11vnc bound to loopback on THIS session's rfbport (no session dbus).
        procs["x11vnc"] = subprocess.Popen(
            ["x11vnc", "-display", h.display, "-forever", "-shared", "-nopw",
             "-listen", "127.0.0.1", "-rfbport", str(h.vnc_port), "-noxdamage", "-quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 4. websockify WS bridge (only when assets present — SHOULD_HAVE).
        if _live_view_available():
            procs["websockify"] = subprocess.Popen(
                ["websockify", f"127.0.0.1:{h.ws_port}", f"127.0.0.1:{h.vnc_port}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            h.live_view = True
        self._procs[h.session_id] = procs
        h.pids = {role: p.pid for role, p in procs.items()}  # introspection mirror

    def get(self, session_id: str) -> Optional[DisplayHandle]:
        with self._lock:
            return self._sessions.get(session_id)

    def release(self, session_id: str) -> None:
        with self._lock:
            h = self._sessions.pop(session_id, None)
            procs = self._procs.pop(session_id, {})
            if h is not None:
                self._slots.pop(h.slot, None)
        # Teardown the tracked children we own (Popen objects), outside the lock.
        # Dependents before Xvfb. This kills the SPECIFIC pids we spawned — never
        # a process-name pkill.
        for role in ("websockify", "x11vnc", "openbox", "xvfb"):
            p = procs.get(role)
            if p is not None:
                _terminate_proc(p)

    def active_sessions(self) -> List[dict]:
        with self._lock:
            return [h.to_public() for h in self._sessions.values()]

    def shutdown_all(self) -> None:
        for sid in list(self._sessions):
            self.release(sid)
```

4. Add module-level singleton + backward-compat shim at the END of the file (so
   Task 9.4 can flip callers incrementally without an outage). The shim reserves
   a default slot and exposes exactly the surface `VirtualDisplay` did that other
   modules still import — but with ZERO global pkill/pgrep:

```python
# ── Module singleton ──
_allocator: Optional[DisplayAllocator] = None
_allocator_lock = threading.Lock()

def get_allocator() -> DisplayAllocator:
    global _allocator
    with _allocator_lock:
        if _allocator is None:
            _allocator = DisplayAllocator()
        return _allocator


# ── Backward-compat shim (removed in Task 9.4 once all callers use handles) ──
# A minimal singleton-style facade over a reserved "__default__" allocation so
# chrome.py / session_manager.py / browser_routes.py keep importing get_display /
# ensure_display_running until 9.4 rewires them. NO global process kills.
class _DefaultDisplayShim:
    _SID = "__default__"

    def _handle(self) -> Optional[DisplayHandle]:
        return get_allocator().get(self._SID)

    def start(self) -> bool:
        try:
            get_allocator().allocate(self._SID, backend="anthropic", operator="system")
            return True
        except Exception as e:
            print(f"[DISPLAY] default allocation failed: {e}")
            return False

    def is_running(self) -> bool:
        return self._handle() is not None

    def get_env(self) -> dict:
        h = self._handle()
        if h is None:
            env = os.environ.copy(); env["DISPLAY"] = f":{ACTIVE_DISPLAY}"; return env
        return h.get_env()

    @property
    def display_number(self) -> int:
        h = self._handle()
        return h.display_num if h else ACTIVE_DISPLAY

    @property
    def width(self) -> int:
        h = self._handle(); return h.width if h else 1280

    @property
    def height(self) -> int:
        h = self._handle(); return h.height if h else 720

    def health_check(self) -> bool:
        h = self._handle()
        return bool(h) and _xvfb_ready(h.display_num)

    def stop(self) -> None:
        get_allocator().release(self._SID)


_display_shim = _DefaultDisplayShim()

def get_display() -> _DefaultDisplayShim:
    return _display_shim

def ensure_display_running() -> bool:
    d = get_display()
    return True if d.is_running() else d.start()
```

5. Run the test, expect PASS.
   Run: `python -m pytest Orchestrator/tests/test_cu_display_allocator.py -q`
   Expected: `7 passed`.

6. Sanity: confirm nothing else broke at import (the shim keeps old importers alive).
   Run: `python -m pytest Orchestrator/tests/test_cu_display_arbiter.py Orchestrator/tests/test_cu_preflight.py -q`
   Expected: existing CU tests still `passed` (no regressions from the display rewrite).

7. Commit.
   Run: `git add Orchestrator/browser/display.py Orchestrator/tests/test_cu_display_allocator.py && git commit -m "feat(cu): per-session DisplayAllocator replacing the VirtualDisplay singleton (pid-tracked, no global pkill)"`
   Expected: one commit, two files.

---

### Task 9.2: Concurrency cap enforcement + boot & TTL orphan reapers (by pid)

**Depends on:** 9.1

**Files:**
- Modify: `Orchestrator/browser/display.py` (add `reap_orphans`, `reap_idle` to `DisplayAllocator`; the cap already raises in `_free_slot`)
- Test: `Orchestrator/tests/test_cu_display_allocator.py` (extend)

1. Add failing tests to the existing file:

```python
def test_fourth_concurrent_session_raises_cap(alloc):
    for i in range(disp.MAX_VIRTUAL_SESSIONS):
        alloc.allocate(f"s{i}", backend="anthropic", operator="op")
    with pytest.raises(RuntimeError, match="cap reached"):
        alloc.allocate("s-over", backend="anthropic", operator="op")


def test_reap_idle_releases_only_stale_sessions(alloc, monkeypatch):
    a = alloc.allocate("fresh", backend="anthropic", operator="op")
    b = alloc.allocate("stale", backend="anthropic", operator="op")
    b.last_activity = 0.0  # ancient
    alloc.reap_idle()
    assert "fresh" in alloc._sessions
    assert "stale" not in alloc._sessions


def test_reap_orphans_kills_untracked_slot_survivors(alloc, monkeypatch):
    # Simulate a restart-survivor Xvfb on slot 2 with pid 4242 that we do NOT track.
    killed = []
    monkeypatch.setattr(disp, "_terminate_pid", lambda pid: killed.append(pid))
    def fake_matching(pattern):
        return [4242] if "Xvfb :102" in pattern else []
    monkeypatch.setattr(disp, "_pids_matching", fake_matching)
    alloc.reap_orphans()
    assert 4242 in killed


def test_reap_orphans_spares_tracked_pids(alloc, monkeypatch):
    h = alloc.allocate("live", backend="anthropic", operator="op")  # slot 0 -> :100
    tracked_xvfb = alloc._procs["live"]["xvfb"].pid
    killed = []
    monkeypatch.setattr(disp, "_terminate_pid", lambda pid: killed.append(pid))
    monkeypatch.setattr(disp, "_pids_matching",
                        lambda pattern: [tracked_xvfb] if "Xvfb :100" in pattern else [])
    alloc.reap_orphans()
    assert tracked_xvfb not in killed  # never kill a pid we own
```

2. Run, expect failure (`reap_orphans`/`reap_idle` don't exist).
   Run: `python -m pytest Orchestrator/tests/test_cu_display_allocator.py -q -k "reap or cap"`
   Expected: `AttributeError: 'DisplayAllocator' object has no attribute 'reap_orphans'` — FAIL.

3. Add the two reapers to `DisplayAllocator` (insert after `active_sessions`):

```python
    def reap_idle(self) -> None:
        """Tear down sessions idle past the TTL. Call from the periodic sweep."""
        now = time.time()
        with self._lock:
            stale = [sid for sid, h in self._sessions.items()
                     if (now - h.last_activity) > VIRTUAL_DISPLAY_TTL]
        for sid in stale:
            print(f"[DISPLAY] TTL-reaping idle CU display session {sid[:8]}")
            self.release(sid)

    def reap_orphans(self) -> None:
        """Sweep restart-survivor children on OUR slot displays/ports that we no
        longer track (a service restart reparents them to init — KillMode=process).
        Targets SPECIFIC slot identifiers, one pid at a time; never a blanket
        process-name kill. Call once at boot."""
        with self._lock:
            tracked = {p.pid for procs in self._procs.values() for p in procs.values()}
        for slot in range(MAX_VIRTUAL_SESSIONS):
            display = f":{DISPLAY_BASE + slot}"
            vnc = VNC_BASE_PORT + slot
            ws = WEBSOCKIFY_BASE_PORT + slot
            for pattern in (f"Xvfb {display}", f"rfbport {vnc}", f"websockify 127.0.0.1:{ws}"):
                for pid in _pids_matching(pattern):
                    if pid not in tracked:
                        print(f"[DISPLAY] boot-reaping orphan pid {pid} ({pattern})")
                        _terminate_pid(pid)
```

4. Run, expect PASS.
   Run: `python -m pytest Orchestrator/tests/test_cu_display_allocator.py -q`
   Expected: `11 passed`.

5. Commit.
   Run: `git add Orchestrator/browser/display.py Orchestrator/tests/test_cu_display_allocator.py && git commit -m "feat(cu): DisplayAllocator concurrency cap + boot/TTL orphan reapers (pid-targeted)"`
   Expected: one commit, two files.

---

### Task 9.3: Per-session display wiring — session fields, ActionExecutor, Chrome, driver capture seam

**Depends on:** 9.1

Threads the allocated display through the parts that actually drive it: the session carries its `DisplayHandle` + a `native_mode` flag; `ActionExecutor` becomes per-session-display-aware (input goes to `:N`, coords unscaled); Chrome accepts a handle so it launches inside `:N` at the backend resolution; the driver capture calls route through a per-session seam. **This task is strictly ADDITIVE and behavior-preserving** — the session's `display` stays `None` and `native_mode` defaults from the global `CU_NATIVE_MODE`, so every seam falls back to the exact legacy path until Task 9.4 flips allocation + the launch sites. `ensure_browser`/`destroy` are NOT touched here (they change behavior by allocating — that is 9.4's atomic flip), so the tree stays runnable.

**Files:**
- Modify: `Orchestrator/browser/actions.py` (`_run_xdotool` else-branch line 147; `ActionExecutor.__init__` line 213; `to_native` line 228-240; pass `self.display_number` at the 10 action call sites)
- Modify: `Orchestrator/browser/session_manager.py` (`ComputerUseSession.__init__` line 62-91 — add `native_mode`/`display` fields + `display_number` property + `capture_screenshot_bytes`; do NOT touch `ensure_browser`/`destroy`/`is_alive`)
- Modify: `Orchestrator/browser/chrome.py` (`start` line 25-94 — accept an optional handle, use its DISPLAY + resolution; `handle=None` preserves the legacy path)
- Modify: `Orchestrator/gemini_cu/session_manager.py` (add `native_mode`/`display`/`display_number` to `GeminiCUSession`)
- Modify: `Orchestrator/browser/driver_anthropic.py` (line 45, 538), `Orchestrator/openai_cu/agent_loop.py` (`_capture_cu_screenshot` line 212 + call sites 357/534/538), `Orchestrator/gemini_cu/agent_loop.py` (line 192)
- Test: `Orchestrator/tests/test_cu_display_wiring.py` (new)

1. Write the failing test:

```python
"""M9: per-session display wiring — ActionExecutor targets the session's :N with
unscaled coords; ComputerUseSession exposes display_number + a capture seam."""
import pytest
from Orchestrator.browser.actions import ActionExecutor
from Orchestrator.browser import session_manager as sm


def test_action_executor_records_display_and_native_flag():
    ex = ActionExecutor(display_number=101, native_mode=False)
    assert ex.display_number == 101
    assert ex.native_mode is False


def test_action_executor_virtual_coords_unscaled():
    ex = ActionExecutor(display_number=101, native_mode=False)
    assert ex.to_native(640, 360) == (640, 360)  # scale 1.0 in virtual mode


def test_xdotool_targets_the_instances_display(monkeypatch):
    seen = {}
    def fake_run(*args, **kw):
        seen["env_display"] = kw["env"].get("DISPLAY")
        class R:  # minimal CompletedProcess stand-in
            returncode = 0; stdout = ""; stderr = ""
        return R()
    monkeypatch.setattr("Orchestrator.browser.actions.subprocess.run", fake_run)
    monkeypatch.setattr("Orchestrator.browser.actions._use_ydotool", lambda: False)
    ex = ActionExecutor(display_number=102, native_mode=False)
    ex.mouse_move(10, 20)
    assert seen["env_display"] == ":102"  # NOT the singleton's display


def test_session_display_number_defaults_to_active_when_no_handle():
    s = sm.ComputerUseSession("op")
    from Orchestrator.browser.config import ACTIVE_DISPLAY
    assert s.display is None
    assert s.display_number == ACTIVE_DISPLAY
```

2. Run, expect failure (`ActionExecutor` has no `native_mode` kwarg; session no `display`).
   Run: `python -m pytest Orchestrator/tests/test_cu_display_wiring.py -q`
   Expected: `TypeError: __init__() got an unexpected keyword argument 'native_mode'` — FAIL.

3. `actions.py` — make `_run_xdotool` honor the passed display (not the singleton) and `ActionExecutor` carry a per-session display + native flag:

   3a. Replace the else-branch of `_run_xdotool` (line 146-147):
```python
    else:
        env = {"DISPLAY": f":{display_number}", "PATH": "/usr/bin:/usr/local/bin:/bin"}
```
   3b. `ActionExecutor.__init__` (line 213) — add `native_mode`:
```python
    def __init__(self, display_number: int = DISPLAY_NUMBER,
                 coord_space: str = COORD_SPACE_ANTHROPIC, native_mode: bool = None):
        self.display_number = display_number
        if coord_space not in _COORD_SPACES:
            raise ValueError(
                f"unknown coord_space {coord_space!r}; expected one of {_COORD_SPACES}")
        self.coord_space = coord_space
        self.use_ydotool = _use_ydotool()
        self.native_mode = NATIVE_MODE if native_mode is None else native_mode
```
   3c. In `to_native` (line 228-240) branch on the INSTANCE flag, not the global — replace `from ... import (..., NATIVE_MODE,)` usage:
```python
        from Orchestrator.browser.config import (
            detect_native_resolution, CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT,
        )
        if not self.native_mode:
            return int(x), int(y)
        w, h = detect_native_resolution()
```
   3d. Pass `self.display_number` at EVERY `_run_xdotool(...)` action call site (lines 265/274/282/290/297/303/313/321/330/424) — append `, display_number=self.display_number`. Example (line 265):
```python
            _run_xdotool("mousemove", "--sync", str(x), str(y), display_number=self.display_number)
```
   (Do this for all 10 call sites listed above; `_run_ydotool` is unchanged — Wayland/native only.)

4. `session_manager.py` — add the fields + seam ONLY (do NOT touch `ensure_browser`/`destroy`/`is_alive` — those flip in 9.4). In `ComputerUseSession.__init__` replace line 68 (`self.actions = ActionExecutor()`) with:
```python
        self.native_mode: bool = False       # virtual by default; native is opt-in (M9)
        self.display = None                  # DisplayHandle when virtual, else None
        self.actions = ActionExecutor()
```
   Add methods after `__init__` (before `request_stop`):
```python
    @property
    def display_number(self) -> int:
        from Orchestrator.browser.config import ACTIVE_DISPLAY
        return self.display.display_num if self.display is not None else ACTIVE_DISPLAY

    def capture_screenshot_bytes(self) -> bytes:
        """Screenshot THIS session's surface. Branches on whether a virtual
        display is ALLOCATED (not on native_mode), so with display=None it is
        byte-identical to the legacy capture_screenshot() path — additive and
        behavior-preserving until 9.4 allocates a per-session display."""
        from Orchestrator.browser.screenshot import (
            capture_screenshot, capture_screenshot_display,
        )
        if self.display is not None:
            return capture_screenshot_display(self.display.display_num)
        return capture_screenshot()
```
   (`ensure_browser`, `destroy`, `is_alive` stay exactly as they are — the allocation flip is Task 9.4, which rewrites `ensure_browser`/`destroy` atomically alongside the launch-site changes and the shim retirement.)

5. `chrome.py` — `start` accepts the handle so it launches inside the session's display at its resolution. Replace `start`'s signature + the `display`/`env`/`--window-size` lines (25-40):
```python
    def start(self, url: str = "about:blank", handle=None) -> bool:
        """Launch Chrome on the session's virtual display with CDP."""
        if self.is_running():
            print(f"[CHROME] Already running for operator {self.operator}")
            return True
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if handle is not None:
            env = handle.get_env()
            win_w, win_h = handle.width, handle.height
        else:
            from Orchestrator.browser.display import get_display
            display = get_display()
            env = display.get_env()
            win_w, win_h = DISPLAY_WIDTH, DISPLAY_HEIGHT
        cmd = [
            CHROME_PATH,
            f"--user-data-dir={self.profile_dir}",
            f"--window-size={win_w},{win_h}",
            "--window-position=0,0",
            # ... (rest of cmd unchanged from line 42 down) ...
```
   (Leave the rest of the `cmd` list and Popen block from line 42 onward exactly as-is.)

6. `gemini_cu/session_manager.py` — give `GeminiCUSession` the same seam. In its `__init__` add:
```python
        self.native_mode: bool = False
        self.display = None
```
   And a property:
```python
    @property
    def display_number(self) -> int:
        from Orchestrator.browser.config import ACTIVE_DISPLAY
        return self.display.display_num if self.display is not None else ACTIVE_DISPLAY
```

7. Driver capture seams — route to the session's display:
   - `driver_anthropic.py:45`: `return capture_screenshot()` → `return session.capture_screenshot_bytes()`
   - `driver_anthropic.py:538`: `fresh_png = capture_screenshot()` → `fresh_png = session.capture_screenshot_bytes()`
   - `openai_cu/agent_loop.py`: `_capture_cu_screenshot` (line 212) is MODULE-level — `session` is NOT in scope. Give it the session: change its signature `async def _capture_cu_screenshot(session) -> bytes:`, change its body line 220 `png = await asyncio.to_thread(capture_screenshot)` → `png = await asyncio.to_thread(session.capture_screenshot_bytes)` (the trailing `resize_screenshot(..., OPENAI_CU_WIDTH, OPENAI_CU_HEIGHT)` stays — a no-op for a virtual 1280×720 display), and pass `session` at all three call sites inside `run_openai_cu_loop(session, …)`: lines 357/534/538 `_capture_cu_screenshot()` → `_capture_cu_screenshot(session)`.
   - `gemini_cu/agent_loop.py:192`: `return await asyncio.to_thread(capture_screenshot_display, ACTIVE_DISPLAY)` → `return await asyncio.to_thread(capture_screenshot_display, session.display_number)`
   - (The two INITIAL-screenshot sites `headless.py:665` and `chat_routes.py:4224` also call bare `capture_screenshot()`; those files are rewritten in 9.4, so their capture seam is switched to `session.capture_screenshot_bytes()` there — not here.)

8. Run the wiring test + the CU regression set, expect PASS.
   Run: `python -m pytest Orchestrator/tests/test_cu_display_wiring.py Orchestrator/tests/test_cu_display_arbiter.py Orchestrator/tests/test_gemini_cu_actions.py -q`
   Expected: all `passed` (new wiring green; arbiter + gemini action tests unregressed).

9. Commit.
   Run: `git add Orchestrator/browser/actions.py Orchestrator/browser/session_manager.py Orchestrator/browser/chrome.py Orchestrator/gemini_cu/session_manager.py Orchestrator/browser/driver_anthropic.py Orchestrator/openai_cu/agent_loop.py Orchestrator/gemini_cu/agent_loop.py Orchestrator/tests/test_cu_display_wiring.py && git commit -m "feat(cu): thread per-session virtual display through ActionExecutor, Chrome, and the capture seam"`
   Expected: one commit, eight files.

---

### Task 9.4: Launch sites default to virtual; native becomes an explicit opt-in; retire the singleton shim

**Depends on:** 9.3

Flip `use_computer`/`/browser/run`/scheduler (headless) and the three chat CU launch sites so a session is virtual by default; native ("act on my desktop") is a per-launch opt-in still guarded by `display_arbiter` (its native-only mutex is unchanged). Then delete the backward-compat shim from 9.1 so the singleton is fully gone.

**Files:**
- Modify: `Orchestrator/browser/session_manager.py` (rewrite `ensure_browser` line 184-195 to allocate the per-session virtual display; rewrite `destroy` line 197-204 to release it)
- Modify: `Orchestrator/browser/headless.py` (`run_cu_task` line 490-592, `_run_gemini_cu_task` line 403-487; add `native_mode` param; virtual branch skips the arbiter, native keeps it; initial-capture seam line 665)
- Modify: `Orchestrator/routes/chat_routes.py` (`stream_computer_use` 4074/4166-4188/4224/4293, `stream_gemini_computer_use` 4368/4518, `stream_openai_computer_use` 4657/4775)
- Modify: `Orchestrator/routes/browser_routes.py` (`BrowserRunIn` model line 16-21, `/browser/run` line 24-49 — pass `native_mode` through to the task)
- Modify: `Orchestrator/tasks.py` (the USE_COMPUTER dispatch that calls `run_cu_task` at line 1726 — thread `native_mode` from `result_data`)
- Modify: `Orchestrator/browser/display_arbiter.py` (`_browser_holds_local` line 171-173, `_gemini_holds_local` line 176-178 — only NATIVE sessions contend)
- Modify: `Orchestrator/browser/display.py` (delete the `_DefaultDisplayShim`/`get_display`/`ensure_display_running` shim now that no caller uses it)
- Test: `Orchestrator/tests/test_cu_virtual_default.py` (new)

1. Write the failing test — a virtual launch allocates a per-session display and does NOT take the native arbiter claim; a native launch does:

```python
"""M9: virtual is the default launch mode (per-session display, no native
arbiter claim); native is an explicit opt-in that still claims the shared display."""
import pytest
from Orchestrator.browser import session_manager as bsm
from Orchestrator.browser.session_manager import ComputerUseSession
from Orchestrator.browser import display_arbiter as da


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(ComputerUseSession, "is_alive", lambda self: True)
    monkeypatch.setattr(ComputerUseSession, "destroy", lambda self: None)
    for reg in (bsm._sessions, bsm._operator_sessions):
        reg.clear()
    da._reservations.clear()
    yield
    for reg in (bsm._sessions, bsm._operator_sessions):
        reg.clear()
    da._reservations.clear()


def test_default_session_is_virtual_not_native():
    s = bsm.get_or_create_session("op")
    assert s.native_mode is False


def test_virtual_launch_leaves_the_native_display_free(monkeypatch):
    # A virtual session running does NOT register as the native-display owner.
    s = bsm.get_or_create_session("op")
    s.native_mode = False
    s.status = "running"
    assert da.local_display_owner() is None  # native mutex untouched by virtual work
```

2. Run, expect failure (`native_mode` may not gate `local_display_owner` correctly yet, or the default assertion differs).
   Run: `python -m pytest Orchestrator/tests/test_cu_virtual_default.py -q`
   Expected: FAIL (a running browser session with `device_id=="blackbox"` still reports as native owner via `_browser_holds_local`).

3. `display_arbiter.py` — teach `_browser_holds_local` (line 171-173) and `_gemini_holds_local` (line 176-178) to ignore VIRTUAL sessions (only NATIVE sessions contend for the one physical display):
```python
def _browser_holds_local(session) -> bool:
    return (getattr(session, "device_id", None) == "blackbox"
            and getattr(session, "native_mode", False)
            and getattr(session, "status", None) in _BUSY_STATUSES)

def _gemini_holds_local(session) -> bool:
    return (is_local_environment(getattr(session, "environment", None))
            and getattr(session, "native_mode", False)
            and getattr(session, "status", None) in _BUSY_STATUSES)
```

4. `session_manager.py` — flip `ensure_browser` to allocate the per-session virtual display and `destroy` to release it (this is the behavior change deferred from 9.3). Replace `ensure_browser` (line 184-195):
```python
    async def ensure_browser(self, url: str = "about:blank", backend: str = "anthropic") -> bool:
        """Start this session's display + Chrome. Native: nothing to start."""
        if self.native_mode:
            return True
        from Orchestrator.browser.display import get_allocator
        if self.display is None:
            try:
                self.display = get_allocator().allocate(
                    self.session_id, backend=backend, operator=self.operator)
            except Exception as e:
                print(f"[CU-SESSION] display allocation failed for {self.operator}: {e}")
                return False
            # Re-bind the input executor to THIS session's display, unscaled.
            from Orchestrator.browser.actions import (
                ActionExecutor, COORD_SPACE_GEMINI, COORD_SPACE_ANTHROPIC)
            coord = COORD_SPACE_GEMINI if backend in ("google", "gemini") else COORD_SPACE_ANTHROPIC
            self.actions = ActionExecutor(display_number=self.display.display_num,
                                          coord_space=coord, native_mode=False)
        self.display.touch()
        if not self.chrome.is_running():
            return self.chrome.start(url, handle=self.display)
        return True
```
   Replace `destroy` (line 197-204):
```python
    def destroy(self):
        """Release this session's virtual display + Chrome. Native: nothing."""
        if self.native_mode:
            return
        try:
            self.chrome.stop()
        except Exception as e:
            print(f"[CU-SESSION] Error stopping Chrome for {self.operator}: {e}")
        if self.display is not None:
            from Orchestrator.browser.display import get_allocator
            get_allocator().release(self.session_id)
            self.display = None
```

5. `headless.run_cu_task` — add `native_mode: bool = False`; branch the display setup. Replace the claim + display block (lines 568-592):
```python
        # ── VIRTUAL (default): per-session display, no shared-display claim.
        #    NATIVE opt-in: claim the shared physical display via the arbiter. ──
        if session.device_id == "blackbox" and native_mode:
            session.native_mode = True
            owner = try_claim("browser", operator, task_id, session_id=session.session_id)
            if owner is not None:
                return _failure(f"Cannot start Computer Use — {owner.describe()}. Stop it first.")
        else:
            session.native_mode = False

        if session.device_id == "blackbox":
            if url and not is_domain_allowed(url):
                return _failure(f"Domain blocked by security policy: {url}")
            if not await session.ensure_browser(url or "about:blank", backend=backend):
                return _failure("Failed to start browser session")
            if not session.native_mode and (session.display is None
                                            or not session.display.get_env()):
                return _failure("Virtual display allocation failed")
            if not session.conversation_history:
                await asyncio.sleep(2)
        else:
            # ... (remote VNC branch unchanged, lines 595-606) ...
```
   (`try_claim`/`release_claim` are still imported at line 555; the `finally: release_claim(task_id)` is a no-op for a virtual launch that never claimed — safe.) Add `native_mode=False` to the `run_cu_task` signature (line 490-492) and thread it into `_run_gemini_cu_task` (pass `native_mode` and, in that helper, only `try_claim` when `native_mode` is True — mirror step above around line 457-461).

6. `tasks.py` USE_COMPUTER dispatch — read `native_mode` from `result_data` and pass it to `run_cu_task(...)` (the caller sets `result_data["native_mode"]`). At the `run_cu_task(` call (line 1726) add `native_mode=task.result_data.get("native_mode", False)`.

7. `browser_routes.py` — `BrowserRunIn` gains `native_mode: Optional[bool] = False`; `/browser/run` stores it in `result_data` (line 36-41 dict):
```python
        result_data={
            "url": req.url,
            "system_prompt": req.system_prompt,
            "device_id": req.device_id or "blackbox",
            "native_mode": bool(req.native_mode),
        }
```

8. `chat_routes.py` — the three streams take `native_mode: bool = False` and gate the arbiter claim + set `session.native_mode`. In `stream_computer_use` replace the display-setup block (4166-4188) with the virtual/native branch (same shape as step 5: `session.native_mode = bool(native_mode)`, only `try_claim` when native at line 4293, call `await session.ensure_browser("about:blank", backend="anthropic")`, drop the singleton `get_display()` health check for the virtual path) and switch the initial-capture at line 4224 (`initial_png = capture_screenshot()`) to `initial_png = session.capture_screenshot_bytes()`. Repeat for `stream_gemini_computer_use` (backend `"google"`, claim at 4518) and `stream_openai_computer_use` (backend `"openai"`, claim at 4775). Wherever the try_claim currently fires unconditionally, wrap it in `if native_mode:`. Also switch `headless.py:665` (`initial_png = capture_screenshot()` → `session.capture_screenshot_bytes()`).

9. Delete the shim from `display.py` (the `_DefaultDisplayShim` class + `get_display`/`ensure_display_running` added in 9.1 step 4). Confirm no remaining importers:
   Run: `grep -rn "get_display\|ensure_display_running\|VirtualDisplay" Orchestrator/ --include=*.py | grep -v test_`
   Expected: NO matches (all callers now use `get_allocator()` / session handles). If any remain, fix them before deleting.

10. Run the full CU suite, expect PASS.
    Run: `python -m pytest Orchestrator/tests/test_cu_virtual_default.py Orchestrator/tests/test_cu_display_arbiter.py Orchestrator/tests/test_cu_display_allocator.py Orchestrator/tests/test_cu_headless_runner.py -q`
    Expected: all `passed`.

11. Commit.
    Run: `git add Orchestrator/browser/session_manager.py Orchestrator/browser/headless.py Orchestrator/routes/chat_routes.py Orchestrator/routes/browser_routes.py Orchestrator/tasks.py Orchestrator/browser/display_arbiter.py Orchestrator/browser/display.py Orchestrator/tests/test_cu_virtual_default.py && git commit -m "feat(cu): default CU launches to per-session virtual displays; native is an explicit arbiter-guarded opt-in"`
    Expected: one commit, eight files.

---

### Task 9.5: Live view — noVNC viewer page, WS reverse-proxy, static mount

**Depends on:** 9.1 (websockify member), 9.4 (sessions exist)

Serves a per-session live view: an Orchestrator-hosted noVNC viewer that opens a WebSocket back through `/cu/view/{session_id}/ws`, which the Orchestrator reverse-proxies to that session's loopback websockify. noVNC assets are served from the system apt path.

**Files:**
- Modify: `Orchestrator/routes/browser_routes.py` (add `GET /cu/view/{session_id}` HTML + `WS /cu/view/{session_id}/ws` proxy)
- Modify: `Orchestrator/app.py` (conditional static mount of `/usr/share/novnc` at `/cu/novnc`, near the existing StaticFiles mount ~line 209)
- Test: `Orchestrator/tests/test_cu_view_routes.py` (new)

1. Write the failing test (uses FastAPI `TestClient` against the shared `app`):

```python
"""M9: /cu/view — viewer HTML resolves the session's ws port; unknown session
degrades gracefully; the WS proxy rejects an unknown session."""
from starlette.testclient import TestClient
from Orchestrator.checkpoint import app
from Orchestrator.browser import display as disp


def _fake_handle(monkeypatch, session_id="sess-1", ws_port=6101, live=True):
    h = disp.DisplayHandle(session_id=session_id, slot=0, backend="anthropic",
                           operator="op", width=1280, height=720, display_num=100,
                           vnc_port=5901, ws_port=ws_port, live_view=live)
    monkeypatch.setattr(disp.DisplayAllocator, "get",
                        lambda self, sid: h if sid == session_id else None)


def test_view_page_renders_for_known_session(monkeypatch):
    _fake_handle(monkeypatch)
    r = TestClient(app).get("/cu/view/sess-1")
    assert r.status_code == 200
    assert "/cu/view/sess-1/ws" in r.text          # socket path injected
    assert "/cu/novnc/core/rfb.js" in r.text        # noVNC module referenced


def test_view_page_unknown_session_is_friendly(monkeypatch):
    _fake_handle(monkeypatch)
    r = TestClient(app).get("/cu/view/nope")
    assert r.status_code == 404
    assert "No active" in r.text


def test_view_page_live_view_unavailable_notice(monkeypatch):
    _fake_handle(monkeypatch, live=False)
    r = TestClient(app).get("/cu/view/sess-1")
    assert r.status_code == 200
    assert "novnc" in r.text.lower()  # install-novnc notice
```

2. Run, expect failure (routes don't exist → 404 with no body match).
   Run: `python -m pytest Orchestrator/tests/test_cu_view_routes.py -q`
   Expected: FAIL (`assert "/cu/view/sess-1/ws" in r.text` — body is a bare 404).

3. Add the routes to `browser_routes.py` (append at end, after `/cu/preflight`):

```python
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

_CU_VIEW_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>CU Live View — {session_id}</title>
<style>html,body{{margin:0;height:100%;background:#0b0b0d;overflow:hidden}}
#screen{{width:100vw;height:100vh}}</style></head>
<body><div id="screen"></div>
<script type="module">
import RFB from '/cu/novnc/core/rfb.js';
const proto = location.protocol === 'https:' ? 'wss' : 'ws';
const url = `${{proto}}://${{location.host}}/cu/view/{session_id}/ws`;
const rfb = new RFB(document.getElementById('screen'), url, {{}});
rfb.viewOnly = true;          // D11: watch only, no takeover
rfb.scaleViewport = true;     // fit 1280x720 / 1440x900 into the panel
rfb.resizeSession = false;    // never resize the agent's screen
</script></body></html>"""

_CU_VIEW_UNAVAILABLE = ("<!doctype html><meta charset=utf-8><body "
    "style='font-family:system-ui;background:#0b0b0d;color:#ddd;padding:2rem'>"
    "<h3>Live view unavailable</h3><p>noVNC / websockify are not installed on "
    "this box (SHOULD_HAVE in system-packages.txt). The CU session is still "
    "running — install <code>novnc</code> + <code>websockify</code> to watch.</p>")


@app.get("/cu/view/{session_id}", response_class=HTMLResponse)
def cu_view(session_id: str):
    from Orchestrator.browser.display import get_allocator
    h = get_allocator().get(session_id)
    if h is None:
        return HTMLResponse("<!doctype html><body>No active CU session for that id.",
                            status_code=404)
    if not h.live_view:
        return HTMLResponse(_CU_VIEW_UNAVAILABLE)
    return HTMLResponse(_CU_VIEW_HTML.format(session_id=session_id))


@app.websocket("/cu/view/{session_id}/ws")
async def cu_view_ws(websocket: WebSocket, session_id: str):
    """Reverse-proxy the viewer's WebSocket to this session's loopback websockify.
    Loopback-only target; the Tailscale perimeter is the auth boundary (§9)."""
    import asyncio
    import websockets
    from websockets.exceptions import ConnectionClosed
    from Orchestrator.browser.display import get_allocator

    h = get_allocator().get(session_id)
    # Plain accept() — mirror the proven app_proxy_websocket pattern. noVNC 1.x
    # and websockify both default to binary frames without requiring the
    # Sec-WebSocket-Protocol header, and a transparent proxy does not forward it
    # (forcing subprotocol="binary" when the client offered none breaks the
    # handshake).
    await websocket.accept()
    if h is None or not h.live_view:
        await websocket.close(code=1008, reason="No live view for session")
        return
    target = f"ws://127.0.0.1:{h.ws_port}/"
    try:
        upstream = await websockets.connect(target, max_size=None, open_timeout=10)
    except Exception as e:
        print(f"[CU-VIEW] upstream connect failed ({target}): {e}")
        await websocket.close(code=1011, reason="Upstream unavailable")
        return

    async def c2u():
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
                elif msg.get("text") is not None:
                    await upstream.send(msg["text"])
        except (WebSocketDisconnect, ConnectionClosed):
            pass

    async def u2c():
        try:
            async for frame in upstream:
                if isinstance(frame, bytes):
                    await websocket.send_bytes(frame)
                else:
                    await websocket.send_text(frame)
        except (ConnectionClosed, WebSocketDisconnect, RuntimeError):
            pass

    t1 = asyncio.create_task(c2u())
    t2 = asyncio.create_task(u2c())
    try:
        _done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    finally:
        await upstream.close()
```

4. `app.py` — conditional static mount for noVNC assets (near line 209, AFTER the router includes so it never shadows API routes):
```python
import os as _os
_novnc_dir = "/usr/share/novnc"
if _os.path.isdir(_novnc_dir):
    app.mount("/cu/novnc", StaticFiles(directory=_novnc_dir), name="cu-novnc")
```

5. Run, expect PASS.
   Run: `python -m pytest Orchestrator/tests/test_cu_view_routes.py -q`
   Expected: `3 passed`.

6. Commit.
   Run: `git add Orchestrator/routes/browser_routes.py Orchestrator/app.py Orchestrator/tests/test_cu_view_routes.py && git commit -m "feat(cu): per-session noVNC live-view page + WS reverse-proxy + novnc static mount"`
   Expected: one commit, three files.

---

### Task 9.6: `GET /cu/sessions` in-use endpoint + reaper startup/sweep hooks

**Depends on:** 9.1, 9.2, 9.4

The D11 in-use flag source: a lightweight endpoint listing active virtual sessions (visible to all users). Also wires the boot orphan reaper into startup and the TTL reaper into the existing periodic CU sweep.

**Files:**
- Modify: `Orchestrator/routes/browser_routes.py` (add `GET /cu/sessions`)
- Modify: `Orchestrator/startup.py` (new `@app.on_event("startup")` — startup hooks live here, not app.py; runs `reap_orphans()` once + spawns a periodic `reap_idle()` task. `cleanup_inactive_sessions` in session_manager is NOT scheduled anywhere in the tree, so the TTL reaper needs its OWN loop)
- Test: `Orchestrator/tests/test_cu_sessions_endpoint.py` (new)

1. Write the failing test:

```python
"""M9: /cu/sessions surfaces active virtual CU sessions for the in-use flag."""
from starlette.testclient import TestClient
from Orchestrator.checkpoint import app
from Orchestrator.browser import display as disp


def test_sessions_empty(monkeypatch):
    monkeypatch.setattr(disp.DisplayAllocator, "active_sessions", lambda self: [])
    r = TestClient(app).get("/cu/sessions")
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is False and body["count"] == 0 and body["sessions"] == []


def test_sessions_reports_active(monkeypatch):
    fake = [{"session_id": "s1", "operator": "Brandon", "backend": "anthropic",
             "width": 1280, "height": 720, "display": ":100", "live_view": True,
             "view_url": "/cu/view/s1", "started_at": 1.0}]
    monkeypatch.setattr(disp.DisplayAllocator, "active_sessions", lambda self: fake)
    r = TestClient(app).get("/cu/sessions")
    body = r.json()
    assert body["active"] is True and body["count"] == 1
    assert body["sessions"][0]["operator"] == "Brandon"
    assert body["sessions"][0]["view_url"] == "/cu/view/s1"
```

2. Run, expect failure (404).
   Run: `python -m pytest Orchestrator/tests/test_cu_sessions_endpoint.py -q`
   Expected: FAIL (`assert r.status_code == 200` → 404).

3. Add the endpoint to `browser_routes.py`:
```python
@app.get("/cu/sessions")
def cu_sessions():
    """Active virtual CU sessions (D11 in-use flag). Visible to all users; no
    per-operator gating (Tailscale perimeter is the boundary)."""
    from Orchestrator.browser.display import get_allocator
    sessions = get_allocator().active_sessions()
    return {"active": bool(sessions), "count": len(sessions), "sessions": sessions}
```

4. Boot + periodic reaper — add a new startup event in `Orchestrator/startup.py` (which already owns `@app.on_event("startup")` handlers and imports `app`). It sweeps restart-survivors once, then loops the TTL reaper (the survivor sweep is the §9 requirement that teardown-by-pid also runs at boot):
```python
@app.on_event("startup")
async def _cu_display_reaper():
    import asyncio
    from Orchestrator.browser.display import get_allocator, VIRTUAL_DISPLAY_TTL
    try:
        get_allocator().reap_orphans()  # sweep restart-survivor children once
    except Exception as e:
        print(f"[STARTUP] CU display orphan reap skipped: {e}")

    async def _loop():
        interval = max(60.0, VIRTUAL_DISPLAY_TTL / 3.0)
        while True:
            await asyncio.sleep(interval)
            try:
                get_allocator().reap_idle()
            except Exception as e:
                print(f"[CU-DISPLAY] TTL reap skipped: {e}")

    asyncio.create_task(_loop())
```

5. Run, expect PASS.
   Run: `python -m pytest Orchestrator/tests/test_cu_sessions_endpoint.py -q`
   Expected: `2 passed`.

6. Commit.
   Run: `git add Orchestrator/routes/browser_routes.py Orchestrator/startup.py Orchestrator/tests/test_cu_sessions_endpoint.py && git commit -m "feat(cu): GET /cu/sessions in-use endpoint + boot orphan sweep + periodic TTL display reaper"`
   Expected: one commit, three files.

---

### Task 9.7: Preflight — Xvfb (MUST_HAVE) + websockify/noVNC (SHOULD_HAVE) checks

**Depends on:** nothing (independent; can land any time)

**Files:**
- Modify: `Orchestrator/browser/preflight.py` (add `check_virtual_display`, `check_live_view`; register in `run_preflight` line 122-134)
- Test: `Orchestrator/tests/test_cu_preflight.py` (extend)

1. Add failing tests to the existing file:
```python
def test_virtual_display_missing_xvfb_fails(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda b: None)
    c = preflight.check_virtual_display()
    assert c["status"] == "fail"
    assert "xvfb" in c["remediation"].lower()


def test_virtual_display_present_ok(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda b: f"/usr/bin/{b}")
    assert preflight.check_virtual_display()["status"] == "ok"


def test_live_view_missing_deps_warns(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda b: None)
    monkeypatch.setattr(preflight.os.path, "isdir", lambda p: False)
    c = preflight.check_live_view()
    assert c["status"] == "warn"  # SHOULD_HAVE — never fails the aggregate


def test_preflight_includes_virtual_display_check():
    ids = {c["id"] for c in preflight.run_preflight(skip_screenshot=True)["checks"]}
    assert "virtual_display" in ids and "live_view" in ids
```

2. Run, expect failure (`check_virtual_display` missing).
   Run: `python -m pytest Orchestrator/tests/test_cu_preflight.py -q -k "virtual_display or live_view"`
   Expected: `AttributeError` — FAIL.

3. Add the checks to `preflight.py` (after `check_chrome`, before `_RANK`):
```python
def check_virtual_display() -> dict:
    if not shutil.which("Xvfb"):
        return _check("virtual_display", "fail",
                      "Xvfb not installed — per-session CU virtual displays unavailable",
                      "Install xvfb (MUST_HAVE in Scripts/onboarding/system-packages.txt); "
                      "re-run Scripts/install.sh")
    return _check("virtual_display", "ok", "Xvfb present (per-session CU displays)")


def check_live_view() -> dict:
    have_ws = bool(shutil.which("websockify"))
    have_novnc = os.path.isdir("/usr/share/novnc")
    if have_ws and have_novnc:
        return _check("live_view", "ok", "websockify + noVNC present (live view enabled)")
    missing = [n for n, ok in (("websockify", have_ws), ("novnc", have_novnc)) if not ok]
    return _check("live_view", "warn",
                  f"Live view degraded — missing {', '.join(missing)}",
                  "Install websockify/novnc (SHOULD_HAVE in system-packages.txt) to watch "
                  "CU sessions in the Portal/Android live-view panel")
```
   Register both in `run_preflight`'s `plan` (after the `chrome` entry, line ~133):
```python
        ("virtual_display", lambda: check_virtual_display()),
        ("live_view", lambda: check_live_view()),
```

4. Run, expect PASS.
   Run: `python -m pytest Orchestrator/tests/test_cu_preflight.py -q`
   Expected: all `passed`.

5. Commit.
   Run: `git add Orchestrator/browser/preflight.py Orchestrator/tests/test_cu_preflight.py && git commit -m "feat(cu): preflight checks for Xvfb (MUST_HAVE) and websockify/noVNC live view (SHOULD_HAVE)"`
   Expected: one commit, two files.

---

### Task 9.8: apt allowlist — verify xvfb / websockify / novnc (NO-OP; owned by M2 Task 2.1)

**Depends on:** nothing. **⚠ Cross-milestone — single owner:** the `xvfb`/`websockify`/`novnc` allowlist lines are added by **M2 Task 2.1** (the install-layer milestone), which lands BEFORE M9 in the execution order. This task does NOT edit `Scripts/onboarding/system-packages.txt` again (a second edit would create duplicate lines). It is a **verification-only no-op** confirming M2 already landed them. (The earlier attribution to "M8" was wrong — M8 is the wizard milestone and never touches this file.)

**Files:** none (M2 Task 2.1 owns the edit).

1. Verify M2 Task 2.1 already added the three allowlist lines (the exact grep the apt dispatcher uses):
   - Run: `grep -E '^[a-zA-Z0-9._+-]+\s+#\s+(MUST_HAVE|SHOULD_HAVE)' Scripts/onboarding/system-packages.txt | awk '{print $1}' | grep -E '^(xvfb|websockify|novnc)$' | sort | tr '\n' ' '`
   - Expected: `novnc websockify xvfb ` (all three present). If any is missing, M2 Task 2.1 was skipped — land it there, NOT here (only one milestone edits this file).
2. No edit, no commit here.

---

### Task 9.9: Portal — live-view panel + "CU in use" indicator (manual browser step)

**Depends on:** 9.5, 9.6. House rule: Portal frontend is validated by a manual browser step + a `?v=` cache bump; no automated frontend test.

**Files:**
- Create: `Portal/modules/cu-live-view.js`
- Modify: `Portal/index.html` (add a live-view panel container + bump `?v=genui318` → `?v=genui319` at lines 11 and 21)
- Modify: `Portal/app-modular.js` (import the new module, mirroring the existing `import './modules/...'` block ~line 46-65)

1. Create `Portal/modules/cu-live-view.js` — polls `/cu/sessions`, shows a global "CU in use" pill visible to all users, and mounts the first active session's live view in an iframe (the same iframe pattern as `Portal/modules/cli-agents-zellij-iframe.js`; the CU drawer already exists in `cu-drawer.js` — hang the panel next to it):
```javascript
// CU live-view panel + in-use indicator (M9 / D11).
// One shared live view; any BlackBox user may watch. When a CU session is
// active an "in use" pill is shown to everyone (no per-operator gating).
const POLL_MS = 4000;
let _timer = null;

async function fetchSessions() {
  try {
    const r = await fetch('/cu/sessions', { cache: 'no-store' });
    if (!r.ok) return { active: false, sessions: [] };
    return await r.json();
  } catch { return { active: false, sessions: [] }; }
}

function renderPill(state) {
  let pill = document.getElementById('cuInUsePill');
  if (!state.active) { if (pill) pill.remove(); return; }
  if (!pill) {
    pill = document.createElement('button');
    pill.id = 'cuInUsePill';
    pill.className = 'cu-inuse-pill';
    pill.onclick = () => openLiveView(state.sessions[0]);
    (document.getElementById('statusLine') || document.body).appendChild(pill);
  }
  const s = state.sessions[0];
  pill.textContent = `● CU in use — ${s.operator} (${s.backend} ${s.width}×${s.height})`;
}

function openLiveView(session) {
  if (!session) return;
  let panel = document.getElementById('cuLiveViewPanel');
  let frame = document.getElementById('cuLiveViewFrame');
  if (!panel || !frame) return;
  frame.src = session.view_url;            // /cu/view/{session_id}
  panel.style.display = 'block';
}

export function initCuLiveView() {
  const closeBtn = document.getElementById('cuLiveViewClose');
  if (closeBtn) closeBtn.onclick = () => {
    const panel = document.getElementById('cuLiveViewPanel');
    const frame = document.getElementById('cuLiveViewFrame');
    if (frame) frame.src = 'about:blank';
    if (panel) panel.style.display = 'none';
  };
  const tick = async () => renderPill(await fetchSessions());
  tick();
  _timer = setInterval(tick, POLL_MS);
}
```

2. In `Portal/index.html`, add the panel container (near the existing CU drawer / app-preview iframe markup, ~line 1334) and bump the two `?v=genui318` markers to `?v=genui319`:
```html
<div id="cuLiveViewPanel" class="cu-liveview-panel" style="display:none">
  <div class="cu-liveview-head">
    <span>Computer Use — live view (watch-only)</span>
    <button id="cuLiveViewClose" class="cu-liveview-close">✕</button>
  </div>
  <iframe id="cuLiveViewFrame" class="cu-liveview-frame"
          sandbox="allow-scripts allow-same-origin" src="about:blank"></iframe>
</div>
```

3. In `Portal/app-modular.js`, add the import + init alongside the other module imports (~line 46):
```javascript
import { initCuLiveView } from './modules/cu-live-view.js';
// ... after DOM ready / app-init runs:
initCuLiveView();
```

4. Manual validation (house rule): restart the service, hard-refresh the Portal, start a CU task in virtual mode (default), confirm (a) the "CU in use" pill appears for a DIFFERENT operator's browser too, and (b) clicking it opens the live-view iframe showing the agent's private screen while the physical desktop is untouched.
   Run: `sudo systemctl restart blackbox.service` (pre-authorized), then open the Portal, trigger a `use_computer`/browser task, watch the panel.
   Expected: pill visible to all users; iframe streams the virtual display; desktop never changes.

5. Commit.
   Run: `git add Portal/modules/cu-live-view.js Portal/index.html Portal/app-modular.js && git commit -m "feat(portal): CU live-view panel + shared 'CU in use' indicator (genui319)"`
   Expected: one commit, three files.

---

### Task 9.10: Android — live-view WebView + `/cu/sessions` client + in-use flag (unit gate + manual Fold)

**Depends on:** 9.5, 9.6. House rule: Android unit gate is `./gradlew :app:testDebugUnitTest --offline`; UI is validated on the Fold device manually.

**Files:** (under `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/`)
- Create: `data/api/CuSessionsClient.kt`
- Create: `ui/cu/CuLiveViewScreen.kt` (thin WebView loading `${baseUrl}/cu/view/{sessionId}`)
- Create test: `.../app/src/test/java/com/aiblackbox/portal/data/api/CuSessionsClientTest.kt`

1. Write the failing unit test (MockWebServer, mirroring `LocalModelApiTest`):
```kotlin
package com.aiblackbox.portal.data.api

import kotlinx.coroutines.test.runTest
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okio.Buffer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class CuSessionsClientTest {
    private lateinit var server: MockWebServer
    private lateinit var client: CuSessionsClient

    @Before fun setUp() {
        server = MockWebServer(); server.start()
        val baseUrl = server.url("").toString().trimEnd('/')
        client = CuSessionsClient(BlackBoxApi(baseUrl))
    }
    @After fun tearDown() = server.close()

    @Test fun `parses active sessions`() = runTest {
        server.enqueue(MockResponse(body = Buffer().writeUtf8(
            """{"active":true,"count":1,"sessions":[
               {"session_id":"s1","operator":"Brandon","backend":"anthropic",
                "width":1280,"height":720,"display":":100","live_view":true,
                "view_url":"/cu/view/s1","started_at":1.0}]}""")))
        val state = client.sessions()
        assertTrue(state.active)
        assertEquals(1, state.sessions.size)
        assertEquals("Brandon", state.sessions[0].operator)
        assertEquals("/cu/view/s1", state.sessions[0].viewUrl)
    }

    @Test fun `empty when idle`() = runTest {
        server.enqueue(MockResponse(body = Buffer().writeUtf8(
            """{"active":false,"count":0,"sessions":[]}""")))
        val state = client.sessions()
        assertFalse(state.active)
        assertTrue(state.sessions.isEmpty())
    }
}
```

2. Run, expect failure (`CuSessionsClient` missing).
   Run: `cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --offline --tests "*CuSessionsClientTest*"`
   Expected: compilation failure — `unresolved reference: CuSessionsClient`.

3. Create `data/api/CuSessionsClient.kt` (uses the existing generic `BlackBoxApi.get(path)` at `BlackBoxApi.kt:131`):
```kotlin
package com.aiblackbox.portal.data.api

import org.json.JSONObject

data class CuSession(
    val sessionId: String, val operator: String, val backend: String,
    val width: Int, val height: Int, val liveView: Boolean, val viewUrl: String,
)
data class CuSessionsState(val active: Boolean, val sessions: List<CuSession>)

/** Polls GET /cu/sessions for the D11 in-use flag + live-view targets. */
class CuSessionsClient(private val api: BlackBoxApi) {
    suspend fun sessions(): CuSessionsState {
        val body = api.get("/cu/sessions")
        val root = JSONObject(body)
        val arr = root.optJSONArray("sessions")
        val out = ArrayList<CuSession>()
        if (arr != null) for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            out.add(CuSession(
                sessionId = o.getString("session_id"),
                operator = o.optString("operator", ""),
                backend = o.optString("backend", ""),
                width = o.optInt("width", 0), height = o.optInt("height", 0),
                liveView = o.optBoolean("live_view", false),
                viewUrl = o.optString("view_url", ""),
            ))
        }
        return CuSessionsState(active = root.optBoolean("active", false), sessions = out)
    }
}
```

4. Create `ui/cu/CuLiveViewScreen.kt` — a Compose `AndroidView` WebView loading the Orchestrator-served viewer (mirror the WebView settings in `PortalActivity.kt:303-324`: `javaScriptEnabled`, `domStorageEnabled`, `mediaPlaybackRequiresUserGesture=false`):
```kotlin
package com.aiblackbox.portal.ui.cu

import android.annotation.SuppressLint
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.viewinterop.AndroidView

/** Watch-only CU live view: loads ${baseUrl}/cu/view/{sessionId}, which serves
 *  the Orchestrator's noVNC viewer (the JS runs in the WebView). */
@SuppressLint("SetJavaScriptEnabled")
@Composable
fun CuLiveViewScreen(baseUrl: String, sessionId: String, modifier: Modifier = Modifier) {
    AndroidView(modifier = modifier.fillMaxSize(), factory = { ctx ->
        WebView(ctx).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.mediaPlaybackRequiresUserGesture = false
            webViewClient = WebViewClient()
            loadUrl("${baseUrl.trimEnd('/')}/cu/view/$sessionId")
        }
    })
}
```
   Surface the in-use flag: wherever the Android home/status surface shows CLI-agent/device chips, poll `CuSessionsClient.sessions()` and render a "CU in use" chip when `active`, tapping it to navigate to `CuLiveViewScreen(baseUrl, sessions[0].sessionId)`. (Exact nav wiring is a manual Fold step per the house rule; mirror the existing chip/nav pattern used by the CLI-agent terminal surface.)

5. Run the unit gate, expect PASS.
   Run: `cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --offline --tests "*CuSessionsClientTest*"`
   Expected: `BUILD SUCCESSFUL`, tests pass.

6. Manual Fold validation (house rule): sideload the debug APK, start a CU task from any surface, confirm the "CU in use" chip appears and tapping it opens the live view streaming the agent's virtual screen.
   Expected: chip visible; WebView streams the virtual display; no takeover of the phone or the box desktop.

7. Commit.
   Run: `git add "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/data/api/CuSessionsClient.kt" "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/cu/CuLiveViewScreen.kt" "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/data/api/CuSessionsClientTest.kt" && git commit -m "feat(android): CU live-view WebView + /cu/sessions client + in-use flag"`
   Expected: one commit, three files.

---

### Task 9.11: Two-concurrent-session smoke (integration)

**Depends on:** 9.4, 9.5, 9.6. A manual/scripted end-to-end check that two virtual sessions coexist on distinct displays and the cap holds — the §10 acceptance "GPU/CU parallel" line for this feature.

**Files:**
- Create: `diagnostics/cu_virtual_smoke.py` (a runnable script, not a pytest — exercises the real allocator with real Xvfb on a box that has it)

1. Write the smoke script:
```python
#!/usr/bin/env python3
"""M9 smoke: two concurrent virtual CU displays coexist; the 4th trips the cap.
Run on a box WITH Xvfb installed (MS02 / any GPU box). Not a CI test — real spawns."""
import sys
from Orchestrator.browser.display import DisplayAllocator, MAX_VIRTUAL_SESSIONS

def main():
    a = DisplayAllocator()
    try:
        h1 = a.allocate("smoke-1", backend="anthropic", operator="system")
        h2 = a.allocate("smoke-2", backend="google", operator="system")
        assert h1.display == ":100" and h2.display == ":101", (h1.display, h2.display)
        assert h1.vnc_port == 5901 and h2.vnc_port == 5902
        assert (h2.width, h2.height) == (1440, 900)   # per-backend resolution
        print(f"[OK] two concurrent displays: {h1.display} + {h2.display}")
        print(f"[OK] active_sessions -> {len(a.active_sessions())} sessions")
        # Fill to cap, then assert the next allocate raises.
        for i in range(2, MAX_VIRTUAL_SESSIONS):
            a.allocate(f"smoke-fill-{i}", backend="anthropic", operator="system")
        try:
            a.allocate("smoke-over", backend="anthropic", operator="system")
            print("[FAIL] cap not enforced"); return 1
        except RuntimeError as e:
            print(f"[OK] cap enforced: {e}")
        return 0
    finally:
        a.shutdown_all()
        print("[OK] all sessions torn down")

if __name__ == "__main__":
    sys.exit(main())
```

2. Run on a box with Xvfb (dev box has `xvfb` after Task 9.8's install, or run on MS02).
   Run: `python diagnostics/cu_virtual_smoke.py`
   Expected: `[OK]` lines for two concurrent displays, distinct ports, Gemini 1440×900, cap enforced, clean teardown; exit 0. Cross-check with `ss -tlnp | grep -E '590[1-3]|610[1-3]'` while it runs (loopback binds only) and `pgrep -a Xvfb` shows `:100`/`:101` gone after teardown.

3. Commit.
   Run: `git add diagnostics/cu_virtual_smoke.py && git commit -m "test(cu): two-concurrent-virtual-display smoke script (real Xvfb, cap enforcement)"`
   Expected: one commit, one file.

---

**Milestone 9 exit criteria:** the full CU suite is green (`python -m pytest Orchestrator/tests/test_cu_display_allocator.py Orchestrator/tests/test_cu_display_wiring.py Orchestrator/tests/test_cu_virtual_default.py Orchestrator/tests/test_cu_view_routes.py Orchestrator/tests/test_cu_sessions_endpoint.py Orchestrator/tests/test_cu_preflight.py Orchestrator/tests/test_cu_display_arbiter.py -q`); the Android unit gate passes; and on a box with Xvfb the two-session smoke + a manual Portal/Android live-view pass confirm: a CU session runs on a private virtual display at the model's native resolution, the physical desktop is never touched, all users see the "CU in use" flag and can watch the shared live view, `GET /local-models/status` is untouched by this work, and a service restart orphans-then-reaps the virtual children (boot reaper) rather than leaking them.


---

## Milestone 10: MS02 Phase-2 Runbook — Gate Harnesses, Deploy, G1–G6, Acceptance & Rollback

**Depends on:** Milestones 1–9 (the entire on-box local-model stack must be code-complete and merged to `main`: `Orchestrator/local_stack.py` resolver; the `localstack` embeddings + rerank providers and their `registry.py`/`RERANK_MODELS` entries; the `onbox` STT routing token in `resolve_stt_provider`/`_local_bridge`/`_local_transcribe`; the Orchestrator-level voice-stream serialization; `LocalModels/qwen_tts_server/` + the `qwen:` TTS catalog group and `POST /tts` routing branch; `installer/templates/llama-swap-config.yaml.template` + `installer/templates/blackbox-install-localstack.sh` (install.sh Step 2f) + the `blackbox-models` systemd/sudoers grants; the `local_models` wizard step + `GET /local-models/status` + `POST /local-models/download`; and the CU virtual-display allocator). This milestone adds NO new production feature code — only the gate-measurement harnesses under `diagnostics/localstack/` and `eval/`, one threshold recalibration in `registry.py`, and the deploy/reset/acceptance/rollback runbook.

**Goal:** Take the code-complete stack from `main` and prove it on the real GPU box (MS02 Ultra, RTX 2000 Ada, `ssh bbx@192.168.1.153`). We author the six benchmark harnesses first (pure-Python + TDD on this dev box, no GPU needed), then execute the Phase-2 runbook on MS02: retire the pinned pair, deploy in-place, run the corpus-quality gate **before** the destructive reset, wipe only the transplanted snapshot ledger, cut the four capabilities on-box through the wizard, and run the live audio/swap gates. Every gate result lands in `eval/results/`; the milestone closes on the §10 acceptance checklist and a documented rollback path.

### ⚠ Phase-2 execution order (read before executing — the spec's table order is corrected here)

The design's §10 table lists **Step-0 reset → deploy → gates**. That order is impossible for **G1**: the chunk-gate harness (`eval/run_bench.py`) scores `eval/labeled_set.jsonl` (~500 rows keyed to snapshot ids from the dev-box corpus) against the live embedding stores — and the corpus those rows point at **is** the transplanted ledger that Step-0 wipes. Building the 8B-Q8 embedding of a *labeled* corpus also requires MS02's GPU (impossible on the no-GPU dev box) and the deployed `localstack` provider. Therefore the only runnable sequence is:

**author harnesses (dev box) → MS02 pre-flight (retire pinned pair, free VRAM) → deploy (non-activating) → G1 + G2 + G3 on the still-present transplant → Step-0 snapshot wipe → wizard cutover onto the fresh empty corpus → G4 + G5 + G6 on the live stack → acceptance → (rollback on failure).**

This does **not** violate D8 (which governs what the reset *wipes*, not *when* it runs) and is consistent with §10's note that Brandon's "giant-corpus re-embed worked great" on the transplant was itself the G1 feasibility signal. **Flagged as an author decision** — see the summary. The tasks below are in true execution order; Step-0 is Task 10.11, not first.

Throughout, `$REPO` on MS02 is `/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main` and remote commands run inside an `ssh bbx@192.168.1.153` session (shown once per block). `$DATE` = the run date, e.g. `2026-07-DD`.

---

### Task 10.1: Gate-harness infra — pure metrics helpers, VRAM sampler, VRAM-idle preflight

Pure, unit-tested helpers shared by G1/G3/G5, a live `nvidia-smi` sampler, and the "GPU near-idle before first retrieval activation" assertion. Authored and committed on this dev box (no GPU needed); MS02 receives them via the Task 10.7 `git pull`.

**Files:**
- Create: `diagnostics/localstack/__init__.py` (empty — makes the probe package importable under `pythonpath=.`)
- Create: `diagnostics/localstack/metrics.py`
- Create: `diagnostics/localstack/vram.py`
- Create: `Scripts/preflight-vram-idle.sh`
- Test: `Orchestrator/tests/test_localstack_metrics.py`

1. Write the failing test `Orchestrator/tests/test_localstack_metrics.py`:
   ```python
   import struct
   import pytest
   from diagnostics.localstack.metrics import (
       parse_nvidia_smi_used_mib, wav_duration_seconds, rtf, summarize_latencies)


   def _wav(sample_rate=24000, channels=1, bits=16, seconds=1.0):
       n = int(sample_rate * seconds)
       data = (b"\x00\x00") * n * channels
       byte_rate = sample_rate * channels * bits // 8
       block_align = channels * bits // 8
       fmt = struct.pack("<HHIIHH", 1, channels, sample_rate, byte_rate, block_align, bits)
       body = (b"fmt " + struct.pack("<I", len(fmt)) + fmt +
               b"data" + struct.pack("<I", len(data)) + data)
       return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body


   def test_parse_used_mib_first_line():
       assert parse_nvidia_smi_used_mib("10278\n") == 10278

   def test_parse_used_mib_skips_blank_takes_first():
       assert parse_nvidia_smi_used_mib("\n  \n11842\n3284\n") == 11842

   def test_parse_used_mib_strips_stray_column():
       assert parse_nvidia_smi_used_mib("11800, 16380\n") == 11800

   def test_parse_used_mib_raises_on_empty():
       with pytest.raises(ValueError):
           parse_nvidia_smi_used_mib("\n   \n")

   def test_wav_duration_one_second():
       assert abs(wav_duration_seconds(_wav(seconds=1.0)) - 1.0) < 1e-6

   def test_wav_duration_half_second_48k_stereo():
       assert abs(wav_duration_seconds(_wav(48000, 2, 16, 0.5)) - 0.5) < 1e-6

   def test_wav_duration_rejects_garbage():
       with pytest.raises(ValueError):
           wav_duration_seconds(b"not a wav at all")

   def test_rtf_basic():
       assert rtf(0.45, 1.0) == 0.45

   def test_rtf_rejects_zero_audio():
       with pytest.raises(ValueError):
           rtf(1.0, 0.0)

   def test_summarize_latencies():
       assert summarize_latencies([1.0, 3.0, 2.0]) == {
           "n": 3, "min_s": 1.0, "median_s": 2.0, "max_s": 3.0}

   def test_summarize_latencies_empty():
       with pytest.raises(ValueError):
           summarize_latencies([])
   ```
2. Run it, expect FAIL (module does not exist):
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_localstack_metrics.py -q`
   - Expected: `ModuleNotFoundError: No module named 'diagnostics.localstack'` (collection error).
3. Create `diagnostics/localstack/__init__.py` (empty file).
4. Create `diagnostics/localstack/metrics.py`:
   ```python
   #!/usr/bin/env python3
   """diagnostics/localstack/metrics.py — pure measurement helpers for the
   local-model benchmark gates (G1/G3/G5). No I/O, no hardware; unit-tested in
   Orchestrator/tests/test_localstack_metrics.py. The live probes import these."""
   from __future__ import annotations
   import struct
   from statistics import median


   def parse_nvidia_smi_used_mib(text: str) -> int:
       """First GPU line of `nvidia-smi --query-gpu=memory.used
       --format=csv,noheader,nounits` -> used MiB. Raises ValueError if no
       numeric line is present."""
       for line in text.splitlines():
           line = line.strip()
           if not line:
               continue
           return int(line.split(",")[0].strip())
       raise ValueError(f"no GPU memory line in: {text!r}")


   def wav_duration_seconds(wav: bytes) -> float:
       """Duration of a PCM WAV from its RIFF header = data_bytes / byte_rate.
       Raises ValueError on a non-PCM-WAV blob. Used for TTS RTF."""
       if len(wav) < 44 or wav[0:4] != b"RIFF" or wav[8:12] != b"WAVE":
           raise ValueError("not a RIFF/WAVE blob")
       pos, byte_rate, data_size = 12, None, None
       while pos + 8 <= len(wav):
           cid = wav[pos:pos + 4]
           (csize,) = struct.unpack_from("<I", wav, pos + 4)
           body = pos + 8
           if cid == b"fmt ":
               _fmt, _ch, _sr, byte_rate, _ba, _bits = struct.unpack_from(
                   "<HHIIHH", wav, body)
           elif cid == b"data":
               data_size = csize
               break
           pos = body + csize + (csize & 1)  # chunks are word-aligned
       if not byte_rate or data_size is None:
           raise ValueError("missing fmt/data chunk")
       return data_size / float(byte_rate)


   def rtf(wall_seconds: float, audio_seconds: float) -> float:
       """Real-time factor: <1.0 = faster than real time."""
       if audio_seconds <= 0:
           raise ValueError("audio_seconds must be > 0")
       return wall_seconds / audio_seconds


   def summarize_latencies(samples) -> dict:
       """min/median/max over a non-empty list of latency seconds."""
       s = list(samples)
       if not s:
           raise ValueError("no samples")
       return {"n": len(s), "min_s": round(min(s), 3),
               "median_s": round(median(s), 3), "max_s": round(max(s), 3)}
   ```
5. Run the test, expect PASS:
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_localstack_metrics.py -q`
   - Expected: `10 passed`.
6. Create `diagnostics/localstack/vram.py` (live sampler — no unit test; hardware probe):
   ```python
   #!/usr/bin/env python3
   """diagnostics/localstack/vram.py — sample nvidia-smi memory.used while a
   command runs and report PEAK used-MiB. Used by G1 (re-embed batch peak),
   G3 (TTS variant-transition peak), G5 (swap peak). Run on MS02 only."""
   from __future__ import annotations
   import argparse, json, subprocess, sys, threading, time
   from pathlib import Path

   REPO = Path(__file__).resolve().parents[2]
   sys.path.insert(0, str(REPO))
   from diagnostics.localstack.metrics import parse_nvidia_smi_used_mib  # noqa: E402

   BUDGET_MIB = 16380  # RTX 2000 Ada


   def sample_used_mib(gpu: int) -> int:
       out = subprocess.check_output(
           ["nvidia-smi", f"--id={gpu}", "--query-gpu=memory.used",
            "--format=csv,noheader,nounits"], text=True, timeout=10)
       return parse_nvidia_smi_used_mib(out)


   def main(argv=None):
       ap = argparse.ArgumentParser()
       ap.add_argument("--gpu", type=int, default=0)
       ap.add_argument("--interval", type=float, default=0.5)
       ap.add_argument("--duration", type=float, default=10.0,
                       help="seconds to sample when no command is given")
       ap.add_argument("--label", default="")
       ap.add_argument("--out", default=None)
       ap.add_argument("cmd", nargs=argparse.REMAINDER,
                       help="-- <command> to run while sampling (optional)")
       args = ap.parse_args(argv)

       samples, stop = [], threading.Event()

       def _loop():
           while not stop.is_set():
               try:
                   samples.append(sample_used_mib(args.gpu))
               except Exception as e:  # noqa: BLE001
                   print(f"[vram] sample error: {e}", file=sys.stderr)
               stop.wait(args.interval)

       baseline = sample_used_mib(args.gpu)
       t = threading.Thread(target=_loop, daemon=True)
       t.start()
       t0 = time.time()
       cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
       rc = subprocess.call(cmd) if cmd else (time.sleep(args.duration) or 0)
       stop.set()
       t.join(timeout=2)

       peak = max(samples) if samples else baseline
       summary = {"label": args.label, "gpu": args.gpu,
                  "baseline_mib": baseline, "peak_mib": peak,
                  "delta_mib": peak - baseline, "n_samples": len(samples),
                  "elapsed_s": round(time.time() - t0, 2),
                  "budget_mib": BUDGET_MIB, "headroom_mib": BUDGET_MIB - peak,
                  "fits_budget": peak < BUDGET_MIB, "command_rc": rc}
       print(json.dumps(summary, indent=2))
       if args.out:
           Path(args.out).parent.mkdir(parents=True, exist_ok=True)
           Path(args.out).write_text(json.dumps(summary, indent=2))
       return 0 if summary["fits_budget"] else 2


   if __name__ == "__main__":
       sys.exit(main())
   ```
7. Create `Scripts/preflight-vram-idle.sh`:
   ```bash
   #!/usr/bin/env bash
   # preflight-vram-idle.sh — assert the GPU is near-idle BEFORE the retrieval
   # group is first activated (i.e. before the wizard re-embed). Guards against a
   # stale pinned Ollama 8B (~7GB) + retrieval group (~11.5-13GB) > 16,380 MiB OOM
   # (§10 Step-0). Exits non-zero if used VRAM exceeds the threshold.
   set -euo pipefail
   THRESHOLD_MIB="${1:-1500}"   # near-idle ceiling; desktop/compositor headroom
   USED="$(nvidia-smi --id=0 --query-gpu=memory.used --format=csv,noheader,nounits | tr -d ' ')"
   echo "[preflight] GPU0 used = ${USED} MiB (threshold ${THRESHOLD_MIB} MiB)"
   if [ "${USED}" -gt "${THRESHOLD_MIB}" ]; then
     echo "[preflight] FAIL: GPU not idle — retire the pinned pair first" >&2
     echo "  systemctl disable --now vllm-reranker.service" >&2
     echo "  ollama stop qwen3-embedding:8b   # or: systemctl stop ollama" >&2
     nvidia-smi >&2
     exit 1
   fi
   echo "[preflight] OK — GPU near-idle; safe to activate the retrieval group"
   ```
8. Make the shell script executable and byte-check the sampler imports:
   - Run: `chmod +x Scripts/preflight-vram-idle.sh && Orchestrator/venv/bin/python -c "import diagnostics.localstack.vram; print('ok')"`
   - Expected: `ok`.
9. Commit (explicit paths only):
   - Run: `git add diagnostics/localstack/__init__.py diagnostics/localstack/metrics.py diagnostics/localstack/vram.py Scripts/preflight-vram-idle.sh Orchestrator/tests/test_localstack_metrics.py && git commit -m "test(localstack): gate metrics helpers + VRAM sampler + idle preflight"`
   - Expected: one commit, 5 files changed.

---

### Task 10.2: G3 harness — Qwen3-TTS RTF + first-packet latency probe

Author the G3 live probe (RTF, streaming first-packet, runtime sample-rate readback). Dev-box authoring; runs on MS02 in Task 10.10.

**Files:**
- Create: `diagnostics/localstack/tts_rtf.py`

1. Create `diagnostics/localstack/tts_rtf.py`:
   ```python
   #!/usr/bin/env python3
   """diagnostics/localstack/tts_rtf.py — G3 live probe (MS02). Measures, against
   the on-box qwen-tts member through the llama-swap front door
   (127.0.0.1:9098/v1/audio/speech):
     * RTF per voice = wall / audio_seconds (metrics.wav_duration_seconds)
     * streaming first-packet latency (stream=true; time to first audio byte)
     * output sample-rate READ FROM the returned WAV (never hardcode 24k; §5.4)
   Variant-transition PEAK VRAM is a separate step (vram.py wrapping a
   CustomVoice->Base->VoiceDesign sweep). The <0.9 RTF result INFORMS the
   streaming-default decision (§7); it is not a hard pass/fail — exit 0 always."""
   from __future__ import annotations
   import argparse, json, struct, sys, time
   from pathlib import Path
   import requests

   REPO = Path(__file__).resolve().parents[2]
   sys.path.insert(0, str(REPO))
   from diagnostics.localstack.metrics import wav_duration_seconds, rtf  # noqa: E402

   BASE = "http://127.0.0.1:9098/v1"
   # model="qwen-tts" routes to the member; the VARIANT is inferred from the
   # voice (preset -> CustomVoice hot path; clone/design slugs -> Base/VoiceDesign).
   CASES = [("qwen-tts", "Vivian", "customvoice-Vivian"),
            ("qwen-tts", "Dylan", "customvoice-Dylan")]


   def measure_batch(model, voice, text):
       t0 = time.time()
       r = requests.post(f"{BASE}/audio/speech", timeout=300, json={
           "model": model, "input": text, "voice": voice,
           "response_format": "wav", "stream": False})
       r.raise_for_status()
       wall, wav = time.time() - t0, r.content
       audio_s = wav_duration_seconds(wav)
       sr = struct.unpack_from("<I", wav, 24)[0]  # RIFF fmt sample_rate
       return {"wall_s": round(wall, 3), "audio_s": round(audio_s, 3),
               "rtf": round(rtf(wall, audio_s), 3), "sample_rate": sr,
               "bytes": len(wav)}


   def measure_first_packet(model, voice, text):
       t0 = time.time()
       with requests.post(f"{BASE}/audio/speech", stream=True, timeout=300, json={
               "model": model, "input": text, "voice": voice,
               "response_format": "wav", "stream": True}) as r:
           r.raise_for_status()
           for chunk in r.iter_content(chunk_size=1024):
               if chunk:
                   return round(time.time() - t0, 3)
       return None


   def main(argv=None):
       ap = argparse.ArgumentParser()
       ap.add_argument("--out", default=None)
       ap.add_argument("--gate-rtf", type=float, default=0.9)
       ap.add_argument("--text", default="The quick brown fox jumps over the "
                       "lazy dog near the riverbank at dawn.")
       args = ap.parse_args(argv)
       results = []
       for model, voice, label in CASES:
           row = measure_batch(model, voice, args.text)
           row.update({"label": label, "model": model, "voice": voice,
                       "first_packet_s": measure_first_packet(model, voice, args.text)})
           print(json.dumps(row))
           results.append(row)
       worst = max(r["rtf"] for r in results)
       summary = {"gate": "G3", "gate_rtf": args.gate_rtf, "worst_rtf": worst,
                  "streams_faster_than_realtime": worst < args.gate_rtf,
                  "recommend_streaming_variant":
                      "1.7B" if worst < args.gate_rtf else "0.6B-CustomVoice",
                  "cases": results}
       print(json.dumps(summary, indent=2))
       if args.out:
           Path(args.out).parent.mkdir(parents=True, exist_ok=True)
           Path(args.out).write_text(json.dumps(summary, indent=2))
       return 0


   if __name__ == "__main__":
       sys.exit(main())
   ```
2. Byte-check it imports (no live call):
   - Run: `Orchestrator/venv/bin/python -c "import diagnostics.localstack.tts_rtf; print('ok')"`
   - Expected: `ok`.
3. Commit:
   - Run: `git add diagnostics/localstack/tts_rtf.py && git commit -m "feat(localstack): G3 Qwen3-TTS RTF + first-packet probe"`
   - Expected: one commit, 1 file.

---

### Task 10.3: G4 harness — on-box vs gemma-box streaming-STT parity probe

**Files:**
- Create: `diagnostics/localstack/stt_parity.py`

1. Create `diagnostics/localstack/stt_parity.py`:
   ```python
   #!/usr/bin/env python3
   """diagnostics/localstack/stt_parity.py — G4 live probe (MS02). Streams the
   same 24kHz reference clip through /ws/stt for one provider and records
   first-partial latency, final-transcript latency, and the transcript. Run once
   per provider and diff the two JSONs:
     --provider onbox : on-box Speaches (:9098, the new localstack STT)
     --provider local : the gemma-box custom-server Speaches (today's path)
   The full Portal + Android mic-flow parity is a manual device step (house rule).
   Reference clip must be 24kHz / 16-bit / mono PCM WAV (see the G4 runbook)."""
   from __future__ import annotations
   import argparse, asyncio, json, sys, time, wave
   from pathlib import Path
   import websockets

   WS_URL = "ws://127.0.0.1:9091/ws/stt"


   def pcm_frames(wav_path, frame_ms=100):
       with wave.open(str(wav_path), "rb") as w:
           assert w.getframerate() == 24000 and w.getsampwidth() == 2 \
               and w.getnchannels() == 1, "clip must be 24kHz/16-bit/mono PCM"
           n = int(w.getframerate() * frame_ms / 1000)
           frames = []
           while True:
               data = w.readframes(n)
               if not data:
                   break
               frames.append(data)
           return frames


   async def run(provider, wav_path):
       frames = pcm_frames(wav_path)
       first_partial = final_at = None
       finals, got_done = [], False
       async with websockets.connect(WS_URL, max_size=None) as ws:
           await ws.send(json.dumps({"type": "start", "provider": provider,
                                     "sample_rate": 24000}))
           t0 = time.time()

           async def reader():
               nonlocal first_partial, final_at, got_done
               async for msg in ws:
                   ev = json.loads(msg)
                   t = ev.get("type")
                   if t in ("partial", "delta") and first_partial is None:
                       first_partial = time.time() - t0
                   elif t in ("final", "transcript"):
                       finals.append(ev.get("text", ""))
                       final_at = time.time() - t0
                   elif t == "stt_done":
                       got_done = True
                       return

           rtask = asyncio.create_task(reader())
           for fr in frames:
               await ws.send(fr)
               await asyncio.sleep(0.1)  # ~real-time pacing
           await ws.send(json.dumps({"type": "stop"}))
           await asyncio.wait_for(rtask, timeout=30)
       return {"provider": provider,
               "first_partial_s": round(first_partial, 3) if first_partial else None,
               "final_s": round(final_at, 3) if final_at else None,
               "transcript": " ".join(finals).strip(), "stt_done": got_done}


   def main(argv=None):
       ap = argparse.ArgumentParser()
       ap.add_argument("--provider", required=True, choices=["onbox", "local"])
       ap.add_argument("--wav", required=True)
       ap.add_argument("--out", default=None)
       args = ap.parse_args(argv)
       out = asyncio.run(run(args.provider, args.wav))
       print(json.dumps(out, indent=2))
       if args.out:
           Path(args.out).parent.mkdir(parents=True, exist_ok=True)
           Path(args.out).write_text(json.dumps(out, indent=2))
       return 0


   if __name__ == "__main__":
       sys.exit(main())
   ```
2. Byte-check import:
   - Run: `Orchestrator/venv/bin/python -c "import diagnostics.localstack.stt_parity; print('ok')"`
   - Expected: `ok`.
3. Commit:
   - Run: `git add diagnostics/localstack/stt_parity.py && git commit -m "feat(localstack): G4 STT streaming-parity probe"`
   - Expected: one commit, 1 file.

---

### Task 10.4: G5 harness — cross-group swap-cost probe

**Files:**
- Create: `diagnostics/localstack/swap_cost.py`

1. Create `diagnostics/localstack/swap_cost.py`:
   ```python
   #!/usr/bin/env python3
   """diagnostics/localstack/swap_cost.py — G5 live probe (MS02). Times the
   cross-group first-interaction stall in BOTH directions by alternating a
   retrieval-group request (embed) and an audio-group request (TTS) through the
   llama-swap front door, forcing an exclusive evict+load each turn:
     audio->retrieval : first embed after a TTS = evict(audio)+load(embed-8b+rerank)
                        — expect ~6-10s (§5.2/D9)
     retrieval->audio : first TTS after an embed = evict(retrieval)+load(speaches+qwen-tts)
                        — expect ~5-8s
   Run with --cache warm, then again with --cache cold after the caller drops the
   page cache: sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'."""
   from __future__ import annotations
   import argparse, json, sys, time
   from pathlib import Path
   import requests

   REPO = Path(__file__).resolve().parents[2]
   sys.path.insert(0, str(REPO))
   from diagnostics.localstack.metrics import summarize_latencies  # noqa: E402

   BASE = "http://127.0.0.1:9098/v1"


   def one_embed():
       t0 = time.time()
       requests.post(f"{BASE}/embeddings", timeout=120, json={
           "model": "embed-qwen3-8b", "input": "cross-group swap probe"}
       ).raise_for_status()
       return time.time() - t0


   def one_tts():
       t0 = time.time()
       requests.post(f"{BASE}/audio/speech", timeout=120, json={
           "model": "qwen-tts", "input": "swap probe", "voice": "Vivian",
           "response_format": "wav"}).raise_for_status()
       return time.time() - t0


   def main(argv=None):
       ap = argparse.ArgumentParser()
       ap.add_argument("--iters", type=int, default=5)
       ap.add_argument("--cache", choices=["warm", "cold"], default="warm")
       ap.add_argument("--out", default=None)
       args = ap.parse_args(argv)
       one_tts()  # prime: land in the audio group so the first embed swaps
       a2r, r2a = [], []
       for _ in range(args.iters):
           a2r.append(one_embed())
           r2a.append(one_tts())
       summary = {"gate": "G5", "cache": args.cache, "iters": args.iters,
                  "audio_to_retrieval_s": summarize_latencies(a2r),
                  "retrieval_to_audio_s": summarize_latencies(r2a)}
       print(json.dumps(summary, indent=2))
       if args.out:
           Path(args.out).parent.mkdir(parents=True, exist_ok=True)
           Path(args.out).write_text(json.dumps(summary, indent=2))
       return 0


   if __name__ == "__main__":
       sys.exit(main())
   ```
2. Byte-check import:
   - Run: `Orchestrator/venv/bin/python -c "import diagnostics.localstack.swap_cost; print('ok')"`
   - Expected: `ok`.
3. Commit:
   - Run: `git add diagnostics/localstack/swap_cost.py && git commit -m "feat(localstack): G5 cross-group swap-cost probe"`
   - Expected: one commit, 1 file.

---

### Task 10.5: G6 harness — streaming-STT eviction-safety probe

Fires a retrieval/embedding request *while* a local voice stream is mid-utterance and asserts zero audio cut-off — the direct test of the Orchestrator serialization (D12).

**Files:**
- Create: `diagnostics/localstack/stt_evict_safety.py`

1. Create `diagnostics/localstack/stt_evict_safety.py`:
   ```python
   #!/usr/bin/env python3
   """diagnostics/localstack/stt_evict_safety.py — G6 live probe (MS02). Fires a
   retrieval/embedding request WHILE a local streaming-STT utterance is in flight
   and asserts ZERO audio cut-off. The on-box Speaches stream ships as Design B
   (direct-to-member WS, invisible to llama-swap's in-flight drain counter), so it
   is protected by ORCHESTRATOR SERIALIZATION (§5.3/D12): a retrieval request
   arriving mid-utterance must be HELD until the finalize gap, never force-swap the
   audio group out from under the open stream.

   Method: open /ws/stt (provider=onbox), stream a 24kHz reference clip as PCM; at
   the midpoint fire an embed at :9098 in a background thread (non-blocking); assert
   (a) no partial-arrival gap > GAP_S, (b) stt_done terminal, (c) transcript matches
   a control run with no injection. PASS iff the injected run's max gap is within
   tolerance of the control's."""
   from __future__ import annotations
   import argparse, asyncio, json, sys, threading, time, wave
   from pathlib import Path
   import requests, websockets

   WS_URL = "ws://127.0.0.1:9091/ws/stt"
   EMBED_URL = "http://127.0.0.1:9098/v1/embeddings"


   def pcm_frames(wav_path, frame_ms=100):
       with wave.open(str(wav_path), "rb") as w:
           assert w.getframerate() == 24000 and w.getsampwidth() == 2 \
               and w.getnchannels() == 1, "clip must be 24kHz/16-bit/mono PCM"
           n = int(w.getframerate() * frame_ms / 1000)
           frames = []
           while True:
               data = w.readframes(n)
               if not data:
                   break
               frames.append(data)
           return frames


   def _fire_embed():
       try:
           requests.post(EMBED_URL, timeout=120, json={
               "model": "embed-qwen3-8b",
               "input": "G6 mid-utterance retrieval probe"})
       except Exception as e:  # noqa: BLE001
           print(f"[g6] embed fire error (expected if held/slow): {e}",
                 file=sys.stderr)


   async def run(wav_path, inject):
       frames = pcm_frames(wav_path)
       mid = len(frames) // 2
       partial_times, finals, got_done, fired = [], [], False, False
       async with websockets.connect(WS_URL, max_size=None) as ws:
           await ws.send(json.dumps({"type": "start", "provider": "onbox",
                                     "sample_rate": 24000}))

           async def reader():
               nonlocal got_done
               async for msg in ws:
                   ev = json.loads(msg)
                   t = ev.get("type")
                   if t in ("partial", "delta"):
                       partial_times.append(time.time())
                   elif t in ("final", "transcript"):
                       finals.append(ev.get("text", ""))
                   elif t == "stt_done":
                       got_done = True
                       return

           rtask = asyncio.create_task(reader())
           for i, fr in enumerate(frames):
               await ws.send(fr)
               await asyncio.sleep(0.1)
               if inject and i == mid and not fired:
                   fired = True
                   threading.Thread(target=_fire_embed, daemon=True).start()
           await ws.send(json.dumps({"type": "stop"}))
           await asyncio.wait_for(rtask, timeout=30)
       gaps = [b - a for a, b in zip(partial_times, partial_times[1:])]
       return {"inject": inject, "n_partials": len(partial_times),
               "max_partial_gap_s": round(max(gaps), 3) if gaps else None,
               "transcript": " ".join(finals).strip(), "stt_done": got_done}


   def main(argv=None):
       ap = argparse.ArgumentParser()
       ap.add_argument("--wav", required=True)
       ap.add_argument("--gap-tolerance-s", type=float, default=1.0,
                       help="allowed extra partial-gap vs the control run")
       ap.add_argument("--out", default=None)
       args = ap.parse_args(argv)
       control = asyncio.run(run(args.wav, inject=False))
       injected = asyncio.run(run(args.wav, inject=True))
       ctl_gap = control["max_partial_gap_s"] or 0.0
       inj_gap = injected["max_partial_gap_s"] or 0.0
       cut_off = (inj_gap - ctl_gap) > args.gap_tolerance_s
       ok = injected["stt_done"] and not cut_off
       summary = {"gate": "G6", "control": control, "injected": injected,
                  "extra_gap_s": round(inj_gap - ctl_gap, 3),
                  "gap_tolerance_s": args.gap_tolerance_s,
                  "audio_cut_off": cut_off, "pass": ok}
       print(json.dumps(summary, indent=2))
       if args.out:
           Path(args.out).parent.mkdir(parents=True, exist_ok=True)
           Path(args.out).write_text(json.dumps(summary, indent=2))
       return 0 if ok else 2


   if __name__ == "__main__":
       sys.exit(main())
   ```
2. Byte-check import:
   - Run: `Orchestrator/venv/bin/python -c "import diagnostics.localstack.stt_evict_safety; print('ok')"`
   - Expected: `ok`.
3. Run the full metrics unit suite once more (guards the shared helpers before hand-off):
   - Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_localstack_metrics.py -q`
   - Expected: `10 passed`.
4. Commit:
   - Run: `git add diagnostics/localstack/stt_evict_safety.py && git commit -m "feat(localstack): G6 streaming-STT eviction-safety probe"`
   - Expected: one commit, 1 file.
5. Push the harness suite so MS02's deploy pull picks it up:
   - Run: `git push origin main`
   - Expected: `main -> main` (the six harness commits land on origin).

---

### Task 10.6: MS02 pre-flight — reconcile git, retire the pinned pair, assert VRAM idle

Non-destructive prep. Frees the GPU so the retrieval group can later load, and cleans MS02's stray tree so the deploy is a clean fast-forward. **No snapshots are wiped here** (that is Task 10.11).

**Files:** none (runbook; operates on MS02's working tree + services).

1. Open the session and confirm MS02's identity + starting SHA:
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && git rev-parse --short HEAD && git status --porcelain'`
   - Expected: `b8167dd` (or later) and two dirty entries — ` M ToolVault/embeddings.json` and `?? config.ini.bak-pre-rerank`.
2. Reconcile the stray tree (NEVER commit `embeddings.json` — house rule; the `.bak` is an untracked stray):
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && git checkout -- ToolVault/embeddings.json && rm -f config.ini.bak-pre-rerank && git status --porcelain'`
   - Expected: empty output (clean tree; `config.ini`/`.env`/`credentials/` are gitignored and untouched).
3. Capture the baseline VRAM footprint (the pinned pair should show ~10GB):
   - Run: `ssh bbx@192.168.1.153 'nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits && systemctl is-active vllm-reranker.service && ollama ps'`
   - Expected: ~`10278` MiB used; `active`; an `ollama ps` row for `qwen3-embedding:8b` resident (~7GB).
4. Retire the vLLM reranker (interactive sudo; Brandon has root on MS02):
   - Run: `ssh -t bbx@192.168.1.153 'sudo systemctl disable --now vllm-reranker.service'`
   - Expected: `Removed .../vllm-reranker.service` and the unit stops.
5. Explicitly UNLOAD the pinned Ollama 8B — a pointer flip alone leaves it resident (§10):
   - Run: `ssh bbx@192.168.1.153 'ollama stop qwen3-embedding:8b && sleep 3 && ollama ps'`
   - Expected: `ollama ps` shows no resident model (empty table).
6. Assert the GPU is near-idle (the precondition for first retrieval-group activation):
   - Run: `ssh bbx@192.168.1.153 'nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits'`
   - Expected: a value under ~1500 MiB (near-idle; only compositor/desktop residue). If not, repeat steps 4–5 and inspect `nvidia-smi`.

---

### Task 10.7: Deploy the stack in-place — git pull main + install.sh re-run + wizard weight downloads

Exercises the real customer update-in-place journey. **Nothing activates on install** (§8) — this only lays down `blackbox-models.service`, the binaries, and the wizard step; the four capabilities stay on their current providers until the deliberate cutover (Task 10.12).

**Files:** none (runbook; runs `Scripts/install.sh` + wizard on MS02).

1. Fast-forward MS02 to `origin/main` (config/secrets are gitignored → survive):
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && git fetch origin main && git reset --hard origin/main && git rev-parse --short HEAD'`
   - Expected: HEAD now matches origin/main's tip (the Milestone 1–10 merge, incl. the six harnesses from Tasks 10.1–10.5).
2. Re-run the installer (idempotent; Step 2f is self-gating and picks up the localstack templates):
   - Run: `ssh -t bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && sudo bash Scripts/install.sh'`
   - Expected: install completes; Step 2f logs `llama-swap` + `llama-server` binary install, `xvfb`/`websockify`/`novnc` apt install, and `blackbox-models.service` written to `/etc/systemd/system/`. No weights downloaded (deferred to the wizard).
3. Restart the Orchestrator (pre-authorized) and confirm the models service is present but idle:
   - Run: `ssh -t bbx@192.168.1.153 'sudo systemctl restart blackbox.service && sleep 90 && systemctl is-enabled blackbox-models.service && curl -fsS http://127.0.0.1:9091/local-models/status | python3 -m json.tool'`
   - Expected: `blackbox.service` healthy after warm-up; `blackbox-models.service` enabled; `/local-models/status` returns tier `HIGH`, disk headroom, and every capability's download state `missing` (weights not yet pulled), routing still the pre-cutover providers (embeddings=gemini cloud, rerank was vLLM→now none, STT/TTS=cloud/gemma-box).
4. Pull the GPU-tier weights through the wizard endpoint (streamed NDJSON progress), gated on ≥40GB free. **The endpoint takes ONE `{"artifact": ...}` per call** (Task 2.7) — loop over the two endpoint-downloadable artifacts. The other members are provisioned out-of-band and are NOT `/local-models/download` targets: **whisper** auto-pulls in the Speaches member on first transcription; the **reranker GGUF** is self-converted from a pinned llama.cpp build (Task 4.4 / §5.2); **embed-qwen3-0.6b** is the CPU-tier fallback (not needed on this GPU box):
   - Run: `ssh bbx@192.168.1.153 'for A in embed-qwen3-8b qwen-tts; do echo "== $A =="; curl -N -X POST http://127.0.0.1:9091/local-models/download -H "Content-Type: application/json" -d "{\"artifact\":\"$A\"}"; done'`
   - Expected: for each artifact, NDJSON progress lines ending in a terminal `{"state":"done"}` line — `embed-qwen3-8b` (~8.1GB single GGUF) and `qwen-tts` (~13.5GB, the three variant checkpoints via HF snapshot). ~21.6GB pulled through the endpoint; whisper (~1.6–2.5GB Speaches auto-pull) + the self-converted reranker bring the on-disk total toward §14's ~27.5GB. No activation occurs.
5. Confirm the endpoint-downloaded members show `done` (per M1's real status shape: `models` is a LIST, each item carries a `download` dict — there is no `capabilities` key):
   - Run: `ssh bbx@192.168.1.153 'curl -fsS http://127.0.0.1:9091/local-models/status | python3 -c "import sys,json; d=json.load(sys.stdin); print({m[\"model\"]: (m[\"download\"] or {}).get(\"state\") for m in d[\"models\"]})"'`
   - Expected: `embed-qwen3-8b` and `qwen-tts` show `done`; `speaches`/`rerank-qwen3-0.6b` may show `pending` (provisioned out-of-band, not via this endpoint). `/local-models/status` still reports the retrieval + audio groups **not resident** (llama-swap starts idle — correct).

---

### Task 10.8: G1 — embedding quality (8B-Q8 vs gemini) + real VRAM + threshold recalibration

**Runs on the still-present transplant corpus, BEFORE Step-0.** Builds the 8B-Q8 candidate store on MS02's GPU, benches it against the incumbent gemini baseline through the existing chunk-gate harness, measures the real Q8_0 steady-state + non-causal-compute-buffer peak VRAM, recalibrates the on-box slug thresholds, and writes the authoritative `chunk-gate` artifact.

> **G1 4B arm DROPPED (design D5's "vs 4B-FP16" comparison):** no `qwen3-embedding-4b-local` slug is registered anywhere in this plan (M3 registers only `-8b-local`/`-0.6b-local`; the DOWNLOAD_MANIFEST has no `embed-qwen3-4b`; there is no 4B llama-swap member), so the 4B arm was unbuildable. 8B-Q8 (the locked D5 default) is gated against the **gemini incumbent baseline** instead — the meaningful bar for "wholesale-local without a quality regression." If a 4B comparison is ever wanted, register the slug + manifest artifact + member first (M1–M3), then re-add the arm.

**Files:**
- Modify: `Orchestrator/embeddings/registry.py` (the `qwen3-embedding-8b-local` entry added in M4 — recalibrate `semantic_threshold` + `junk_floor` from G1)
- Results: `eval/results/$DATE-chunk-gate.{md,json}`, `eval/results/$DATE-g1-vram.json`

1. Build the 8B-Q8 candidate store from the transplant corpus (activates it; that is fine — Step-0 wipes it anyway, and the gate reads the store dir directly):
   - Run: `ssh bbx@192.168.1.153 'curl -fsS -X POST http://127.0.0.1:9091/embeddings/reembed -H "Content-Type: application/json" -d "{\"target\":\"qwen3-embedding-8b-local\"}"'`
   - Expected: `{"status":"started",...}`; poll `GET /embeddings/status` until `migration_state` shows `state: "done"`. This is the "giant re-embed" path — fast on the GPU-served 8B.
2. Measure the real Q8_0 VRAM (steady-state + heavy-batch peak incl. the ub=8192 non-causal compute buffer). Sample while firing a batch of ~8k-token strings straight at the front door:
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && Orchestrator/venv/bin/python diagnostics/localstack/vram.py --label g1-embed-peak --out eval/results/'"$DATE"'-g1-vram.json -- Orchestrator/venv/bin/python -c "import requests; big=(\"lorem ipsum \"*3000); [requests.post(\"http://127.0.0.1:9098/v1/embeddings\", json={\"model\":\"embed-qwen3-8b\",\"input\":[big]*8}, timeout=300) for _ in range(6)]"'`
   - Expected: JSON with `peak_mib` (the retrieval group loaded: embed-8b + rerank-0.6b) and `fits_budget: true`, `headroom_mib` positive (§4 budgets ~11.5–13GB → ~3GB headroom). If `fits_budget: false`, STOP — Q8_0 does not fit; escalate (the design's fallback is a smaller resident embedder).
3. Bench the 8B-Q8 candidate against the live gemini + qwen06 arms (comparison report):
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && Orchestrator/venv/bin/python eval/run_bench.py --candidate-dir Manifest/embeddings --candidate-slug qwen3-embedding-8b-local --out-date '"$DATE"'-g1-8b'`
   - Expected: a report table with arms `gemini2-hybrid`, `gemini2-semantic`, `qwen06-semantic`, and the candidate `qwen3-embedding-8b-local` (hybrid + semantic); written to `eval/results/$DATE-g1-8b.{md,json}`.
4. Compare (the G1 quality criterion): 8B-Q8 nDCG@10 / recall within the agreed delta of `gemini2-hybrid` (the incumbent). Read the report `.md` table.
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && grep -A6 "qwen3-embedding-8b-local" eval/results/'"$DATE"'-g1-8b.md'`
   - Expected: 8B-Q8 within the documented delta of gemini on the covered-only metrics. If it regresses beyond the delta, STOP and reconsider the D5 default before cutover.
5. Run the authoritative six-gate chunk-gate on the 8B-Q8 candidate (the swap-authorization pass; exits non-zero on any gate failure):
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && Orchestrator/venv/bin/python eval/run_bench.py --gate --candidate-dir Manifest/embeddings --candidate-slug qwen3-embedding-8b-local --out-date '"$DATE"' ; echo EXIT=$?'`
   - Expected: writes `eval/results/$DATE-chunk-gate.{md,json}`, prints `ALL GATES PASS — cutover authorized`, `EXIT=0`. `EXIT=1` = a gate failed → STOP, do not cut over.
6. Recalibrate the on-box 8B slug thresholds from the measured score distributions (per-model calibration is mandatory). Run the noise probe against the freshly-built store, then edit `registry.py`:
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && Orchestrator/venv/bin/python eval/noise_probe.py --slug qwen3-embedding-8b-local --out eval/results/'"$DATE"'-g1-noise.json'`
   - Expected: a JSON with the recommended `semantic_threshold` / `junk_floor` for the 8B store (seeded from the Ollama-qwen entries, now recalibrated).
7. Edit `Orchestrator/embeddings/registry.py` — set the `qwen3-embedding-8b-local` entry's `semantic_threshold` and `junk_floor` to the recalibrated values from step 6 (replace the M4 placeholders). Do this on the dev box, commit, push, and re-pull on MS02 (do NOT hand-edit MS02's tree — keep it a clean checkout):
   - On dev box, apply the two-field edit, then:
   - Run: `git add Orchestrator/embeddings/registry.py && git commit -m "feat(localstack): recalibrate qwen3-embedding-8b-local thresholds from G1" && git push origin main`
   - Expected: one commit, 1 file; pushed.
8. Land the G1 result artifacts in the repo (scp back from MS02 → commit on the dev box → push):
   - Run: `scp bbx@192.168.1.153:/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/eval/results/$DATE-chunk-gate.* bbx@192.168.1.153:/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/eval/results/$DATE-g1-*.json eval/results/`
   - Then: `git add eval/results/$DATE-chunk-gate.md eval/results/$DATE-chunk-gate.json eval/results/$DATE-g1-8b.md eval/results/$DATE-g1-8b.json eval/results/$DATE-g1-vram.json eval/results/$DATE-g1-noise.json && git commit -m "eval(localstack): G1 chunk-gate + VRAM + threshold results (MS02)"`
   - Expected: one commit with the six G1 artifacts.

---

### Task 10.9: G2 — reranker GGUF validity + latency

Runs the reranker validity harness delivered by Milestone 4 as a gate. **The harness itself is M4's deliverable** (score-validity vs HF reference + 40-passage latency); this task only runs it, asserts the pass criteria, and records the result. If M4 named the harness differently, substitute M4's path here — the contract (below) is what gates.

**Files:**
- Results: `eval/results/$DATE-g2-rerank.json`

1. Confirm the self-converted reranker GGUF is the one llama-swap serves (no community-broken `cls.output.weight`):
   - Run: `ssh bbx@192.168.1.153 'curl -fsS -X POST http://127.0.0.1:9098/v1/rerank -H "Content-Type: application/json" -d "{\"model\":\"rerank-qwen3-0.6b\",\"query\":\"What is the capital of France?\",\"documents\":[\"Paris is the capital of France.\",\"Bananas are yellow.\"]}"'`
   - Expected: `{"results":[{"index":0,"relevance_score":<high>},{"index":1,"relevance_score":<low>}]}` — the relevant doc scores clearly above the decoy, and scores are NOT ~1e-28 (the broken-conversion signature). If scores collapse, STOP — the GGUF conversion is broken (§5.2 trap).
2. Run the M4 G2 harness (score validity on golden pairs + 40-passage latency):
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && Orchestrator/venv/bin/python diagnostics/localstack/rerank_validity.py --slug qwen3-reranker-0.6b-local --out eval/results/'"$DATE"'-g2-rerank.json ; echo EXIT=$?'`
   - Expected: JSON with per-golden-pair score correlation vs the HF reference (Spearman ≥ the M4 threshold, no collapse), and `rerank_40_latency_s` inside the ceiling (~1–2s at ~1000-token passages; ~0.5–1s at ~512 tokens). `EXIT=0`.
3. Flip the reranker selection to the on-box model (sidecar-driven, exactly as today) now that G2 passed:
   - Run: `ssh bbx@192.168.1.153 'curl -fsS -X POST http://127.0.0.1:9091/rerank/select -H "Content-Type: application/json" -d "{\"provider\":\"localstack\",\"model\":\"qwen3-reranker-0.6b-local\",\"enabled\":true}" && cat /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/Manifest/embeddings/rerank.json'`
   - Expected: `rerank.json` now `{"enabled": true, "provider": "localstack", "model": "qwen3-reranker-0.6b-local"}`.
4. Land the G2 artifact (scp back → commit on dev box → push):
   - Run: `scp bbx@192.168.1.153:/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/eval/results/$DATE-g2-rerank.json eval/results/ && git add eval/results/$DATE-g2-rerank.json && git commit -m "eval(localstack): G2 reranker validity + latency (MS02)"`
   - Expected: one commit, 1 file.

---

### Task 10.10: G3 — Qwen3-TTS RTF + first-packet + variant-transition peak VRAM

Runs the Task 10.2 probe on MS02 and measures the FREE-BEFORE-LOAD variant-transition peak. Confirms the §7 planning expectation (1.7B streaming near-certainly fails <0.9 RTF on the 2000 Ada → 0.6B-CustomVoice streaming default, 1.7B batch tier).

**Files:**
- Results: `eval/results/$DATE-g3-tts.json`, `eval/results/$DATE-g3-vram.json`

1. Create a consent-gated clone profile and a design profile (so the variant sweep can exercise Base + VoiceDesign, not just CustomVoice presets):
   - Run: `ssh bbx@192.168.1.153 'curl -fsS -X POST http://127.0.0.1:9098/v1/voices/clone -H "Content-Type: application/json" -d "{\"name\":\"g3-clone\",\"confirm_consent\":true,\"reference_audio_path\":\"/tmp/ref24k.wav\"}" ; curl -fsS -X POST http://127.0.0.1:9098/v1/voices/design -d "{\"name\":\"g3-design\",\"description\":\"a calm low-pitched narrator\"}" '`
   - Expected: both return a saved profile slug under `Manifest/voices/qwen/`. (Generate `/tmp/ref24k.wav` first — see Task 10.13 step 1 for the 24kHz-clip recipe.) A clone without `confirm_consent:true` must 422 (mirrors the ElevenLabs gate).
2. Measure RTF + streaming first-packet across the CustomVoice hot path:
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && Orchestrator/venv/bin/python diagnostics/localstack/tts_rtf.py --out eval/results/'"$DATE"'-g3-tts.json'`
   - Expected: JSON with per-voice `rtf`, `first_packet_s`, and `sample_rate` (read from the WAV — confirm the true rate, likely 24000, and adjust browser-playback if not). `recommend_streaming_variant` will read `0.6B-CustomVoice` if `worst_rtf ≥ 0.9` (the expected 2000-Ada outcome).
3. Measure the variant-transition PEAK VRAM (proves FREE-BEFORE-LOAD keeps the audio group under budget across CustomVoice→Base→VoiceDesign):
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && Orchestrator/venv/bin/python diagnostics/localstack/vram.py --label g3-variant-transition --out eval/results/'"$DATE"'-g3-vram.json -- bash -c "for V in Vivian g3-clone g3-design; do curl -s -X POST http://127.0.0.1:9098/v1/audio/speech -d \"{\\\"model\\\":\\\"qwen-tts\\\",\\\"input\\\":\\\"peak probe\\\",\\\"voice\\\":\\\"\$V\\\",\\\"response_format\\\":\\\"wav\\\"}\" -o /dev/null; done"'`
   - Expected: `fits_budget: true` — the mid-transition peak (old variant dropped → gc → empty_cache → new variant loaded, + resident whisper) stays under 16,380 MiB. If `false`, FREE-BEFORE-LOAD is not draining before the next allocate — file against the qwen-tts server milestone; do NOT enable multi-variant TTS until fixed.
4. Land the G3 artifacts:
   - Run: `scp bbx@192.168.1.153:/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/eval/results/$DATE-g3-*.json eval/results/ && git add eval/results/$DATE-g3-tts.json eval/results/$DATE-g3-vram.json && git commit -m "eval(localstack): G3 TTS RTF + variant-transition VRAM (MS02)"`
   - Expected: one commit, 2 files.

---

### Task 10.11: Step-0 — destructive snapshot-only reset (executable checklist)

**Runs only after G1/G2/G3 have their numbers** (they needed the transplant corpus). Wipes ONLY the transplanted snapshot ledger + embedding stores per D8; every other identity/config artifact stays. **DESTRUCTIVE — no undo for the snapshot ledger.** The authoritative wipe list below was enumerated from the live `Manifest/` on the dev box (the transplant source).

**Files:** none (runbook; destructive ops on MS02's data dirs).

1. Stop the Orchestrator (releases file handles on the stores/index):
   - Run: `ssh -t bbx@192.168.1.153 'sudo systemctl stop blackbox.service && systemctl is-active blackbox.service || true'`
   - Expected: `inactive`.
2. **Snapshot the exact wipe set** (dry-run listing before deleting — verify nothing unexpected):
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && ls -la Volume Fossils Manifest/manifest.json Manifest/snapshot_index.json* Manifest/embeddings 2>/dev/null | head -60'`
   - Expected: shows the transplant ledger + all embedding store dirs (`gemini-embedding-2/`, `qwen3-embedding-8b-local/`, the `.pre-*` backups, `_build*`, sidecars).
3. **WIPE — exactly these, nothing else** (KEEP list follows in step 4):
   - Run:
     ```
     ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && \
       rm -rf Volume/* Fossils/* && \
       rm -f  Manifest/manifest.json \
              Manifest/snapshot_index.json \
              Manifest/snapshot_index.json.bak.pre-embeddings-v2 && \
       rm -rf Manifest/embeddings && \
       mkdir -p Volume Fossils Manifest/embeddings && \
       echo WIPED'
     ```
   - Expected: `WIPED`. (`Volume/`, `Fossils/`, `Manifest/embeddings/` are recreated empty so the service starts clean.)
   - **The wipe set is exactly:** `Volume/*`, `Fossils/*`, `Manifest/manifest.json`, `Manifest/snapshot_index.json`, `Manifest/snapshot_index.json.bak.pre-embeddings-v2`, and the entire `Manifest/embeddings/` directory (all stores + `.pre-chunk`/`.pre-rebuild.*` backups + `_build*` + the sidecars `active.json`/`rerank.json`/`keep_alive.json`/`placement.json`/`migration_state.json`/`health.json`). The wizard cutover re-establishes `active.json`/`rerank.json` in Task 10.12.
4. **VERIFY the KEEP set survived** (per D8: operators, config, secrets, devices, custom servers, onboarding, uploads, apps, code-analysis artifacts, SMS/task DBs, schema version):
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && ls config.ini .env credentials/custom_models.json Manifest/operator_state.json Manifest/operator_preferences.json Manifest/apps_registry.json Manifest/schema_version.json Manifest/sms_messages.db Manifest/tasks.db Manifest/gmail_tokens Manifest/mcp_tokens.json 2>&1'`
   - Expected: every path lists (exists). If any is missing, STOP — the wipe over-reached; restore from the box (these were never in the wipe set).
5. Restart the Orchestrator and confirm an empty-but-healthy ledger:
   - Run: `ssh -t bbx@192.168.1.153 'sudo systemctl start blackbox.service && sleep 90 && curl -fsS http://127.0.0.1:9091/timeline | python3 -c "import sys,json; print(\"snapshots:\", len(json.load(sys.stdin).get(\"snapshots\", [])))"'`
   - Expected: `snapshots: 0` (fresh ledger; the box now mints its own history from zero). Operators still present (`curl /operators` or the Portal shows the retained roster).
6. Re-assert VRAM idle before the first retrieval-group activation (the wizard re-embed is imminent):
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && bash Scripts/preflight-vram-idle.sh'`
   - Expected: `[preflight] OK — GPU near-idle; safe to activate the retrieval group`.

---

### Task 10.12: Wizard cutover — activate the four capabilities on-box (fresh corpus)

Walk the new `local_models` wizard step on the existing install: the deliberate per-capability activation onto the empty-but-growing corpus. Embeddings cut over via re-embed (the sole writer of `active.json`); STT/TTS via the `[local_models]` precedence flags; rerank already flipped in G2.

**Files:** none (runbook; wizard + config on MS02).

1. Open the wizard `local_models` step and confirm the recommendations:
   - Run: `ssh bbx@192.168.1.153 'curl -fsS http://127.0.0.1:9091/local-models/status | python3 -m json.tool'`
   - Expected: tier HIGH; per-capability recommendation = on-box for all four; download states `ready`; groups idle.
2. Cut embeddings over to the on-box 8B (re-embeds the fresh corpus — tiny now, so instant; this is also the first retrieval-group activation, so it lazy-loads the group):
   - Run: `ssh bbx@192.168.1.153 'curl -fsS -X POST http://127.0.0.1:9091/embeddings/reembed -H "Content-Type: application/json" -d "{\"target\":\"qwen3-embedding-8b-local\"}"'`
   - Expected: started → `done` (empty/near-empty corpus; migrate.py's "empty corpus — nothing to activate" path is fine). `active.json` now `{"active":"qwen3-embedding-8b-local"}`.
3. **Cache-coherence** — bust the tool/code embedding caches keyed by the old active slug (else the first hot-path query embeds at 4096-dim while caches hold 3072-dim; §5.1):
   - Run: `ssh bbx@192.168.1.153 'curl -fsS -X POST http://127.0.0.1:9091/toolvault/reload && curl -fsS -X POST http://127.0.0.1:9091/embeddings/code/rebuild 2>/dev/null || echo "(code-embeddings rebuild endpoint per M-embeddings)"'`
   - Expected: `/toolvault/reload` re-embeds tool descriptions at 4096-dim; code-embeddings rebuilt. (NEVER commit `ToolVault/embeddings.json` — house rule.)
4. Flip STT + TTS precedence to on-box via the **real per-capability endpoint** `POST /local-models/capability` (Task 8.3 — one call per capability; it accepts ONLY `stt`/`tts`, returns 400 for embeddings/rerank). Embeddings already cut over in step 2 (the re-embed is the sole writer of `active.json`) and rerank in G2 (Task 10.9 → `rerank.json`) — neither uses a `[local_models]` seed flag. This seeds the wizard-default; an explicit credentialed user pick still wins at runtime (D2). config.ini is written on-box (gitignored):
   - Run: `ssh bbx@192.168.1.153 'for C in stt tts; do curl -fsS -X POST http://127.0.0.1:9091/local-models/capability -H "Content-Type: application/json" -d "{\"capability\":\"$C\",\"enabled\":true}"; done && grep -A6 "\[local_models\]" /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/config.ini'`
   - Expected: `[local_models]` shows `enabled = true` + `base_url = http://127.0.0.1:9098/v1` (set at install) plus `stt = true` and `tts = true` (just flipped). embeddings/rerank are NOT `[local_models]` flags — their on-box routing lives in `Manifest/embeddings/active.json` (step 2) and `Manifest/embeddings/rerank.json` (G2).
5. Restart and confirm on-box routing across all four capabilities. **M1's real status shape** (Task 1.5) is a top-level `routing` map keyed by capability → `{enabled, healthy, decision}` with `decision ∈ {"on-box","unhealthy","off"}` — there is **no `capabilities` key** and the value is `on-box`, not `onbox`/`localstack`. The `routing` block's decision keys on the `[local_models]` seed flag, so it reflects **stt/tts** directly; embeddings/rerank are activated by `active.json`/`rerank.json` and are verified from those:
   - Run: `ssh -t bbx@192.168.1.153 'sudo systemctl restart blackbox.service && sleep 90 && curl -fsS http://127.0.0.1:9091/local-models/status | python3 -c "import sys,json; d=json.load(sys.stdin); print({k:v[\"decision\"] for k,v in d[\"routing\"].items()})"'`
   - Expected: `stt` and `tts` show `on-box` (their seed flags just flipped). (embeddings/rerank show `off` in this seed-flag view — expected; their real on-box routing is confirmed next.)
   - Run: `ssh bbx@192.168.1.153 'cat /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/Manifest/embeddings/active.json /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/Manifest/embeddings/rerank.json'`
   - Expected: `active.json` → `qwen3-embedding-8b-local`; `rerank.json` → `{"enabled": true, "provider": "localstack", "model": "qwen3-reranker-0.6b-local"}`. All four capabilities now resolve on-box.

---

### Task 10.13: G4 — on-box whisper streaming parity vs the gemma-box path

Compares first-partial + final-transcript latency and transcript quality for on-box Speaches vs the gemma-box custom-server Speaches, over an identical clip, plus a manual Portal + Fold mic-flow parity check (house rule for UI).

**Files:**
- Results: `eval/results/$DATE-g4-stt-parity-{onbox,local}.json`

1. Generate a 24kHz/16-bit/mono reference clip on MS02 (known text → cloud TTS → resample):
   - Run: `ssh bbx@192.168.1.153 'curl -s -X POST http://127.0.0.1:9091/tts -H "Content-Type: application/json" -d "{\"text\":\"the quick brown fox jumps over the lazy dog near the riverbank at dawn\",\"voice\":\"openai:onyx\"}" -o /tmp/ref.mp3 && ffmpeg -y -i /tmp/ref.mp3 -ar 24000 -ac 1 -c:a pcm_s16le /tmp/ref24k.wav && soxi /tmp/ref24k.wav 2>/dev/null | grep -E "Sample Rate|Channels" || true'`
   - Expected: `/tmp/ref24k.wav` at 24000 Hz, 1 channel.
2. Probe the on-box provider:
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && Orchestrator/venv/bin/python diagnostics/localstack/stt_parity.py --provider onbox --wav /tmp/ref24k.wav --out eval/results/'"$DATE"'-g4-stt-parity-onbox.json'`
   - Expected: JSON with `first_partial_s`, `final_s`, `transcript` (matches the reference text), `stt_done: true`.
3. Probe the gemma-box path (requires the gemma-box custom server registered + reachable at 192.168.1.50):
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && Orchestrator/venv/bin/python diagnostics/localstack/stt_parity.py --provider local --wav /tmp/ref24k.wav --out eval/results/'"$DATE"'-g4-stt-parity-local.json'`
   - Expected: comparable `first_partial_s`/`final_s`; same transcript. G4 passes iff on-box first-partial latency is within parity of gemma-box (same model — `large-v3-turbo` — so quality should be identical).
4. **Manual Fold + Portal parity (house rule):** on the Galaxy Fold, open the Portal mic flow and the Android terminal mic; speak; confirm live partials + final land the same as gemma-box did, with the provider showing on-box. Record the observation in the commit message.
   - Expected: live partials stream, no 60s cap, final transcript correct, provider = on-box.
5. Land the G4 artifacts:
   - Run: `scp bbx@192.168.1.153:/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/eval/results/$DATE-g4-stt-parity-*.json eval/results/ && git add eval/results/$DATE-g4-stt-parity-onbox.json eval/results/$DATE-g4-stt-parity-local.json && git commit -m "eval(localstack): G4 STT streaming parity vs gemma-box (MS02 + Fold-validated)"`
   - Expected: one commit, 2 files.

---

### Task 10.14: G5 — cross-group swap cost (both directions, warm + cold)

Measures the audio↔retrieval evict+load first-interaction stall. Expected ~6–10s (dominated by the 8B embedder cold-load; keep-warm gives zero cross-group relief). Brandon signs off on the stall (Q12/D9) or the design shifts to a hybrid.

**Files:**
- Results: `eval/results/$DATE-g5-swap-warm.json`, `eval/results/$DATE-g5-swap-cold.json`

1. Warm page-cache run (both directions, 5 iters):
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && Orchestrator/venv/bin/python diagnostics/localstack/swap_cost.py --cache warm --iters 5 --out eval/results/'"$DATE"'-g5-swap-warm.json'`
   - Expected: JSON with `audio_to_retrieval_s.median_s` ~6–10 and `retrieval_to_audio_s.median_s` ~5–8.
2. Cold page-cache run (drop caches first, then measure the true cold-load):
   - Run: `ssh -t bbx@192.168.1.153 'sudo sh -c "echo 3 > /proc/sys/vm/drop_caches" && cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && Orchestrator/venv/bin/python diagnostics/localstack/swap_cost.py --cache cold --iters 5 --out eval/results/'"$DATE"'-g5-swap-cold.json'`
   - Expected: cold medians somewhat higher than warm (PCIe + CUDA init on a cold page-cache); both directions recorded.
3. Present the numbers to Brandon for the Q12/D9 sign-off (accept ~6–10s first interaction, or pivot to a hybrid). Record his decision in the commit message.
4. Land the G5 artifacts:
   - Run: `scp bbx@192.168.1.153:/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/eval/results/$DATE-g5-swap-*.json eval/results/ && git add eval/results/$DATE-g5-swap-warm.json eval/results/$DATE-g5-swap-cold.json && git commit -m "eval(localstack): G5 cross-group swap cost warm+cold (MS02; Brandon signed off on ~6-10s)"`
   - Expected: one commit, 2 files.

---

### Task 10.15: G6 — streaming-STT eviction safety (Design B + Orchestrator serialization)

Fires a retrieval/embedding request while a voice stream is mid-utterance and asserts zero audio cut-off. This is the direct test that the Orchestrator holds retrieval-group dispatch while a local voice stream is open (D12).

**Files:**
- Results: `eval/results/$DATE-g6-evict-safety.json`

1. Run the eviction-safety probe (control run + injected run over the 24kHz clip from Task 10.13):
   - Run: `ssh bbx@192.168.1.153 'cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main && Orchestrator/venv/bin/python diagnostics/localstack/stt_evict_safety.py --wav /tmp/ref24k.wav --out eval/results/'"$DATE"'-g6-evict-safety.json ; echo EXIT=$?'`
   - Expected: JSON with `audio_cut_off: false`, `pass: true`, `injected.stt_done: true`, and `extra_gap_s` within `gap_tolerance_s` (the mid-utterance embed was HELD, not served — so the audio stream never lost frames). `EXIT=0`. `pass: false` = the serialization is not holding retrieval dispatch → STOP, file against the audio-serialization milestone.
2. Land the G6 artifact:
   - Run: `scp bbx@192.168.1.153:/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/eval/results/$DATE-g6-evict-safety.json eval/results/ && git add eval/results/$DATE-g6-evict-safety.json && git commit -m "eval(localstack): G6 streaming-STT eviction safety (MS02)"`
   - Expected: one commit, 1 file.

---

### Task 10.16: Acceptance — end-to-end checklist on MS02 (§10 verbatim)

Walk the design's acceptance checklist verbatim. Each item is a live check on the cutover box.

**Files:** none (runbook; final acceptance evidence recorded in the closing commit message).

1. **Fresh install → wizard → all four capabilities local; `GET /local-models/status` green.** (M1's real status shape: top-level `routing[cap].decision`, value `on-box`; there is no `capabilities` key. STT/TTS decisions are seed-flag-driven; embeddings/rerank on-box routing lives in `active.json`/`rerank.json`.)
   - Run: `ssh bbx@192.168.1.153 'curl -fsS http://127.0.0.1:9091/local-models/status | python3 -c "import sys,json; d=json.load(sys.stdin); dec={k:v[\"decision\"] for k,v in d[\"routing\"].items()}; print(dec); assert dec[\"stt\"]==\"on-box\" and dec[\"tts\"]==\"on-box\", dec; assert d[\"healthy\"] is True; print(\"STT/TTS GREEN\")"'`
   - Then confirm embeddings + rerank resolve on-box from their activation artifacts:
   - Run: `ssh bbx@192.168.1.153 'python3 -c "import json; a=json.load(open(\"/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/Manifest/embeddings/active.json\")); r=json.load(open(\"/home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/Manifest/embeddings/rerank.json\")); assert a[\"active\"]==\"qwen3-embedding-8b-local\", a; assert r.get(\"provider\")==\"localstack\" and r.get(\"enabled\"), r; print(\"EMBED/RERANK GREEN\")"'`
   - Expected: `STT/TTS GREEN` then `EMBED/RERANK GREEN` — all four on-box.
2. **Full local voice conversation (live STT → chat → streaming Qwen TTS) with zero cloud audio calls.**
   - Manual: on the Portal, hold a short voice conversation. Watch `journalctl -u blackbox.service -f` for zero cloud STT/TTS provider calls (no ElevenLabs/OpenAI/Gemini audio lines); confirm partials from `:9098` Speaches and TTS from the `qwen-tts` member.
   - Expected: the loop runs entirely on-box; no cloud audio provider is invoked.
3. **Search E2E on the migrated 8B-Q8 store, reranked locally.**
   - Mint a couple of snapshots on the fresh box, then: `ssh bbx@192.168.1.153 'curl -fsS -X POST http://127.0.0.1:9091/search -H "Content-Type: application/json" -d "{\"query\":\"local model stack\"}" | python3 -m json.tool | head -30'`
   - Expected: results ranked by the local 8B embed + local 0.6B rerank (confirm rerank ran in the logs: `[TOOLVAULT-EXEC]`/rerank scores present).
4. **CU session on a virtual display, agent opens apps privately, live view in Portal + Android, desktop untouched.**
   - Manual: launch a `use_computer` session; confirm it opens on `:100+n` Xvfb (not the physical desktop), live view renders in the Portal panel and the Android WebView, and the in-use indicator shows to a second user. The physical desktop is untouched throughout.
   - Expected: private virtual display, live view working on both surfaces, desktop unaffected.
5. **GPU behavior observed: queue → drain → evict → load; 10-min TTL eviction confirmed; voice loop never thrashes.**
   - Run: `ssh bbx@192.168.1.153 'curl -fsS http://127.0.0.1:9098/running | python3 -m json.tool'` right after a voice turn (audio group resident), then after a search (retrieval group swapped in), then idle 11 minutes and re-check (both groups unloaded by the 600s TTL).
   - Expected: `/running` shows the demanded group resident and the other evicted; after >10 min idle, nothing resident. A full voice conversation shows zero mid-conversation swaps (whisper + qwen-tts co-resident).
6. **Kill the stack mid-use → per-capability graceful degradation (D2): STT/TTS fall back to cloud, retrieval returns un-reranked, mints go vector-less + gap-heal on recovery — the active embedding model is NOT switched (the embeddings carve-out).**
   - Run: `ssh -t bbx@192.168.1.153 'sudo systemctl stop blackbox-models.service'`, then exercise voice + search + a mint, then `sudo systemctl start blackbox-models.service`.
   - Expected: STT/TTS fall back to the wizard-chosen cloud provider per request; search returns un-reranked results (`score()` → `None`, never raises); the mint is stored **vector-less** and gap-heals on recovery; and — critically — `Manifest/embeddings/active.json` STILL reads `qwen3-embedding-8b-local` (the active embedding model is **NEVER** health-switched; a crash must not fragment the corpus across dim-incompatible cloud/local stores, §6 invariant).
7. Record acceptance evidence in a closing commit (no code — the message captures the six checks' outcomes):
   - Run: `git commit --allow-empty -m "chore(localstack): MS02 Phase-2 acceptance PASSED — 4 caps on-box, voice loop clean, CU virtual displays live, degradation per-capability, embedding model not health-switched"`
   - Expected: one empty acceptance commit. Then `git push origin main`.

---

### Task 10.17: Rollback runbook — per-capability revert paths

Documented, tested rollback for each capability if a gate regresses in production. **The embeddings carve-out is different from the other three** (§6 invariant): reverting embeddings is a *re-embed*, never a flag flip. Keep this task's steps in the plan even though they run only on failure.

**Files:** none (runbook; reversions on MS02).

1. **STT / TTS — flag revert (immediate, per-request).** Flip each on-box precedence flag off via the real per-capability endpoint (`POST /local-models/capability`, one call per capability — the same endpoint the cutover used, Task 8.3); the resolver falls back to the wizard-chosen cloud provider on the next request (no re-embed, no data change):
   - Run: `ssh bbx@192.168.1.153 'for C in stt tts; do curl -fsS -X POST http://127.0.0.1:9091/local-models/capability -H "Content-Type: application/json" -d "{\"capability\":\"$C\",\"enabled\":false}"; done && sudo systemctl restart blackbox.service'`
   - Expected: `[local_models] stt = false`, `tts = false` (disabling `stt` also clears the `STT_PROVIDER=onbox` mirror in `.env`, Task 8.3); `/local-models/status` `routing.stt`/`routing.tts` decision now `off` and the resolver serves the cloud (or gemma-box `local`) provider. An explicit credentialed user pick was already winning regardless (D2), so this only moves the *default*.
2. **Rerank — flag revert + optional vLLM re-enable.** Point the sidecar back to the previous reranker. To cloud/Vertex: `POST /rerank/select {provider: vertex, ...}`. To the dark vLLM FP16 seam (only if the retrieval group is DOWN — the two must never co-run on the GPU, §5.2):
   - Run (cloud): `ssh bbx@192.168.1.153 'curl -fsS -X POST http://127.0.0.1:9091/rerank/select -H "Content-Type: application/json" -d "{\"provider\":\"vertex\",\"model\":\"vertex-semantic-ranker\",\"enabled\":true}"'`
   - Run (vLLM, retrieval group must be down first): `ssh -t bbx@192.168.1.153 'sudo systemctl stop blackbox-models.service && sudo systemctl enable --now vllm-reranker.service'`
   - Expected: `rerank.json` updated; a dead reranker only costs latency (never recall) so even "rerank off" is safe.
3. **Embeddings — re-embed rollback (NOT a flag flip).** The active embedding model changes ONLY via re-embed cutover (the sole writer of `active.json`). Two cases:
   - **Fresh MS02 corpus (post-Step-0, small):** re-embed back to the wizard's cloud model — fast on the small self-minted corpus:
     - Run: `ssh bbx@192.168.1.153 'curl -fsS -X POST http://127.0.0.1:9091/embeddings/reembed -H "Content-Type: application/json" -d "{\"target\":\"gemini-embedding-2\"}"'`
     - Expected: started → done; `active.json` → `gemini-embedding-2`; then bust caches (`POST /toolvault/reload` + code-embeddings rebuild) as in Task 10.12 step 3.
   - **A box with a prior store backup (update-in-place case):** the reembed cutover left a `Manifest/embeddings/<slug>.pre-rebuild.<ts>` backup (migrate.py:891/906). Restore it in place and repoint active:
     - Run: `ssh -t bbx@192.168.1.153 'sudo systemctl stop blackbox.service && cd /home/bbx/Desktop/blackbox-poc-main./blackbox-poc-main/Manifest/embeddings && LATEST=$(ls -d gemini-embedding-2.pre-rebuild.* 2>/dev/null | sort | tail -1) && [ -n "$LATEST" ] && rm -rf gemini-embedding-2 && mv "$LATEST" gemini-embedding-2 && printf "{\"active\": \"gemini-embedding-2\"}" > active.json && sudo systemctl start blackbox.service'`
     - Expected: the pre-rebuild store is promoted back to live and `active.json` repointed; search serves the restored dim-matched store. (The `.pre-chunk` backups are the older WI-2 rollback lineage — same restore shape.)
4. **Whole-stack disable.** If the local stack must be fully retired, disable the models service (the resolver keys on install/config/process-liveness, so this degrades every capability to its cloud fallback per Task 10.17.1–3, WITHOUT switching the active embedding model):
   - Run: `ssh -t bbx@192.168.1.153 'sudo systemctl disable --now blackbox-models.service && curl -fsS http://127.0.0.1:9091/local-models/status | python3 -c "import sys,json; print(json.load(sys.stdin).get(\"installed\"), json.load(open(\"/dev/stdin\")) if False else \"\")" 2>/dev/null; echo done'`
   - Expected: `blackbox-models.service` disabled+stopped; `/local-models/status` reports the stack down; STT/TTS route to cloud, rerank falls through to un-reranked, and the active embedding model is untouched (the corpus stays queryable via its existing local vectors + vector-less new mints that gap-heal if/when the stack returns).
5. **Re-enable the retired fallbacks (return to the pre-Phase-2 topology).** If reverting MS02 to its old pinned pair: re-enable vLLM rerank and re-pin the Ollama 8B keep-warm:
   - Run: `ssh -t bbx@192.168.1.153 'sudo systemctl enable --now vllm-reranker.service && ollama run qwen3-embedding:8b "" >/dev/null 2>&1 || true'`
   - Expected: the old ~10GB pinned topology is back; ensure `blackbox-models.service` stays disabled (the vLLM `/score` seam must never co-run with the retrieval group on the GPU, §5.2).


---

