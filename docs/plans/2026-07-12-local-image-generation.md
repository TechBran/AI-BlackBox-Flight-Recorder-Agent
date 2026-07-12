# Local Image Generation (Z-Image Turbo) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to execute this plan task-by-task (fresh subagent per task, spec review then quality review between tasks). Work on `main` per the staging-box-as-production convention ([[feedback-staging-box-as-production]]) — no test branch.

**Goal:** Wire the local llama-swap image server (Z-Image Turbo, OpenAI-compatible at `http://192.168.1.50:8080/v1`, credentials already in `credentials/custom_models.json` as `gemma-box`) into the BlackBox image pipeline as a FREE provider `local` / tool `local_image`, reusing the custom-server registry for credentials.

**Architecture:** `provider` is a free-string passthrough (`GenIn` → `/generate/image` → worker `IMAGE_PROVIDERS.get(provider)`); both frontends render whatever `GET /image/catalog` advertises. So the work is a new adapter + catalog entry + registry-gated availability + a ToolVault tool, plus fixing the chat-catalog leak of `z-image`. Parity params (`size`, `numberOfImages`) mean **no request-path and no frontend code changes**.

**Tech Stack:** FastAPI (Orchestrator, :9091), Python 3.12, `requests` (sync image adapters), ToolVault v2 modules, pytest. Design doc: `docs/plans/2026-07-12-local-image-generation-design.md`.

**Validated decisions:** (1) classification = name-pattern allowlist; (2) naming = `local_image` / provider `local` / label "Local (free)"; (3) params = parity only (`size` + `numberOfImages`).

**Reference anchors (verified this session):**
- `Orchestrator/image_providers.py:22-70` — adapter pattern `(prompt, options) -> list[bytes]`, `requests.post(..., timeout=180)`, `IMAGE_PROVIDERS` / `DEFAULT_IMAGE_PROVIDER` / `IMAGE_TOOL_PROVIDERS`.
- `Orchestrator/image_catalog.py:20-47` — `IMAGE_PROVIDER_SPECS` + hardcoded order list `["gemini","openai","grok"]`.
- `Orchestrator/toolvault/availability.py:35-48,91-128` — `FEATURES["image"]`, `enabled_providers()`, `is_available()`.
- `Orchestrator/onboarding/custom_servers.py:179-320` — `list_servers()`, `resolve_model()`, `qualify()`, `SEP="::"`.
- `Orchestrator/tasks.py:822-871` — `process_image_generation`, provider dispatch, `_IMAGE_MODELS` provenance.
- `Orchestrator/routes/admin_routes.py:721-818` — `_fetch_custom_models` (chat catalog merge; append loop at 804-810).
- `Orchestrator/routes/tts_routes.py:675-726` — `/generate/image` (size copied through at 695) + `/image/catalog`. **No change needed.**
- `Orchestrator/startup.py:827-843` — `GenIn` (has `size`, `numberOfImages`, `provider`). **No change needed.**
- `Orchestrator/tests/test_image_catalog.py` — coherence contract + `_ADAPTER_PROBES`.
- `ToolVault/tools/openai_image/{schema.json,executor.py}` — tool template.

---

## Phase 1 — Model classifier + image-server resolver (`custom_servers.py`)

Foundation: one place that decides "is this bare model id a text-to-image model?" and resolves which custom server + model to hit. Consumed by the adapter, the availability gate, and the chat-catalog filter.

### Task 1.1: `is_image_model()` classifier

**Files:**
- Modify: `Orchestrator/onboarding/custom_servers.py` (add after the `resolve_model` function, ~line 320)
- Test: `Orchestrator/tests/test_custom_servers_image.py` (create)

