"""Snapshot chunker — WI-2 chunk-for-scoring window walker (M6 task 6b).

SNAPSHOT-ONLY HELPER (audit A7). chunk_snapshot() feeds the snapshot embed
pipeline exclusively (mint path + migrate backfill, wired in 6c/6d). It must
NEVER be wired into providers.embed or generate_embedding_sync: ToolVault
descriptions, the watcher health probe, and queries are single-vector
documents that CLAMP, never chunk — chunking them would multiply vectors
into stores that expect one row per document (v1) or fabricate groups for
non-snapshot ids. The only production consumer is
search.embed_snapshot_for_index (the 6c mint seam); 6d adds the migrate
backfill.

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

# C2: chars sliced ahead of each clamp. Bounds the per-window encode cost —
# clamping the WHOLE remaining suffix re-encodes O(n) text per window, which
# is O(n²) overall (measured 26.7s for 145k chars on the exact backend).
# 8 chars/token is a generous over-provision (corpus mean 2.9, prose ~4-5),
# so a full-budget window always fits inside the slice; hyper-compressible
# text (long single-char runs) merely yields slightly shorter windows, which
# only affects chunk count, never correctness.
_SLICE_CHARS_PER_TOKEN = 8


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


def _verbatim_prefix(source: str, pos: int, window: str) -> str:
    """Longest prefix of `window` that matches `source` verbatim at `pos`.

    C1: exact-tokenizer clamps DECODE token ids — a window cut mid-grapheme
    (astral/ZWJ sequences) decodes with a trailing U+FFFD or otherwise drifts
    from the source bytes. Chunks must be verbatim substrings (position
    recovery, dedupe, delivery all rely on it), so trim to the matched
    prefix; trimming strictly shortens, preserving the token budget intent.
    """
    limit = min(len(window), len(source) - pos)
    i = 0
    while i < limit and window[i] == source[pos + i]:
        i += 1
    return window[:i]


def _fit_budget(source: str, pos: int, window: str, budget: int,
                model_key) -> str:
    """Shrink `window` (already verbatim at pos) until it measures <= budget.

    Rarely needed: verbatim repair and newline cuts shorten the clamped
    window, and BPE token counts are not strictly monotone under prefixing.
    Each pass strictly shrinks the window, so termination is trivial.
    """
    while window and estimate_tokens(window, model_key) > budget:
        shorter, _ = clamp_to_tokens(window, budget, model_key)
        shorter = _verbatim_prefix(source, pos, shorter)
        window = shorter if 0 < len(shorter) < len(window) else window[:-1]
    return window


def chunk_snapshot(text: str, model_key: str | None = None) -> list[str]:
    """Split one snapshot document into overlapping scoring windows.

    Returns [] for empty input, [text] when the whole document fits the
    token budget (the common case: p50 snapshot ≈ 1,500 tokens → 1-2
    chunks), else VERBATIM substrings of `text` in document order (never a
    decode artifact — see _verbatim_prefix). Every chunk fits `[retrieval]
    chunk_tokens` under model_key's token counter; consecutive chunks
    overlap by `[retrieval] chunk_overlap_pct` percent and their union
    covers the text with no gaps. A non-positive configured budget degrades
    to [text] (whole-snapshot behavior) rather than emitting confetti.
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
            # C2: clamp a BOUNDED slice, not the whole remaining suffix —
            # whole-suffix clamping re-encodes O(n) text per window (O(n²)
            # total). The slice always holds a full-budget window (8
            # chars/token over-provision).
            remaining = clean[pos:pos + budget * _SLICE_CHARS_PER_TOKEN]
            window, _ = clamp_to_tokens(remaining, budget, model_key)
            window = _verbatim_prefix(clean, pos, window)
            if not window:
                # Degenerate backend result (e.g. budget below the encoder's
                # special-token floor, or decode drift from char 0): fall
                # back to the char floor so the walk always makes progress.
                window = clean[pos:pos + max(1, budget * 2)]
            else:
                window = _fit_budget(clean, pos, window, budget, model_key)
                if not window:
                    window = clean[pos:pos + max(1, budget * 2)]
            if pos + len(window) < n:
                # Prefer a paragraph/newline boundary in the last 10% of the
                # window; keep the newline with the leading chunk. A prefix
                # of a verbatim window stays verbatim; _fit_budget keeps the
                # token budget airtight under BPE boundary shifts.
                tail_start = max(1, int(len(window) * (1 - _BOUNDARY_TAIL_FRACTION)))
                cut = window.rfind("\n", tail_start)
                if cut != -1:
                    fitted = _fit_budget(
                        clean, pos, window[:cut + 1], budget, model_key
                    )
                    if fitted:
                        window = fitted
            chunks.append(window)
            if pos + len(window) >= n:
                break
            step = len(window) - (len(window) * overlap_pct) // 100
            pos += max(1, step)
        return chunks
    except Exception:
        # Never-raise contract: degrade to today's whole-snapshot behavior.
        return [clean]
