import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from .path_validator import PathValidator
from .operator_config import OperatorConfig


_APPS_ROOT_SLUG = "_root"
_FIELD_SEP = "__"


# --- Per-provider post-attach hooks --------------------------------------
#
# After a tmux session is created and the inner CLI has had time to come
# online, we may need to send a one-time keystroke sequence to nudge the
# CLI into the configuration we want. The classic example is Claude Code,
# which boots in alt-screen mode by default — PgUp/PgDn cannot scroll the
# conversation history in that mode because the bytes never get retained
# outside Claude's process. The fullscreen renderer (Claude's `/tui
# fullscreen` slash command) manages its own scroll buffer instead.
#
# Each value is a list of strings that gets passed verbatim to
# `tmux send-keys -t <name> ...`. tmux interprets bare strings as text
# to type and bracketed names like "Enter" / "C-c" / "Escape" as keys.
#
# Adding a new provider? Two rules:
#   1. Only add an entry if the provider has a real first-run hook.
#      A provider that needs no hook should NOT appear in this table —
#      `None` (i.e., absence) is the default and that is correct.
#   2. The hook must be idempotent: it gets queued onto the inner pty
#      even if the CLI hasn't finished booting, and runs whenever it
#      drains. Don't put state-mutating commands here.
#
# Keys MUST be a subset of cli_agent_routes.SUPPORTED_PROVIDERS — adding a hook
# for an unregistered provider is dead code (the route layer rejects it).
POST_ATTACH_HOOKS: dict[str, list[str]] = {
    "claude": ["/tui fullscreen", "Enter"],
    # "gemini" deliberately absent — Ink renderer manages scroll fine.
    # "codex"  deliberately absent — pending real-world verification.
}


def _post_attach_hook_for(provider: str) -> list[str] | None:
    """Return the post-attach send-keys sequence for `provider`, or None."""
    return POST_ATTACH_HOOKS.get(provider)


# --- Spawn-time PATH augmentation ----------------------------------------
#
# The orchestrator runs under systemd with a restricted PATH that omits
# user-local install dirs (~/.local/bin) and per-version nvm dirs
# (~/.nvm/versions/node/<ver>/bin). The bridge resolves the provider
# binary itself via cli_agent_routes._resolve_provider_bin, but that's
# only enough for tmux to spawn the entry-point script. The script can
# still fail at exec time if its shebang relies on PATH — Gemini CLI's
# `#!/usr/bin/env node` is the canonical example: `gemini` is found,
# then `node` is not, and the shebang resolution fails before Gemini
# even runs.
#
# So we extend the PATH that tmux passes to the inner command to include
# the same nvm dirs the resolver searches. Keep this in sync with
# cli_agent_routes._resolve_provider_bin's search list.
def _augmented_spawn_path() -> str:
    """Build a PATH that includes nvm node version bin dirs.

    Mirrors cli_agent_routes._resolve_provider_bin's search list — the
    binary the bridge spawns is itself a Node script with a
    `#!/usr/bin/env node` shebang, so the spawn-time PATH must include
    `node` for the shebang lookup to succeed.

    See Orchestrator.cli_agent.path_extension for the shared dir list.
    """
    from .path_extension import extended_path_dirs
    return os.pathsep.join([os.environ.get("PATH", ""), *extended_path_dirs()])


def session_name(operator: str, provider: str, app: str) -> str:
    if _FIELD_SEP in operator or _FIELD_SEP in provider:
        raise ValueError(
            f"Operator/provider names must not contain {_FIELD_SEP!r} "
            f"(reserved as session-name field separator)"
        )
    slug = app if app else _APPS_ROOT_SLUG
    return f"cli-agent-{operator}{_FIELD_SEP}{provider}{_FIELD_SEP}{slug}"


def parse_session_name(name: str) -> tuple:
    prefix = "cli-agent-"
    if not name.startswith(prefix):
        raise ValueError(f"Not a CLI Agent session name: {name}")
    parts = name[len(prefix):].split(_FIELD_SEP)
    if len(parts) != 3:
        raise ValueError(f"Malformed session name: {name}")
    op, prov, app = parts
    if app == _APPS_ROOT_SLUG:
        app = ""
    return op, prov, app


@dataclass
class SessionInfo:
    name: str
    created: bool
    cwd: Path


