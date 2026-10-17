"""Secret redaction for print(), logging and error strings (audit rank 23:
O07-tts-voice-01 + X03-security-backend-11 + O02-chat-routes-A-07).

Most of the Orchestrator logs with print(), and requests/httpx put the full
request URL in their exception text, so one masking layer sits on
sys.stdout/sys.stderr and on every logging handler. These tests pin:

  * the current values of secret-named env vars are masked (and a rotated
    value is picked up without a restart),
  * the generic key shapes (Google, OpenAI/Anthropic, xAI, ?key=, Bearer) are
    masked, while look-alike identifiers ("task-...") are left alone,
  * the stdio wrapper masks print() output, is idempotent, and never breaks
    fileno/isatty/flush/buffer (binary) writes,
  * the logging filter masks the message, %-args and tracebacks without
    collapsing args tuples that formatters such as uvicorn's unpack,
  * app.py installs the layer before any Orchestrator module can print.

Every key below is assembled from pieces so no literal key shape sits in this
public repo.
"""
import io
import logging
import sys
from pathlib import Path

import pytest

from Orchestrator import secret_redaction as sr

FAKE_GOOGLE = "AIza" + "Q" * 35          # Google API key shape (39 chars)
FAKE_OPENAI = "sk-" + "proj-" + "t" * 24  # OpenAI/Anthropic key shape
FAKE_XAI = "xai-" + "x" * 24
ENV_VALUE = "env-secret-" + "v" * 16
# "?key=<value>" as a leaked URL carries it; split so the URL-key guard
# (test_no_key_in_url_guard.py) does not flag this file.
KEY_QS = "?" + "key="


@pytest.fixture(autouse=True)
def _fresh_snapshot():
    sr.refresh_secrets()
    yield
    sr.refresh_secrets()


# --------------------------------------------------------------------------- #
# redact(): env values                                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("suffix", ["_API_KEY", "_SECRET", "_SECRET_KEY", "_PRIVATE_KEY", "_TOKEN", "_PASSWORD", "_AUTH", "_SID"])
def test_masks_current_value_of_secret_named_env_vars(monkeypatch, suffix):
    name = f"BBX_REDACT_TEST{suffix}"
    monkeypatch.setenv(name, ENV_VALUE)
    out = sr.redact(f"calling upstream with {ENV_VALUE} now")
    assert ENV_VALUE not in out
    assert f"[REDACTED:{name}]" in out


def test_short_values_and_non_secret_names_are_left_alone(monkeypatch):
    monkeypatch.setenv("BBX_REDACT_SHORT_TOKEN", "abc12345")          # < 12 chars
    monkeypatch.setenv("BBX_REDACT_HOSTNAME", "box-hostname-long-value")  # not a secret name
    text = "abc12345 box-hostname-long-value"
    assert sr.redact(text) == text


def test_new_env_secret_is_masked_without_explicit_refresh(monkeypatch):
    sr.redact("warm the snapshot")
    monkeypatch.setenv("BBX_REDACT_LATE_API_KEY", ENV_VALUE)
    assert ENV_VALUE not in sr.redact(ENV_VALUE)


def test_rotated_env_value_is_masked_after_refresh_window(monkeypatch):
    monkeypatch.setenv("BBX_REDACT_ROT_API_KEY", ENV_VALUE)
    assert ENV_VALUE not in sr.redact(ENV_VALUE)
    rotated = "rotated-secret-" + "r" * 16
    monkeypatch.setenv("BBX_REDACT_ROT_API_KEY", rotated)   # same env size
    monkeypatch.setattr(sr, "_ENV_REFRESH_S", 0.0)
    assert rotated not in sr.redact(rotated)


# --------------------------------------------------------------------------- #
# redact(): generic patterns                                                  #
# --------------------------------------------------------------------------- #

def test_masks_google_key_in_url_and_keeps_other_params():
    url = f"https://generativelanguage.googleapis.com/v1beta/models/m:streamGenerateContent{KEY_QS}{FAKE_GOOGLE}&alt=sse"
    out = sr.redact(f"404 Client Error for url: {url}")
    assert FAKE_GOOGLE not in out
    assert "key=[REDACTED]" in out
    assert "&alt=sse" in out


def test_masks_non_google_key_query_values():
    out = sr.redact("GET /x?alt=json&key=plainsecretvalue123 HTTP/1.1")
    assert "plainsecretvalue123" not in out
    assert "&key=[REDACTED]" in out


@pytest.mark.parametrize("secret", [FAKE_GOOGLE, FAKE_OPENAI, FAKE_XAI])
def test_masks_bare_key_shapes(secret):
    out = sr.redact(f'{{"api_key": "{secret}"}}')
    assert secret not in out
    assert "[REDACTED]" in out


def test_masks_bearer_tokens():
    token = "eyJ" + "a" * 30
    out = sr.redact(f"Authorization: Bearer {token}")
    assert token not in out
    assert out.startswith("Authorization: Bearer ")


def test_look_alike_identifiers_are_not_masked():
    text = ("task-" + "1" * 30) + " risk-" + "a" * 30 + " desk-" + "b" * 30
    assert sr.redact(text) == text


