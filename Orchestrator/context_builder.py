"""Shared fossil-context retrieval builder.

Single source of truth for the four-source fossil retrieval pipeline used by
`/chat/stream` (and, going forward, by voice / agent-WebSocket routes).

Transport-agnostic: this module does NOT build the final message list, does NOT
add device-registry context, and does NOT assemble a system prompt. Those
concerns live with each caller because they vary by transport.

Contract:
    build_fossil_context(user_text, operator, log_prefix="[CONTEXT]")
        -> (fossil_context_str, provenance_dict)

    provenance_dict keys: "recent", "keyword", "semantic", "checkpoint"
    (each a list of snap_ids, may be empty).

Per-operator scoping is a first-class invariant: retrieval helpers are invoked
with the caller-provided operator, and a missing/unknown operator returns
empty lists for all four sources.
"""
from __future__ import annotations

from typing import Tuple

from Orchestrator.config import CFG, VOL_PATH
from Orchestrator.fossils import (
    extract_snap_ids,
    get_recent_checkpoints_for_operator,
    get_recent_fossils_for_operator,
    keyword_retrieve_for_operator,
    semantic_retrieve,
)
from Orchestrator.media import get_recent_media_artifacts
from Orchestrator.volume import read_text_safe


# Global hard cap on total fossil-context chars. 200,000 chars ≈ 50K tokens.
# Comfortably fits within every provider's input window (Opus 4.7 / Gemini
# 3.1 Pro = 1M; Sonnet 4.6 / Grok 4.1 / GPT-5.1 = 200-256K).
MAX_TOTAL_CONTEXT_CHARS = 200000

# Per-provider tighter caps. Hard input limits aren't the issue — TTFB
# latency from prefill + adaptive-thinking is. Opus 4.7 in particular pauses
# 30-60+ seconds before emitting any token when given 50K+ tokens of system
# input plus tool schemas, which exceeds Android OkHttp's default SSE
# timeout and causes silent stalls. Confirmed empirically (Brandon test
# 2026-04-25): same 210K-char payload streams cleanly via Gemini 3.1 Pro
# but stalls via Opus 4.7. Per-provider caps trade some retrieval breadth
# for reliable end-to-end completion.
#
# Anthropic/Opus 4.7 tokenizer runs 1.0x-1.35x more tokens than Opus 4.6
# for the same text (per Anthropic 'What's new in 4.7' docs), so 75K chars
# is ~19-25K tokens — fast TTFB, well under any timeout pressure.
PROVIDER_CAPS = {
    "anthropic": 75000,    # Opus 4.7 adaptive-thinking TTFB constraint
    "computer-use": 75000,  # Anthropic-backed CU model uses same path
    "openai":    100000,   # GPT-5.1 has 256K window but conservative for prefill
    "local":     16000,    # on-device Gemma: chars ≈ ~4K tokens, reserves ~12K of the phone's 16K window for the agent loop
    # "google" / "gemini" / "xai" / "grok" → fall back to MAX_TOTAL_CONTEXT_CHARS
}


def _resolve_cap(provider: str | None) -> int:
    """Return the tighter of (per-provider cap, global cap) for the request.
    Falls back to the global MAX_TOTAL_CONTEXT_CHARS if no per-provider entry.
    """
    if provider:
        provider_lower = provider.lower()
        if provider_lower in PROVIDER_CAPS:
            return min(PROVIDER_CAPS[provider_lower], MAX_TOTAL_CONTEXT_CHARS)
    return MAX_TOTAL_CONTEXT_CHARS


def fill_unseen(ranked_snaps: list[str], k: int, seen_ids: set[str]) -> list[str]:
    """WI-7b: fill a context section with the first `k` snaps from a channel's
    ranked list whose snap_id is not already claimed by an earlier section
    (precedence: recent → keyword → semantic).

    Callers over-fetch each channel by len(seen_ids) — sufficient because at
    most that many candidates can be dupes — so dedupe backfills from deeper
    in the channel's own ranking instead of shrinking the section. Preserves
    rank order; never invents items (an exhausted channel returns short).
    Blocks with no extractable snap_id are treated as unseen (kept), matching
    the historical filter semantics. `seen_ids` is not mutated.

    Shared by build_fossil_context (all streaming/voice/agent transports via
    chat_routes.build_streaming_context), chat_routes.build_cu_context, and
    the non-stream worker in tasks.process_chat_task.
    """
    filled: list[str] = []
    local_seen = set(seen_ids)
    for snap in ranked_snaps:
        if len(filled) >= k:
            break
        ids = extract_snap_ids([snap])
        if any(sid in local_seen for sid in ids):
            continue
        filled.append(snap)
        local_seen.update(ids)
    return filled