**Step 1 — Write the failing test:**
```python
# Orchestrator/tests/test_custom_servers_image.py
"""Local-image classification + resolution over the custom-server registry."""
from Orchestrator.onboarding import custom_servers as cs


def test_is_image_model_matches_known_families():
    for mid in ["z-image", "Z-Image", "flux.2-klein-4b", "qwen-image",
                "sdxl-turbo", "sd3-medium", "stable-diffusion-xl", "my-cool-image"]:
        assert cs.is_image_model(mid) is True, mid


def test_is_image_model_rejects_chat_models():
    for mid in ["gemma-12b", "gemma-26b", "gemma-31b", "qwen3-8b",
                "llama-3.3-70b", "mistral-small"]:
        assert cs.is_image_model(mid) is False, mid


def test_is_image_model_non_string():
    assert cs.is_image_model(None) is False
    assert cs.is_image_model(123) is False
```

**Step 2 — Run, expect FAIL** (`AttributeError: module ... has no attribute 'is_image_model'`):
`.venv/bin/python -m pytest Orchestrator/tests/test_custom_servers_image.py -x -q`

**Step 3 — Implement** (append to `custom_servers.py`):
```python
# ------------------------------------------------------------- image models
# Name-pattern allowlist (Brandon-approved v1 classification). A model served on
# an OpenAI-compatible endpoint carries no modality flag, so we classify by id.
# EDIT HERE to teach the box a new local text-to-image family.
IMAGE_MODEL_PATTERNS = (
    "z-image", "zimage", "flux", "qwen-image", "sdxl", "sd3", "sd-turbo",
    "stable-diffusion", "playground-v", "kolors", "hidream", "pixart",
)


def is_image_model(model_id: object) -> bool:
    """Heuristic: does this bare model id name a text-to-image model?

    Used to (a) gate/route the local image provider and (b) keep image models
    out of the CHAT model catalog. Name-pattern allowlist + an ``*-image`` /
    ``*_image`` suffix fallback. Chat models (gemma-*, llama-*, qwen3-*) do not
    match. Deliberately conservative on false positives is impossible without a
    server capability flag; misclassification risk is accepted (design doc).
    """
    if not isinstance(model_id, str):
        return False
    m = model_id.lower()
    if any(p in m for p in IMAGE_MODEL_PATTERNS):
        return True
    return m.endswith("-image") or m.endswith("_image")
```

**Step 4 — Run, expect PASS.**

**Step 5 — Commit:** `git add Orchestrator/onboarding/custom_servers.py Orchestrator/tests/test_custom_servers_image.py && git commit -m "feat(image): name-pattern classifier for local image models"`

### Task 1.2: `resolve_image_server()` + `list_image_models()`

**Files:**
- Modify: `Orchestrator/onboarding/custom_servers.py` (after `is_image_model`)
- Test: `Orchestrator/tests/test_custom_servers_image.py`

**Step 1 — Add failing tests** (monkeypatch `list_servers` so no real registry/network):
```python
def _fake_servers(monkeypatch, servers):
    monkeypatch.setattr(cs, "list_servers", lambda enabled_only=False: list(servers))


def test_resolve_image_server_picks_first_image_model(monkeypatch):
    _fake_servers(monkeypatch, [
        {"alias": "box", "base_url": "http://h/v1", "api_key": "k",
         "enabled": True, "last_models": ["gemma-31b", "z-image"]},
    ])
    srv, model = cs.resolve_image_server()
    assert srv["base_url"] == "http://h/v1" and model == "z-image"


def test_resolve_image_server_none_when_no_image_model(monkeypatch):
    _fake_servers(monkeypatch, [
        {"alias": "box", "base_url": "http://h/v1", "enabled": True,
         "last_models": ["gemma-31b", "gemma-12b"]},
    ])
    assert cs.resolve_image_server() is None


def test_list_image_models_qualified(monkeypatch):
    _fake_servers(monkeypatch, [
        {"alias": "box", "base_url": "http://h/v1", "enabled": True,
         "last_models": ["gemma-31b", "z-image", "flux.2-klein-4b"]},
    ])
    assert cs.list_image_models() == ["box::z-image", "box::flux.2-klein-4b"]
```

**Step 2 — Run, expect FAIL.**