class TmuxSessionManager:
    def __init__(self, path_validator: PathValidator,
                 operator_config: OperatorConfig,
                 tmux_socket: Optional[Path] = None):
        self.pv = path_validator
        self.oc = operator_config
        self.socket = tmux_socket

    def _tmux_cmd(self, *args) -> List[str]:
        cmd = ["tmux"]
        if self.socket:
            cmd += ["-S", str(self.socket)]
        return cmd + list(args)

    def _tmux(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(self._tmux_cmd(*args), capture_output=True, text=True)

    def has_session(self, name: str) -> bool:
        return self._tmux("has-session", "-t", name).returncode == 0

    # Default TERM so the inner Claude Code TUI has a sane terminal type
    # (systemd-launched parent processes typically have TERM unset).
    _DEFAULT_TERM = "xterm-256color"

    def _new_session_cmd(self, name: str, cwd: str,
                          env_args: list, command: Sequence[str]) -> list:
        """Build the tmux new-session command.

        For tmux server to survive blackbox.service restarts, the service
        must be configured with `KillMode=process` (see drop-in at
        /etc/systemd/system/blackbox.service.d/cli-agent-killmode.conf).
        That config tells systemd to only kill the main uvicorn process
        on restart, leaving child processes — including tmux servers —
        alone. The session_manager itself stays simple: just spawn tmux
        normally and let the service config handle persistence.
        """
        base = ["tmux"]
        if self.socket:
            base += ["-S", str(self.socket)]
        base += ["new-session", "-d", "-s", name, "-c", cwd, *env_args, *command]
        return base

    def attach_or_create(self, *, operator: str, provider: str,
                          app: str, command: Sequence[str]) -> SessionInfo:
        cwd = self.pv.validate(app)  # may raise WorkspaceViolation
        env = {
            "TERM": self._DEFAULT_TERM,
            "PATH": _augmented_spawn_path(),
            **self.oc.env_for(operator),
        }
        name = session_name(operator, provider, app)
        if self.has_session(name):
            return SessionInfo(name=name, created=False, cwd=cwd)
        # Use `-e KEY=VAL` to propagate env vars to the new session's child.
        # Setting them only on the tmux client process is insufficient because
        # an existing tmux server uses its own env, not the client's.
        env_args = []
        for key, val in env.items():
            env_args.extend(["-e", f"{key}={val}"])
        # Prefix the command with `env TERM=xterm-256color` so the child
        # CLI sees a proper xterm-256color TERM. tmux silently overrides
        # the child's TERM to its `default-terminal` (`tmux-256color`)
        # regardless of `-e TERM=…`, which breaks keypress parsing in
        # Ink-based TUIs (Claude Code's slash menu, in particular). The
        # `env` binary forces TERM at exec-time, after tmux's wrapper.
        prefixed_command = ["env", f"TERM={self._DEFAULT_TERM}", *command]
        full_env = {**os.environ, **env}
        result = subprocess.run(
            self._new_session_cmd(name, str(cwd), env_args, prefixed_command),
            capture_output=True, text=True, env=full_env
        )
        if result.returncode != 0:
            raise RuntimeError(f"tmux new-session failed: {result.stderr}")
        # Apply per-session tmux options needed for Claude Code (and
        # other modern TUIs) to behave correctly inside tmux:
        #
        #   • extended-keys on  — relay modified keys (Shift+PgUp,
        #     Ctrl+Arrow, etc.) with full xterm encoding instead of
        #     stripping the modifier when the inner TERM doesn't seem
        #     to support it. Default `off` in tmux 3.4.
        #
        #   • focus-events on   — relay terminal focus-in/focus-out
        #     escape sequences (ESC[I / ESC[O) to the inner app.
        #     Default `off`. Claude Code requests focus events via
        #     ESC[?1004h and uses them to know when it's active;
        #     without focus-events on, it never sees them and behaves
        #     as if always unfocused, which suppresses scroll and
        #     other "active" behaviors. Claude Code's startup banner
        #     specifically warns about this misconfiguration.
        #
        # Failure to apply is non-fatal so we don't block session
        # creation if a future tmux version drops/renames an option.
        for opt, val in (("extended-keys", "on"),
                         ("focus-events", "on")):
            try:
                subprocess.run(
                    self._tmux_cmd("set-option", "-t", name, opt, val),
                    capture_output=True, text=True, env=full_env, timeout=3,
                )
            except Exception:
                pass
        # Auto-switch Claude Code into the fullscreen renderer after
        # a brief startup delay. The default renderer uses the alt-
        # screen buffer, which means PgUp/PgDn cannot scroll the
        # conversation history (the bytes never get retained outside
        # Claude Code's process). The fullscreen renderer manages its
        # own scroll buffer and binds PgUp / PgDn (no Shift needed) to
        # navigate it. See upstream issue #28077. Detached daemon
        # thread so we don't block the request — claude-code's
        # startup takes ~3-5s to be ready for input.
        hook_keys = _post_attach_hook_for(provider)
        if hook_keys is not None:
            self._schedule_send_keys(name, hook_keys)
        return SessionInfo(name=name, created=True, cwd=cwd)

    def _schedule_send_keys(self, session_name: str,
                             keys: list[str],
                             delay_s: float = 5.0) -> None:
        """Send a keystroke sequence to a freshly-created session after a
        short delay so the inner CLI has had time to boot.

        Runs in a daemon thread; failures are silent. The bytes get queued
        on the inner pty if the CLI isn't quite ready yet, and are processed
        once it is.
        """
        cmd = self._tmux_cmd("send-keys", "-t", session_name, *keys)

        def _go() -> None:
            try:
                time.sleep(delay_s)
                subprocess.run(
                    cmd, capture_output=True, text=True, timeout=5,
                )
            except Exception:
                pass

        threading.Thread(target=_go, daemon=True).start()

    def kill(self, name: str) -> None:
        self._tmux("kill-session", "-t", name)

    def list_for_operator(self, operator: str) -> List[SessionInfo]:
        prefix = f"cli-agent-{operator}{_FIELD_SEP}"
        result = self._tmux("list-sessions", "-F", "#{session_name}")
        if result.returncode != 0:
            return []
        sessions = []
        for line in result.stdout.splitlines():
            if line.startswith(prefix):
                op, prov, app = parse_session_name(line)
                cwd = self.pv.apps_root if app == "" else self.pv.apps_root / app
                sessions.append(SessionInfo(name=line, created=False, cwd=cwd))
        return sessions
