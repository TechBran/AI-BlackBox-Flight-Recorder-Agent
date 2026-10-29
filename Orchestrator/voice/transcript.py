"""The transcript a live-voice session saves to the ledger (one copy for all
three providers and the phone legs).

The routes' save functions differ only in the header they write and the label
of the AI's turns; persistence itself is voice_ws_shared.save_voice_transcript
(/chat/save with the write-ahead retry spool), which the route passes in as
``persist`` so a route-level stand-in (the tests') is honoured.
"""

from typing import Awaitable, Callable, Iterable, Tuple

from Orchestrator.routes.voice_ws_shared import conversation_releaser


def format_transcript(conversation, ai_label: str = "AI") -> str:
    """The conversation as "[User]: ..." / "[<ai_label>]: ..." lines, in
    timestamp order (a Whisper transcript can land after the reply it preceded)."""
    ordered = sorted(conversation, key=lambda x: x.get("timestamp", ""))
    lines = []
    for msg in ordered:
        role = "User" if msg["role"] == "user" else ai_label
        lines.append(f"[{role}]: {msg['content']}")
    return "\n\n".join(lines)


async def save_session_transcript(session, *, title: str, fields: Iterable[Tuple[str, str]],
                                  ai_label: str, user_message: str, model_label: str,
                                  log_prefix: str,
                                  persist: Callable[..., Awaitable[bool]]) -> None:
    """Save `session`'s conversation, and drop the turns once they are safe.

    ``fields`` are the header lines after "Session ID:", in order, as
    (label, value) pairs. The conversation is cleared ONLY once the transcript
    is safe — saved, or in the retry spool (P1b cross-route contract; rank 51
    write-ahead spool): a failed save keeps it for the next teardown path or
    the reaper. A turn that lands while the save runs stays for the next save.
    """
    if not session.conversation:
        print(f"{log_prefix} No conversation to save for session {session.session_id}")
        return

    if not session.operator:
        print(f"{log_prefix} No operator set, cannot save session {session.session_id}")
        return

    header = "\n".join([f"=== {title} ===", f"Session ID: {session.session_id}"]
                       + [f"{label}: {value}" for label, value in fields])
    session_summary = f"""{header}
Messages: {len(session.conversation)}

--- Transcript ---
{format_transcript(session.conversation, ai_label)}
--- End Session ---"""

    print(f"{log_prefix} Saving session {session.session_id} with "
          f"{len(session.conversation)} messages to BlackBox")

    release = conversation_releaser(session)
    saved = await persist(
        operator=session.operator,
        user_message=user_message,
        session_summary=session_summary,
        model_label=model_label,
        log_prefix=log_prefix,
        session_id=session.session_id,
        on_safe=release,
    )

    if saved:
        release()
    else:
        print(f"{log_prefix} Save FAILED and the retry spool is unavailable — keeping "
              f"{len(session.conversation)} turns in memory for the next save attempt")