**Step 3 — Implement** (append):
```python
def resolve_image_server(model: str | None = None) -> tuple[dict, str] | None:
    """Pick the custom (server, bare_model) for a local image request.

    ``model`` (optional, may be alias-qualified) is honored when it resolves to
    a real enabled server AND is an image model. Otherwise: the first enabled
    server that hosts a name-matched image model, and its first such model.
    Returns ``(server_dict, bare_model)`` or ``None`` when no local image model
    is available. Reads the registry fresh (no import-time cache)."""
    if model:
        srv, bare = resolve_model(model)
        if srv is not None and is_image_model(bare):
            return srv, bare
    for srv in list_servers(enabled_only=True):
        for m in (srv.get("last_models") or []):
            if isinstance(m, str) and is_image_model(m):
                return srv, m
    return None


def list_image_models() -> list[str]:
    """Alias-qualified ids ('alias::model') of every image model on every enabled
    server — the future source for a local-model picker; today used by tests."""
    out = []
    for srv in list_servers(enabled_only=True):
        alias = srv.get("alias", "")
        for m in (srv.get("last_models") or []):
            if isinstance(m, str) and is_image_model(m):
                out.append(qualify(alias, m))
    return out
```

**Step 4 — Run, expect PASS. Step 5 — Commit:** `feat(image): resolve_image_server + list_image_models over custom registry`

---

## Phase 2 — Local image adapter (`image_providers.py`)

### Task 2.1: `_local_images()` adapter + registration

**Files:**
- Modify: `Orchestrator/image_providers.py`
- Test: `Orchestrator/tests/test_image_providers_local.py` (create)

**Step 1 — Failing test:**
```python
# Orchestrator/tests/test_image_providers_local.py
import base64
import pytest
from Orchestrator import image_providers
from Orchestrator.onboarding import custom_servers


class _Resp:
    def __init__(self, data): self._d = {"data": data}
    def raise_for_status(self): pass
    def json(self): return self._d


def test_local_adapter_posts_openai_images_shape(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return _Resp([{"b64_json": base64.b64encode(b"PNG").decode()}])

    monkeypatch.setattr(custom_servers, "resolve_image_server",
                        lambda model=None: ({"base_url": "http://h/v1", "api_key": "k"}, "z-image"))
    monkeypatch.setattr(image_providers.requests, "post", fake_post)

    out = image_providers._local_images("a fox", {"size": "768x768", "numberOfImages": 2})
    assert out == [b"PNG"]
    assert captured["url"] == "http://h/v1/images/generations"
    assert captured["json"] == {"model": "z-image", "prompt": "a fox",
                                "n": 2, "output_format": "png", "size": "768x768"}
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["timeout"] == 180


def test_local_adapter_raises_when_no_server(monkeypatch):
    monkeypatch.setattr(custom_servers, "resolve_image_server", lambda model=None: None)
    with pytest.raises(RuntimeError):
        image_providers._local_images("x", {})


def test_local_registered():
    assert image_providers.IMAGE_PROVIDERS.get("local") is image_providers._local_images
    assert image_providers.IMAGE_TOOL_PROVIDERS.get("local_image") == "local"
```

**Step 2 — Run, expect FAIL.**

**Step 3 — Implement** — insert `_local_images` after `_gemini_images` (before the `IMAGE_PROVIDERS` dict, ~line 64) and extend the two registry dicts:
```python
def _local_images(prompt, options):
    """FREE local text-to-image via a registered OpenAI-compatible LAN server
    (Z-Image Turbo / stable-diffusion.cpp). Credentials come from the custom-
    server registry (custom_models.json) — never hardcoded. 180s timeout absorbs
    a cold llama-swap swap (~35s); the server queues one request at a time."""
    from Orchestrator.onboarding.custom_servers import resolve_image_server  # lazy: no import-time cost / cycle
    resolved = resolve_image_server(options.get("model"))
    if not resolved:
        raise RuntimeError(
            "No local image model available — add an OpenAI-compatible server "
            "hosting an image model (e.g. z-image) in the onboarding wizard.")
    srv, model = resolved
    n = int(options.get("numberOfImages") or options.get("n") or 1)
    body = {"model": model, "prompt": prompt, "n": n, "output_format": "png"}
    if options.get("size"):
        body["size"] = options["size"]
    headers = {"Content-Type": "application/json"}
    api_key = srv.get("api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = requests.post(f"{srv['base_url']}/images/generations",
                      headers=headers, json=body, timeout=180)
    r.raise_for_status()
    return [base64.b64decode(d["b64_json"]) for d in r.json().get("data", []) if d.get("b64_json")]
```
Then update:
```python
IMAGE_PROVIDERS = {"gemini": _gemini_images, "openai": _openai_images,
                   "grok": _xai_images, "local": _local_images}
DEFAULT_IMAGE_PROVIDER = "gemini"
IMAGE_TOOL_PROVIDERS = {"gemini_image": "gemini", "openai_image": "openai",
                        "grok_image": "grok", "local_image": "local"}
```

