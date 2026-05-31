# TTS Voice Catalog — Single Source of Truth Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the backend the single source of truth for the TTS voice picker (OpenAI HD 11 + Gemini Flash 30 + Gemini Pro 30), have Android and the web portal both fetch it, fix the Android picker that only showed 6/30 Gemini voices, and add a single on-demand ▶ preview button next to the Android voice selector.

**Architecture:** Define the catalog ONCE in `Orchestrator/config.py` as a pure builder + serve it via `GET /tts/catalog`. Android `TtsRepository` fetches it (with a full offline fallback) and `SettingsSheet` renders it + adds a preview button reusing the existing submit→poll→play TTS plumbing. The web portal builds its `#ttsVoiceSelect` optgroups from the same endpoint.

**Tech Stack:** Python/FastAPI + pytest (backend); Kotlin/Jetpack Compose + JUnit4 (Android); vanilla JS (Portal).

**Design doc:** `docs/plans/2026-05-31-tts-voice-catalog-sot-design.md`

---

## Working directories & commands

**Backend** (repo root):
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
```
- Run backend tests: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/<file> -v`
- Restart service (pre-authorized, 60–90s warmup — wait before curling): `sudo systemctl restart blackbox.service`
- Smoke an endpoint: `curl -s http://localhost:9091/tts/catalog | python3 -m json.tool`

**Android module** (quote — space + parens):
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal"
```
- Targeted tests: `./gradlew testDebugUnitTest --tests "com.aiblackbox.portal.<Class>"` (add `--rerun-tasks` if UP-TO-DATE)
- Build APK: `./gradlew assembleDebug`
- SRC = `app/src/main/java/com/aiblackbox/portal`, TEST = `app/src/test/java/com/aiblackbox/portal`

**Portal:** static files under `Portal/`, served by the Orchestrator at `:9091`.

> Out of scope: live Voice Agent voices (`Constants.VOICES_GEMINI_LIVE`, `/gemini-live/voices`). Do NOT touch them.

---

## Task 0: Baseline

Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests -q` and (Android) `./gradlew test`.
Expected: green (or note pre-existing failures). If broken, STOP and report.

---

## Task 1: Backend — canonical catalog in config.py (pure, TDD)

**Files:**
- Modify: `Orchestrator/config.py` (after `GEMINI_LIVE_VOICE_DESCRIPTORS`, ~line 433)
- Test: `Orchestrator/tests/test_tts_catalog.py` (create)

**Step 1: Write failing test** — create `Orchestrator/tests/test_tts_catalog.py`:
```python
"""Single-source-of-truth TTS voice catalog (2026-05-31)."""
from Orchestrator.config import (
    build_tts_catalog,
    GEMINI_TTS_VOICE_DESCRIPTIONS,
    GEMINI_LIVE_VOICES,
    OPENAI_TTS_VOICES,
)

def test_three_groups_in_order():
    assert [g["id"] for g in build_tts_catalog()] == ["openai", "gemini-flash", "gemini-pro"]

def test_group_counts():
    g = {x["id"]: x for x in build_tts_catalog()}
    assert len(g["openai"]["voices"]) == 11
    assert len(g["gemini-flash"]["voices"]) == 30
    assert len(g["gemini-pro"]["voices"]) == 30

def test_ids_prefixed_and_fully_described():
    for grp in build_tts_catalog():
        for v in grp["voices"]:
            assert v["id"].startswith(grp["id"] + ":")
            assert v["name"] and v["description"]

def test_gemini_descriptions_cover_all_30_live_names():
    # Names reuse GEMINI_LIVE_VOICES ordering; descriptions must cover them 1:1.
    assert set(GEMINI_TTS_VOICE_DESCRIPTIONS) == set(GEMINI_LIVE_VOICES)

def test_flash_and_pro_share_names_differ_by_prefix():
    g = {x["id"]: x for x in build_tts_catalog()}
    flash = [v["name"] for v in g["gemini-flash"]["voices"]]
    pro = [v["name"] for v in g["gemini-pro"]["voices"]]
    assert flash == pro == GEMINI_LIVE_VOICES
    assert g["gemini-flash"]["voices"][0]["id"] == "gemini-flash:Zephyr"
    assert g["gemini-pro"]["voices"][0]["id"] == "gemini-pro:Zephyr"
```

