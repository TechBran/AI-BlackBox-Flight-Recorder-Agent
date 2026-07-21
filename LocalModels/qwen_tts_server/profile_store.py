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
