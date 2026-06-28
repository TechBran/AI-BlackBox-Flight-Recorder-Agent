#!/usr/bin/env python3
"""catalog.py — On-device Gemma model DISCOVERY + CURATION catalog.

The BlackBox hub no longer moves model bytes: phones download the Gemma 4 LiteRT
``.litertlm`` bundles DIRECTLY from the Hugging Face CDN. This module is the
DISCOVERY + CURATION layer that tells the phone WHAT is downloadable and from
WHERE: it queries the HF Hub API for ``litert-community`` gemma
``*-it-litert-lm`` repos, merges real ``size_bytes`` / ``sha256`` (from
``lfs.oid``) / ``gated`` / ``download_url`` onto a small curated config floor,
and caches the result with a TTL. New repos appear automatically; if HF is
unreachable the curated floor is served unchanged (fresh-box safe).

This catalog is DISTINCT from the picker catalog (``LOCAL_MODELS`` in
``local_routes``): the picker entries are id/name/provider descriptors for the
model selector; the entries here carry DOWNLOAD/METADATA fields (including the
HF ``download_url`` the phone streams from). The slugs are kept IDENTICAL across
both so a downloaded bundle maps cleanly to a picker entry.

The ONLY network touch is ``_fetch_hf_models`` / ``_fetch_hf_tree`` (via
``_http_get_json``); tests monkeypatch those so no real HF request ever runs.
"""

import os
import time

import requests  # already in venv

# ---------------------------------------------------------------------------
# Task A2 — HF Hub API fetchers (the ONLY network code in this module)
#
# These two functions are the only network touch. Tests monkeypatch
# ``_http_get_json`` so no real Hugging Face request ever runs under pytest
# (same hermetic pattern the old ``_download_bundle`` used).
# ---------------------------------------------------------------------------

_HF_BASE = "https://huggingface.co"
_HF_AUTHOR = "litert-community"
_HF_TIMEOUT = 20  # HF Hub API JSON calls are tiny; 20s is generous.


