# Auto-Ingest Local Model Capabilities — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (fresh subagent per task, spec then quality review). Work on `main` (staging-box-as-production). Design: `docs/plans/2026-07-12-auto-ingest-local-capabilities-design.md`.

**Goal:** Register one OpenAI-compatible LAN server (key + URL) → auto-detect + confirm each discovered model's modality → wire it into chat / image / TTS / STT with zero per-modality setup.

**Architecture:** A single `classify_model()` seed + an authoritative persisted `model_modalities` map + a unified `model_modality()` / `resolve_modality_server()` resolver (persisted-first, name-pattern fallback), consumed by per-modality registrars. TTS/STT reuse their existing OpenAI-shaped adapters.

**Tech Stack:** FastAPI (:9091), Python 3.12 (`Orchestrator/venv`), `requests`, ToolVault v2, vanilla-JS wizard, pytest.

**Reference anchors (verified via subsystem map):**
- Registry: `Orchestrator/onboarding/custom_servers.py` — `add_server:222`, `_PATCHABLE_FIELDS:40`, `_validate_field_types:145`, `is_image_model:361`, `resolve_image_server:377`, `resolve_model:288`.
- Validate: `Orchestrator/onboarding/validators.py:validate_custom:137-167`; success stamp `Orchestrator/routes/onboarding_routes.py:495-519`; CRUD `:341-395`; `CustomServerCreate:326`.
- Chat filter: `Orchestrator/routes/admin_routes.py:_fetch_custom_models:805`.
- Image: `Orchestrator/image_providers.py`, `Orchestrator/image_catalog.py`, `Orchestrator/toolvault/availability.py:_local_image_available:92`.
- TTS: `Orchestrator/config.py:build_tts_catalog:649`, `OPENAI_TTS_URL:496`; `Orchestrator/routes/tts_routes.py:tts_batch` dispatch `:359`, `_openai_tts_chunk:294`.
- STT: `Orchestrator/stt/catalog.py:build_stt_catalog:5`, `Orchestrator/stt/resolve.py` (`stt_availability`, `resolve_stt_provider`), `Orchestrator/stt/file_transcribe.py:transcribe_bytes:23`, `_openai_transcribe:54`, `config.py:OPENAI_STT_URL:495`.
- Wizard: `Portal/onboarding/steps/api_keys.js` (custom-servers section).

---

## Phase 1 — Unified classifier, modality map, resolver (`custom_servers.py`)

The foundation everything else reads. Refactors the shipped image helpers onto the unified core (no behavior change for image).

### Task 1.1: `classify_model()` + per-modality patterns

**Test:** `Orchestrator/tests/test_modality_classifier.py` (create) — assert each family classifies (z-image→image, whisper-large→stt, kokoro-tts→tts, bge-m3→embedding, gemma-31b→chat, llama-3.3-70b→chat); non-string→chat.

**Implement** in `custom_servers.py` (replace the image-only `IMAGE_MODEL_PATTERNS`/`is_image_model` block):
```python
# Per-modality name-pattern allowlists (v1 classification seed). OpenAI /v1/models
# carries NO modality flag, so classify by id; the wizard confirm step overrides.
MODALITY_PATTERNS = {
    "image": ("z-image", "zimage", "flux", "qwen-image", "sdxl", "sd3", "sd-turbo",
              "stable-diffusion", "playground-v", "kolors", "hidream", "pixart"),
    "tts":   ("tts", "-speech", "speech-", "kokoro", "piper", "xtts", "bark",
              "vibevoice", "orpheus", "parler", "styletts", "melotts", "chatterbox"),
    "stt":   ("whisper", "-stt", "stt-", "transcrib", "parakeet", "scribe",
              "distil-whisper", "faster-whisper", "canary", "moonshine"),
    "embedding": ("embed", "bge", "gte", "e5-", "nomic-embed", "mxbai", "jina-embed",
                  "arctic-embed", "snowflake-arctic-embed"),
}
_ROUTABLE = ("image", "tts", "stt", "embedding")  # order = precedence; else -> chat


def classify_model(model_id: object) -> str:
    """Seed modality for a bare model id: 'image'|'tts'|'stt'|'embedding'|'chat'.
    Name-pattern allowlist + an *-image suffix fallback; default 'chat'. This is
    only a SEED -- the persisted model_modalities map (wizard-confirmed) wins at
    runtime via model_modality()."""
    if not isinstance(model_id, str):
        return "chat"
    m = model_id.lower()
    for modality in _ROUTABLE:
        if any(p in m for p in MODALITY_PATTERNS[modality]):
            return modality
    if m.endswith("-image") or m.endswith("_image"):
        return "image"
    return "chat"


def classify_models(model_ids: list) -> dict:
    """Seed map {model_id: modality} for a discovered model list (for /validate)."""
    return {m: classify_model(m) for m in model_ids if isinstance(m, str)}


def is_image_model(model_id: object) -> bool:  # back-compat wrapper (shipped callers)
    return classify_model(model_id) == "image"
```

