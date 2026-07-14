"""Pure builders for the UI-only ``system_activity`` SSE event ("The Signal").

This telemetry is PRESENTATION-ONLY. The dicts produced here are streamed to the
frontends solely to render the live HUD line that morphs through what the exosuit
is *actually* doing this turn (embed / search / rank / generate / mint). They are
NEVER injected into the LLM prompt or context, and NEVER written into a
snapshot/ledger. Keep this module pure: no I/O, no globals, no heavy imports, no
side effects -- data in, data out.

Honest degradation is the contract: emit only stages we have a real live metric
for. A stage with no number is OMITTED, never faked. Corpus size, candidate
counts, dims and token counts all come from the caller's metrics dict -- nothing
here is hardcoded, so a fresh box with an empty corpus or a silent provider
simply narrates fewer (true) stages.
"""


def _ev(seq: int, stage: str, label: str, **detail) -> dict:
    """Wrap one honest stage into the SSE envelope shape the frontends expect."""
    return {
        "type": "system_activity",
        "data": {"stage": stage, "label": label, "detail": detail, "seq": seq},
    }


def build_retrieval_activity(m: dict) -> list:
    """Given the metrics the retrieval pipeline already computed for this turn,
    return the ordered, honest ``system_activity`` events for the retrieval half.

    Each element is ``{"type": "system_activity", "data": {stage, label, detail,
    seq}}`` with ``seq`` a monotonic 0-based counter over the emitted events.
    Stages with no real value are omitted (honest degradation). A non-retrieval
    turn -- one where no embedding/memory search happened -- yields ``[]``; the
    frontend still renders generate/mint from other events.

    Pure: no side effects, no persistence. The output is presentation-only.
    """
    m = m or {}
    out: list = []
    seq = 0

    def add(stage: str, label: str, **detail) -> None:
        nonlocal seq
        out.append(_ev(seq, stage, label, **detail))
        seq += 1

    # No embedding model => no memory was searched this turn. There is nothing
    # honest to narrate in the retrieval half, so emit nothing at all (not even a
    # bare "brain" line, which would imply retrieval that never happened).
    if not m.get("embed_model"):
        return []

    # resolve brain + context window (only when we truly know both).
    provider = m.get("provider")
    model = m.get("model")
    if provider and model:
        win = m.get("window_tokens")
        win_s = f" · {round(win / 1000)}k window" if win else ""
        add("resolve_model", f"brain · {model}{win_s}", provider=provider, model=model)

    # embed the query (we are here because embed_model is set).
    embed_model = m["embed_model"]
    dims = m.get("embed_dims")
    dims_s = f" · {dims}d" if dims else ""
    add("embed", f"embed · {embed_model}{dims_s}", model=embed_model, dims=dims)

    # corpus + candidate search: needs both a real corpus size and a survivor count.
    corpus = m.get("corpus_count")
    candidates = m.get("candidates")
    if corpus is not None and candidates is not None:
        add(
            "search",
            f"search · {corpus:,} snapshots → {candidates} cleared floor",
            corpus=corpus,
            candidates=candidates,
        )

    mmr_topk = m.get("mmr_topk")

    # rerank: only when the reranker actually ran (it is dark on most boxes).
    if m.get("rerank_enabled"):
        rr_model = m.get("rerank_model", "reranker")
        arrow = ""
        if candidates is not None and mmr_topk is not None:
            arrow = f" · {candidates}→{mmr_topk}"
        add("rerank", f"rerank · {rr_model}{arrow}", model=rr_model,
            candidates=candidates, top_k=mmr_topk)

    # MMR diversity selection.
    if mmr_topk is not None:
        add("mmr", f"MMR · top-{mmr_topk} selected", top_k=mmr_topk)

    # assemble context: post-drop memory count; window-guard trim shown honestly.
    memories = m.get("memories")
    if memories is not None:
        tok = m.get("context_tokens")
        tok_s = f" · {round(tok / 1000)}k tokens" if tok else ""
        dropped = m.get("dropped") or 0
        fit_s = f" · trimmed {dropped} to fit" if dropped else ""
        add(
            "context",
            f"context · {memories} memories{tok_s}{fit_s}",
            memories=memories,
            tokens=tok,
            dropped=dropped,
        )

    return out
