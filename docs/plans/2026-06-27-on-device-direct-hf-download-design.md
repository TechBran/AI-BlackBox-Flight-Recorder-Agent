# On-Device Gemma Download — Direct-from-Hugging-Face Redesign

**Date:** 2026-06-27
**Status:** Design validated, ready for implementation planning
**Surfaces:** Orchestrator (Python) + Android MVP (Kotlin); Portal/WebView read-only at the catalog level

## Problem

On-device Gemma downloads (E4B / E2B) in the Android MVP get **stuck at 0% or 1% and never complete.**

### Root cause (evidence-backed)

The phone never talks to Hugging Face. It downloads from the hub
(`GET /local/models/download/{slug}`), which proxies bytes through a server-side
mirror. That handler does this **synchronously, before sending any response**:

```python
path = mirror.ensure_present(slug)   # downloads the ENTIRE 2.6–3.7 GB from HF first
return FileResponse(path, ...)
```

`ensure_present` (`Orchestrator/local_provider/mirror.py`) downloads the whole
bundle from HF to the hub and only then hands it to `FileResponse`.

- **E2B → stuck at 0%:** E2B is not mirrored on the hub, so the first request
  triggers a fresh 2.59 GB hub-side fetch. The phone is connected but receives
  zero bytes for the entire fetch. The phone's download client uses
  `readTimeout(0)` (no read timeout), so it waits forever instead of erroring.
- **E4B → stuck at ~1%:** `ensure_present` is a blocking call inside an
  `async def` handler with no `await asyncio.to_thread(...)`, so it freezes
  uvicorn's single event loop for the whole fetch — a concurrent E2B mirror
  freezes an in-flight E4B stream (and catalog/status) mid-transfer.

### What was verified

| Check | Result |
|---|---|
| E4B served to localhost | `200`, **3.3 GB in 8 s (~412 MB/s)**, correct `206`/`Content-Range` — serving is healthy |
| E4B via Tailscale serve proxy (real phone path) | `200`, streams fine but **~18 MB/s** (23× slower than loopback); Range preserved |
| HF repos resolve? | **Yes, both public/ungated.** E4B `200` len `3659530240`; E2B `200` len `2588147712` — not a 404 |
| `huggingface_hub` / `HF_TOKEN` on hub | Neither present — falls to a raw `requests` streamed GET (works, but fully blocking) |
| Phone download client timeouts | `connectTimeout 30s`, `readTimeout 0` (none), `writeTimeout 60s` — a stall never surfaces as an error |

The stuck symptom is **entirely a property of the hub proxy** (synchronous
blocking fetch + the ~18 MB/s Tailscale hop). Removing the hop deletes the
failure mode rather than patching it.

## Decisions (locked)

1. **Direct phone → Hugging Face downloads.** Bytes flow phone ↔ HF CDN, never
   through the hub. The `litert-community/gemma-4-E*B-it-litert-lm` repos are
   public/ungated and the HF CDN natively supports `Range`/resume.
2. **Storage: app-private only.** Download into `filesDir/local_models/` (today's
   location). No shared storage, no SAF, no Edge Gallery file-sharing
   (sandboxing makes literal cross-app reuse impractical; YAGNI).
3. **Catalog: hub auto-discovers from HF.** The hub queries the HF Hub API to
   auto-populate the model list, merges curated per-model config, caches, and
   serves `GET /local/models/catalog`. One catalog for all surfaces; bytes still
   go phone → HF direct.
4. **Drop the hub mirror entirely.** Delete `mirror_store/`, `ensure_present`,
   `_download_bundle`, `bundle_sha256`, and the `/local/models/download/{slug}`
   byte-proxy endpoint. The blocking-fetch bug class disappears with it.
5. **Durable download via an Android foreground Service** (not WorkManager — the
   `androidx.work` dep was just dropped in `1095d3e`, and this is a
   user-initiated, actively-watched transfer).
6. **Gated-model token (BYOK) deferred.** Carry a `gated` flag in the catalog as
   a forward hook; do not build per-user HF-token entry now.

## Architecture & data flow

```
DISCOVERY (hub, cached):
  hub ──GET HF Hub API──▶ huggingface.co     (author=litert-community, gemma *-litert-lm)
  hub merges HF facts (repo, filename, size, sha256 from lfs.oid)
      with curated config (max_tokens, min_ram_gb, recommended, support_image)
  hub serves  ▶  GET /local/models/catalog   (auto-generated; was hardcoded BUNDLES)

DOWNLOAD (phone ↔ HF direct — hub NOT in the byte path):
  phone ──GET catalog──▶ hub                  (list models; cached on device)
  phone ──GET <download_url>──▶ huggingface.co CDN   (ranged, resumable)
        ──▶ filesDir/local_models/<file>.part
        ──▶ rename ▶ <file>, verify sha (now real), write <slug>.json sidecar
  phone ──POST /local/device/attest──▶ hub    (unchanged: registers the device)
```

