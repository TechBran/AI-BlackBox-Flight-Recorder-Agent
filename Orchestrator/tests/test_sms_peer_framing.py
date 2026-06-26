"""M3 Task 3.3 -- peer reply framing in the SMS system prefix (tasks.py).

When an inbound SMS is classified ``peer`` (a whitelisted non-operator, e.g.
Anna, texting the operator's AI), the chat payload carries ``sms_peer=True``.
The SMS system prefix must then frame the reply as being sent to the sender ON
THE OPERATOR'S BEHALF -- the operator's own persona still loads separately via
build_core_system_prompt; only the SMS prefix text gains the peer framing.

A ``self`` inbound (sms_peer absent/False -- the operator texting their own AI)
keeps today's framing with NO "on behalf" language.

Targets the pure prefix builder ``build_sms_prompt_prefix`` so the assertion does
not require spinning up the whole process_chat_task pipeline.
"""
from Orchestrator.tasks import build_sms_prompt_prefix


def test_peer_prefix_frames_on_behalf_of_operator():
    prefix = build_sms_prompt_prefix(
        sms_contact_name="Anna", sms_sender="+2223334444",
        sms_peer=True, operator="Brandon",
    )
    low = prefix.lower()
    assert "on behalf" in low or "on the operator's behalf" in low or "on brandon's behalf" in low
    # The peer's name must be named as the person being replied to.
    assert "Anna" in prefix


def test_self_prefix_has_no_on_behalf_framing():
    prefix = build_sms_prompt_prefix(
        sms_contact_name="Me", sms_sender="+13335557777",
        sms_peer=False, operator="Brandon",
    )
    assert "on behalf" not in prefix.lower()
    # Still a valid SMS prefix (today's framing preserved).
    assert "SMS" in prefix
    assert "Me" in prefix


def test_peer_prefix_is_superset_of_self_prefix_rules():
    """Peer framing is ADDITIVE -- the base SMS rules still apply in both modes."""
    self_prefix = build_sms_prompt_prefix("X", "+1", sms_peer=False, operator="Op")
    peer_prefix = build_sms_prompt_prefix("X", "+1", sms_peer=True, operator="Op")
    # Both keep the core 160-char SMS rule line.
    assert "160 characters" in self_prefix
    assert "160 characters" in peer_prefix
    # Only the peer prefix carries the on-behalf framing ("on <operator>'s behalf").
    assert "behalf" in peer_prefix.lower()
    assert "behalf" not in self_prefix.lower()
