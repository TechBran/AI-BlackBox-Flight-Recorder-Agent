"""Root conftest — puts repo root on sys.path so `from Orchestrator...` works,
and keeps every test session away from this box's credentials, network, live
services and stores (see "Isolation" and "Checkout write guard" below)."""
import errno
import ipaddress
import os
import re
import shlex
import shutil
import socket
import sys
import tempfile
import time
import urllib.parse
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
#   * refuses socket connects, datagram sends and name lookups to anything but
#     loopback, and connects to the box's own loopback services, with a
#     message naming the fix (an audit hook, so it covers every socket,
#     including a raw _socket one),
#   * refuses to start the host programs that act on the box itself (sudo,
#     systemctl, tailscale, the real display ...), as if they were not
#     installed; a fake one on PATH under the temp dir still runs.
# CI runs exactly this; a test that needs a key sets it with monkeypatch. The
# voice probes (probe_live, diagnostics/voice_probes) read their keys from the
# box's .env themselves and are only ever selected by name, so the network is
# open for those items.
# ---------------------------------------------------------------------------
_LIVE = os.environ.get("BLACKBOX_TEST_LIVE", "").strip() == "1"

_CREDENTIAL_ENV = re.compile(
    r"(_API_KEY|_SECRET|_SECRET_KEY|_PASSWORD|_TOKEN|_ACCOUNT_SID|_PIN_CODE)$")
_CREDENTIAL_NAMES = {"GOOGLE_APPLICATION_CREDENTIALS"}


def _is_credential(name: str) -> bool:
    return name in _CREDENTIAL_NAMES or bool(_CREDENTIAL_ENV.search(name))


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


# The services a box runs on loopback. A unit test that reaches one is a
# live_service test: on this box it would drive the real thing (place a call
# through AMI, load a model, print a page), and on CI nothing listens there.
_LIVE_SERVICE_PORTS = {
    9091,   # the Orchestrator
    9093,   # the MCP server
    9097,   # zellij web
    9098,   # llama-swap, the on-box models
    11434,  # Ollama
    5037,   # the adb server (attached phones)
    5038,   # Asterisk AMI
    8088,   # Asterisk ARI
    631,    # CUPS
    53,     # the DNS stub resolver, which forwards off the box
}

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
    addr = getattr(addr, "ipv4_mapped", None) or addr
    return addr.is_loopback or addr.is_unspecified


class _NetworkGuard:
    # socket.connect also covers connect_ex; sendmsg passes None for a
    # connected socket.
    _ADDRESS_EVENTS = {"socket.connect", "socket.sendto", "socket.sendmsg"}
    _LOOKUP_EVENTS = {"socket.getaddrinfo", "socket.gethostbyname",
                      "socket.gethostbyaddr", "socket.getnameinfo"}

    def __init__(self):
        self.open = False  # True while a probe_live item runs

    def hook(self, event, args):
        if self.open or not event.startswith("socket."):
            return
        if event in self._ADDRESS_EVENTS:
            sock, address = args[0], args[1]
            if getattr(sock, "family", None) not in (socket.AF_INET, socket.AF_INET6):
                return  # unix sockets stay local
            if not isinstance(address, tuple) or len(address) < 2:
                return
            host, port = address[0], address[1]
            verb = "connect to" if event == "socket.connect" else "send to"
            if not _is_loopback(host):
                raise NetworkBlocked(f"{verb} {host}:{port} refused: {_NETWORK_REFUSED}")
            if port in _LIVE_SERVICE_PORTS:
                raise NetworkBlocked(f"{verb} the live service on :{port} refused: "
                                     f"{_NETWORK_REFUSED}")
        elif event in self._LOOKUP_EVENTS:
            host = args[0]
            if event == "socket.getnameinfo" and isinstance(host, tuple):
                host = host[0] if host else None
            if not _is_loopback(host):
                raise NetworkBlocked(f"DNS lookup of {host!r} refused: {_NETWORK_REFUSED}")


_network_guard = _NetworkGuard()


# Programs that act on the box itself rather than on a test's own files: the
# privilege, service and network managers, the tailnet, the real display and
# input, the desktop's defaults, attached phones and the PBX. Run from a test
# they read or change this box (a test woke the monitor through xset on every
# run, and the MCP status test ran `sudo -n tailscale funnel status`), and on
# CI they are missing or report nothing. The code that calls them treats a
# missing binary as "not on this box", which is what the suite then sees.
_HOST_COMMANDS = frozenset({
    "sudo", "pkexec", "su", "doas",
    "systemctl", "loginctl",
    "nmcli", "tailscale",
    "xset", "xrandr", "xdotool", "ydotool",
    "xdg-mime", "xdg-settings", "gsettings",
    "adb", "asterisk",
})

_HOST_COMMAND_REFUSED = (
    "running a host command is blocked in the test suite (conftest.py "
    "isolation): stub the call, or put a fake one on PATH under tmp_path"
)


class HostCommandBlocked(FileNotFoundError):
    """Raised for a refused host program: a FileNotFoundError, so the caller
    sees what it sees on a box that does not have it."""


_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _shell_program(command) -> str:
    """The program a shell command line starts (after any VAR=value)."""
    if isinstance(command, bytes):
        command = command.decode("utf-8", "replace")
    try:
        words = shlex.split(str(command))
    except ValueError:
        words = str(command).split()
    for word in words:
        if not _ASSIGNMENT.match(word):
            return word
    return ""


