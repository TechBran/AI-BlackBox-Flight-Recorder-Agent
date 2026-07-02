#!/usr/bin/env python3
"""One-time BUILD-TIME fetcher for the vendored tokenizer assets (WI-11).

Populates Orchestrator/tokenizers_vendored/ so Orchestrator/tokenization.py
can produce EXACT local token counts with networking disabled at runtime:

  tiktoken_cache/<sha1>   — tiktoken BPE cache for cl100k_base (the encoding
                            tiktoken.encoding_for_model maps
                            text-embedding-3-large to; verified below).
                            Fetched via the TIKTOKEN_CACHE_DIR trick: point
                            the env var at the vendored dir, encode once, and
                            tiktoken writes its download there.
  qwen3/tokenizer.json    — HF fast-tokenizer definition for
                            Qwen/Qwen3-Embedding-0.6B. The 8B model shares
                            the tokenizer family; this script downloads the
                            8B copy to a temp file and PROVES sample-encode
                            equality before vendoring only the 0.6B file
                            (if they ever diverge, the 8B copy is vendored
                            too as qwen3_8b/tokenizer.json).

Idempotent and re-runnable: existing assets are refreshed in place.
Run from the repo root:  Orchestrator/venv/bin/python scripts/vendor_tokenizers.py
"""
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

VENDORED_DIR = REPO_ROOT / "Orchestrator" / "tokenizers_vendored"
TIKTOKEN_CACHE = VENDORED_DIR / "tiktoken_cache"
QWEN_DIR = VENDORED_DIR / "qwen3"
QWEN_8B_DIR = VENDORED_DIR / "qwen3_8b"

OPENAI_EMBED_MODEL = "text-embedding-3-large"
EXPECTED_ENCODING = "cl100k_base"

HF_QWEN_06B = "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/resolve/main/tokenizer.json"
HF_QWEN_8B = "https://huggingface.co/Qwen/Qwen3-Embedding-8B/resolve/main/tokenizer.json"

# Deliberately diverse: prose + code + CJK + special-token text — if two
# tokenizer.json files encode all of this identically, they are the same
# tokenizer for our counting purposes.
SAMPLE_TEXT = (
    "The BlackBox mints immutable snapshots; retrieval fuses embeddings "
    "with keyword rank.\ndef f(x):\n    return x // 2\n"
    "中文分词测试 <|endoftext|> tail 12345"
)


def vendor_tiktoken() -> Path:
    os.environ["TIKTOKEN_CACHE_DIR"] = str(TIKTOKEN_CACHE)
    TIKTOKEN_CACHE.mkdir(parents=True, exist_ok=True)
    import tiktoken  # import AFTER env var so any cache read honors it

    mapped = tiktoken.encoding_for_model(OPENAI_EMBED_MODEL).name
    if mapped != EXPECTED_ENCODING:
        raise SystemExit(
            f"tiktoken maps {OPENAI_EMBED_MODEL} -> {mapped}, expected "
            f"{EXPECTED_ENCODING}; update the backend table before vendoring"
        )
    enc = tiktoken.get_encoding(EXPECTED_ENCODING)
    n = len(enc.encode(SAMPLE_TEXT, disallowed_special=()))
    files = [p for p in TIKTOKEN_CACHE.iterdir() if p.is_file()]
    if not files:
        raise SystemExit("tiktoken produced no cache file in TIKTOKEN_CACHE_DIR")
    print(f"[tiktoken] {EXPECTED_ENCODING} OK (sample encodes to {n} tokens)")
    for p in files:
        print(f"[tiktoken] cached: {p} ({p.stat().st_size:,} bytes)")
    return files[0]


def _download(url: str, dest: Path) -> None:
    import requests

    resp = requests.get(url, timeout=120)
    if resp.status_code in (401, 403):
        raise SystemExit(
            f"HF download requires auth ({resp.status_code}) for {url} — STOP: "
            "vendoring needs an anonymous-downloadable tokenizer"
        )
    resp.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: never leave a half-written vendored asset if the run dies
    # mid-write (a truncated tokenizer.json would floor every count at runtime).
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(resp.content)
    os.replace(tmp, dest)
    print(f"[qwen] downloaded {url} -> {dest} ({dest.stat().st_size:,} bytes)")


def vendor_qwen() -> None:
    from tokenizers import Tokenizer

    target = QWEN_DIR / "tokenizer.json"
    _download(HF_QWEN_06B, target)
    tok_06b = Tokenizer.from_file(str(target))
    ids_06b = tok_06b.encode(SAMPLE_TEXT).ids
    print(f"[qwen] 0.6B sample encodes to {len(ids_06b)} tokens")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_8b = Path(tmp) / "tokenizer.json"
        _download(HF_QWEN_8B, tmp_8b)
        tok_8b = Tokenizer.from_file(str(tmp_8b))
        ids_8b = tok_8b.encode(SAMPLE_TEXT).ids
        if ids_8b == ids_06b:
            print("[qwen] 8B sample-encode IDENTICAL to 0.6B — vendoring one "
                  "tokenizer.json for the family")
            if QWEN_8B_DIR.exists():
                print(f"[qwen] note: stale {QWEN_8B_DIR} exists; family is "
                      "shared again — remove it manually if desired")
        else:
            print("[qwen] 8B sample-encode DIVERGES from 0.6B — vendoring both")
            dest_8b = QWEN_8B_DIR / "tokenizer.json"
            dest_8b.parent.mkdir(parents=True, exist_ok=True)
            tmp_dest = dest_8b.with_suffix(dest_8b.suffix + ".tmp")
            tmp_dest.write_bytes(tmp_8b.read_bytes())
            os.replace(tmp_dest, dest_8b)
            print(f"[qwen] vendored {dest_8b}")
            print("[qwen] REQUIRED FOLLOW-UP (8B counts stay on the shared "
                  "tokenizer until done):")
            print("[qwen]   1. Orchestrator/tokenization.py — add an "
                  "'hf:qwen3_8b' loader (qwen3_8b/tokenizer.json) to "
                  "_BACKEND_LOADERS")
            print("[qwen]   2. Orchestrator/embeddings/registry.py — repoint "
                  "the 8B entry's \"tokenizer\" spec to 'hf:qwen3_8b'")


def main() -> None:
    vendor_tiktoken()
    vendor_qwen()
    print("\n[vendored assets]")
    total = 0
    for p in sorted(VENDORED_DIR.rglob("*")):
        if p.is_file():
            size = p.stat().st_size
            total += size
            print(f"  {p.relative_to(REPO_ROOT)}  {size:,} bytes")
    print(f"  TOTAL {total:,} bytes")


if __name__ == "__main__":
    main()
