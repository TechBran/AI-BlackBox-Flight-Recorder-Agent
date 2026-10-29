"""Per-turn request context: the originating device, the carried location,
upload loading, the operator check, the media auto-route and the
streaming / Computer Use context builders.

Part of the chat routes (audit rank 74): see Orchestrator/routes/chat_routes.py,
the namespace every importer and test uses.
"""
from __future__ import annotations

import base64
import contextvars
import threading
import time
from pathlib import Path
from typing import Dict, Optional, List

from Orchestrator.utils.operators import validate_operator
from Orchestrator.utils.paths import (
    DOC_EXTS,
    INLINE_MAX_BYTES,
    UploadNotAcceptedError,
    UploadPathError,
    resolve_upload_path,
)
from Orchestrator.config import GOOGLE_API_KEY, current_default
from Orchestrator.fossils import (
    extract_snap_ids,
    format_snapshot_for_delivery,
    get_recent_checkpoints_for_operator,
    get_recent_fossils_for_operator,
    keyword_retrieve_for_operator,
    semantic_retrieve,
)
from Orchestrator.media import get_recent_media_artifacts
from Orchestrator.behavioral_core import PERSONA_PREF_KEY
from Orchestrator.providers.common import _last_user_msg
from Orchestrator.config import TOOLVAULT_ENABLED


# -----------------------------------------------------------------------------
# M3 origin-aware routing (frontier device control).
#
# The tailnet identity of the device that ORIGINATED the current chat request
# (the Android app self-reports its 100.64/10 tailnet IPv4; Portal/MCP leave it
# unset and fall back to the operator's primary device). It is request-scoped via
# a ContextVar so it threads into EVERY BlackBoxToolExecutor built during the turn
# without changing 60+ call sites' signatures — device-control tools
# (control_phone/control_device) read ctx.origin_device_id to default their target
# to the originating device. Absent -> None -> mesh primary-device fallback
# (unchanged behavior; fully back-compat).
# -----------------------------------------------------------------------------
_ORIGIN_DEVICE_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "origin_device_id", default=None)


def _set_origin_device_id(value: Optional[str]) -> None:
    """Stamp the current request's originating tailnet device id (or clear it).
    Normalizes blank/whitespace to None so back-compat callers stay on the
    primary-device fallback path."""
    v = (value or "").strip()
    _ORIGIN_DEVICE_ID.set(v or None)


# -----------------------------------------------------------------------------
# M1 location ride-along (per-turn metadata, sibling of origin_device_id above).
#
# The phone attaches {lat, lon, city, state} to the prompt it was already
# sending; one line is appended to the user text so it reaches the model AND the
# conversation_log item the mint renders. NOT a tracking system: no polling, no
# history, no location-minted snapshots.
#
# The streaming routes only STREAM — the client records the turn afterwards via
# POST /chat/save, a separate request. This single-slot carry hands the turn's
# location from the stream request to the save that records it. It is NOT a
# location store: exactly one slot per operator, OVERWRITTEN by every stream
# turn (including with None when the client sends none, so a Portal turn can
# never inherit a phone turn's fix), consumed once, and expired after
# _LOCATION_CARRY_TTL_S. An explicit `location` on /chat/save always wins.
# -----------------------------------------------------------------------------
_LOCATION_CARRY: Dict[str, tuple] = {}
_LOCATION_CARRY_TTL_S = 900.0
_LOCATION_CARRY_LOCK = threading.Lock()


def _stash_turn_location(operator: str, loc: Optional[dict]) -> None:
    """Record (or clear) the location for this operator's in-flight turn."""
    try:
        with _LOCATION_CARRY_LOCK:
            if loc:
                _LOCATION_CARRY[operator or ""] = (time.time(), loc)
            else:
                _LOCATION_CARRY.pop(operator or "", None)
    except Exception:
        pass


