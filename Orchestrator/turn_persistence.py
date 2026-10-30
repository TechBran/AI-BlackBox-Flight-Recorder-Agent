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

One policy for the snapshot metadata, decided here once (O03-chat-routes-B-11):
"relevant" provenance is every retrieval channel that informed the turn
(keyword, checkpoint and semantic; three copies dropped semantic, the worker
kept keyword only), every turn sets its own LOCATION gauge ("" when it carried
none, so a CU, on-device or worker turn never inherits a phone turn's fix),
and every turn records its own token usage (the on-device turn showed the
previous turn's).

What stays with each caller: the turn's text and snapshot body (the worker's
keyword digest, /chat/save's [REASONING] block, the on-device [TOOLS USED]
block, each masked first on a wellness turn: wellness/mask.py), and the knobs
it is configured with. Those arrive as a MintGate built
from the CALLER's module namespace on every call, so a stub on
chat_routes.perform_mint or tasks.AUTO_ENABLE reaches the gate as it reached
the inline copies.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from types import ModuleType
from typing import Callable, Optional

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

    @classmethod
    def of(cls, module: ModuleType) -> "MintGate":
        """The gate as ``module`` sees it, read now: its AUTO_ENABLE, ON_RED,
        ON_YELLOW, TOKENS_THRESHOLD, TURNS_THRESHOLD, DEBOUNCE_MS, perform_mint,
        should_create_checkpoint, create_task, save_operator_state and
        drift_state_for (a stub on the module's name is honoured)."""
        return cls(
            auto_enable=module.AUTO_ENABLE, on_red=module.ON_RED, on_yellow=module.ON_YELLOW,
            tokens_threshold=module.TOKENS_THRESHOLD, turns_threshold=module.TURNS_THRESHOLD,
            debounce_ms=module.DEBOUNCE_MS, perform_mint=module.perform_mint,
            should_create_checkpoint=module.should_create_checkpoint,
            create_task=module.create_task, save_operator_state=module.save_operator_state,
            drift_state_for=module.drift_state_for,
        )


# The retrieval channels a snapshot lists as "relevant" (recent is its own list).
RELEVANT_CHANNELS = ("keyword", "checkpoint", "semantic")


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


def relevant_ids(provenance: dict) -> list:
    """The snapshot's "relevant" provenance: every RELEVANT_CHANNELS id."""
    out = []
    for key in RELEVANT_CHANNELS:
        out += provenance.get(key, [])
    return out


def _record_turn(state, operator: str, user_text: str, assistant_text: str, *,
                 gate: MintGate, snap_text: Optional[str], prompt_tokens: int,
                 completion_tokens: int, total_tokens: Optional[int], model: Optional[str],
                 location_gauge: str, provenance: Optional[dict], count_context: bool,
                 tag: str, wellness: Optional[dict] = None) -> Optional[str]:
    """Everything before the mint; returns the mint reason (None: no mint now)."""
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
    # The snapshot's LOCATION gauge (perform_mint reads it into `gauges`).
    meta["location"] = location_gauge
    # The WELLNESS tag of a turn a wellness result reached (its text arrives
    # masked). Merged, not replaced: the next mint covers every turn since the
    # last one, and perform_mint consumes it. Absent for every other turn.
    if wellness:
        from Orchestrator.wellness.mask import merge_gauge
        meta["wellness"] = merge_gauge(meta.get("wellness"), wellness)
    meta["usage_last"] = {"prompt_tokens": prompt_tokens,
                          "completion_tokens": completion_tokens,
                          "total_tokens": total_tokens}
    # perform_mint reads recent_ids and relevant_ids from last_context_meta; a
    # turn that brings none records none (never the previous turn's).
    provenance = provenance or {}
    if provenance:
        meta["gm_excerpt"] = True  # the turn's context always carried the system prompt
    meta["recent_ids"] = provenance.get("recent", [])
    meta["relevant_ids"] = relevant_ids(provenance)
    if provenance:
        print(f"{tag} Provenance - Recent: {meta['recent_ids']}, Relevant: {meta['relevant_ids']}")

    if not gate.auto_enable:
        return None
    reason = mint_reason(state, gate)
    if reason and (int(time.time() * 1000) - state.last_mint_ms >= gate.debounce_ms):
        return reason
    return None


def _finish_turn(state, operator: str, reason: Optional[str], minted: Optional[dict], *,
                 gate: MintGate, tag: str) -> TurnResult:
    """Everything after the mint: the result, the checkpoint cadence, the save."""
    result = TurnResult()
    if reason:
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


async def persist_turn_and_maybe_mint(
    state, operator: str, user_text: str, assistant_text: str, *,
    gate: MintGate,
    snap_text: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: Optional[int] = None,
    model: Optional[str] = None,
    location_gauge: str = "",
    provenance: Optional[dict] = None,
    count_context: bool = False,
    tag: str = "[TURN]",
    wellness: Optional[dict] = None,
) -> TurnResult:
    """Record one finished turn in ``state`` and mint / checkpoint as configured.

    ``user_text`` "" records no user turn. ``total_tokens`` defaults to
    prompt + completion (a provider total that counts thinking tokens is
    passed as is). ``model`` None leaves the recorded model as it is (the worker
    sets it before the provider call); ``location_gauge`` "" is a turn without
    a location; no ``provenance`` records none. ``count_context`` also adds the
    tokens to context_used (the worker's gauge). ``wellness`` is the WELLNESS
    tag of a turn whose text the caller masked (mask.Carry.gauge()); None for
    every other turn.
    """
    reason = _record_turn(state, operator, user_text, assistant_text, gate=gate,
                          snap_text=snap_text, prompt_tokens=prompt_tokens,
                          completion_tokens=completion_tokens, total_tokens=total_tokens,
                          model=model, location_gauge=location_gauge, provenance=provenance,
                          count_context=count_context, tag=tag, wellness=wellness)
    # On the event loop: the mint (ledger append + embedding) runs in a thread.
    minted = await run_blocking(gate.perform_mint, operator, reason) if reason else None
    return _finish_turn(state, operator, reason, minted, gate=gate, tag=tag)


def persist_turn_and_maybe_mint_blocking(
    state, operator: str, user_text: str, assistant_text: str, *,
    gate: MintGate,
    snap_text: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: Optional[int] = None,
    model: Optional[str] = None,
    location_gauge: str = "",
    provenance: Optional[dict] = None,
    count_context: bool = False,
    tag: str = "[TURN]",
    wellness: Optional[dict] = None,
) -> TurnResult:
    """persist_turn_and_maybe_mint for a caller on a worker thread (the /chat
    worker): the same arguments, and the mint runs inline."""
    reason = _record_turn(state, operator, user_text, assistant_text, gate=gate,
                          snap_text=snap_text, prompt_tokens=prompt_tokens,
                          completion_tokens=completion_tokens, total_tokens=total_tokens,
                          model=model, location_gauge=location_gauge, provenance=provenance,
                          count_context=count_context, tag=tag, wellness=wellness)
    # On a worker thread (the /chat worker): the mint runs inline.
    minted = gate.perform_mint(operator, reason) if reason else None
    return _finish_turn(state, operator, reason, minted, gate=gate, tag=tag)
