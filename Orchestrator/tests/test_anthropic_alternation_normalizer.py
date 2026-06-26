"""Anthropic alternation normalizer (Workstream C, defense-in-depth).

The Anthropic Messages API 400s with `messages: roles must alternate` when the
conversation array contains two consecutive same-role turns. M4 fixed the SMS
path's consecutive-[user, user] case in Orchestrator/sms/router.py, but ANY
caller producing consecutive same-role turns would still 400. `call_anthropic`
in chat_routes.py now runs every conversation through `_normalize_alternation`
before building the Anthropic request: consecutive same-role user/assistant
turns are merged into one (content concatenated), system messages are left in
place, and a correctly-alternating list is returned unchanged.

Content may be a plain str OR a list of content blocks (multimodal). The merge
must never drop or corrupt content blocks.
"""
from Orchestrator.routes.chat_routes import (
    _normalize_alternation,
    _prepare_anthropic_messages,
)


def _roles(msgs):
    return [m["role"] for m in msgs]


def test_consecutive_users_merge_into_one():
    out = _normalize_alternation([
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ])
    assert _roles(out) == ["user"]
    assert "first" in out[0]["content"]
    assert "second" in out[0]["content"]


def test_consecutive_assistants_merge_into_one():
    out = _normalize_alternation([
        {"role": "assistant", "content": "a1"},
        {"role": "assistant", "content": "a2"},
    ])
    assert _roles(out) == ["assistant"]
    assert "a1" in out[0]["content"]
    assert "a2" in out[0]["content"]


def test_normal_alternating_list_unchanged():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "how are you"},
    ]
    out = _normalize_alternation(msgs)
    assert out == msgs
    # First conversation message must still be user.
    assert out[0]["role"] == "user"


def test_str_plus_str_join_preserves_both():
    out = _normalize_alternation([
        {"role": "user", "content": "alpha"},
        {"role": "user", "content": "beta"},
    ])
    assert len(out) == 1
    content = out[0]["content"]
    assert isinstance(content, str)
    assert content == "alpha\nbeta" or content == "alpha\n\nbeta"


def test_list_plus_list_concatenates_blocks_none_dropped():
    blocks_a = [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
    ]
    blocks_b = [
        {"type": "text", "text": "and this"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "BBBB"}},
    ]
    out = _normalize_alternation([
        {"role": "user", "content": blocks_a},
        {"role": "user", "content": blocks_b},
    ])
    assert len(out) == 1
    content = out[0]["content"]
    assert isinstance(content, list)
    # All four blocks survive, order preserved.
    assert content == blocks_a + blocks_b
    # Both images survive.
    images = [b for b in content if b.get("type") == "image"]
    assert len(images) == 2


def test_str_plus_list_normalizes_to_combined_list():
    blocks_b = [
        {"type": "text", "text": "with image"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "ZZZZ"}},
    ]
    out = _normalize_alternation([
        {"role": "user", "content": "just text"},
        {"role": "user", "content": blocks_b},
    ])
    assert len(out) == 1
    content = out[0]["content"]
    assert isinstance(content, list)
    # The str became a text block, then the list blocks followed; nothing dropped.
    assert content[0] == {"type": "text", "text": "just text"}
    assert content[1:] == blocks_b
    images = [b for b in content if b.get("type") == "image"]
    assert len(images) == 1


def test_list_plus_str_normalizes_to_combined_list():
    blocks_a = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "ZZZZ"}},
        {"type": "text", "text": "leading image"},
    ]
    out = _normalize_alternation([
        {"role": "user", "content": blocks_a},
        {"role": "user", "content": "trailing text"},
    ])
    assert len(out) == 1
    content = out[0]["content"]
    assert isinstance(content, list)
    assert content[:2] == blocks_a
    assert content[2] == {"type": "text", "text": "trailing text"}
    images = [b for b in content if b.get("type") == "image"]
    assert len(images) == 1


def test_realistic_sms_thread_merges_trailing_users():
    msgs = [
        {"role": "user", "content": "yo"},
        {"role": "assistant", "content": "hey"},
        {"role": "user", "content": "you there"},
        {"role": "user", "content": "hello??"},
    ]
    out = _normalize_alternation(msgs)
    assert _roles(out) == ["user", "assistant", "user"]
    # Valid strict alternation (no two adjacent same roles).
    roles = _roles(out)
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))
    # First conversation message stays user.
    assert out[0]["role"] == "user"
    # The two trailing user turns are both represented in the merged turn.
    assert "you there" in out[-1]["content"]
    assert "hello??" in out[-1]["content"]