def _take_turn_location(operator: str) -> Optional[dict]:
    """Pop this operator's carried location (once). None when absent/stale."""
    try:
        with _LOCATION_CARRY_LOCK:
            entry = _LOCATION_CARRY.pop(operator or "", None)
        if not entry:
            return None
        stamped, loc = entry
        if (time.time() - stamped) > _LOCATION_CARRY_TTL_S:
            return None
        return loc
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Containment for every path a chat message part or a model tool call names
# (audit 2026-10-09 rank 16). Each attachment / reference-image loader below
# used to join the value onto a CWD-relative Path("Portal") — or take an
# absolute path verbatim — then base64 the bytes into a third-party request.
# Now they all ask resolve_upload_path() and treat a refusal like the old
# "file does not exist" branch (pass the part through / skip it) -- except an
# upload refused only for its size or type, which the user-attachment loaders
# replace with a short note so the model can say why it was not seen.
# -----------------------------------------------------------------------------
def _load_attachment(url, allowed_exts, max_bytes=None):
    """``(path, None)`` for a contained upload this loader can inline;
    ``(None, note)`` for an upload refused for its size or type, ``note``
    being text for the model; ``(None, None)`` for anything else (a missing
    file, an external URL, a reference outside the uploads)."""
    cap = INLINE_MAX_BYTES if max_bytes is None else max_bytes
    try:
        return resolve_upload_path(url, allowed_exts=allowed_exts, max_bytes=cap), None
    except UploadNotAcceptedError as e:
        print(f"[ATTACH] Not loading {str(url)[:120]!r}: {e}")
        return None, f"[Note: an attached file was not loaded: {e}]"
    except UploadPathError as e:
        print(f"[ATTACH] Not loading {str(url)[:120]!r}: {e}")
        return None, None


def _resolve_attachment(url, allowed_exts) -> Optional[Path]:
    """The contained upload ``url`` names, or None (refused or missing)."""
    return _load_attachment(url, allowed_exts)[0]


def _checked_operator(raw) -> str:
    """The request's operator, safe to join into a path or write to the ledger.

    Missing/blank falls back to the box default and surrounding whitespace is
    trimmed (as /chat always did); anything else validate_operator refuses
    ('/', '..', NUL, newlines, ...) raises InvalidOperatorName for a 400.
    """
    op = raw.strip() if isinstance(raw, str) else raw
    if op is None or op == "":
        op = current_default()
    return validate_operator(op)


