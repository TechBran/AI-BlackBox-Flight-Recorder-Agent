from Orchestrator.asterisk import credentials


def test_roundtrip():
    # Neutral fixture — never use a real credential as a test constant.
    sample = "unit-test-plaintext-7f3a"
    enc = credentials.encrypt(sample)
    assert enc != sample
    assert enc.startswith("enc:")
    assert credentials.decrypt(enc) == sample


def test_plaintext_passthrough_decrypt():
    # legacy/plaintext values (no enc: prefix) decrypt to themselves (migration tolerance)
    assert credentials.decrypt("plain") == "plain"
    assert credentials.decrypt("") == ""


def test_mask():
    assert credentials.mask("anything") is True
    assert credentials.mask("") is False
    assert credentials.mask(None) is False


def test_encrypt_empty_is_empty():
    # don't wrap empty strings — keeps "no secret set" semantics clean
    assert credentials.encrypt("") == ""
