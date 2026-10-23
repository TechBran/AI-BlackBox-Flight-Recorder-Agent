"""Root conftest — puts repo root on sys.path so `from Orchestrator...` works,
and keeps every test session away from this box's credentials, network, live
services and stores (see "Isolation" and "Checkout write guard" below)."""
import ipaddress
import os
import re
import socket
import sys
import tempfile
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
# Checkout write guard: no test writes into the checkout it runs from. Run from
# a box's serving tree (or a worktree nested inside one), the suite used to
# rewrite that box's state: a telephony wizard test generated a phone PIN into
# .env, the onboarding tests rewrote .onboarding_state.json, a re-embed test
# replaced ToolVault/embeddings.json and the worker tests wrote rows into
# data/tasks.db. While a test runs, an audit hook refuses every write,
# replace, delete or sqlite open under this checkout, or under a BlackBox
# checkout it sits inside, by raising CheckoutWriteBlocked (an OSError) before
# anything happens, and the test then fails with the list. Allowed: bytecode
# caches, the temp dir and pytest's basetemp, and module import-time set-up
# (config.py creating Manifest/manifest.json on a fresh tree). The stores
# themselves are pointed off the checkout below (_box_stores_off_the_checkout)
# and per test by isolated_stores (Orchestrator/tests/conftest.py).
# ---------------------------------------------------------------------------
class CheckoutWriteBlocked(PermissionError):
    """A test tried to write into the checkout."""


class _Refusal:
    def __init__(self, event: str, path: str, where: str):
        self.event, self.path, self.where = event, path, where

    def __repr__(self):
        return f"{self.event} {self.path} (from {self.where})"


def _enclosing_checkouts(root: Path):
    """This checkout plus any BlackBox checkout it sits inside (a worktree
    under the serving tree's .claude/worktrees/)."""
    roots = [root]
    for parent in root.parents:
        if (parent / "Orchestrator" / "app.py").is_file() and (parent / "CLAUDE.md").is_file():
            roots.append(parent)
    return roots


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


class _CheckoutWriteGuard:
    # event -> ((index of a path it writes, index of the dir_fd a relative
    # path is then relative to, or None), ...). os.replace audits as os.rename.
    _PATH_EVENTS = {
        "os.remove": ((0, 1),),
        "os.rmdir": ((0, 1),),
        "shutil.rmtree": ((0, 1),),
        "os.rename": ((0, 2), (1, 3)),
        "os.symlink": ((1, 2),),
        "os.link": ((1, 3),),
        "os.truncate": ((0, None),),
    }
    _WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    _ALLOWED_PARTS = {"__pycache__", ".pytest_cache"}

    def __init__(self, roots):
        self.roots = [os.path.realpath(r) for r in roots]
        self.here = os.path.realpath(__file__)
        self.allowed = []
        tmp = os.path.realpath(tempfile.gettempdir())
        if any(_under(tmp, r) for r in self.roots):  # a TMPDIR inside a checkout
            self.allowed.append(tmp)
        self.active = False
        self._refused = []

    def allow(self, path) -> None:
        self.allowed.append(os.path.realpath(path))

    def take(self):
        """The refusals recorded since the last take(), cleared."""
        refused, self._refused = self._refused, []
        return refused

    def _protected(self, path):
        try:
            path = os.fsdecode(path)
            if path == ":memory:" or path.startswith("file:"):
                return None  # in-memory sqlite, or a URI (read-only opens use one)
            full = os.path.realpath(os.path.join(os.getcwd(), path))
        except (TypeError, ValueError, OSError):
            return None
        if not any(_under(full, r) for r in self.roots):
            return None
        if any(_under(full, a) for a in self.allowed):
            return None
        if self._ALLOWED_PARTS.intersection(full.split(os.sep)):
            return None
        return full

    def _paths(self, event, args):
        if event == "open":
            path, mode, flags = args
            if path is None or isinstance(path, int):
                return []
            writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & self._WRITE_FLAGS)
            return [path] if writes else []
        if event == "sqlite3.connect":
            return args[:1] if args and isinstance(args[0], (str, bytes, os.PathLike)) else []
        out = []
        for at, dir_fd_at in self._PATH_EVENTS[event]:
            if at >= len(args) or not isinstance(args[at], (str, bytes, os.PathLike)):
                continue
            dir_fd = args[dir_fd_at] if dir_fd_at is not None and dir_fd_at < len(args) else None
            if dir_fd not in (None, -1) and not os.path.isabs(os.fsdecode(args[at])):
                continue  # relative to a directory descriptor, not the cwd
            out.append(args[at])
        return out

    def _origin(self, frame):
        """(import-time?, the innermost checkout frame that asked)."""
        where = None
        while frame is not None:
            name = frame.f_code.co_filename
            if name.startswith("<frozen importlib._bootstrap"):
                return True, where
            if where is None and not name.startswith("<"):
                full = os.path.realpath(name)
                if full != self.here and any(_under(full, r) for r in self.roots):
                    where = f"{os.path.relpath(full, self.roots[0])}:{frame.f_lineno}"
            frame = frame.f_back
        return False, where or "?"

    def hook(self, event, args):
        if not self.active:
            return
        if event != "open" and event != "sqlite3.connect" and event not in self._PATH_EVENTS:
            return
        for path in self._paths(event, args):
            full = self._protected(path)
            if full is None:
                continue
            during_import, where = self._origin(sys._getframe(1))
            if during_import:
                return
            self._refused.append(_Refusal(event, full, where))
            raise CheckoutWriteBlocked(
                f"{event} {full} refused: a test may not write into the checkout "
                "(conftest.py write guard). Point the store at tmp_path; see "
                "isolated_stores in Orchestrator/tests/conftest.py")


_checkout_guard = _CheckoutWriteGuard(_enclosing_checkouts(Path(_repo_root)))
if not _LIVE:
    sys.addaudithook(_checkout_guard.hook)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _checkout_guard.active = not _LIVE
    try:
        return (yield)
    finally:
        _checkout_guard.active = False


@pytest.fixture
def checkout_guard():
    """The write guard, for the tests that check it: take() returns (and
    clears) what it refused, so a test that expects a refusal consumes it."""
    return _checkout_guard


@pytest.fixture(autouse=True)
def _no_writes_into_the_checkout():
    yield
    refused = _checkout_guard.take()
    if refused:
        pytest.fail("the test wrote into the checkout (refused by the conftest.py "
                    "write guard; point the store at tmp_path):\n  "
                    + "\n  ".join(map(repr, refused)), pytrace=False)


@pytest.fixture(scope="session", autouse=True)
def _box_stores_off_the_checkout(tmp_path_factory):
    """For the whole session, every store in Orchestrator/tests/box_stores.py
    (task DB, cron, contacts, devices, .env, onboarding state ...) lives in a
    sandbox under pytest's basetemp, so a worker, route or tool call in any
    test writes there and never reads the box's copy."""
    _checkout_guard.allow(tmp_path_factory.getbasetemp())
    try:
        import Orchestrator.config  # noqa: F401
    except (ImportError, SystemExit):
        # The lean MCP venv cannot load the Orchestrator, and a tree without
        # config.ini cannot configure it: no Orchestrator store to sandbox.
        yield None
        return
    from Orchestrator.tests import box_stores

    root = tmp_path_factory.mktemp("box")
    with pytest.MonkeyPatch.context() as patch:
        box_stores.point_at(root, patch, definitions=False)
        box_stores.session_root = root
        try:
            yield root
        finally:
            box_stores.session_root = None


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