def build_cu_context(user_text: str, operator: str) -> Tuple[str, dict]:
    """Build fossil context string for Computer Use agent (reduced counts).
    Returns:
        Tuple of (context_string, provenance_dict)
    """
    CU_RF, CU_KF, CU_SF, CU_ST = 4, 3, 5, 0.60
    # WI-10 (M7): CU delivery is cap-free — CU_CAP=None delivers recent
    # snapshots WHOLE (the old 10k per-snapshot cap is gone). The safety net is
    # the token window guard below, sized for the tightest CU backend window
    # (gemini-2.5-CU 131,072 tokens — the ONE cloud window that can bind).
    CU_CP, CU_CAP, CU_MA = 2, None, 5
    from Orchestrator.embeddings.search import active_threshold  # lazy: avoid startup cycle
    # CU_ST is display/log-only: it feeds semantic_retrieve's retained-but-
    # unused threshold param. The ranking floor lives in retrieval.py's
    # junk-floor resolution (M9/WI-3).
    CU_ST = active_threshold(CU_ST)

    from Orchestrator.context_builder import fill_unseen  # lazy: matches build_streaming_context's import pattern

    # No volume text: the index-backed helpers read only what they return
    # (audit X05-architecture-14; see build_fossil_context).
    recent_snaps = get_recent_fossils_for_operator("", operator, CU_RF, CU_CAP)
    recent_ids = set(extract_snap_ids(recent_snaps))

    # WI-7b dedupe-with-backfill (same pattern as build_fossil_context):
    # over-fetch each channel by len(seen_so_far), then fill the section with
    # the first section_k UNSEEN snaps — dedupe no longer shrinks sections.
    keyword_snaps_raw = keyword_retrieve_for_operator("", user_text, CU_KF + len(recent_ids), operator) if user_text else []
    keyword_snaps = fill_unseen(keyword_snaps_raw, CU_KF, recent_ids)
    seen_ids = recent_ids | set(extract_snap_ids(keyword_snaps))
    semantic_snaps_raw = semantic_retrieve(user_text, operator=operator, k=CU_SF + len(seen_ids), threshold=CU_ST) if user_text else []
    semantic_snaps = fill_unseen(semantic_snaps_raw, CU_SF, seen_ids)
    # Checkpoints stay UN-deduped: pinned section, not a discovery channel.
    checkpoint_snaps = get_recent_checkpoints_for_operator("", operator, count=CU_CP)

    print(f"[CU-CONTEXT] Operator: {operator} | Recent: {len(recent_snaps)}, Keyword: {len(keyword_snaps)}, Semantic: {len(semantic_snaps)}, Checkpoints: {len(checkpoint_snaps)}")

    # Build provenance for snapshot tracing
    provenance = {
        "recent": list(recent_ids),
        "keyword": extract_snap_ids(keyword_snaps),
        "semantic": extract_snap_ids(semantic_snaps),
        "checkpoint": extract_snap_ids(checkpoint_snaps)
    }

    context_parts = []

    # Inject operator preferences at top for priority
    from Orchestrator.state import get_operator_preference, OPERATOR_PREFERENCES
    if operator in OPERATOR_PREFERENCES:
        prefs = OPERATOR_PREFERENCES[operator]
        if prefs and isinstance(prefs, dict):
            pref_lines = ["=== OPERATOR PREFERENCES ==="]
            for key, val in prefs.items():
                if key == PERSONA_PREF_KEY:
                    continue
                pref_lines.append(f"- {key}: {val}")
            context_parts.append("\n".join(pref_lines))

    media_artifacts = get_recent_media_artifacts(operator, limit=CU_MA)
    if media_artifacts:
        lines = ["=== RECENT MEDIA ==="]
        for a in media_artifacts:
            lines.append(f"- [{a.get('type','').upper()}] {a.get('url','')} | \"{a.get('prompt','')[:80]}\"")
        context_parts.append("\n".join(lines))

    # M15.2: deliver body-only text to the CU model — same envelope-stripping as
    # build_fossil_context (_fenced). Format only the RENDERED text; the
    # recent/keyword/semantic lists stay whole so the window guard + provenance
    # below keep operating on the full blocks (and now cap CLEANER text).
    if checkpoint_snaps:
        blocks = []
        for i, cp in enumerate(checkpoint_snaps, 1):
            blocks.append(f"=== CHECKPOINT #{i} ===\n{format_snapshot_for_delivery(cp)}")
        context_parts.append("\n\n".join(blocks))

    fixed_parts = list(context_parts)  # prefs + media + checkpoints — never dropped

    def _assemble() -> str:
        parts = list(fixed_parts)
        if recent_snaps:
            parts.append("=== Recent Snapshots ===\n" + "\n\n".join(format_snapshot_for_delivery(s) for s in recent_snaps))
        if keyword_snaps:
            parts.append("=== Keyword-Matched Snapshots ===\n" + "\n\n".join(format_snapshot_for_delivery(s) for s in keyword_snaps))
        if semantic_snaps:
            parts.append("=== Semantically Relevant Snapshots ===\n" + "\n\n".join(format_snapshot_for_delivery(s) for s in semantic_snaps))
        return "\n\n".join(parts)

    cu_context = _assemble()

    # WI-10 window safety guard (not a cap): if the assembled CU context would
    # exceed the gemini-CU-sized budget, drop whole LOWEST-RANKED snapshots
    # (keyword lowest-rank first, then semantic, then oldest recent) — never
    # truncate mid-snapshot. Same semantics as context_builder's guard.
    from Orchestrator.context_builder import window_guard_budget_tokens
    from Orchestrator.tokenization import estimate_tokens
    budget = window_guard_budget_tokens("computer-use")
    est = estimate_tokens(cu_context)
    dropped = []
    while est > budget:
        if keyword_snaps:
            victim, section = keyword_snaps.pop(), "keyword"
        elif semantic_snaps:
            victim, section = semantic_snaps.pop(), "semantic"
        elif recent_snaps:
            victim, section = recent_snaps.pop(0), "recent"
        else:
            break  # prefs/media/checkpoints only — nothing droppable left
        vid = (extract_snap_ids([victim]) or ["?"])[0]
        dropped.append(f"{vid}({section})")
        print(
            f"[CU-CONTEXT] window guard dropped {vid} ({section}, whole snapshot, "
            f"{len(victim):,} chars): est {est:,} > budget {budget:,} tokens"
        )
        cu_context = _assemble()
        est = estimate_tokens(cu_context)
    if dropped:
        # Provenance reflects what is DELIVERED, not what was retrieved.
        provenance = {
            "recent": extract_snap_ids(recent_snaps),
            "keyword": extract_snap_ids(keyword_snaps),
            "semantic": extract_snap_ids(semantic_snaps),
            "checkpoint": extract_snap_ids(checkpoint_snaps),
        }
        print(
            f"[CU-CONTEXT] window guard summary: dropped {len(dropped)} whole "
            f"snapshot(s) to fit budget {budget:,} tokens: {dropped}"
        )

    return cu_context, provenance


