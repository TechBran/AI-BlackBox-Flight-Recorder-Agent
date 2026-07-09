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
    format_snapshot_for_delivery,
    get_recent_checkpoints_for_operator,
    get_recent_fossils_for_operator,
    keyword_retrieve_for_operator,
    semantic_retrieve,
)
from Orchestrator.media import get_recent_media_artifacts
from Orchestrator.volume import read_text_safe


# ─────────────────────────────────────────────────────────────────────────────
# WI-10 (M7): delivery caps are GONE on cloud surfaces.
#
# Brandon's directive (audit doc §5 decision 6): caps exist ONLY at the
# embedding/chunking layer (so ranking picks the best snapshots); the context
# the chat model receives is governed by the config.ini COUNT knobs
# (recent/keyword/semantic/checkpoint) and nothing else — every delivered
# snapshot arrives WHOLE. The historical char caps here (MAX_TOTAL_CONTEXT_CHARS
# = 200k, PROVIDER_CAPS anthropic 75k / computer-use 75k / openai 100k) were
# transport guards for the 2026-04-25 Opus TTFB stall; that stall is now
# handled at the transport layer instead (server SSE keepalive comment frames
# + Android M7.1a 300s read timeout + Portal stall watchdog).
#
# What remains is a WINDOW SAFETY GUARD (not a cap): a per-provider TOKEN
# budget derived from the documented/live-verified context window (M3 audit,
# docs/plans/artifacts/2026-07-02-provider-window-audit.md §3) minus response
# + prompt-overhead headroom. Token math via tokenization.estimate_tokens
# (chars/2 conservative floor — measured to OVER-estimate true provider
# tokenizers 2.2-2.4x on this corpus; never a network call). If the assembled
# fossil context would exceed the budget, whole LOWEST-RANKED snapshots are
# dropped (never mid-snapshot truncation) with a "window guard dropped" log
# line. With live count knobs (RF=5 KF=3 SF=6 CP=2) the measured worst case
# is ≈119k floor-tokens — the guard essentially never binds on the 1M-class
# chat windows; it exists for pathological outliers and the Gemini
# Computer-Use 131,072-token window (the one cloud window that can bind).
#
# Budget math (floor-token budgets for the FOSSIL BLOCK; window / max output
# from the M3 table; "overhead" reserves core system prompt + ToolVault
# schemas — measured ≈19.4k chars ≈ 10k floor-tokens — plus user history):
#   anthropic     claude-opus-4-8      1,000,000 − 128,000 out − 32,000 ovh =   840,000
#   openai        gpt-5.1                272,000 input share  − 32,000 ovh =   240,000
#                 (400k total = 272k input + 128k output — output does NOT
#                 consume the input share)
#   gemini/google gemini-3.1-pro       1,048,576 −  65,536 out − 32,000 ovh =  951,040
#   xai/grok      grok-4.3             1,000,000 − 128,000 out (assumed sym.)
#                                                            − 32,000 ovh =   840,000
#   computer-use  gemini-2.5-CU (the binding backend; anthropic/openai CU
#                 backends are 1M-class) 131,072 − 65,536 out − 16,384
#                 loop/screenshot growth                                  =    49,152
#                 (floor-tokens ⇒ ≈98k chars of fossils — looser than the old
#                 75k-char cap but still a real guard on the tightest window)
PROVIDER_WINDOW_GUARD_TOKENS = {
    "anthropic": 840_000,
    "openai": 240_000,
    "gemini": 951_040,
    "google": 951_040,
    "xai": 840_000,
    "grok": 840_000,
    "computer-use": 49_152,
    # custom = user-registered OpenAI-compatible servers (llama.cpp/vLLM/
    # Ollama). Windows vary per server, so callers thread the resolved
    # server's context_tokens via build_fossil_context(window_guard_tokens=…);
    # this static entry is the NO-OVERRIDE FLOOR (0.6 × the 32,768-token
    # default context_tokens) so any path that misses the thread never
    # inherits the 240K default and overflows a 32K window on turn one.
    "custom": 19_200,
}
# Unknown/absent provider (voice session-open, CLI agent transports): the most
# conservative CLOUD chat budget. Voice routes additionally apply their own
# REALTIME_CONTEXT_MAX_CHARS session budget on top (their windows are
# per-session-model, audit §6 — not inherited from chat).
DEFAULT_WINDOW_GUARD_TOKENS = 240_000

# The ONE remaining char-capped profile: the on-device phone lean path
# (provider="local"). Its window is genuinely small (6,144-token engine
# default, device-proven GPU ceiling — audit §7); M8 (WI-7a matched-chunk
# windowing) owns its delivery mechanism. Cloud surfaces no longer appear
# here. local_routes reports this value as the package budget.
PROVIDER_CAPS = {
    "local": 16000,  # chars ≈ ~4K tokens, reserves the rest of the phone window for the agent loop
}