### Task 1.2: persist `model_modalities` in the registry

**Test:** extend `test_custom_servers_image.py` — `add_server(..., model_modalities={"z-image":"image"})` round-trips; `update_server(id, {"model_modalities": {...}})` patches; bad shape → `ValueError`.

**Implement** in `custom_servers.py`:
- `_PATCHABLE_FIELDS` (`:40`): add `"model_modalities"`.
- `add_server` (`:213`): add param `model_modalities: dict | None = None` and store `"model_modalities": model_modalities or {}` in the record (`:222`).
- `_validate_field_types` (`:145`): add
```python
    if "model_modalities" in fields:
        v = fields["model_modalities"]
        if not isinstance(v, dict) or not all(
            isinstance(k, str) and isinstance(val, str) for k, val in v.items()
        ):
            raise ValueError("model_modalities must be a dict of {model_id: modality}")
```

### Task 1.3: unified `model_modality()` + `resolve_modality_server()`

**Test:** `test_modality_classifier.py` — persisted map wins over name-pattern; fallback to `classify_model` when absent; `resolve_modality_server("tts")` picks the first enabled server's tts model; honor-branch requires the model be hosted (mirror the image tests).

**Implement** in `custom_servers.py` (replace `resolve_image_server`, generalize):
```python
def model_modality(server: dict, model_id: str) -> str:
    """AUTHORITATIVE modality for a model on a server: the wizard-confirmed
    model_modalities map first, name-pattern classify() as fallback (servers
    registered before the confirm feature, or models discovered since)."""
    mm = server.get("model_modalities")
    if isinstance(mm, dict):
        val = mm.get(model_id)
        if isinstance(val, str) and val:
            return val
    return classify_model(model_id)


def resolve_modality_server(modality: str, model: str | None = None) -> tuple[dict, str] | None:
    """Pick the (server, bare_model) for a request of `modality`. An explicit
    `model` is honored only if it IS that modality AND the resolved server hosts
    it; else the first enabled server hosting a model of that modality. None if
    unavailable. Fresh registry read."""
    if model:
        srv, bare = resolve_model(model)
        if srv is not None and model_modality(srv, bare) == modality and bare in (srv.get("last_models") or []):
            return srv, bare
    for srv in list_servers(enabled_only=True):
        for m in (srv.get("last_models") or []):
            if isinstance(m, str) and model_modality(srv, m) == modality:
                return srv, m
    return None


def has_modality_model(modality: str) -> bool:
    return resolve_modality_server(modality) is not None


def resolve_image_server(model: str | None = None):  # back-compat wrapper
    return resolve_modality_server("image", model)
```
(Delete the old standalone `resolve_image_server` body; keep `list_image_models()` but reroute through `classify_model`.)

**Commit:** `feat(ingest): unified model-modality classifier + resolver + persisted map`

---

## Phase 2 — Capability detection at `/validate`

### Task 2.1: return the seed modality map from validate

**Test:** `Orchestrator/tests/test_validate_modalities.py` — monkeypatch the OpenAI client's `models.list()` to return `[gemma-31b, z-image, whisper-1, kokoro-tts]`; assert `validate_custom` returns `model_modalities == {gemma-31b:chat, z-image:image, whisper-1:stt, kokoro-tts:tts}` plus `capabilities == {chat,image,stt,tts}`.

**Implement** in `validators.py:validate_custom` (after building `ids`, `:157`):
```python
    from Orchestrator.onboarding.custom_servers import classify_models
    modalities = classify_models(ids)
    capabilities = sorted(set(modalities.values()))
    return {"model_count": len(ids), "models": ids[:50],
            "model_modalities": modalities, "capabilities": capabilities}
```

### Task 2.2: persist confirmed modalities on add/validate

**Test:** extend `test_validate_modalities.py` — POST `/custom-servers` with `model_modalities` persists it; the `/validate` success stamp (`onboarding_routes.py:504`) writes `model_modalities` when the client didn't supply a corrected map (seed as default).