**Step 2: Run, verify fail:** `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_tts_catalog.py -v` → ImportError (build_tts_catalog missing).

**Step 3: Implement** — append to `Orchestrator/config.py` after `GEMINI_LIVE_VOICE_DESCRIPTORS`:
```python
# ── TTS voice catalog (single source of truth for the TTS voice PICKER) ──────
# Served by GET /tts/catalog; consumed by the web Portal + Android Settings.
# DISTINCT from GEMINI_LIVE_VOICES above (the live Voice Agent, /gemini-live/voices)
# — different feature. Do not merge the two.

# Gemini TTS descriptions (Flash + Pro share these — defined ONCE). Names come
# from GEMINI_LIVE_VOICES (the 30-name catalog) so names live in one place.
GEMINI_TTS_VOICE_DESCRIPTIONS: Dict[str, str] = {
    "Zephyr": "Bright, cheerful", "Puck": "Playful, mischievous", "Charon": "Calm, informative",
    "Kore": "Clear, versatile", "Fenrir": "Bold, confident", "Leda": "Warm, youthful",
    "Orus": "Deep, firm", "Aoede": "Breezy, conversational", "Callirrhoe": "Smooth, flowing",
    "Autonoe": "Gentle, measured", "Enceladus": "Rich, resonant", "Iapetus": "Deep, steady",
    "Umbriel": "Soft, mysterious", "Algieba": "Warm, articulate", "Despina": "Light, energetic",
    "Erinome": "Serene, melodic", "Algenib": "Crisp, precise", "Rasalgethi": "Grand, theatrical",
    "Laomedeia": "Graceful, elegant", "Achernar": "Bright, radiant", "Alnilam": "Strong, commanding",
    "Schedar": "Regal, distinguished", "Gacrux": "Earthy, grounded", "Pulcherrima": "Beautiful, refined",
    "Achird": "Friendly, approachable", "Zubenelgenubi": "Balanced, neutral", "Vindemiatrix": "Mature, wise",
    "Sadachbia": "Lucky, optimistic", "Sadaltager": "Hopeful, bright", "Sulafat": "Lyrical, musical",
}

# OpenAI TTS HD voices (11): (id, name, description).
OPENAI_TTS_VOICES = [
    ("alloy", "Alloy", "Neutral, balanced"), ("ash", "Ash", "Clear, direct"),
    ("ballad", "Ballad", "Warm, gentle"), ("coral", "Coral", "Friendly, conversational"),
    ("echo", "Echo", "Smooth, authoritative"), ("fable", "Fable", "Expressive, British"),
    ("nova", "Nova", "Energetic, confident"), ("onyx", "Onyx", "Deep, authoritative"),
    ("sage", "Sage", "Thoughtful, measured"), ("shimmer", "Shimmer", "Soft, ethereal"),
    ("verse", "Verse", "Poetic, dramatic"),
]

def build_tts_catalog() -> list:
    """Grouped TTS voice catalog — the single source of truth for the picker.
    Returns [{id,label,voices:[{id,name,description}]}]. Gemini ids are
    'gemini-flash:<Name>' / 'gemini-pro:<Name>'; OpenAI 'openai:<id>'."""
    def gemini_group(provider: str, label: str) -> dict:
        return {"id": provider, "label": label, "voices": [
            {"id": f"{provider}:{n}", "name": n, "description": GEMINI_TTS_VOICE_DESCRIPTIONS[n]}
            for n in GEMINI_LIVE_VOICES
        ]}
    return [
        {"id": "openai", "label": "OpenAI TTS HD", "voices": [
            {"id": f"openai:{vid}", "name": nm, "description": ds}
            for vid, nm, ds in OPENAI_TTS_VOICES
        ]},
        gemini_group("gemini-flash", "Gemini Flash TTS"),
        gemini_group("gemini-pro", "Gemini Pro TTS"),
    ]
```
(`Dict` is already imported in config.py — verify; if not, add `from typing import Dict`.)

**Step 4: Run, verify pass** (5 tests).

