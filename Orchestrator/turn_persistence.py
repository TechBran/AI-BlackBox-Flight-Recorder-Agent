"""persist_turn_and_maybe_mint(): the tail every finished chat turn ends in.

Audit rank 74 (O03-chat-routes-B-11, X05-architecture-12). Four paths record a
turn and decide whether to mint it: POST /chat/save (every Portal, Android,
voice and CLI turn), the Computer Use loops (_cu_save_to_blackbox), the
on-device turn (persist_local_turn_and_mint) and the /chat worker
(tasks.process_chat_task: cron, SMS, MCP). Each carried its own copy of the
same sequence — add the turns, bump the counters, map the retrieval provenance
into the snapshot metadata, the drift / tokens / turns mint gate with its
debounce, the checkpoint cadence, save the state — and the copies drifted. The
sequence now lives here once.

What stays with each caller: the turn's text and snapshot body (the worker's
keyword digest, /chat/save's [REASONING] block, the on-device [TOOLS USED]
block), and the knobs it is configured with. Those arrive as a MintGate built
from the CALLER's module namespace on every call, so a stub on
chat_routes.perform_mint or tasks.AUTO_ENABLE reaches the gate as it reached
the inline copies.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from Orchestrator.models import TaskType
from Orchestrator.utils.async_helpers import run_blocking
from Orchestrator.volume import now_utc_iso


@dataclass(frozen=True)
class MintGate:
    """The auto-mint knobs and hooks as the calling module sees them."""
    auto_enable: bool
    on_red: str
    on_yellow: str
    tokens_threshold: int
    turns_threshold: int
    debounce_ms: int
    perform_mint: Callable          # perform_mint(operator, reason) -> {"snap_id", "dims", ...}
    should_create_checkpoint: Callable
    create_task: Callable
    save_operator_state: Callable
    drift_state_for: Callable


@dataclass
class TurnResult:
    minted: bool = False
    snap_id: Optional[str] = None
    dims: Optional[int] = None       # embedding dimension of the minted snapshot, when one was made
    checkpoint_triggered: bool = False


def mint_reason(state, gate: MintGate) -> Optional[str]:
    """Why this turn mints (DRIFT_RED / DRIFT_YELLOW / TOKENS / TURNS), or None."""
    d = gate.drift_state_for(state)
    if d == "red" and gate.on_red == "auto":
        return "DRIFT_RED"
    if d == "yellow" and gate.on_yellow == "auto":
        return "DRIFT_YELLOW"
    if state.conv_tokens_since >= gate.tokens_threshold:
        return "TOKENS"
    if state.conv_turns_since >= gate.turns_threshold:
        return "TURNS"
    return None


def relevant_ids(provenance: dict, keys: Iterable[str]) -> list:
    """The snapshot's "relevant" provenance: the retrieval channels in ``keys``."""
    out = []
    for key in keys:
        out += provenance.get(key, [])
    return out


async def persist_turn_and_maybe_mint(
    state, operator: str, user_text: str, assistant_text: str, *,
    gate: MintGate,
    snap_text: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: Optional[int] = None,
    model: Optional[str] = None,
    record_usage: bool = True,
    location_gauge: Optional[str] = None,
    provenance: Optional[dict] = None,
    relevant_keys: Iterable[str] = ("keyword", "checkpoint"),
    count_context: bool = False,
    offload: bool = True,
    tag: str = "[TURN]",
) -> TurnResult:
    """Record one finished turn in ``state`` and mint / checkpoint as configured.

    ``user_text`` "" records no user turn. ``total_tokens`` defaults to
    prompt + completion (a provider total that counts thinking tokens is
    passed as is). ``model`` None, ``record_usage``
    False, ``location_gauge`` None and ``provenance`` empty leave that piece of
    the snapshot metadata as it is (the worker sets them before the provider
    call). ``count_context`` also adds the tokens to context_used (the worker's
    gauge). ``offload`` runs the mint off the event loop; a caller on a worker
    thread mints inline.
    """
    utc_now = now_utc_iso()
    if user_text:
        state.add_conversation_turn({"role": "user", "utc": utc_now, "text": user_text})
    state.add_conversation_turn({
        "role": "assistant", "utc": utc_now, "text": assistant_text,
        "snap_text": assistant_text if snap_text is None else snap_text,
    })

    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens
    state.conv_turns_since += 1
    state.conv_tokens_since += total_tokens
    if count_context:
        state.context_used += total_tokens
    state.total_turns += 1
    state.total_tokens += total_tokens

    meta = state.last_context_meta
    if model is not None:
        meta["model"] = model
    if location_gauge is not None:
        # The snapshot's LOCATION gauge (perform_mint reads it into `gauges`).
        meta["location"] = location_gauge
    if record_usage:
        meta["usage_last"] = {"prompt_tokens": prompt_tokens,
                              "completion_tokens": completion_tokens,
                              "total_tokens": total_tokens}
    if provenance:
        # perform_mint reads recent_ids and relevant_ids from last_context_meta.
        meta["gm_excerpt"] = True  # the turn's context always carried the system prompt
        meta["recent_ids"] = provenance.get("recent", [])
        meta["relevant_ids"] = relevant_ids(provenance, relevant_keys)
        print(f"{tag} Provenance - Recent: {meta['recent_ids']}, Relevant: {meta['relevant_ids']}")

    result = TurnResult()
    if gate.auto_enable:
        now_ms = int(time.time() * 1000)
        reason = mint_reason(state, gate)
        if reason and (now_ms - state.last_mint_ms >= gate.debounce_ms):
            if offload:
                minted = await run_blocking(gate.perform_mint, operator, reason)
            else:
                minted = gate.perform_mint(operator, reason)
            state.last_mint_ms = int(time.time() * 1000)
            result.minted = True
            result.snap_id = (minted or {}).get("snap_id")
            # None when no vector was generated (fresh box / soft failure)
            result.dims = (minted or {}).get("dims")
            print(f"{tag} Auto-mint triggered for {operator}: {reason} -> {result.snap_id}")

    if gate.should_create_checkpoint(operator):
        print(f"{tag} Creating checkpoint task for {operator} at turn {state.total_turns}")
        result.checkpoint_triggered = True
        checkpoint_task = gate.create_task(TaskType.CHECKPOINT, operator=operator,
                                           result_data={"manual": False})
        print(f"{tag} Checkpoint task created: {getattr(checkpoint_task, 'task_id', '?')}")

    gate.save_operator_state()
    return result