def test_redact_is_idempotent_and_passes_non_strings_through():
    once = sr.redact(f"x {FAKE_GOOGLE} {KEY_QS}{FAKE_GOOGLE}")
    assert sr.redact(once) == once
    assert sr.redact(None) is None
    assert sr.redact(b"raw bytes") == b"raw bytes"
    assert sr.redact("") == ""


# --------------------------------------------------------------------------- #
# stdout / stderr wrapper                                                     #
# --------------------------------------------------------------------------- #

def test_print_output_is_masked(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdout", sys.stdout)   # restored at teardown
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    sr.install_stdio_redaction()
    print(f"[GEMINI] request failed for {KEY_QS}{FAKE_GOOGLE}")
    print(f"traceback mentions {FAKE_OPENAI}", file=sys.stderr)
    captured = capsys.readouterr()
    assert FAKE_GOOGLE not in captured.out
    assert "key=[REDACTED]" in captured.out
    assert FAKE_OPENAI not in captured.err


def test_stdio_install_is_idempotent(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    sr.install_stdio_redaction()
    first = sys.stdout
    sr.install_stdio_redaction()
    assert sys.stdout is first
    assert not getattr(first._stream, "_blackbox_redacting", False)


class _FakeStream:
    def __init__(self):
        self.text = []
        self.flushed = 0
        self.buffer = io.BytesIO()
        self.encoding = "utf-8"

    def write(self, s):
        self.text.append(s)
        return len(s)

    def flush(self):
        self.flushed += 1

    def fileno(self):
        return 7

    def isatty(self):
        return True


def test_wrapper_forwards_stream_api_and_binary_writes():
    raw = _FakeStream()
    w = sr.RedactingStream(raw)
    assert w.write(f"k={FAKE_GOOGLE}") == len(f"k={FAKE_GOOGLE}")
    w.writelines([f"a {FAKE_XAI}\n", "b\n"])
    w.flush()
    assert w.fileno() == 7
    assert w.isatty() is True
    assert w.encoding == "utf-8"
    w.buffer.write(b"\x00\xff" + FAKE_GOOGLE.encode())   # binary path untouched
    assert raw.buffer.getvalue() == b"\x00\xff" + FAKE_GOOGLE.encode()
    joined = "".join(raw.text)
    assert FAKE_GOOGLE not in joined and FAKE_XAI not in joined
    assert raw.flushed == 1


def test_wrapper_passes_non_str_writes_through_unchanged():
    raw = _FakeStream()
    w = sr.RedactingStream(raw)
    w.write(b"bytes")
    assert raw.text == [b"bytes"]


# --------------------------------------------------------------------------- #
# logging filter                                                              #
# --------------------------------------------------------------------------- #

class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.lines = []
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        self.records.append(record)
        self.lines.append(self.format(record))


@pytest.fixture
def isolated_logger():
    logger = logging.getLogger("bbx.test.secret_redaction")
    handler = _ListHandler()
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    try:
        yield logger, handler
    finally:
        logger.removeHandler(handler)


def test_logging_masks_message_and_keeps_args_tuple(isolated_logger):
    logger, handler = isolated_logger
    sr.install_log_redaction()
    logger.warning('%s "%s %s" %d', "127.0.0.1:5000", "GET", f"/x{KEY_QS}{FAKE_GOOGLE}", 200)
    assert FAKE_GOOGLE not in handler.lines[0]
    rec = handler.records[0]
    assert isinstance(rec.args, tuple) and len(rec.args) == 4   # uvicorn-style unpack still works
    assert rec.args[3] == 200


def test_logging_masks_exception_args_and_tracebacks(isolated_logger):
    logger, handler = isolated_logger
    sr.install_log_redaction()
    logger.error("failed: %s", RuntimeError(f"for url {KEY_QS}{FAKE_GOOGLE}"))
    try:
        raise RuntimeError(f"boom {FAKE_OPENAI}")
    except RuntimeError:
        logger.exception("upstream call failed")
    joined = "\n".join(handler.lines)
    assert FAKE_GOOGLE not in joined
    assert FAKE_OPENAI not in joined
    assert "upstream call failed" in joined and "RuntimeError" in joined


def test_log_filter_install_is_idempotent(isolated_logger):
    _, handler = isolated_logger
    sr.install_log_redaction()
    sr.install_log_redaction()
    assert sum(isinstance(f, sr.RedactingFilter) for f in handler.filters) == 1


def test_setup_logging_puts_the_filter_on_the_root_handler():
    from Orchestrator.logging_config import setup_logging

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        setup_logging()
        assert root.handlers, "setup_logging must configure a root handler"
        for h in root.handlers:
            assert any(isinstance(f, sr.RedactingFilter) for f in h.filters)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_app_installs_redaction_before_any_orchestrator_import():
    src = (Path(__file__).resolve().parents[1] / "app.py").read_text()
    install_at = src.find("install_redaction()")
    first_import = src.find("from Orchestrator.config import")
    assert install_at != -1, "app.py must install the redaction layer"
    assert install_at < first_import, "install it before config can print"