**Step 5: Commit**
```bash
git add Orchestrator/config.py Orchestrator/tests/test_tts_catalog.py
git commit -m "feat(tts): canonical TTS voice catalog (SoT) in config.py"
```

---

## Task 2: Backend — GET /tts/catalog endpoint

**Files:** Modify `Orchestrator/routes/tts_routes.py` (near the other `@app.get` TTS routes, e.g. after `/tts/google/voices` ~line 776)

**Step 1: Implement**
```python
@app.get("/tts/catalog")
async def tts_catalog():
    """Grouped TTS voice catalog — single source of truth for the voice picker
    (web Portal + Android both fetch this). See config.build_tts_catalog()."""
    from Orchestrator.config import build_tts_catalog
    return {"groups": build_tts_catalog()}
```

**Step 2: Restart + smoke test**
```bash
sudo systemctl restart blackbox.service
sleep 75   # snapshot index rebuild
curl -s http://localhost:9091/tts/catalog | python3 -m json.tool | head -40
```
Expected: JSON with 3 groups; counts 11 / 30 / 30. Verify with:
```bash
curl -s http://localhost:9091/tts/catalog | python3 -c "import sys,json; g=json.load(sys.stdin)['groups']; print({x['id']:len(x['voices']) for x in g})"
```
Expected: `{'openai': 11, 'gemini-flash': 30, 'gemini-pro': 30}`

**Step 3: Commit**
```bash
git add Orchestrator/routes/tts_routes.py
git commit -m "feat(tts): GET /tts/catalog serves the voice catalog SoT"
```

---

## Task 3: Android — fix parseVoice + expand offline fallback (TDD)

**Files:**
- Modify: `SRC/data/repository/TtsRepository.kt` (`parseVoice` ~line 60-82; `TTS_VOICE_GROUPS` ~line 210-235)
- Test: `TEST/data/repository/TtsVoiceParseTest.kt` (create)

**Step 1: Read** `TtsRepository.kt` to get exact current `parseVoice` text.

**Step 2: Write failing test** — `TEST/data/repository/TtsVoiceParseTest.kt`:
```kotlin
package com.aiblackbox.portal.data.repository

import org.junit.Assert.assertEquals
import org.junit.Test

class TtsVoiceParseTest {
    @Test fun `openai voice maps to openai provider + tts model`() {
        val c = TtsRepository.parseVoice("openai:nova")
        assertEquals("openai", c.provider); assertEquals("nova", c.voice)
    }
    @Test fun `gemini-flash maps to flash tts model`() {
        val c = TtsRepository.parseVoice("gemini-flash:Zephyr")
        assertEquals("gemini-flash", c.provider)
        assertEquals("Zephyr", c.voice)
        assertEquals("gemini-2.5-flash-tts", c.model)
    }
    @Test fun `gemini-pro maps to pro tts model`() {
        val c = TtsRepository.parseVoice("gemini-pro:Charon")
        assertEquals("gemini-pro", c.provider)
        assertEquals("gemini-2.5-pro-tts", c.model)
    }
    @Test fun `bare legacy voice falls back to openai`() {
        val c = TtsRepository.parseVoice("onyx")
        assertEquals("openai", c.provider); assertEquals("onyx", c.voice)
    }
}
```
> NOTE: if `parseVoice` is currently an instance method, make it (and `VoiceConfig`) accessible to the test — move `parseVoice` into a `companion object` of `TtsRepository`, or keep it top-level. Confirm during Step 1 and adjust the test's call site accordingly.

**Step 3: Run, verify fail** (gemini-flash unknown / wrong model).

**Step 4: Implement** — update `parseVoice` so the provider prefix selects the model:
```kotlin
// "openai:nova" -> openai/tts-1-hd ; "gemini-flash:Zephyr" -> gemini-2.5-flash-tts ;
// "gemini-pro:Charon" -> gemini-2.5-pro-tts ; bare "onyx" -> openai (legacy).
fun parseVoice(voiceValue: String): VoiceConfig {
    val (provider, voice) = if (":" in voiceValue) {
        val parts = voiceValue.split(":", limit = 2); parts[0] to parts[1]
    } else "openai" to voiceValue
    return when (provider) {
        "gemini-flash" -> VoiceConfig("gemini-flash", voice, "gemini-2.5-flash-tts")
        "gemini-pro"   -> VoiceConfig("gemini-pro", voice, "gemini-2.5-pro-tts")
        else            -> VoiceConfig("openai", voice, "tts-1-hd")
    }
}
```
Also expand the hardcoded `TTS_VOICE_GROUPS` offline fallback so the Gemini Pro group has all 30 voices AND add a Gemini Flash group of 30 (mirror the catalog; descriptions from the design doc / `GEMINI_TTS_VOICE_DESCRIPTIONS`). This guarantees the picker is complete even if `/tts/catalog` is unreachable.