# Providers a media auto-route never moves: Google reads audio and video
# itself, and "custom" is exempt because a LAN llama.cpp server has no audio/
# video path and a custom-only box has no Google key - the switch would fail
# the turn instead of replying (custom turns get a text note per part).
_MEDIA_ROUTE_KEEP = frozenset({"google", "gemini", "custom"})


def _unreadable_attachment_parts(part, pdf_block: bool = False) -> Optional[List[Dict]]:
    """The parts that stand in for an audio, video or document attachment the
    turn's provider cannot read, or None for any other part (the caller keeps
    handling it).

    Audio and video become a note naming the file; a document's text is
    inlined when it is plain text, a PDF becomes Claude's native document
    block when ``pdf_block`` is set, and anything else is a note. This is how
    a turn degrades when media_auto_route keeps it off Google (provider
    "custom", or a box with no Google key): the model is told what was
    attached instead of the raw part being forwarded (llama.cpp 500s on an
    audio_url part; OpenAI and xAI reject part types they do not know) or
    silently dropped, as the Anthropic stream did.
    """
    if not isinstance(part, dict):
        return None
    kind = part.get("type")
    if kind not in ("audio_url", "video_url", "document_url"):
        return None
    ref = part.get(kind)
    url = ref.get("url", "") if isinstance(ref, dict) else ""
    if kind == "video_url":
        return [{"type": "text", "text": f"[Note: Video file attached: {url}. This model doesn't support direct video analysis.]"}]
    if kind == "audio_url":
        return [{"type": "text", "text": f"[Note: Audio file attached: {url}. This model doesn't support audio input.]"}]
    try:
        local_path, refused = _load_attachment(url, DOC_EXTS)
        if refused:
            return [{"type": "text", "text": refused}]
        if local_path is None:
            return [{"type": "text", "text": f"[Note: an attached document could not be found: {url}]"}]
        suffix = local_path.suffix.lower()
        if suffix == ".pdf" and pdf_block:
            data = base64.b64encode(local_path.read_bytes()).decode("utf-8")
            return [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}]
        if suffix in (".txt", ".md", ".csv"):
            text = local_path.read_text(encoding="utf-8", errors="replace")
            return [{"type": "text", "text": f"[Document content: {local_path.name}]\n\n{text}"}]
        return [{"type": "text", "text": f"[Note: Document {local_path.name} attached but content extraction is not supported for this file type with this model]"}]
    except Exception as e:
        print(f"[ATTACH] Could not read document {str(url)[:120]!r}: {e}")
        return [{"type": "text", "text": f"[Note: an attached document could not be read: {url}]"}]


def _unreadable_attachments_as_text(messages: List[Dict]) -> List[Dict]:
    """``messages`` with each audio, video and document part replaced by what
    _unreadable_attachment_parts gives for it; every other part and message
    passes through as it is."""
    out = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            out.append(m)
            continue
        new_content = []
        for part in content:
            stand_in = _unreadable_attachment_parts(part)
            new_content.extend(stand_in if stand_in is not None else [part])
        out.append({**m, "content": new_content})
    return out


def media_auto_route(messages: list, provider: str) -> tuple[str, bool]:
    """The provider a chat turn runs on, given what is attached to it.

    An audio or video part moves the turn to Google, which reads both; a PDF
    moves an OpenAI turn there too. Returns (provider, switched): `provider` is
    unchanged unless switched, and a switched caller resolves Google's own
    default model, since the requested id belongs to the original provider.

    The switch needs a Google key: onboarding accepts any one LLM key, and on
    a box set up with only an Anthropic, OpenAI or xAI key the Google handlers
    (call_gemini, stream_gemini_with_thinking - they check the chat routes'
    GOOGLE_API_KEY) end the turn in "GOOGLE_API_KEY not set". There the turn
    stays on its provider, whose handler notes the attachment instead
    (_unreadable_attachment_parts).

    The one rule for every chat path - /chat/stream (Portal, Android, cron)
    and the non-stream worker (/chat, inbound SMS). It used to live only in
    the worker, so a streamed OpenAI or Anthropic turn sent the recording to
    a provider that cannot read it (audit W12).
    """
    has_audio_or_video = False
    has_pdf = False
    for msg in messages or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type", "")
            if part_type in ("video_url", "audio_url"):
                has_audio_or_video = True
            elif part_type == "document_url":
                url = (part.get("document_url") or {}).get("url", "")
                if isinstance(url, str) and url.lower().endswith(".pdf"):
                    has_pdf = True
    if has_audio_or_video and provider not in _MEDIA_ROUTE_KEEP:
        reason = "video/audio detected"
    elif has_pdf and provider == "openai":
        reason = "PDF document detected"
    else:
        return provider, False
    if not GOOGLE_API_KEY:
        print(f"[AUTO-ROUTE] Kept {provider} ({reason}): no Google key is configured")
        return provider, False
    print(f"[AUTO-ROUTE] Switched from {provider} to google ({reason})")
    return "google", True


