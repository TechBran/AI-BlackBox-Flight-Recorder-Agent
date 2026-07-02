"""Snapshot chunker — WI-2 chunk-for-scoring window walker (M6 task 6b).

SNAPSHOT-ONLY HELPER (audit A7). chunk_snapshot() feeds the snapshot embed
pipeline exclusively (mint path + migrate backfill, wired in 6c/6d). It must
NEVER be wired into providers.embed or generate_embedding_sync: ToolVault
descriptions, the watcher health probe, and queries are single-vector
documents that CLAMP, never chunk — chunking them would multiply vectors
into stores that expect one row per document (v1) or fabricate groups for
non-snapshot ids. Nothing imports this module until 6c.

Sizing is token-aware via Orchestrator.tokenization: exact local backends
(tiktoken/HF, vendored) produce exact windows; floor models (Gemini remote
spec, unknown keys) use the calibrated chars≈tokens×2 floor. The walk takes
a window clamped to `[retrieval] chunk_tokens` (default 1024 — a scoring-
resolution choice, audit A12), emits it, then advances by
(window length − `[retrieval] chunk_overlap_pct`% of it), preferring a
paragraph/newline break inside the last 10% of each window (cheap
rfind('\\n') heuristic, not NLP). Consecutive chunks therefore overlap and
their union covers the text with no gaps. Deterministic; never raises.
"""
from Orchestrator import config
from Orchestrator.tokenization import clamp_to_tokens, estimate_tokens

# Look for a newline break only inside the trailing fraction of a window —
# breaking earlier would waste budget; breaking mid-line is the fallback.
_BOUNDARY_TAIL_FRACTION = 0.10


def _coerce_text(text) -> str:
    """Never-raise input normalization (mirrors tokenization's contract)."""
    if text is None:
        return ""
    if isinstance(text, str):
        return text
    if isinstance(text, bytes):
        try:
            return text.decode("utf-8", errors="replace")
        except Exception:
            return ""
    try:
        return str(text)
    except Exception:
        return ""


def chunk_snapshot(text: str, model_key: str | None = None) -> list[str]:
    """Split one snapshot document into overlapping scoring windows.

    Returns [] for empty input, [text] when the whole document fits the
    token budget (the common case: p50 snapshot ≈ 1,500 tokens → 1-2
    chunks), else verbatim substrings of `text` in document order. Every
    chunk fits `[retrieval] chunk_tokens` under model_key's token counter;
    consecutive chunks overlap by `[retrieval] chunk_overlap_pct` percent.
    A non-positive configured budget degrades to [text] (whole-snapshot
    behavior) rather than emitting confetti.
    """
    clean = _coerce_text(text)
    if not clean:
        return []
    try:
        budget = config.CFG.getint("retrieval", "chunk_tokens", fallback=1024)
        overlap_pct = config.CFG.getint(
            "retrieval", "chunk_overlap_pct", fallback=15
        )
        if budget <= 0:
            return [clean]
        if estimate_tokens(clean, model_key) <= budget:
            return [clean]
        overlap_pct = min(max(overlap_pct, 0), 90)

        chunks: list[str] = []
        pos = 0
        n = len(clean)
        while pos < n:
            window, _ = clamp_to_tokens(clean[pos:], budget, model_key)
            if not window:
                # Degenerate backend result (e.g. budget below the encoder's
                # special-token floor): fall back to the char floor so the
                # walk always makes progress instead of looping.
                window = clean[pos:pos + max(1, budget * 2)]
            if pos + len(window) < n:
                # Prefer a paragraph/newline boundary in the last 10% of the
                # window; keep the newline with the leading chunk.
                tail_start = max(1, int(len(window) * (1 - _BOUNDARY_TAIL_FRACTION)))
                cut = window.rfind("\n", tail_start)
                if cut != -1:
                    # Re-clamp the shortened prefix: BPE token counts are not
                    # strictly monotone under prefixing, so this keeps the
                    # <=budget invariant airtight (a no-op in practice).
                    window, _ = clamp_to_tokens(
                        window[:cut + 1], budget, model_key
                    )
                    if not window:
                        window = clean[pos:pos + max(1, budget * 2)]
            chunks.append(window)
            if pos + len(window) >= n:
                break
            step = len(window) - (len(window) * overlap_pct) // 100
            pos += max(1, step)
        return chunks
    except Exception:
        # Never-raise contract: degrade to today's whole-snapshot behavior.
        return [clean]