**Step 5: Run, verify pass.** Then `./gradlew compileDebugKotlin`.

**Step 6: Commit**
```bash
git add "app/src/main/java/com/aiblackbox/portal/data/repository/TtsRepository.kt" "app/src/test/java/com/aiblackbox/portal/data/repository/TtsVoiceParseTest.kt"
git commit -m "fix(android-tts): parseVoice handles gemini-flash/pro models; full offline voice fallback"
```

---

## Task 4: Android — TtsRepository.fetchCatalog()

**Files:** Modify `SRC/data/repository/TtsRepository.kt`

**Step 1: Implement** a suspend fetch that GETs `/tts/catalog` and parses to `List<VoiceGroup>`, falling back to `TTS_VOICE_GROUPS` on any error:
```kotlin
suspend fun fetchCatalog(): List<VoiceGroup> = try {
    val raw = api.get("/tts/catalog")
    val groups = json.parseToJsonElement(raw).jsonObject["groups"]?.jsonArray ?: return TTS_VOICE_GROUPS
    groups.map { g ->
        val o = g.jsonObject
        VoiceGroup(
            label = o["label"]?.jsonPrimitive?.content ?: "",
            voices = (o["voices"]?.jsonArray ?: JsonArray(emptyList())).map { v ->
                val vo = v.jsonObject
                VoiceOption(
                    id = vo["id"]!!.jsonPrimitive.content,
                    name = vo["name"]!!.jsonPrimitive.content,
                    description = vo["description"]?.jsonPrimitive?.content ?: "",
                )
            },
        )
    }.ifEmpty { TTS_VOICE_GROUPS }
} catch (e: Exception) {
    Log.w(TAG, "fetchCatalog failed, using offline fallback: ${e.message}")
    TTS_VOICE_GROUPS
}
```
(Add needed kotlinx.serialization.json imports: `jsonArray`, `JsonArray`, `jsonObject`, `jsonPrimitive`.)

**Step 2: Verify** `./gradlew compileDebugKotlin` SUCCESSFUL. (No unit test — network fetch; the fallback path is the parse safety net.)

**Step 3: Commit**
```bash
git add "app/src/main/java/com/aiblackbox/portal/data/repository/TtsRepository.kt"
git commit -m "feat(android-tts): fetch voice catalog from /tts/catalog with offline fallback"
```

---

## Task 5: Android — SettingsSheet renders fetched catalog

**Files:** Modify `SRC/ui/settings/SettingsSheet.kt` (voice picker ~line 417-470) and its ViewModel `SRC/ui/settings/SettingsViewModel.kt`

**Step 1:** In `SettingsViewModel`, add catalog state + loader:
```kotlin
private val _voiceGroups = MutableStateFlow(com.aiblackbox.portal.data.repository.TTS_VOICE_GROUPS)
val voiceGroups: StateFlow<List<VoiceGroup>> = _voiceGroups.asStateFlow()
fun loadVoiceCatalog() = viewModelScope.launch {
    _voiceGroups.value = TtsRepository(/* api for current origin */).fetchCatalog()
}
```
(Match how other repos/APIs are constructed in this VM — reuse the existing api/origin wiring; check neighboring VM methods.)

**Step 2:** In `SettingsSheet`, replace `val allVoiceGroups = ...TTS_VOICE_GROUPS` with the collected state, and trigger the load once:
```kotlin
val allVoiceGroups by viewModel.voiceGroups.collectAsState()
LaunchedEffect(Unit) { viewModel.loadVoiceCatalog() }
```
Rendering loop stays the same (it already iterates groups → voices).

