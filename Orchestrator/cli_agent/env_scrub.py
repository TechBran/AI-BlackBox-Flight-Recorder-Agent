"""The one environment table every CLI-agent and terminal spawn inherits.

Why this exists (audit O14-cli-agents-devices-02 / -03): config.py's
load_dotenv() puts the whole .env into os.environ, so anything the orchestrator
spawns with ``os.environ`` carries the server's credentials. Before this module
there were three divergent strip lists — zellij panes dropped only
ANTHROPIC_API_KEY, the tmux backend dropped nothing, the headless runner dropped
2-3 keys per provider — so ``env`` in any terminal pane printed the Google /
OpenAI / xAI / Twilio / AMI secrets and any YOLO agent launched there could
exfiltrate them. Every spawn site now builds its child env through
``scrubbed_child_env()``:

  * headless CLI runs ............ cli_agent/headless.build_child_env
  * zellij sessions + CLI calls .. cli_agent/zellij_client (launch_session, _run)
  * tmux sessions ................ cli_agent/session_manager.TmuxSessionManager
  * tmux attach PTY .............. routes/cli_agent_routes + cli_agent/pty_bridge
  * legacy agent-mode CLIs ....... routes/agent_routes, routes/gemini_agent_routes
  * wizard terminals/diagnostics . routes/onboarding_routes
  * CU desktops (Xvfb, openbox,
    tint2, pcmanfm -> the menu's
    xterm, the desktop's Chrome) . browser/display.DisplayHandle.get_env
  * CU default-display Chrome .... browser/chrome._active_display_env
  * CU driver's `bash` tool ...... routes/chat_routes.execute_bash_command

What this is NOT: a boundary against a process running as the service user.
Every pane, agent and CU shell runs as the same uid as the orchestrator, so it
can still read .env or the orchestrator's /proc/<pid>/environ directly. The
scrub stops inheritance — `env` in a pane, an agent dumping its environment
into a transcript, a CLI silently billing the server's key. A real boundary
needs a separate uid or a credential broker, and credentials that panes could
read before this module existed still need rotating.

The rule is a DENYLIST by name shape plus an explicit keep-list, not a strict
allowlist: a coding agent legitimately needs the operator's broad shell /
toolchain env (NVM_DIR, SSH_AUTH_SOCK, proxies, ...), it just must never hold
the BlackBox's credentials. Denied:
  * names ending in _API_KEY, _SECRET, _TOKEN, _PASSWORD, _SID or _AUTH (or
    equal to one of those words), and names containing SECRET, PASSWORD, PASSWD
    or PRIVATE_KEY — so a key added to .env tomorrow is covered automatically;
  * the BlackBox's own credentials whose names escape that shape (below);
  * the provider redirect vars (a CLI talks to its own vendor, never our proxy);
  * ZELLIJ* — the orchestrator can itself run inside a zellij pane, and an
    inherited ZELLIJ_SESSION_NAME makes a child target the user's live session.
Kept even when a name rule would match (the keep-list wins): PATH, HOME, USER,
LOGNAME, LANG/LANGUAGE/LC_*, TERM, SHELL, DBUS_SESSION_BUS_ADDRESS, XDG_*,
DISPLAY, WAYLAND_DISPLAY, XAUTHORITY, and GOOGLE_APPLICATION_CREDENTIALS (a
Vertex/ADC service-account PATH, not a pay-per-call key).

Per-provider auth (verified on the dev box 2026-10-17) — NO supported CLI needs
a credential from the orchestrator's environment:
  * claude      — subscription login in ~/.claude/.credentials.json. An
                  inherited ANTHROPIC_API_KEY makes it bill the key instead.
  * codex       — ChatGPT login in ~/.codex/auth.json. An inherited
                  OPENAI_API_KEY flips it to API-key billing (codex doctor).
  * gemini      — its own stored key / Google login under ~/.gemini (gemini-api-
                  key mode reads GEMINI_API_KEY, which .env does not define;
                  GOOGLE_API_KEY is only consulted in Vertex mode).
  * grok        — xAI OAuth login in ~/.grok/auth.json.
  * antigravity — OS keyring over D-Bus (DBUS_SESSION_BUS_ADDRESS / XDG_* kept).
The single deliberate exception is the legacy Gemini agent-mode route, which
has always authenticated the gemini CLI with the server's key: it scrubs, then
sets GEMINI_API_KEY explicitly (see routes/gemini_agent_routes.py). A caller
that must re-admit an inherited variable passes it via ``keep=`` and documents
why at the call site.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Mapping, Optional

# Name shapes that mark a credential. Matched against the upper-cased name.
_SECRET_SUFFIX_RE = re.compile(r"(?:^|_)(?:API_KEY|SECRET|TOKEN|PASSWORD|SID|AUTH)$")
_SECRET_SUBSTRINGS = ("SECRET", "PASSWORD", "PASSWD", "PRIVATE_KEY")

# The BlackBox's own credentials whose names escape the shapes above, plus the
# provider redirect vars the headless runner has always stripped (M5).
_DENY_EXACT = frozenset({
    "BLACKBOX_MCP_TOKENS",       # MCP bearer tokens (plural — escapes _TOKEN$)
    "BLACKBOX_MCP_TOKENS_FILE",  # ...and the pointer to the file holding them
    "PHONE_PIN_CODE",            # phone-agent PIN
    "GOOGLE_OAUTH_CLIENT_ID",    # the box's own OAuth client (pairs with _SECRET)
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
})
_DENY_PREFIXES = ("ZELLIJ",)

_KEEP_EXACT = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TERM", "SHELL",
    "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
    "GOOGLE_APPLICATION_CREDENTIALS",
})
_KEEP_PREFIXES = ("LC_", "XDG_")


def is_secret_name(name: str) -> bool:
    """True when ``name`` looks like a credential by the module's rules."""
    upper = name.upper()
    if upper in _DENY_EXACT:
        return True
    if _SECRET_SUFFIX_RE.search(upper):
        return True
    return any(s in upper for s in _SECRET_SUBSTRINGS)


def _is_kept(name: str) -> bool:
    return name in _KEEP_EXACT or name.startswith(_KEEP_PREFIXES)


def scrubbed_child_env(base: Optional[Mapping[str, str]] = None, *,
                       keep: Iterable[str] = ()) -> dict:
    """Return a copy of ``base`` (default: os.environ) that is safe to hand to
    a CLI agent or terminal: every credential-shaped variable and every ZELLIJ*
    variable removed, session plumbing kept. ``keep`` re-admits specific names
    a caller has a documented reason to pass through. ``base`` is never mutated.
    """
    source = os.environ if base is None else base
    extra_keep = frozenset(keep)
    env: dict = {}
    for name, value in source.items():
        if name in extra_keep:
            env[name] = value
            continue
        if name.startswith(_DENY_PREFIXES):
            continue
        if _is_kept(name) or not is_secret_name(name):
            env[name] = value
    return env
