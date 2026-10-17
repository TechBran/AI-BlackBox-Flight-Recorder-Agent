"""Root conftest — puts repo root on sys.path so `from Orchestrator...` works,
and keeps every test session away from this box's credentials, network and
live services (see "Isolation" below)."""
import ipaddress
import os
import re
import socket
import sys
import time
from pathlib import Path

import pytest

_repo_root = str(Path(__file__).parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# Tests run against THIS checkout. install.sh appends BLACKBOX_ROOT=<install> to
# the service's .env (and the service's child processes inherit it), and
# config.py's load_dotenv() walks up from a nested .claude/worktrees/* checkout
# into that .env, so without this pin a worktree run would open the LIVE task DB
# and run the tasks-DB migration on the live tree. load_dotenv never overrides a
# variable that is already set.
os.environ["BLACKBOX_ROOT"] = _repo_root

# The env guard: the app skips every startup/shutdown hook that reaches outside
# this process (scheduler, reapers, embeddings watcher, Asterisk/AMI/SMS, sudo
# drift-heals, index rebuild) while BLACKBOX_TESTING=1, so a test that enters
# `with TestClient(app)` never runs them (Orchestrator/utils/testing.py). Set
# before anything imports the app; a test of the production path unsets it with
# monkeypatch.
os.environ["BLACKBOX_TESTING"] = "1"

# ---------------------------------------------------------------------------
# Isolation (audit O21-python-tests-01/-02): a test process must never hold the
# box's real credentials or reach a real provider. On this box the suite used
# to pick up the serving tree's .env (PHONE_ENABLED, Twilio credentials, every
# API key), so test_twilio_preset_role placed a real outbound call, and the
# Google libraries fell back to the gcloud application-default credentials.
# Unless BLACKBOX_TEST_LIVE=1 (the opt-in for `-m live_store` / `-m
# live_service` runs, which need them), the session:
#   * drops credential-shaped variables from the environment,
#   * makes a bare load_dotenv() a no-op, so config.py never walks up into
#     another checkout's .env (an explicit dotenv_path still loads),
#   * hides the gcloud application-default credentials and the GCE metadata
#     probe from google-auth,
#   * refuses socket connects and DNS lookups to anything but loopback, and
#     connects to the live service ports, with a message naming the fix.
# CI runs exactly this; a test that needs a key sets it with monkeypatch.
# ---------------------------------------------------------------------------
_LIVE = os.environ.get("BLACKBOX_TEST_LIVE", "").strip() == "1"

_CREDENTIAL_ENV = re.compile(
    r"(_API_KEY|_SECRET|_SECRET_KEY|_PASSWORD|_TOKEN|_ACCOUNT_SID|_PIN_CODE)$")
_CREDENTIAL_NAMES = {"GOOGLE_APPLICATION_CREDENTIALS"}


def _is_credential(name: str) -> bool:
    return name in _CREDENTIAL_NAMES or bool(_CREDENTIAL_ENV.search(name))


# The Orchestrator and the MCP server on this box. A unit test that reaches
# them is a live_service test.
_LIVE_SERVICE_PORTS = {9091, 9093}

_NETWORK_REFUSED = (
    "network access is blocked in the test suite (conftest.py isolation): stub "
    "the call, or mark the test live_service and run it with BLACKBOX_TEST_LIVE=1"
)


class NetworkBlocked(OSError):
    """Raised for a refused connect or lookup. An OSError, so the HTTP clients
    report it the way they report an unreachable host."""


def _is_loopback(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host in (None, "", "localhost") or str(host).endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return False
    return addr.is_loopback or addr.is_unspecified


def _install_network_guard() -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _check(sock, address):
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return  # unix sockets stay local
        host, port = address[0], address[1]
        if not _is_loopback(host):
            raise NetworkBlocked(f"connect to {host}:{port} refused: {_NETWORK_REFUSED}")
        if port in _LIVE_SERVICE_PORTS:
            raise NetworkBlocked(f"connect to the live service on :{port} refused: "
                                 f"{_NETWORK_REFUSED}")

    def connect(sock, address):
        _check(sock, address)
        return real_connect(sock, address)

    def connect_ex(sock, address):
        _check(sock, address)
        return real_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            raise NetworkBlocked(f"DNS lookup of {host!r} refused: {_NETWORK_REFUSED}")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo


def _isolate_from_box() -> None:
    for name in list(os.environ):
        if _is_credential(name):
            del os.environ[name]
    # google-auth: no gcloud ADC file (it lives under CLOUDSDK_CONFIG) and no
    # metadata-server probe. /dev/null/... can never exist or be created.
    os.environ["CLOUDSDK_CONFIG"] = os.path.join(os.devnull, "gcloud")
    os.environ["NO_GCE_CHECK"] = "true"
    try:
        import dotenv
        import dotenv.main
    except ImportError:  # the lean MCP venv may not carry python-dotenv
        pass
    else:
        real_load = dotenv.main.load_dotenv

        def load_dotenv(dotenv_path=None, stream=None, *args, **kwargs):
            if dotenv_path is None and stream is None:
                return False
            return real_load(dotenv_path, stream, *args, **kwargs)

        dotenv.load_dotenv = dotenv.main.load_dotenv = load_dotenv
    _install_network_guard()


if not _LIVE:
    _isolate_from_box()


@pytest.fixture(autouse=True)
def _no_credential_carries_over():
    """A route under test may mirror a key into os.environ (the rerank key and
    service-account uploads do, by design). Put the credential variables back
    as they were, so no test runs with a key an earlier test left behind."""
    before = {k: v for k, v in os.environ.items() if _is_credential(k)}
    yield
    if _LIVE:
        return
    for name in [k for k in os.environ if _is_credential(k)]:
        if name not in before:
            del os.environ[name]
    os.environ.update(before)

# ---------------------------------------------------------------------------
# Cross-suite settle: live-service tests (chat stream regression, agent WS)
# each leave background Gemini inferences running. Without a brief pause the
# next test file starts while the service event-loop is still saturated and
# WS handshakes time out.  3 seconds is enough for FastAPI to drain the SSE
# task from the previous test before a new WS connection arrives.
# ---------------------------------------------------------------------------
_LIVE_SERVICE_MODULES = {
    "tests/test_chat_stream_retrieval_regression.py",
    "tests/test_agent_route_retrieval.py",
    "tests/test_voice_route_retrieval.py",
}

@pytest.fixture(autouse=True, scope="module")
def _settle_between_live_tests(request):
    yield
    # These modules carry the live_service marker, so this only fires on an
    # explicit `-m live_service` run.
    try:
        rel = Path(request.path).relative_to(_repo_root).as_posix()
    except ValueError:
        return
    if rel in _LIVE_SERVICE_MODULES:
        time.sleep(3)
