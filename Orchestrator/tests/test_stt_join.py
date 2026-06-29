from Orchestrator.stt.streaming import join_transcript_segments as j


def test_empty_prefix_returns_text():
    assert j("", "hello world") == "hello world"


def test_joins_with_single_space():
    assert j("first part", "second part") == "first part second part"


def test_no_double_space_when_prefix_ends_with_space():
    assert j("first ", "second") == "first second"


def test_no_double_space_when_prefix_ends_with_newline():
    assert j("first\n", "second") == "first\nsecond"


def test_empty_text_returns_prefix():
    assert j("first", "") == "first"
