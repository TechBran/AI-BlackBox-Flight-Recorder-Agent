"""Unit tests for NeoGate/Boa discovery fingerprinting.

These exercise the pure detection helpers (`_looks_like_neogate` and
`_detect_model`) so the discovery match logic is testable without sockets.
"""

from Orchestrator.asterisk import gateway_manager as gm


# ---------------------------------------------------------------------------
# _looks_like_neogate
# ---------------------------------------------------------------------------
def test_boa_plus_neogate_body_matches():
    assert gm._looks_like_neogate(
        "Boa/0.94.14rc21", "<html>...astman.js...NeoGate TG...</html>"
    ) is True


def test_boa_alone_does_not_match():
    # Boa is a generic embedded server — must NOT match without a NeoGate token.
    assert gm._looks_like_neogate(
        "Boa/0.94.14rc21", "<html>generic router</html>"
    ) is False


def test_yeastar_server_header_matches():
    assert gm._looks_like_neogate("Yeastar", "whatever") is True


def test_body_keyword_matches():
    assert gm._looks_like_neogate("nginx", "TG800 VoIP Gateway") is True


def test_boa_plus_webcgi_body_matches():
    assert gm._looks_like_neogate("Boa/0.94.14rc21", "loads /cgi/WebCGI") is True


def test_boa_plus_mypbx_body_matches():
    assert gm._looks_like_neogate("Boa/0.94.14rc21", "<script src=mypbx.js>") is True


def test_boa_plus_yeastar_body_matches():
    assert gm._looks_like_neogate("Boa/0.94.14rc21", "Yeastar device") is True


def test_neogate_body_keyword_matches_any_server():
    assert gm._looks_like_neogate("", "Welcome to NeoGate") is True


def test_generic_server_generic_body_no_match():
    assert gm._looks_like_neogate("nginx", "<html>hello world</html>") is False


# ---------------------------------------------------------------------------
# _detect_model
# ---------------------------------------------------------------------------
def test_detect_model():
    assert gm._detect_model("...TG800...") == "TG800"
    assert gm._detect_model("NeoGate TG (no number)") == "TG200"  # default


def test_detect_model_all_variants():
    assert gm._detect_model("model TG100 here") == "TG100"
    assert gm._detect_model("model tg200 here") == "TG200"
    assert gm._detect_model("MODEL TG400") == "TG400"
    assert gm._detect_model("...TG1600...") == "TG1600"


def test_detect_model_default_is_tg200():
    assert gm._detect_model("") == "TG200"
    assert gm._detect_model("nothing relevant") == "TG200"