def build_fossil_context(
    user_text: str,
    operator: str,
    log_prefix: str = "[CONTEXT]",
    provider: str | None = None,
    semantic_k: int | None = None,
    checkpoint_count: int | None = None,
    include_recent: bool = True,
    include_keyword: bool = True,
    include_media: bool = True,
) -> Tuple[str, dict]:
    """Retrieve fossils for `operator` and build the fossil-context string.

    Args:
        user_text: Pre-extracted last-user-message text (caller extracts from
            its transport-specific message list). Empty string is valid —
            keyword/semantic retrieval will be skipped but recent + checkpoint
            still run.
        operator: Operator scope for retrieval. MUST be non-empty.
        log_prefix: Prefix for the debug print lines so voice/agent callers
            can tag their own logs. Pass "[STREAM CONTEXT]" from chat_routes.py
            to preserve the existing log format byte-for-byte.

    Returns:
        (fossil_context_str, provenance_dict)
        provenance_dict = {"recent": [...], "keyword": [...],
                           "semantic": [...], "checkpoint": [...]}
    """
    if not operator or not operator.strip():
        raise ValueError("build_fossil_context requires a non-empty operator")

    # Retrieval config — same keys and fallbacks as build_streaming_context
    RF  = CFG.getint("context", "recent_fossils_per_user", fallback=5)
    KF  = CFG.getint("context", "keyword_fossils_per_user", fallback=4)
    SF  = semantic_k if semantic_k is not None else CFG.getint("context", "semantic_fossils_per_user", fallback=8)
    ST  = CFG.getfloat("context", "semantic_threshold", fallback=0.60)
    from Orchestrator.embeddings.search import active_threshold  # lazy: avoid startup cycle
    ST = active_threshold(ST)
    CP  = checkpoint_count if checkpoint_count is not None else CFG.getint("context", "checkpoint_snapshots", fallback=2)
    CAP = CFG.getint("context", "max_fossil_chars", fallback=10000)

    # Read volume once
    vol_txt = read_text_safe(VOL_PATH)

    # Four separate retrieval sources, all scoped to `operator`.
    # include_recent / include_keyword let lean profiles (e.g. on-device
    # `local`) skip these sources entirely; cloud callers leave them True.
    #
    # WI-7b dedupe-with-backfill: each discovery channel over-fetches by the
    # number of snap_ids already claimed by earlier sections, then fills its
    # section with the first section_k UNSEEN snaps (fill_unseen). Dedupe used
    # to be filter-only, which silently SHRANK the keyword/semantic sections
    # exactly when channels agreed on a snapshot's relevance.
    recent_snaps = (
        get_recent_fossils_for_operator(vol_txt, operator, RF, CAP)
        if include_recent else []
    )
    recent_ids = set(extract_snap_ids(recent_snaps))

    keyword_snaps_raw = (
        keyword_retrieve_for_operator(vol_txt, user_text, KF + len(recent_ids), operator)
        if (include_keyword and user_text) else []
    )
    keyword_snaps = fill_unseen(keyword_snaps_raw, KF, recent_ids)

    # Semantic fills against recent AND keyword. fill_unseen(k=SF) also
    # guarantees the lean `local` profile's item budget even if a retriever
    # over-returns (this replaces the old defensive [:SF] slice).
    seen_ids = recent_ids | set(extract_snap_ids(keyword_snaps))
    semantic_snaps_raw = (
        semantic_retrieve(user_text, operator=operator, k=SF + len(seen_ids), threshold=ST)
        if user_text else []
    )
    semantic_snaps = fill_unseen(semantic_snaps_raw, SF, seen_ids)

    # Checkpoints stay UN-deduped: they are a pinned section, not a discovery channel.
    checkpoint_snaps = get_recent_checkpoints_for_operator(vol_txt, operator, count=CP)

    # Provenance — same shape as chat_routes.build_streaming_context returns
    recent_ids_list = extract_snap_ids(recent_snaps)
    keyword_ids = extract_snap_ids(keyword_snaps)
    semantic_ids = extract_snap_ids(semantic_snaps)
    checkpoint_ids = extract_snap_ids(checkpoint_snaps)

    provenance = {
        "recent": recent_ids_list,
        "keyword": keyword_ids,
        "semantic": semantic_ids,
        "checkpoint": checkpoint_ids,
    }

    print(f"{log_prefix} Operator: {operator}")
    print(f"{log_prefix} Recent snapshots ({len(recent_snaps)}): {recent_ids_list}")
    print(f"{log_prefix} Keyword snapshots ({len(keyword_snaps)}): {keyword_ids}")
    print(f"{log_prefix} Semantic snapshots ({len(semantic_snaps)}, threshold={ST}): {semantic_ids}")
    print(f"{log_prefix} Checkpoints ({len(checkpoint_snaps)}): {checkpoint_ids}")
    print(f"{log_prefix} Query: {user_text[:100]}...")

    # Build the context block — same format as process_chat_task /
    # build_streaming_context so downstream prompts see identical text.
    context_parts = []

    # Recent media artifacts first (images/videos/music) so the model sees
    # them before snapshot text. Top-level import from neutral media module
    # — no circular dependency with chat_routes, errors propagate.
    media_artifacts = get_recent_media_artifacts(operator, limit=10) if include_media else []
    if media_artifacts:
        media_lines = [
            "=== RECENT MEDIA (Available for Reference) ===",
            "These are recently generated media files you can reference by URL:",
        ]
        for artifact in media_artifacts:
            media_type = artifact.get("type", "media")
            url = artifact.get("url", "")
            prompt = artifact.get("prompt", "")[:80]
            created = artifact.get("created_at", "")[:19]  # Trim to datetime
            media_lines.append(f"- [{media_type.upper()}] {url}")
            media_lines.append(f"  Prompt: \"{prompt}...\"")
            media_lines.append(f"  Created: {created}")
        media_lines.append("")
        media_lines.append(
            "To use an image for video generation, pass its URL to "
            "generate_video with image_url parameter."
        )
        context_parts.append("\n".join(media_lines))
        print(
            f"{log_prefix} Media artifacts ({len(media_artifacts)}): "
            f"{[a.get('url') for a in media_artifacts[:3]]}"
        )

    # Each snapshot gets an explicit numbered divider matching the
    # checkpoint pattern. Without these, models under-count items in a wall
    # of \n\n-joined text (Brandon test 2026-04-25 — Opus 4.7 reported "1
    # recent" when 5 were rendered).
    def _fenced(label: str, snaps: list[str], ids: list[str]) -> str:
        lines = [f"=== {label.upper()} SNAPSHOTS ({len(snaps)} total) ==="]
        for i, snap in enumerate(snaps, 1):
            sid = ids[i - 1] if i - 1 < len(ids) else "?"
            lines.append(f"=== {label} #{i}: {sid} ===")
            lines.append(snap)
        return "\n".join(lines)

    # ORDER + FORMAT UNIFORMITY both matter. Two phenomena to balance:
    #
    # (1) Cap survival: items at the END are dropped first when the cap
    #     fires. With MAX_TOTAL_CONTEXT_CHARS=200,000 (since 2026-04-25),
    #     truncation only fires on outlier turns; ordering matters less
    #     than it did at 30K.
    # (2) Lost-in-the-middle attention: long contexts cause models to
    #     under-attend to content in the trailing region. Earlier ordering
    #     put checkpoints LAST, and Brandon's Opus 4.7 reported "0
    #     checkpoints" at byte ~165K of a 173K fossil_context — the
    #     checkpoint content was technically present but in the attention
    #     dead zone (Brandon test, 2026-04-25 round 3).
    #
    # Final order: checkpoint (at top, hot attention region) → recent →
    # semantic → keyword. Checkpoints are compressed session summaries
    # that benefit from being visually prominent. Query-specific
    # retrieval (recent/semantic/keyword) trails — still survives at 200K
    # cap, and recent items are typically more recoverable from operator
    # state if they ever clip.
    #
    # All FOUR sources now use the same _fenced helper for visual
    # uniformity — section header announcing count + numbered per-item
    # fences with SNAP-ID inline. The old checkpoint format had no
    # section-count header, which contributed to the model under-counting.
    if checkpoint_snaps:
        context_parts.append(_fenced("Checkpoint", checkpoint_snaps, checkpoint_ids))
    if recent_snaps:
        context_parts.append(_fenced("Recent", recent_snaps, recent_ids_list))
    if semantic_snaps:
        context_parts.append(_fenced("Semantic", semantic_snaps, semantic_ids))
    if keyword_snaps:
        context_parts.append(_fenced("Keyword-matched", keyword_snaps, keyword_ids))

    fossil_context = "\n\n".join(context_parts)

    # Hard cap — prevents context overflow on tool-calling models.
    # IMPORTANT: capture original size BEFORE the assignment that overwrites
    # fossil_context with the truncated value. Pre-2026-04-25 the print
    # statement read len(fossil_context) AFTER reassignment, so it always
    # reported "30038 → 30000" (the truncation suffix is 38 chars) regardless
    # of how much was actually lost — masking a 170K-char loss in plain sight.
    cap = _resolve_cap(provider)
    if len(fossil_context) > cap:
        original_len = len(fossil_context)
        fossil_context = (
            fossil_context[:cap]
            + "\n\n[Context truncated for token budget]"
        )
        cap_source = (
            f"provider={provider!r}" if provider and provider.lower() in PROVIDER_CAPS
            else "global"
        )
        print(
            f"{log_prefix} WARNING: Fossil context truncated from "
            f"{original_len:,} to {cap:,} chars "
            f"(dropped {original_len - cap:,} chars; cap source: {cap_source}; "
            f"order is checkpoint → recent → semantic → keyword, so keyword "
            f"clips first)"
        )

    return fossil_context, provenance