The hub's role shrinks to **discovery + attestation** — both tiny JSON calls. It
stops being a bandwidth funnel and a single point of failure for downloads.

## Catalog & discovery layer

`mirror.py` becomes a **discovery + curation** module. Two inputs merge:

**1. HF facts (auto-discovered, cached).**
- list: `GET /api/models?author=litert-community&search=gemma` → keep ids
  matching `gemma*-it-litert-lm`
- per-repo tree: `GET /api/models/{repo}/tree/main` → find the `.litertlm` file,
  read its real `size` and `lfs.oid` (the SHA-256)

This fills the long-standing `sha256: None` gap — discovery yields a real digest,
so **on-device verification becomes meaningful** instead of a no-op. Results are
cached with a short TTL (per the "provider API as source of truth, short-TTL
cache" convention) so a catalog GET does not hammer HF.

**2. Curated config (hub-side, by slug).** A small map carries what HF does not
know: `display_name`, `max_tokens`, `min_ram_gb`, `recommended`, `support_image`,
`context_note`. A discovered bundle with **no** curated entry still appears with
safe defaults (`recommended=false`, `min_ram_gb` inferred from byte size) — so
new bundles auto-populate while known ones stay blessed and configured.

**Served catalog** (`GET /local/models/catalog`) gains two fields:
- `download_url` — the HF `resolve/main` URL (the phone never hardcodes HF's URL
  shape; keeps the provider swappable from the hub with no APK rebuild)
- `gated` — `false` today; a forward hook for future gated repos (BYOK token).
  Not acted on now.

## Phone-side download & durability

The existing `LocalModelApi.download()` streaming loop (64 KB chunks, `.part` +
`Range` resume, per-chunk `onProgress`) mostly stays. Three changes:

1. **Point at HF, not the hub.** Download from the catalog's `download_url`; do
   not send the hub-only `X-BlackBox-Client` header to HF. `.part` + `Range`
   resume now runs against HF (confirmed `206`/`Content-Range`).
2. **Add a read timeout (~90 s).** Replaces `readTimeout(0)`; a genuine stall
   surfaces as a retryable failure (row → "Failed / Retry") instead of an
   eternal 0%.
3. **Durable foreground Service.** Move the transfer out of the Settings
   `ViewModel` coroutine scope (which `dispose()` cancels on screen-leave) into a
   foreground Service with an ongoing progress notification, separate from the
   existing engine-warm `LocalModelService`. The ViewModel observes a shared
   progress flow; navigating away no longer cancels the download, and returning
   re-attaches to live progress.

Verify (now a real SHA-256 check), sidecar write, and hub attestation are
unchanged.

## Removals

- `Orchestrator/local_provider/mirror_store/` (frees the 3.66 GB E4B file)
- `mirror.ensure_present`, `mirror._download_bundle`, `mirror.bundle_sha256`
- `GET /local/models/download/{slug}` proxy endpoint (now `404`)
- Mirror-specific tests; new discovery tests added

## 3-surface scope (explicit decision)

The catalog endpoint is shared, but only the phone can run a `.litertlm`, so the
download/install/delete action is **Android-native only**:

- **Android MVP** — full UX: auto-populated picker, foreground-service download,
  install/verify/attest, delete.
- **Portal (web)** — reads the same catalog, shows on-device models
  informationally ("download on your paired phone"), no download button.
- **WebView wrappers** — wrap Portal, inherit that.

This honors "frontend = 3 surfaces" at the catalog-contract level while keeping
device-only work on the device.

## Testing

**Backend:** discovery merge (HF facts + curated config), unknown-bundle
defaults, TTL cache, `download_url`/`gated` schema, removed endpoints return 404.
The HF Hub API is mocked (the hermetic monkeypatch pattern the old
`_download_bundle` used).

**Android:** direct download against a local fake-HF server, resume from `.part`,
read-timeout → Failed/Retry, foreground service survives ViewModel `dispose()`,
SHA mismatch deletes the file, sidecar/attest unchanged. JVM-unit where possible
(`LocalModelManager` is already constructor-seamed).

## Migration / fresh-box

Slugs stay stable (`gemma-4-e2b` / `gemma-4-e4b`), so existing on-device sidecars
and attestations keep mapping cleanly — already-installed models are untouched.
Deleting `mirror_store/` is safe (untracked). A fresh box needs no mirror at all.

## Validation against the original symptom

After this lands: deleting E4B on the phone and reinstalling pulls E4B **direct
from the HF CDN** into app-private storage — resumable, durable across
navigation, with real per-chunk progress and a real SHA-256 verify. No hub
bottleneck, no event-loop freeze, no eternal 0%. E2B works identically (no
mirror prerequisite). The bug is deleted, not patched.