**Implement:**
- `CustomServerCreate` (`onboarding_routes.py:326`): add `model_modalities: Optional[dict] = None`.
- `add_custom_server` (`:347`): pass `model_modalities=req.model_modalities` to `add_server`.
- `/validate` success stamp (`:504-513`): also patch `model_modalities` from the detected seed (only when the server has none yet, so a user correction isn't clobbered by re-validation).

**Commit:** `feat(ingest): /validate detects + returns per-model modality seed; persist confirmed map`

---

## Phase 3 — Wizard confirm UI (`api_keys.js`, single web surface)

### Task 3.1: render the detected modalities for confirmation

**Files:** `Portal/onboarding/steps/api_keys.js` (custom-servers "Validate & Add" flow); bump `?v=genui###`.

**Behavior:**
- After "Validate" returns `{models, model_modalities, capabilities}`, render a compact list: one row per model = `<model id>  [modality <select>]`, the select pre-set from `model_modalities` (options: Chat / Image / Text-to-speech / Speech-to-text / Embedding (not used yet) / Ignore).
- A one-line summary: "Detected: 3 chat, 1 image, 1 speech — everything below will be set up automatically. Adjust any that look wrong."
- "Add server" POSTs `/custom-servers` with the (possibly user-edited) `model_modalities`.
- Zero-friction default: the selects are pre-filled, so the user can just click Add.

**Test:** a lightweight DOM/logic test if the wizard has a JS test harness; otherwise a manual verification step in Phase 8 (the backend contract is covered by Phase 2 tests). Note: Android needs NO change — it loads this wizard in its WebView.

**Commit:** `feat(ingest): wizard shows + confirms detected model modalities (accept-all default)`

---

## Phase 4 — Chat registrar (generalize the filter)

### Task 4.1: chat catalog shows only `modality == "chat"`

**Test:** extend `test_custom_models_chat_filter.py` — a server with `{gemma-31b:chat, z-image:image, whisper-1:stt, bge-m3:embedding}` yields a chat catalog of `[gemma-31b]` only.

**Implement** in `admin_routes.py:_fetch_custom_models` (`:805`): replace the `is_image_model` skip with
```python
        for model_id, status in entries:
            if custom_servers.model_modality(srv, model_id) != "chat":
                continue  # image/tts/stt/embedding models live in their own surfaces
            models.append({...})
```
(`srv` is the server record already in scope in the loop.)

**Commit:** `fix(ingest): chat catalog = modality==chat (generalizes the z-image filter)`

---

## Phase 5 — Image registrar onto the unified resolver

### Task 5.1: image reads `model_modality`, name-pattern as fallback

**Test:** existing image tests stay green; add one where a server's `model_modalities` overrides a name (e.g. a model named "z-image" force-tagged `chat` disappears from image; a blandly-named model tagged `image` appears).

**Implement:**
- `image_providers.py:_local_images` already calls `resolve_image_server` → now the unified wrapper. No change beyond Phase 1.
- `availability.py:_local_image_available` (`:92`): lazy-import `has_modality_model` and return `has_modality_model("image")` (keep the try/except fail-soft).

**Commit:** `refactor(ingest): image provider uses the unified modality resolver`

---

## Phase 6 — TTS registrar (`/v1/audio/speech`)

### Task 6.1: local TTS resolver + voices

**Implement** in `custom_servers.py`: `resolve_tts_server = lambda model=None: resolve_modality_server("tts", model)` (or a def). Add `list_local_tts_voices(server) -> list[str]`: probe `GET {base_url}/audio/voices` (5s, fail-soft); if absent/empty, return `["default"]`.

**Test:** `test_local_tts.py` — resolver picks a tts model; voices probe parsed; fallback `["default"]` on 404.

### Task 6.2: catalog group + dispatch branch

**Test:** `build_tts_catalog` includes a `"local"` group ONLY when a local tts model exists; `tts_batch` routes `provider=="local"` to the OpenAI body with registry base_url/key.

**Implement:**
- `config.py:build_tts_catalog` (`:658`): append a dynamic `local` group (voices from `list_local_tts_voices`) when `has_modality_model("tts")` — mirror how the endpoint appends the elevenlabs group; do it in the builder or at the `GET /tts/catalog` endpoint (`tts_routes.py:967`) if a build-time import cycle bites.
- `tts_routes.py:tts_batch` (`:359`, before the `else: raise`): add
```python
        elif provider == "local":
            from Orchestrator.onboarding.custom_servers import resolve_tts_server
            resolved = resolve_tts_server(effective_model or None)
            if not resolved:
                raise HTTPException(400, "No local text-to-speech model available")
            _srv, _model = resolved
            def _local_tts_chunk(chunk_text: str) -> bytes:
                req = {"model": _model, "input": chunk_text, "voice": voice,
                       "response_format": audio_format}
                headers = {"Content-Type": "application/json"}
                if _srv.get("api_key"):
                    headers["Authorization"] = f"Bearer {_srv['api_key']}"
                r = requests.post(f"{_srv['base_url']}/audio/speech", json=req,
                                  headers=headers, timeout=TTS_TIMEOUT / 1000.0)
                r.raise_for_status()
                return r.content
            _tts_chunk = _local_tts_chunk  # slot into the same synth loop
```
(Match the exact variable the existing chain assigns — the reviewer/implementer aligns on the quoted `_openai_tts_chunk` symbol.)

**Commit:** `feat(ingest): local TTS provider (/v1/audio/speech) auto-registered`

---

## Phase 7 — STT registrar (`/v1/audio/transcriptions`, file-only)

### Task 7.1: local STT resolver + availability

**Implement** in `custom_servers.py`: `resolve_stt_server = resolve_modality_server("stt", ...)`.
- `stt/resolve.py:stt_availability` (`:6`): add a 4th flag `local_ok = has_modality_model("stt")` (lazy import).
- `stt/resolve.py:resolve_stt_provider` (`:50`): add `"local"` to the avail dict + candidate set.

**Test:** `test_local_stt.py` — availability true when a stt model exists; `resolve_stt_provider("local")` returns "local" when available.

### Task 7.2: catalog + dispatch branch

**Test:** `build_stt_catalog` lists `local` when available; `transcribe_bytes(provider="local")` POSTs multipart to `{base_url}/audio/transcriptions` and returns text.

**Implement:**
- `stt/catalog.py:build_stt_catalog` (`:13`): include `local` when available.
- `stt/file_transcribe.py:transcribe_bytes` (`:33`): add
```python
    if provider == "local":
        from Orchestrator.onboarding.custom_servers import resolve_stt_server
        resolved = resolve_stt_server()
        if not resolved:
            raise RuntimeError("No local speech-to-text model available")
        srv, model = resolved
        files = {"file": (filename, audio_bytes, content_type)}
        headers = {}
        if srv.get("api_key"):
            headers["Authorization"] = f"Bearer {srv['api_key']}"
        r = requests.post(f"{srv['base_url']}/audio/transcriptions",
                          headers=headers, data={"model": model}, files=files, timeout=120)
        r.raise_for_status()
        return (r.json().get("text") or "").strip()
```
(Insert as the first branch; leave the existing openai/google/elevenlabs chain intact. Streaming `/ws/stt` is explicitly NOT wired for local in v1 — document in the catalog payload that local is file-only.)

**Commit:** `feat(ingest): local STT provider (/v1/audio/transcriptions, file-transcribe) auto-registered`

---

## Phase 8 — Regression, live validation, snapshot

### Task 8.1: full backend regression
`Orchestrator/venv/bin/python -m pytest Orchestrator/tests/ -q` → 0 failures. ToolVault validator exit 0.

### Task 8.2: live end-to-end (on `gemma-box`, which today hosts chat + z-image)
1. Restart; `POST /onboarding/validate {provider:custom, base_url, api_key}` → response carries `model_modalities` (gemma-*→chat, z-image→image) + `capabilities`.
2. Re-add / patch the server so `model_modalities` persists; confirm `GET /models/custom` = chat models only, `GET /image/catalog` still shows `local`.
3. If a local TTS or STT model is available on any server, smoke `/tts/catalog` + a `tts_batch(provider=local)` and `/stt/catalog` + a file `transcribe_bytes(provider=local)`. (If gemma-box has none, note it — the paths are unit-tested; live TTS/STT smoke is gated on Brandon having such a model.)
4. Wizard: register a server, confirm the detected-modality UI renders + accept-all works.

### Task 8.3: final holistic review + snapshot
Dispatch a code-review subagent (fresh-box portability, MCP-lean-venv fail-soft, no silent misroute, back-compat for pre-existing servers). Then `/snapshot-dev` (operator resolved dynamically) documenting the framework, hook points, and live proof.

---

## Definition of Done
- [ ] Register a server → its models are auto-classified, confirmable in the wizard, and persisted.
- [ ] Chat / image / TTS / STT models each route to their own subsystem with no manual per-modality step.
- [ ] Pre-existing servers (gemma-box) keep working via name-pattern fallback.
- [ ] Full regression 0 failures; validator exit 0; live-validated on gemma-box (chat + image at minimum).
- [ ] Snapshot minted.

## Fast-follows (explicitly out of v1)
Embeddings routing (dims-probe + dynamic registry entry + corpus re-embed), music vertical, agent/CLI, live streaming STT, zero-cost endpoint-capability `OPTIONS` probe.