def window_guard_budget_tokens(provider: str | None) -> int:
    """Floor-token budget for model-bound context on a cloud provider.

    NOT valid for the local/phone profile — callers route provider="local"
    through the char-capped lean path instead (M8's domain).
    """
    if provider:
        budget = PROVIDER_WINDOW_GUARD_TOKENS.get(provider.lower())
        if budget is not None:
            return budget
    return DEFAULT_WINDOW_GUARD_TOKENS


def fill_unseen(ranked_snaps: list[str], k: int, seen_ids: set[str]) -> list[str]:
    """WI-7b: fill a context section with the first `k` snaps from a channel's
    ranked list whose snap_id is not already claimed by an earlier section
    (precedence: recent → keyword → semantic).

    Callers over-fetch each channel by len(seen_ids) — sufficient because at
    most that many candidates can be dupes, assuming the retriever's ranked
    list carries distinct snap_ids (they do; intra-list dupes are collapsed
    defensively anyway) — so dedupe backfills from deeper in the channel's
    own ranking instead of shrinking the section. Preserves rank order; never
    invents items (an exhausted channel returns short). Blocks with no
    extractable snap_id are treated as unseen (kept), matching
    build_fossil_context's historical filter semantics (the old tasks.py loop
    dropped id-less blocks). `seen_ids` is not mutated.

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
    window_guard_tokens: int | None = None,
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
        window_guard_tokens: Optional per-call override for the cloud window
            safety guard budget. None (default) = today's behavior:
            window_guard_budget_tokens(provider). Used by provider="custom"
            callers to thread the resolved server's actual context_tokens
            (windows vary per registered server; the static table entry is
            only the no-override floor).

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
    # ST is display/log-only: it feeds semantic_retrieve's retained-but-unused
    # threshold param. The ranking floor lives in retrieval.py's junk-floor
    # resolution (M9/WI-3).
    ST = active_threshold(ST)
    CP  = checkpoint_count if checkpoint_count is not None else CFG.getint("context", "checkpoint_snapshots", fallback=2)
    # [context] max_fossil_chars is LOCAL-PROFILE-ONLY since WI-10 (M7): the
    # phone lean path keeps its per-snapshot char cap (M8 matched-chunk
    # windowing owns that delivery); CLOUD delivery is cap-free — every
    # snapshot arrives WHOLE (None disables the per-snapshot cap in
    # get_recent_fossils_for_operator). The config key stays live for local.
    is_local = bool(provider) and provider.lower() == "local"
    CAP = CFG.getint("context", "max_fossil_chars", fallback=10000) if is_local else None

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
    # window_budget_chars (M8/WI-7a): CAP is None on every cloud call (whole
    # snapshots, byte-identical); on the local lean profile it is
    # [context] max_fossil_chars, and an over-budget semantic snapshot is
    # delivered as a window CENTERED on its best-matched chunk instead of a
    # blind head truncation (see fossils.window_snapshot_text).
    seen_ids = recent_ids | set(extract_snap_ids(keyword_snaps))
    semantic_snaps_raw = (
        semantic_retrieve(user_text, operator=operator, k=SF + len(seen_ids), threshold=ST,
                          window_budget_chars=CAP)
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
            # M15.2: deliver body-only text to the model — a compact
            # [SNAP-id · date · operator] attribution + Context Provenance +
            # the Raw Session Log, dropping the ~1,000-char/snapshot bookkeeping
            # envelope (BEACON/VOLUME-TRACKER/GAUGES/Kernel-Index) the model
            # can't use. Formatting the RENDERED text only (not the `snaps`
            # lists) keeps extract_snap_ids / the window guard / provenance
            # operating on the whole blocks; the guard/cap below therefore cap
            # CLEANER text. A snapshot lacking the content markers passes through
            # unchanged (never-worse contract). The on-device semantic snaps are
            # already matched-chunk windowed upstream; format composes safely
            # (never-raises) over that.
            lines.append(format_snapshot_for_delivery(snap))
        return "\n".join(lines)

    # ORDER + FORMAT UNIFORMITY both matter. Two phenomena to balance:
    #
    # (1) Window-guard survival: since WI-10 (M7) nothing is char-truncated —
    #     if the token window guard ever binds, whole LOWEST-VALUE snapshots
    #     are dropped in reverse section order (keyword first), so section
    #     order doubles as drop precedence.
    # (2) Lost-in-the-middle attention: long contexts cause models to
    #     under-attend to content in the trailing region. Earlier ordering
    #     put checkpoints LAST, and Brandon's Opus 4.7 reported "0
    #     checkpoints" at byte ~165K of a 173K fossil_context — the
    #     checkpoint content was technically present but in the attention
    #     dead zone (Brandon test, 2026-04-25 round 3).
    #
    # Final order: checkpoint (at top, hot attention region) → recent →
    # semantic → keyword. Checkpoints are compressed session summaries
    # that benefit from being visually prominent.
    #
    # All FOUR sources use the same _fenced helper for visual
    # uniformity — section header announcing count + numbered per-item
    # fences with SNAP-ID inline. The old checkpoint format had no
    # section-count header, which contributed to the model under-counting.
    def _assemble() -> str:
        parts = list(context_parts)  # media block — never dropped by the guard
        if checkpoint_snaps:
            parts.append(_fenced("Checkpoint", checkpoint_snaps, extract_snap_ids(checkpoint_snaps)))
        if recent_snaps:
            parts.append(_fenced("Recent", recent_snaps, extract_snap_ids(recent_snaps)))
        if semantic_snaps:
            parts.append(_fenced("Semantic", semantic_snaps, extract_snap_ids(semantic_snaps)))
        if keyword_snaps:
            parts.append(_fenced("Keyword-matched", keyword_snaps, extract_snap_ids(keyword_snaps)))
        return "\n\n".join(parts)

    fossil_context = _assemble()

    if is_local:
        # LOCAL (phone lean) profile: the one genuinely window-bound surface.
        # Since M8 (WI-7a) each over-budget SEMANTIC snapshot already arrived
        # windowed on its best-matched chunk (max_fossil_chars per snapshot,
        # via semantic_retrieve window_budget_chars above), so this TOTAL cap
        # is a backstop that should rarely bind; when it does, it still
        # head-truncates the assembled block (checkpoint/recent/keyword
        # sections have no chunk identity to window on).
        # IMPORTANT: capture original size BEFORE the assignment that
        # overwrites fossil_context with the truncated value (pre-2026-04-25
        # this print masked a 170K-char loss in plain sight).
        cap = PROVIDER_CAPS["local"]
        if len(fossil_context) > cap:
            original_len = len(fossil_context)
            fossil_context = (
                fossil_context[:cap]
                + "\n\n[Context truncated for token budget]"
            )
            print(
                f"{log_prefix} WARNING: Fossil context truncated from "
                f"{original_len:,} to {cap:,} chars "
                f"(dropped {original_len - cap:,} chars; cap source: provider={provider!r}; "
                f"order is checkpoint → recent → semantic → keyword, so keyword "
                f"clips first)"
            )
    else:
        # CLOUD delivery: cap-free (WI-10). The window SAFETY GUARD below is
        # not a cap — with the live count knobs it essentially never binds
        # (measured worst case ≈119k floor-tokens vs ≥240k budgets). If it
        # ever would, whole LOWEST-RANKED snapshots are dropped — NEVER a
        # mid-snapshot truncation — and every drop is logged.
        from Orchestrator.tokenization import estimate_tokens  # lazy: avoid startup cycle
        budget = (
            window_guard_tokens
            if window_guard_tokens is not None
            else window_guard_budget_tokens(provider)
        )
        est = estimate_tokens(fossil_context)
        dropped: list = []
        while est > budget:
            # Drop order = reverse of section value: keyword (lowest rank
            # first), then semantic (lowest rank first), then recent (oldest
            # first), then checkpoints (oldest first, last resort).
            if keyword_snaps:
                victim, section = keyword_snaps.pop(), "keyword"
            elif semantic_snaps:
                victim, section = semantic_snaps.pop(), "semantic"
            elif recent_snaps:
                victim, section = recent_snaps.pop(0), "recent"
            elif checkpoint_snaps:
                victim, section = checkpoint_snaps.pop(0), "checkpoint"
            else:
                break  # nothing droppable left (media block only)
            vid = (extract_snap_ids([victim]) or ["?"])[0]
            dropped.append(f"{vid}({section})")
            print(
                f"{log_prefix} window guard dropped {vid} ({section}, whole snapshot, "
                f"{len(victim):,} chars): est {est:,} > budget {budget:,} tokens "
                f"(provider={provider!r})"
            )
            fossil_context = _assemble()
            est = estimate_tokens(fossil_context)
        if dropped:
            # Provenance reflects what is DELIVERED, not what was retrieved.
            provenance = {
                "recent": extract_snap_ids(recent_snaps),
                "keyword": extract_snap_ids(keyword_snaps),
                "semantic": extract_snap_ids(semantic_snaps),
                "checkpoint": extract_snap_ids(checkpoint_snaps),
            }
            print(
                f"{log_prefix} window guard summary: dropped {len(dropped)} whole "
                f"snapshot(s) to fit budget {budget:,} tokens: {dropped}"
            )
        largest = max(
            (len(s) for s in (checkpoint_snaps + recent_snaps + semantic_snaps + keyword_snaps)),
            default=0,
        )
        print(
            f"{log_prefix} Delivery: {len(fossil_context):,} chars ≈ {est:,} floor-tokens "
            f"(window-guard budget {budget:,} tokens, provider={provider!r}); "
            f"largest snapshot {largest:,} chars, delivered WHOLE"
        )

    return fossil_context, provenance