**Step 4 — Run, expect PASS. Step 5 — Commit:** `feat(image): local Z-Image adapter (credentials from custom registry, 180s timeout)`

---

## Phase 3 — Availability gating (`availability.py`)

### Task 3.1: registry-gated `local` in `enabled_providers("image")`

**Files:**
- Modify: `Orchestrator/toolvault/availability.py`
- Test: `Orchestrator/tests/test_availability_local_image.py` (create)

**Step 1 — Failing test:**
```python
# Orchestrator/tests/test_availability_local_image.py
from Orchestrator.toolvault import availability as av


def test_local_enabled_when_registry_has_image_model(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {})           # no cloud keys
    monkeypatch.setattr(av, "_local_image_available", lambda: True)
    assert "local" in av.enabled_providers("image")


def test_local_absent_when_registry_empty(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {"GOOGLE_API_KEY": "g"})
    monkeypatch.setattr(av, "_local_image_available", lambda: False)
    enabled = av.enabled_providers("image")
    assert "local" not in enabled and "gemini" in enabled


def test_local_image_tool_available(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {})
    monkeypatch.setattr(av, "_local_image_available", lambda: True)
    entry = {"x-availability": {"feature": "image", "provider": "local"}}
    assert av.is_available(entry) is True


def test_local_image_available_is_failsoft(monkeypatch):
    # A broken import inside _local_image_available must return False, never raise.
    import Orchestrator.onboarding.custom_servers as cs
    monkeypatch.setattr(cs, "list_servers", lambda enabled_only=False: (_ for _ in ()).throw(RuntimeError("boom")))
    assert av._local_image_available() is False
```

**Step 2 — Run, expect FAIL.**

**Step 3 — Implement:**

(a) Add `local` to `FEATURES["image"]` maps (`availability.py:38-43`):
```python
        "provider_env": {
            "gemini": "GOOGLE_API_KEY", "openai": "OPENAI_API_KEY", "grok": "XAI_API_KEY",
            "local": None,   # registry-gated (no API-key env) — see _local_image_available
        },
        "provider_tool": {
            "gemini": "gemini_image", "openai": "openai_image", "grok": "grok_image",
            "local": "local_image",
        },
```

(b) Add the fail-soft helper (after `_read_env`, ~line 89):
```python
def _local_image_available() -> bool:
    """True iff an enabled custom server hosts a name-matched image model.

    Lazy import + fail-soft: a heavy/absent dep in the lean MCP venv must NOT
    raise here (it would break enabled_providers for EVERY tool). Any failure ->
    False (local image simply off in that context)."""
    try:
        from Orchestrator.onboarding.custom_servers import list_servers, is_image_model
        for srv in list_servers(enabled_only=True):
            for m in (srv.get("last_models") or []):
                if isinstance(m, str) and is_image_model(m):
                    return True
    except Exception:
        return False
    return False
```

(c) Wire into `enabled_providers` (before the final `return enabled`, ~line 106):
```python
    # Registry-gated providers (no env key): the local image server is enabled
    # iff a registered+enabled custom server actually hosts an image model.
    if feature == "image" and _local_image_available():
        enabled.add("local")
    return enabled
```

