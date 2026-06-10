from Orchestrator.asterisk import secrets


def test_roundtrip():
    # Neutral fixture — never use a real credential as a test constant.
    sample = "unit-test-plaintext-7f3a"
    enc = secrets.encrypt(sample)
    assert enc != sample
    assert enc.startswith("enc:")
    assert secrets.decrypt(enc) == sample


def test_plaintext_passthrough_decrypt():
    # legacy/plaintext values (no enc: prefix) decrypt to themselves (migration tolerance)
    assert secrets.decrypt("plain") == "plain"
    assert secrets.decrypt("") == ""


def test_mask():
    assert secrets.mask("anything") is True
    assert secrets.mask("") is False
    assert secrets.mask(None) is False


def test_encrypt_empty_is_empty():
    # don't wrap empty strings — keeps "no secret set" semantics clean
    assert secrets.encrypt("") == ""