class _HostCommandGuard:
    def __init__(self):
        self.allowed = [os.path.realpath(tempfile.gettempdir())]

    def allow(self, path) -> None:
        self.allowed.append(os.path.realpath(path))

    @staticmethod
    def _program(event, args):
        """(program, environment) the event is about to start."""
        if event == "subprocess.Popen":
            executable, argv, _cwd, env = args
            argv = [argv] if isinstance(argv, (str, bytes, os.PathLike)) else list(argv or [])
            if len(argv) >= 3 and os.fsdecode(argv[1]) == "-c" and os.path.basename(
                    os.fsdecode(argv[0])) in ("sh", "bash", "dash"):
                return _shell_program(argv[2]), env  # shell=True
            return executable if executable is not None else (argv[0] if argv else ""), env
        if event == "os.system":
            return _shell_program(args[0]), None
        if event in ("os.exec", "os.posix_spawn"):
            return args[0], args[2]
        if event == "os.spawn":
            return args[1], args[3]
        return None, None

    def hook(self, event, args):
        if event not in ("subprocess.Popen", "os.system", "os.exec",
                         "os.posix_spawn", "os.spawn"):
            return
        program, env = self._program(event, args)
        if not program:
            return
        program = os.fsdecode(program)
        if os.path.basename(program) not in _HOST_COMMANDS:
            return
        if os.sep not in program:
            try:  # the search the launch itself does (os.defpath without PATH)
                search = os.pathsep.join(map(os.fsdecode, os.get_exec_path(env)))
            except (TypeError, ValueError):
                search = None
            found = shutil.which(program, path=search)
            if found is None:
                return  # not installed here: the launch fails on its own
            program = found
        full = os.path.realpath(program)
        if any(_under(full, a) for a in self.allowed):
            return  # a test's own fake on PATH
        raise HostCommandBlocked(errno.ENOENT, f"{program} refused: {_HOST_COMMAND_REFUSED}",
                                 program)


_host_command_guard = _HostCommandGuard()


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
    sys.addaudithook(_network_guard.hook)
    sys.addaudithook(_host_command_guard.hook)


if not _LIVE:
    _isolate_from_box()


def pytest_runtest_setup(item):
    """A live_store or live_service test run without BLACKBOX_TEST_LIVE=1
    would fail on the isolation above (no key, no network), not on what it
    checks: skip it and say how to run it."""
    if _LIVE:
        return
    for name in ("live_store", "live_service"):
        if item.get_closest_marker(name) is not None:
            pytest.skip(f"{name} test: run it with BLACKBOX_TEST_LIVE=1 pytest -m {name} "
                        "(the suite's isolation is on)")


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
# data/tasks.db. While a test runs, an audit hook refuses, under this checkout
# or under a BlackBox checkout it sits inside, by raising CheckoutWriteBlocked
# (an OSError) before anything happens: an open() for writing, a remove,
# rename/replace, rmtree, truncate, link or symlink, a mkdir of a new
# directory, a chmod/chown/utime, and a sqlite open (a file: URI too, unless
# it is read-only). The test then fails with the list. Allowed: bytecode
# caches, the temp dir and pytest's basetemp, and module import-time set-up
# (config.py creating Manifest/manifest.json on a fresh tree).
#
# What it cannot see: writes by a child process, by C code that opens files
# itself, or through a path relative to a directory descriptor (the open audit
# event does not carry the dir_fd); writes started during a lazy import count
# as import-time set-up. The stores themselves are pointed off the checkout
# below (_box_stores_off_the_checkout) for every test, live runs included, and
# per test by isolated_stores (Orchestrator/tests/conftest.py).
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
        "os.mkdir": ((0, 2),),
        "os.chmod": ((0, 2),),
        "os.chown": ((0, 3),),
        "os.utime": ((0, 3),),
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

    @staticmethod
    def _sqlite_target(path: str):
        """The file a sqlite database name opens for writing, or None."""
        if path == ":memory:" or path == "":
            return None
        if not path.startswith("file:"):
            return path
        target, _, query = path[len("file:"):].partition("?")
        params = urllib.parse.parse_qs(query)
        if params.get("mode", [""])[-1] in ("ro", "memory") or params.get("immutable", [""])[-1] == "1":
            return None
        target = urllib.parse.unquote(target.split("#", 1)[0])
        if target.startswith("//"):  # file://host/path
            target = "/" + target[2:].partition("/")[2]
        return target or None

    def _protected(self, path):
        try:
            path = os.fsdecode(path)
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
            if not args or not isinstance(args[0], (str, bytes, os.PathLike)):
                return []
            target = self._sqlite_target(os.fsdecode(args[0]))
            return [target] if target else []
        out = []
        for at, dir_fd_at in self._PATH_EVENTS[event]:
            if at >= len(args) or not isinstance(args[at], (str, bytes, os.PathLike)):
                continue
            dir_fd = args[dir_fd_at] if dir_fd_at is not None and dir_fd_at < len(args) else None
            if dir_fd not in (None, -1) and not os.path.isabs(os.fsdecode(args[at])):
                continue  # relative to a directory descriptor, not the cwd
            if event == "os.mkdir" and os.path.exists(args[at]):
                continue  # mkdir(exist_ok=True) of a directory the tree has
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
    # The voice probes call the providers by design (paid, never in the
    # default selection; see diagnostics/voice_probes/test_live_probes.py).
    _network_guard.open = item.get_closest_marker("probe_live") is not None
    try:
        return (yield)
    finally:
        _checkout_guard.active = False
        _network_guard.open = False


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
    _host_command_guard.allow(tmp_path_factory.getbasetemp())
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