**Step 4 — Run, expect PASS. Step 5 — Commit:** `feat(image): registry-gated 'local' provider in availability`

---

## Phase 4 — Catalog entry + coherence probe (`image_catalog.py`, `test_image_catalog.py`)

### Task 4.1: advertise `local` in the catalog + satisfy the coherence lock

**Files:**
- Modify: `Orchestrator/image_catalog.py`
- Modify: `Orchestrator/tests/test_image_catalog.py`

**Step 1 — Make the existing catalog tests hermetic** (they read the real registry, which now has `z-image`). In `test_image_catalog.py`, extend the shared `_patch_env` helper (~line 23) to also force local OFF by default:
```python
def _patch_env(monkeypatch, env: dict):
    from Orchestrator.toolvault import availability
    monkeypatch.setattr(availability, "_read_env", lambda: dict(env))
    # Registry-gated 'local' must not leak the real gemma-box/z-image into these
    # exact-provider-list assertions; tests that WANT local patch it back to True.
    monkeypatch.setattr(availability, "_local_image_available", lambda: False)
```

**Step 2 — Add a `local` adapter probe + spec test.** Add capture fn (after `_capture_gemini`, ~line 148):
```python
def _capture_local(monkeypatch, options):
    captured = {}
    from Orchestrator.onboarding import custom_servers

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        captured["json"] = json
        return _FakeResp(json_data={"data": [{"b64_json": base64.b64encode(b"X").decode()}]})

    monkeypatch.setattr(custom_servers, "resolve_image_server",
                        lambda model=None: ({"base_url": "http://h/v1", "api_key": "k"}, "z-image"))
    monkeypatch.setattr(image_providers.requests, "post", fake_post)
    image_providers._local_images("a fox", options)
    return captured["json"]
```
Add to `_ADAPTER_PROBES` (~line 153):
```python
    "local": {
        "capture": _capture_local,
        "checks": {
            "size": ("768x768", lambda b, v: b.get("size") == v),
            "numberOfImages": (2, lambda b, v: b.get("n") == v),
        },
    },
```
Add a focused catalog test:
```python
def test_local_in_catalog_when_available(monkeypatch):
    from Orchestrator.toolvault import availability
    monkeypatch.setattr(availability, "_read_env", lambda: {})
    monkeypatch.setattr(availability, "_local_image_available", lambda: True)
    cat = {p["provider"]: p for p in build_image_catalog()}
    assert "local" in cat and cat["local"]["label"] == "Local (free)"
    assert {p["name"] for p in cat["local"]["params"]} == {"size", "numberOfImages"}
```

**Step 3 — Run, expect FAIL** (`KeyError: 'local'` in specs / catalog missing local).

**Step 4 — Implement** in `image_catalog.py`: add the spec (in `IMAGE_PROVIDER_SPECS`, after `grok`, ~line 31):
```python
    "local": {"label": "Local (free)", "params": [
        {"name": "size", "type": "enum",
         "options": ["1024x1024", "768x768", "1024x768", "768x1024"], "default": "1024x1024"},
        {"name": "numberOfImages", "type": "int", "min": 1, "max": 4, "default": 1}]},
```
and add `"local"` to the display-order list (`image_catalog.py:41`):
```python
    for prov in ["gemini", "openai", "grok", "local"]:        # stable display order
```

**Step 5 — Run FULL image-catalog suite, expect PASS:**
`.venv/bin/python -m pytest Orchestrator/tests/test_image_catalog.py -q`

**Step 6 — Commit:** `feat(image): advertise 'Local (free)' in /image/catalog + coherence probe`

---

## Phase 5 — Worker provenance (`tasks.py`)

### Task 5.1: correct model + metadata for `local` images

**Files:** Modify `Orchestrator/tasks.py`; Test: extend `Orchestrator/tests/test_image_providers_local.py` (or a worker test if one exists).