**Step 3: Verify** `./gradlew assembleDebug` SUCCESSFUL. Screenshot the picker (should show OpenAI 11 + Gemini Flash 30 + Gemini Pro 30).

**Step 4: Commit**
```bash
git add "app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsSheet.kt" "app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsViewModel.kt"
git commit -m "feat(android-tts): Settings voice picker renders full catalog from backend"
```

---

## Task 6: Android — single ▶ preview button (on-demand)

**Files:** Modify `SRC/ui/settings/SettingsSheet.kt` + `SettingsViewModel.kt`

**Step 1:** Add a preview function to `SettingsViewModel` that mirrors the portal's `btnPreviewVoice` and the `GeminiProTtsScreen` submit→poll→play pattern:
```kotlin
private val _previewing = MutableStateFlow(false)
val previewing: StateFlow<Boolean> = _previewing.asStateFlow()
private var previewPlayer: android.media.MediaPlayer? = null

fun previewVoice(voiceId: String) = viewModelScope.launch {
    if (_previewing.value) return@launch
    _previewing.value = true
    try {
        val repo = TtsRepository(/* api */)
        val text = "Hello! This is a preview of the selected voice."
        val cfg = TtsRepository.parseVoice(voiceId)
        val url: String = if (cfg.provider == "openai") {
            repo.generateTts(text, cfg.voice, cfg.model).audio_url
        } else {
            // Gemini: submit async, poll /tasks/status/{id} until audio_url present.
            val sub = repo.generateGeminiTts(text, cfg.voice, cfg.model)
            repo.pollGeminiTaskForUrl(sub.task_id)   // implement: GET /tasks/status/{id}, ~60×500ms
        }
        if (url.isNotBlank()) playPreview(url)
    } catch (e: Exception) {
        _error.value = "Preview failed: ${e.message}"
    } finally { _previewing.value = false }
}
private fun playPreview(url: String) {
    previewPlayer?.release()
    previewPlayer = android.media.MediaPlayer().apply {
        setDataSource(if (url.startsWith("http")) url else "$origin$url")
        setOnPreparedListener { start() }
        setOnCompletionListener { release(); previewPlayer = null }
        prepareAsync()
    }
}
```
Add `pollGeminiTaskForUrl` to `TtsRepository` reusing `GeminiProTtsScreen.kt:216-235` (GET `/tasks/status/{id}`, parse `result.audio_url` / `audio_url`, poll with `delay(500)` up to ~30s, throw on timeout).

**Step 2:** In `SettingsSheet`, put a ▶ `IconButton` next to the TTS Voice `SettingsDropdown` (in a `Row`), disabled + showing a spinner/"…" while `previewing`:
```kotlin
val previewing by viewModel.previewing.collectAsState()
Row(verticalAlignment = Alignment.CenterVertically) {
    Box(Modifier.weight(1f)) { /* existing SettingsDropdown */ }
    IconButton(enabled = !previewing, onClick = { viewModel.previewVoice(currentVoice) }) {
        Text(if (previewing) "…" else "▶", color = SolidGreen)
    }
}
```

**Step 3: Verify** `./gradlew assembleDebug`. On device: pick an OpenAI voice → ▶ plays instantly; pick a Gemini voice → ▶ shows "…" then plays. Screenshot.

**Step 4: Commit**
```bash
git add "app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsSheet.kt" "app/src/main/java/com/aiblackbox/portal/ui/settings/SettingsViewModel.kt" "app/src/main/java/com/aiblackbox/portal/data/repository/TtsRepository.kt"
git commit -m "feat(android-tts): single on-demand voice preview button in Settings"
```

---

## Task 7: Android — align GeminiProTtsScreen to the catalog (dedupe)

**Files:** Modify `SRC/ui/generation/GeminiProTtsScreen.kt`

**Step 1:** Replace the hardcoded `GEMINI_PRO_VOICES` list usage with the `gemini-pro` group from `TtsRepository.fetchCatalog()` (or the offline fallback). Map `VoiceOption` → the screen's `GeminiVoice(name, desc)`. Keep the screen's generate flow unchanged. If wiring a fetch here is heavy, at minimum delete the duplicate list and derive it from the same canonical fallback constant so there's no second copy.