def build_streaming_context(messages: list, operator: str, provider: str = "openai",
                            window_guard_tokens: int | None = None) -> Tuple[list, dict, dict]:
    """Build context-enriched messages for streaming chat with fossil retrieval.

    Delegates fossil retrieval to `Orchestrator.context_builder.build_fossil_context`
    (the shared cross-transport source of truth). This function keeps the
    transport-specific concerns: extracting user_text from `messages`, adding
    device-registry context, building the system prompt, and assembling the
    final message list.

    Returns:
        Tuple of (context_messages, provenance_dict, telemetry_dict)
        - context_messages: Messages with system prompt and fossils
        - provenance_dict: Info about retrieved fossils for snapshot
        - telemetry_dict: "The Signal" retrieval-stage metrics (opt-in,
          PRESENTATION-ONLY — never injected into any prompt/context/snapshot).
          Populated by build_fossil_context -> retrieve() with the embed/search/
          rerank/MMR/context metrics; this layer additionally records `provider`
          and `window_tokens` (the resolved window-guard budget). The caller
          adds `model` (resolved route-side). Task 1.3 emits it over SSE; today
          it is collected but unused, so the two callers may ignore it.
    """
    from Orchestrator.context_builder import build_fossil_context

    # Extract user text for keyword/semantic search (transport-specific)
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                user_text = next((p.get("text","") for p in content if p.get("type")=="text"), "")
            else:
                user_text = str(content)
            break

    # The Signal (opt-in, presentation-only): a metrics sink filled in place by
    # the retrieval pipeline. NEVER injected into the prompt/context/snapshot.
    telemetry: dict = {}

    # Shared retrieval builder — same output as the old inline pipeline.
    # Pass provider so context_builder sizes the WI-10 window safety guard
    # (whole-snapshot drops against the verified provider window; the old
    # per-provider char caps are gone — see PROVIDER_WINDOW_GUARD_TOKENS).
    fossil_context, provenance = build_fossil_context(
        user_text=user_text,
        operator=operator,
        log_prefix="[STREAM CONTEXT]",
        provider=provider,
        # None = today's behavior (provider window table). provider="custom"
        # callers thread the resolved server's context_tokens here (Task 4.3).
        window_guard_tokens=window_guard_tokens,
        telemetry=telemetry,
    )

    # Brain + window metrics for The Signal: provider is a param; the window
    # budget is the SAME value build_fossil_context handed the cloud guard
    # (the explicit override, else the provider window table). `model` is added
    # route-side (it is resolved there, after this call). Presentation-only.
    telemetry["provider"] = provider
    from Orchestrator.context_builder import window_guard_budget_tokens
    telemetry["window_tokens"] = (
        window_guard_tokens if window_guard_tokens is not None
        else window_guard_budget_tokens(provider)
    )

    # Add device registry context (transport/route-specific: only chat_stream uses this)
    try:
        from Orchestrator.device_registry import get_registry
        device_context = get_registry().to_prompt_context(owner=operator)
        if device_context and "No devices" not in device_context:
            fossil_context += f"\n\n## Available Devices\n{device_context}"
    except Exception:
        pass

    # Build system prompt — TOOLVAULT_ENABLED injects dynamic tool instructions
    from Orchestrator.tasks import build_core_system_prompt
    if TOOLVAULT_ENABLED:
        from Orchestrator.toolvault.injector import inject_for_prompt
        user_text_sp = _last_user_msg(messages)
        if user_text_sp:
            _, tool_instructions = inject_for_prompt(user_text_sp, provider or "openai")
            system_prompt = build_core_system_prompt(tool_instructions, operator=operator)
        else:
            system_prompt = build_core_system_prompt("", operator=operator)
    else:
        system_prompt = build_core_system_prompt("", operator=operator)

    # Final message list (system prompt + fossil block + original messages)
    context_messages = [
        {"role": "system", "content": system_prompt}
    ]
    if fossil_context:
        context_messages.append({"role": "system", "content": fossil_context})
    context_messages.extend(messages)

    return context_messages, provenance, telemetry