def test_system_messages_left_in_place():
    # System is handled separately in call_anthropic; the normalizer must not
    # touch system rows nor merge across them.
    msgs = [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "u1"},
        {"role": "user", "content": "u2"},
    ]
    out = _normalize_alternation(msgs)
    assert _roles(out) == ["system", "user"]
    assert out[0] == {"role": "system", "content": "be nice"}
    assert "u1" in out[-1]["content"]
    assert "u2" in out[-1]["content"]


def test_empty_list_returns_empty():
    assert _normalize_alternation([]) == []


def test_preserves_extra_keys_on_first_of_merged_pair():
    out = _normalize_alternation([
        {"role": "user", "content": "x", "name": "alice", "id": 7},
        {"role": "user", "content": "y"},
    ])
    assert len(out) == 1
    assert out[0].get("name") == "alice"
    assert out[0].get("id") == 7


def test_three_consecutive_users_collapse_to_one():
    out = _normalize_alternation([
        {"role": "user", "content": "1"},
        {"role": "user", "content": "2"},
        {"role": "user", "content": "3"},
    ])
    assert _roles(out) == ["user"]
    for tok in ("1", "2", "3"):
        assert tok in out[0]["content"]


# ---------------------------------------------------------------------------
# _prepare_anthropic_messages: extract system FIRST, then normalize the
# user/assistant-only convo. Guards the dormant gap where an interleaved
# system row breaks list-adjacency so two surrounding user turns never merge,
# then system extraction drops the row -> [user, user] -> Anthropic 400.
# ---------------------------------------------------------------------------


def test_prepare_interspersed_system_between_users_still_merges():
    # [user, system, user]: the system row sits BETWEEN two user turns. After
    # extracting system out first, the two users are adjacent and must merge to
    # ONE user turn. (Pre-fix this produced [user, user] and 400'd.)
    system_text, convo = _prepare_anthropic_messages([
        {"role": "user", "content": "u1"},
        {"role": "system", "content": "sys note"},
        {"role": "user", "content": "u2"},
    ])
    assert _roles(convo) == ["user"], (
        "interspersed system row must not block the two user turns from merging")
    # Strict alternation: no two adjacent same roles.
    roles = _roles(convo)
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))
    # Both user payloads survive in the merged turn.
    assert "u1" in convo[0]["content"]
    assert "u2" in convo[0]["content"]
    # System text is extracted unchanged.
    assert "sys note" in system_text
    # No system row leaks into the conversation array.
    assert all(m["role"] != "system" for m in convo)


def test_prepare_front_loaded_system_normal_path_unchanged():
    # The real-world BlackBox shape: system front-loaded, convo alternates.
    system_text, convo = _prepare_anthropic_messages([
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "thanks"},
    ])
    assert _roles(convo) == ["user", "assistant", "user"]
    assert convo[0]["role"] == "user"
    assert system_text == "be helpful"
    assert all(m["role"] != "system" for m in convo)


def test_prepare_multiple_system_rows_joined():
    system_text, convo = _prepare_anthropic_messages([
        {"role": "system", "content": "first"},
        {"role": "system", "content": "second"},
        {"role": "user", "content": "hi"},
    ])
    assert system_text == "first\n\nsecond"
    assert _roles(convo) == ["user"]


def test_prepare_trailing_consecutive_users_still_merge():
    # Defends the SMS-style thread through the prepare wrapper too.
    system_text, convo = _prepare_anthropic_messages([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "yo"},
        {"role": "assistant", "content": "hey"},
        {"role": "user", "content": "there?"},
        {"role": "user", "content": "hello??"},
    ])
    assert _roles(convo) == ["user", "assistant", "user"]
    assert system_text == "sys"
    assert "there?" in convo[-1]["content"]
    assert "hello??" in convo[-1]["content"]


def test_prepare_no_system_returns_empty_system_text():
    system_text, convo = _prepare_anthropic_messages([
        {"role": "user", "content": "u1"},
        {"role": "user", "content": "u2"},
    ])
    assert system_text == ""
    assert _roles(convo) == ["user"]