**Step 2: Verify** `./gradlew assembleDebug`. Screenshot the Gemini Pro TTS screen voice dropdown (still 30, same source).

**Step 3: Commit**
```bash
git add "app/src/main/java/com/aiblackbox/portal/ui/generation/GeminiProTtsScreen.kt"
git commit -m "refactor(android-tts): GeminiProTtsScreen sources voices from the catalog (dedupe)"
```

---

## Task 8: Web portal — build #ttsVoiceSelect from /tts/catalog

**Files:** Modify `Portal/modules/tts-stt.js` (init ~line 1643) and `Portal/index-modular.html` (the `#ttsVoiceSelect` optgroups ~line 347-399)

**Step 1:** In `tts-stt.js` init, fetch the catalog and build the optgroups in JS:
```javascript
async function populateVoiceCatalog() {
    const sel = document.getElementById("ttsVoiceSelect");
    if (!sel) return;
    try {
        const res = await fetch("/tts/catalog");
        const { groups } = await res.json();
        const prev = sel.value || TTS_DEFAULT_VOICE;
        sel.innerHTML = "";
        for (const g of groups) {
            const og = document.createElement("optgroup");
            og.label = `${g.label} (${g.voices.length} voices)`;
            for (const v of g.voices) {
                const o = document.createElement("option");
                o.value = v.id; o.textContent = `${v.name} - ${v.description}`;
                og.appendChild(o);
            }
            sel.appendChild(og);
        }
        sel.value = [...sel.options].some(o => o.value === prev) ? prev : "gemini-pro:Charon";
    } catch (e) { console.error("voice catalog fetch failed, keeping static options", e); }
}
```
Call `populateVoiceCatalog()` from the existing init (where `btnPreviewVoice` is wired).

**Step 2:** In `index-modular.html`, remove the hardcoded `<option>`s inside `#ttsVoiceSelect` (keep the `<select>` element + maybe one static `gemini-pro:Charon` fallback option so the control isn't empty pre-fetch).

**Step 3: Verify** — restart not needed (static files). Hard-refresh the Portal, open the TTS voice dropdown: OpenAI 11 + Gemini Flash 30 + Gemini Pro 30; default Charon selected; ▶ preview still works.

**Step 4: Commit**
```bash
git add Portal/modules/tts-stt.js Portal/index-modular.html
git commit -m "feat(portal-tts): build voice dropdown from /tts/catalog (SoT)"
```

---

## Task 9: Integration QA + version bump

**Step 1:** Backend `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_tts_catalog.py -v` (green) + `curl /tts/catalog` counts 11/30/30.
**Step 2:** Android `./gradlew test assembleDebug` (green). Manual QA: Settings picker shows 71 voices in 3 groups; ▶ previews OpenAI (instant) + Gemini (after poll); selection persists.
**Step 3:** Portal: dropdown populated from endpoint; preview works.
**Step 4:** Bump Android `app/build.gradle` versionCode (+1) / versionName.
**Step 5: Commit**
```bash
git add "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/build.gradle"
git commit -m "chore(android): bump version for TTS voice catalog release"
```

---

## Definition of done
- Backend `build_tts_catalog()` tests pass; `GET /tts/catalog` returns 11/30/30.
- Android picker shows all three groups (fetched, full offline fallback); parseVoice handles flash+pro; ▶ preview works for both providers.
- Portal dropdown built from `/tts/catalog`.
- Catalog names + Gemini descriptions defined exactly once (backend `config.py`); no remaining 6-voice list.
- Live Voice Agent voices untouched.

## Risks / notes
- **Service restart** required for Task 2 (60–90s). Pre-authorized.
- **TestClient import side effects:** prefer unit-testing the pure `build_tts_catalog()` (done) over importing the whole app; the endpoint is a trivial wrapper verified by curl.
- **`parseVoice` accessibility:** confirm it's reachable from a unit test (companion vs top-level) in Task 3 Step 1.
- **SettingsViewModel api/origin wiring:** reuse the VM's existing pattern for building `TtsRepository`/api; don't invent a new origin source.
- **Gemini preview latency:** acceptable per design (on-demand). Poll cap ~30s then error toast.
- **Portal pre-fetch flash:** keep one static fallback `<option>` so the select isn't empty before the fetch resolves.