**Step 1 — Implement** (`tasks.py`): add `local` to `_IMAGE_MODELS` (~line 845):
```python
    _IMAGE_MODELS = {
        "gemini": GOOGLE_IMAGEN_MODEL,
        "openai": OPENAI_IMAGE_MODEL,
        "grok": XAI_IMAGE_MODEL,
        "local": "z-image",   # v1 default local model; provenance best-effort
    }
```
and add a `local` metadata branch (before the `else:` gemini branch, ~line 865):
```python
    elif provider == "local":
        image_metadata = {
            "size": options.get("size", "1024x1024"),
            "numberOfImages": _num_images,
            "model": recorded_model,
        }
```

**Step 2 — Verify** no regression in the worker path via a routing test that asserts a `local`-tagged task calls `_local_images` and records `model == "z-image"` (mock `_local_images` to return `[b"x"]`, mock `UPLOADS_DIR`/`add_media_entry`). If a `test_image_providers.py` routing harness exists, mirror its style; otherwise a minimal monkeypatched test.

**Step 3 — Commit:** `feat(image): local-provider provenance (model=z-image, size metadata)`

---

## Phase 6 — ToolVault `local_image` tool

### Task 6.1: schema.json + executor.py

**Files:**
- Create: `ToolVault/tools/local_image/schema.json`
- Create: `ToolVault/tools/local_image/executor.py`

**Step 1 — `schema.json`** (description LEADS with the free/local/private steering signal):
```json
{
  "name": "local_image",
  "description": "Generate images for FREE on your own local GPU (Z-Image Turbo via stable-diffusion.cpp on the LAN inference box). No API cost, no rate limits, fully private — nothing leaves your network. Photorealistic, strong at in-image text (English + Chinese). Slower than cloud (~20-35s/image; the first image after idle warms up the model). Prefer this for casual, bulk, or privacy-sensitive image generation where cost matters more than latency; use a cloud image tool when speed is critical. Returns a task_id that completes asynchronously.",
  "category": "media_generation",
  "groups": ["chat", "chat_cu", "realtime", "gemini_live", "grok_live", "phone", "mcp"],
  "tier": 2,
  "x-availability": {
    "feature": "image",
    "provider": "local"
  },
  "parameters": {
    "type": "object",
    "properties": {
      "prompt": { "type": "string", "description": "Detailed description of the image. Be specific about subjects, style, composition, lighting, colors, and mood. English or Chinese; in-image text is supported." },
      "size": { "type": "string", "description": "Image size. '1024x1024' (native/best), '768x768' (faster), '1024x768' (landscape), '768x1024' (portrait). Default '1024x1024'.", "enum": ["1024x1024", "768x768", "1024x768", "768x1024"], "default": "1024x1024" },
      "numberOfImages": { "type": "integer", "description": "Number of images (1-4). Generated sequentially on one GPU, so keep small. Default 1.", "minimum": 1, "maximum": 4, "default": 1 }
    },
    "required": ["prompt"]
  },
  "returns": "task_id (string) for async status tracking via get_task_status",
  "example": "local_image(prompt=\"a red fox in a snowy forest, golden hour, photorealistic\", size=\"1024x1024\")",
  "notes": "Async, free, on-device. No API key — availability is gated on a registered custom server hosting an image model (e.g. z-image). Cold start ~35s; warm ~20-25s."
}
```

**Step 2 — `executor.py`** (clone of `openai_image/executor.py`, provider `local`, forward `size`+`numberOfImages`):
```python
"""Executor for local_image (FREE local Z-Image via a registered custom server)."""
import aiohttp

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Queue a local-provider image generation task via /generate/image."""
    prompt = params.get("prompt", "")
    if not prompt:
        return ToolResult(False, "Image prompt is required")
    try:
        payload = {"prompt": prompt, "operator": ctx.operator, "provider": "local"}
        for k in ("size", "numberOfImages"):
            if params.get(k) is not None:
                payload[k] = params[k]
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ctx.base_url}/generate/image",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    task_id = result.get("task_id", "")
                    if task_id:
                        return ToolResult(
                            success=True,
                            result=f"Image generation started (local, free). Task ID: {task_id}. The image will be available shortly.",
                            data={"task_id": task_id},
                        )
                    return ToolResult(True, f"Image generated: {result.get('url','')}", data={"url": result.get("url", "")})
                error_text = await resp.text()
                return ToolResult(False, f"Image generation failed: {resp.status} - {error_text}")
    except Exception as e:
        return ToolResult(False, f"Image generation error: {e}")
```
> Note: the executor's 120 s timeout is only the enqueue hop (`/generate/image` returns a `task_id` immediately); the real 180 s cold-swap timeout lives in the `_local_images` adapter.