def _http_get_json(url: str, **kw):
    """GET a URL and return parsed JSON. Isolated so tests monkeypatch IT
    (never a real HF request runs under pytest)."""
    token = os.environ.get("HF_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get(url, headers=headers, timeout=_HF_TIMEOUT, **kw)
    r.raise_for_status()
    return r.json()


def _fetch_hf_models() -> list[dict]:
    """List litert-community gemma models from the HF Hub API."""
    url = f"{_HF_BASE}/api/models?author={_HF_AUTHOR}&search=gemma"
    data = _http_get_json(url)
    out = []
    for m in data:
        mid = m.get("id", "")
        if mid.endswith("-litert-lm") and "gemma" in mid and "-it-" in mid:
            out.append({"id": mid, "gated": bool(m.get("gated"))})
    return out


def _fetch_hf_tree(repo: str) -> list[dict]:
    """Return the repo's main-branch file tree (path/size/lfs)."""
    url = f"{_HF_BASE}/api/models/{repo}/tree/main"
    data = _http_get_json(url)
    return [f for f in data if f.get("type", "file") == "file"]


# ---------------------------------------------------------------------------
# Task A3 — file selection + slug derivation + download URL helpers
# ---------------------------------------------------------------------------

def _select_litertlm(tree: list[dict], preferred: str | None) -> dict | None:
    """Pick the canonical .litertlm file from a repo tree.

    A repo ships multiple .litertlm builds (e.g. ``-web`` variants). Selection:
      1. the curated ``preferred`` filename if present;
      2. else the non-``-web`` ``*-it.litertlm`` build;
      3. else the largest .litertlm (last resort).
    Returns a normalised dict {path, size_bytes, sha256} or None.
    """
    litertlms = [f for f in tree if str(f.get("path", "")).endswith(".litertlm")]
    if not litertlms:
        return None

    def _norm(f):
        return {
            "path": f["path"],
            "size_bytes": int(f.get("size") or (f.get("lfs") or {}).get("size") or 0),
            "sha256": (f.get("lfs") or {}).get("oid"),  # LFS oid == content SHA-256
        }

    if preferred:
        for f in litertlms:
            if f.get("path") == preferred:
                return _norm(f)
    non_web = [f for f in litertlms
               if "-web" not in f["path"] and f["path"].endswith("-it.litertlm")]
    if non_web:
        return _norm(non_web[0])
    # Last resort (no curated `preferred` AND no non-web `*-it.litertlm`): the
    # largest .litertlm -- which may itself be a `-web` build.
    return _norm(max(litertlms, key=lambda f: f.get("size") or 0))


def _slug_for_repo(repo: str) -> str:
    """litert-community/gemma-4-E2B-it-litert-lm -> gemma-4-e2b."""
    name = repo.split("/")[-1]
    name = name.removesuffix("-litert-lm").removesuffix("-it")
    return name.lower()


def _download_url(repo: str, filename: str) -> str:
    return f"{_HF_BASE}/{repo}/resolve/main/{filename}"


# ---------------------------------------------------------------------------
# Task A4 — curated config floor + build_catalog() merge (HF-enriched, TTL-cached)
#
# The curated floor is the always-present, blessed config. HF discovery enriches
# it with live facts (size/sha256/gated/download_url) and EXTENDS it (new repos
# appear automatically). If HF is unreachable the curated floor is served
# unchanged — fresh-box / offline safe (hub-side resilience only).
# ---------------------------------------------------------------------------
CURATED: dict[str, dict] = {
    "gemma-4-e2b": {
        "display_name": "Gemma 4 E2B (on-device)",
        "hf_repo": "litert-community/gemma-4-E2B-it-litert-lm",
        "filename": "gemma-4-E2B-it.litertlm",
        "size_bytes": 2588147712,          # fallback if HF is down
        "min_ram_gb": 3.0,
        "recommended_for": "Lighter, faster on-device model for phones with less RAM.",
        "recommended": False,
        "context_note": "Experimental — weaker at multi-step agent loops",
        "max_tokens": 6144,  # GPU-safe default (= LiteRtEngine.DEFAULT_MAX_TOKENS); user-raisable toward 16384 on a higher-RAM phone
        "support_image": True,
    },
    "gemma-4-e4b": {
        "display_name": "Gemma 4 E4B (on-device)",
        "hf_repo": "litert-community/gemma-4-E4B-it-litert-lm",
        "filename": "gemma-4-E4B-it.litertlm",
        "size_bytes": 3659530240,
        "min_ram_gb": 4.5,
        "recommended_for": "Heavier, higher-quality on-device model for high-RAM phones.",
        "recommended": True,
        "context_note": "Recommended — best on-device agent reliability",
        "max_tokens": 6144,  # GPU-safe default (= LiteRtEngine.DEFAULT_MAX_TOKENS); user-raisable toward 16384 on a higher-RAM phone
        "support_image": True,
    },
}

_CACHE_TTL = 3600  # seconds
_cache: dict = {"at": 0.0, "bundles": None}


def _invalidate_cache():
    _cache["at"] = 0.0
    _cache["bundles"] = None


def _infer_min_ram_gb(size_bytes: int) -> float:
    # A model needs roughly its file size in RAM plus headroom; 1.25x, floored at 3.
    return round(max(3.0, (size_bytes / 1_073_741_824) * 1.25), 1)


def _default_entry(slug: str, repo: str, gated: bool, f: dict) -> dict:
    return {
        "slug": slug,
        "display_name": slug.replace("-", " ").title() + " (on-device)",
        "hf_repo": repo,
        "filename": f["path"],
        "size_bytes": f["size_bytes"],
        "sha256": f["sha256"],
        "min_ram_gb": _infer_min_ram_gb(f["size_bytes"]),
        "recommended_for": "Auto-discovered on-device model.",
        "recommended": False,
        "context_note": "Auto-discovered",
        "max_tokens": 6144,  # GPU-safe default (= LiteRtEngine.DEFAULT_MAX_TOKENS); user-raisable toward 16384 on a higher-RAM phone
        "support_image": True,
        "download_url": _download_url(repo, f["path"]),
        "gated": gated,
    }


def _curated_floor() -> dict[str, dict]:
    """The curated entries as full bundle dicts (sha None, download_url built)."""
    out = {}
    for slug, c in CURATED.items():
        b = dict(c)
        b["slug"] = slug
        b.setdefault("sha256", None)
        b["download_url"] = _download_url(c["hf_repo"], c["filename"])
        b["gated"] = False
        out[slug] = b
    return out


def build_catalog() -> list[dict]:
    """Discover + curate the downloadable bundles (TTL-cached).

    HF facts (size/sha256/gated, new repos) enrich/extend the curated floor.
    On any HF error the curated floor is served unchanged (fresh-box safe)."""
    now = time.time()
    if _cache["bundles"] is not None and (now - _cache["at"]) < _CACHE_TTL:
        return [dict(b) for b in _cache["bundles"]]

    bundles = _curated_floor()
    try:
        for m in _fetch_hf_models():
            repo, gated = m["id"], m["gated"]
            slug = _slug_for_repo(repo)
            curated = CURATED.get(slug)
            f = _select_litertlm(_fetch_hf_tree(repo), curated["filename"] if curated else None)
            if f is None:
                continue
            if slug in bundles:  # enrich curated entry with live facts
                bundles[slug].update({
                    "filename": f["path"],
                    "size_bytes": f["size_bytes"],
                    "sha256": f["sha256"],
                    "download_url": _download_url(repo, f["path"]),
                    "gated": gated,
                })
            else:                # extend with an auto-discovered entry
                bundles[slug] = _default_entry(slug, repo, gated, f)
    except Exception as e:
        print(f"[LOCAL CATALOG] HF discovery failed, serving curated floor: {e}")
        # bundles already holds the curated floor.

    result = sorted(bundles.values(), key=lambda b: b["slug"])
    _cache["at"], _cache["bundles"] = now, result
    return [dict(b) for b in result]


def list_bundles() -> list[dict]:
    return build_catalog()


def get_bundle(slug: str) -> dict | None:
    return next((dict(b) for b in build_catalog() if b["slug"] == slug), None)
