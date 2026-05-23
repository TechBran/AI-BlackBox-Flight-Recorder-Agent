"""Shared helpers for building PATH search dirs that include
nvm-installed Node version dirs.

Used by both the binary resolver in cli_agent_routes (which feeds
shutil.which to find a CLI's entry script) and the spawn-time PATH
augmentation in session_manager (which feeds tmux/exec so the
shebang of an nvm-installed CLI can find its Node interpreter).
Same logic, two consumers — extract once.
"""
import os
from pathlib import Path


def _ver_key(p: Path) -> tuple:
    """Sort key for nvm version dir names like 'v20.19.6'.

    Strips leading 'v', splits on '.', coerces numeric parts to int.
    Non-numeric or malformed dirs (e.g. 'iojs-v3.3.1', 'system')
    sort to the bottom via the (-1,) fallback so genuine semver dirs
    always win.
    """
    name = p.name.lstrip("v")
    parts = name.split(".")
    try:
        return tuple(int(x) for x in parts)
    except ValueError:
        return (-1,)


def nvm_node_bin_dirs() -> list[str]:
    """Return every existing ~/.nvm/versions/node/*/bin dir in
    descending semver order (newest first). Empty list if no nvm
    install present or if iteration fails."""
    home = Path.home()
    nvm_versions = home / ".nvm" / "versions" / "node"
    if not nvm_versions.is_dir():
        return []
    try:
        entries = list(nvm_versions.iterdir())
    except OSError:
        return []
    return [
        str(p / "bin")
        for p in sorted(entries, key=_ver_key, reverse=True)
        if (p / "bin").is_dir()
    ]


def extended_path_dirs() -> list[str]:
    """Standard PATH extension list used by the CLI Agent bridge:
    user-local + system bin dirs + nvm node bin dirs in descending
    semver order. Returned as a list of strings so callers can
    compose with `os.pathsep.join` (with their own PATH prefix)."""
    home = Path.home()
    return [
        str(home / ".local" / "bin"),
        "/usr/local/bin",
        "/usr/bin",
        *nvm_node_bin_dirs(),
    ]


def path_shim_dir() -> str:
    """Directory containing exec-shims that get PREPENDED to PATH for
    CLI-agent-spawned processes — overriding the corresponding system
    binaries only within the tmux session, not system-wide.

    Currently ships one shim: `xdg-open`. Antigravity `agy` (and any
    OAuth-flow CLI) calls xdg-open to launch the user's browser; on
    Ubuntu Desktop the xdg-mime default for HTTP(S) reliably drifts
    back to empty/text-editor, so URLs open in gnome-text-editor.
    Our shim bypasses xdg-mime entirely and routes straight to the
    first browser binary on PATH."""
    return str(Path(__file__).parent / "path_shims")
