from Orchestrator.asterisk import secrets


def test_roundtrip():
    enc = secrets.encrypt("6157Ego8@")
    assert enc != "6157Ego8@"
    assert enc.startswith("enc:")
    assert secrets.decrypt(enc) == "6157Ego8@"


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
