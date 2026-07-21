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