**Step 3 — Validate the module (CI gate):**
`.venv/bin/python -m Orchestrator.toolvault.validate` → expect exit 0, `local_image` listed valid.

**Step 4 — Make it live (re-embed + bust caches, no restart):**
`curl -s -X POST http://localhost:9091/toolvault/reload | python3 -m json.tool`

**Step 5 — Verify injection gating:** with the `gemma-box` server enabled (z-image present), confirm `local_image` is available; conceptually, disabling the server removes it. Spot-check: `curl -s http://localhost:9091/image/catalog | python3 -m json.tool` shows a `local` provider labeled "Local (free)".

**Step 6 — Commit:** `git add ToolVault/tools/local_image/ && git commit -m "feat(image): local_image ToolVault tool (free/local/private description)"`

---

## Phase 7 — Dispatch recognition sites (image_task placeholder animation)

The chat/voice/CU/phone dispatchers recognize the *specific* image tool names to emit the `image_task` placeholder event that drives the Portal/Android generation animation ([[project-multi-provider-image-gen]] lesson #4). `local_image` executes via the catch-all regardless, but must be added to these sets so the animation fires.

### Task 7.1: grep + extend every image-tool enumeration

**Step 1 — Discover** (implementer runs these):
```bash
grep -rn "IMAGE_TOOL_PROVIDERS\|openai_image\|grok_image\|gemini_image" \
  Orchestrator --include=*.py | grep -v test
```
**Step 2 — For each site that enumerates the image tool NAMES** (chat streamers, `realtime_routes.py`, `gemini_live_routes.py`, `grok_live_routes.py`, `driver_anthropic.py`, phone/bridge `_execute_tool`), add `local_image`/`local`. Prefer deriving from `IMAGE_TOOL_PROVIDERS` (already extended in Phase 2) where a site imports it; only hardcode where the existing code hardcodes the three names.

**Step 3 — Verify** no site still lists exactly `{gemini_image, openai_image, grok_image}` for image recognition:
```bash
grep -rn "gemini_image.*openai_image.*grok_image\|openai_image.*grok_image" Orchestrator --include=*.py | grep -v test
```

**Step 4 — Commit:** `fix(image): recognize local_image at every image-task dispatch site`

---

## Phase 8 — Chat-catalog leak fix (`admin_routes.py`)

Stop `gemma-box::z-image` (and any image model) from appearing in the CHAT model dropdown. Keep it in the registry's `last_models` (the image subsystem reads it) — filter only the chat-catalog OUTPUT.

### Task 8.1: filter image models out of `_fetch_custom_models`

**Files:** Modify `Orchestrator/routes/admin_routes.py`; Test: `Orchestrator/tests/test_custom_models_chat_filter.py` (create) or extend an existing admin/models test.

**Step 1 — Failing test** (monkeypatch `list_servers` + `httpx.get` so `z-image` is returned by the probe, assert it's filtered from the chat catalog but a chat model survives):
```python
def test_zimage_excluded_from_chat_catalog(monkeypatch):
    from Orchestrator.routes import admin_routes
    from Orchestrator.onboarding import custom_servers
    monkeypatch.setattr(custom_servers, "list_servers",
        lambda enabled_only=False: [{"id": "s1", "alias": "box",
            "base_url": "http://h/v1", "api_key": "", "last_models": ["gemma-31b", "z-image"]}])

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"id": "gemma-31b"}, {"id": "z-image"}]}
    monkeypatch.setattr(admin_routes.httpx, "get", lambda *a, **k: _R())

    out = admin_routes._fetch_custom_models()
    ids = [m["id"] for m in out["models"]]
    assert "box::gemma-31b" in ids
    assert "box::z-image" not in ids
```
> Adjust the envelope key (`out["models"]`) to match `_wrap`'s actual shape when writing the test.

**Step 2 — Run, expect FAIL** (`z-image` present).

**Step 3 — Implement** — in the append loop (`admin_routes.py:804-810`), skip image models:
```python
        for model_id, status in entries:
            if custom_servers.is_image_model(model_id):
                continue  # image models live on the generation screen, not the chat picker
            models.append({
                "id": custom_servers.qualify(alias, model_id),
                "name": f"{model_id} ({alias})",
                "server": alias,
                "status": status,
            })
```
(`default_id = models[0]["id"]` at line 816 now correctly skips image models.)

**Step 4 — Run, expect PASS. Step 5 — Live check:** `curl -s http://localhost:9091/models/custom | python3 -m json.tool` → no `gemma-box::z-image`.

**Step 6 — Commit:** `fix(chat): keep image models (z-image) out of the custom chat catalog`

---

## Phase 9 — (OPTIONAL) Frontend warm-status polish

Deferred by default (parity-params v1 needs no UI change). If desired later: surface the local model's llama-swap `status` (loaded/unloaded, already in `/models/custom`) as a warm-dot on the "Local (free)" option in both generation screens, plus a "first image after idle ~35 s" hint. Requires threading a `status`/`warm` field into `/image/catalog` for the local provider. **Not required for a working, shippable v1** — leave unchecked unless Brandon asks.

---

## Phase 10 — Regression, live end-to-end, snapshot

### Task 10.1: full backend regression
`.venv/bin/python -m pytest Orchestrator/tests/ -q` → expect **0 failures** (baseline: voice-pass left 3058/0). Investigate any red before proceeding.

### Task 10.2: ToolVault validator + reload sanity
`.venv/bin/python -m Orchestrator.toolvault.validate` (exit 0) and `GET /toolvault/health`.

### Task 10.3: LIVE end-to-end (the real proof)
1. Restart if needed: `sudo systemctl restart blackbox.service` (wait ~60-90 s).
2. `curl -s http://localhost:9091/image/catalog` → `local` present, label "Local (free)", params `[size, numberOfImages]`.
3. Enqueue via the real route:
   ```bash
   curl -s -X POST http://localhost:9091/generate/image \
     -H "Content-Type: application/json" \
     -d '{"prompt":"a red fox in a snowy forest, golden hour, photorealistic","provider":"local","size":"1024x1024","numberOfImages":1,"operator":"system"}'
   ```
   Poll `get_task_status` → COMPLETED with a `/ui/uploads/…png` URL; open it and confirm a real image.
4. Model-driven: in a chat turn, confirm the model can call `local_image` and the generation animation fires (Phase 7).
5. `curl -s http://localhost:9091/models/custom` → `z-image` absent from chat list.

### Task 10.4: final holistic review
Dispatch a final code-reviewer subagent over the whole diff (superpowers:requesting-code-review): correctness, the MCP lean-venv fail-soft path, no hardcoded `gemma-box`/operator, coherence test green, no image-tool dispatch site missed.

### Task 10.5: snapshot
Invoke `/snapshot-dev` (operator resolved dynamically) documenting: problem, files, the free-string-passthrough architecture, the classifier decision, live-proof, test totals. Mark it RESOLVED against the "z-image is a chat-catalog dead-end" finding.

---

## Definition of Done
- [ ] `local_image` tool live; models can generate for free on the LAN GPU.
- [ ] "Local (free)" appears in Portal + Android generation dropdowns (automatic, zero UI code change).
- [ ] `z-image` no longer appears in the chat model dropdown.
- [ ] Availability is registry-gated + MCP-lean-venv fail-soft.
- [ ] Full backend suite 0 failures; ToolVault validator exit 0.
- [ ] Live end-to-end image generated via `provider:"local"` and via a model tool call.
- [ ] Snapshot minted.
