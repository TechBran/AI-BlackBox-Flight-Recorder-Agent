"""The ONE renderer of the live llama-swap config (audit O19-local-stack-09).

~/.blackbox/localstack/llama-swap-config.yaml used to have two writers and no
owner: blackbox-install-localstack.sh re-ran a sed over the template on every
run (resetting every keep-warm member to ttl 600 and restarting the proxy),
local_stack.set_member_ttl rewrote it through yaml.safe_dump (dropping every
comment), and a committed template fix reached a box only when someone re-ran
the installer by hand — so the Aug 2026 reranker batch fix and others were
sed'ed into live files, where a slip in a `cmd: |` block's indentation stays
invisible until the next reboot.

Now the file is always template + keep-warm overrides, rendered here:
  * the installer's step 5 runs this file as a script (stdlib only, so the
    system python3 can run it with -I before any venv exists);
  * local_stack.set_member_ttl records the choice in the overrides sidecar
    and re-renders;
  * the Orchestrator re-renders at every boot (startup hook), which is how a
    `git pull` + restart — the in-app updater and Scripts/update.sh both end
    that way — lands a template change on every box.
A render identical to the live file writes nothing (llama-swap --watch-config
restarts the whole proxy on any write).

Keep-warm overrides live beside the config in llama-swap-overrides.json as
{"blackbox_root": "<checkout>", "ttl": {"<member>": <seconds>}}; only ttl
values that differ from the template's are kept. A box upgraded from the
safe_dump era has no sidecar: its live ttl choices are adopted on the first
reconcile instead of being reset.

The file has ONE owner: the checkout it was rendered for (blackbox_root in the
sidecar; for a config rendered before the sidecar existed, the checkout its
qwen-tts PYTHONPATH names). Any Orchestrator can boot on the box, including a
test backend run from a worktree, and rendering with whichever checkout is
running would repoint the qwen-tts member into that worktree and restart the
proxy. So a reconcile from another checkout writes nothing (it reports why),
and only the installer (--blackbox-root, an operator's explicit choice) moves
the config to a different checkout.

    python3 -I Orchestrator/localstack_render.py \\
        --localstack-home ~/.blackbox/localstack --blackbox-root <checkout> [--dest FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

CONFIG_NAME = "llama-swap-config.yaml"
OVERRIDES_NAME = "llama-swap-overrides.json"
_TEMPLATE_REL = ("installer", "templates", "llama-swap-config.yaml.template")

# Within the top-level `models:` block: a member key line, and a member's own
# ttl line (exactly four spaces in — never a cmd: | continuation line).
_MEMBER_RE = re.compile(r'^  "(?P<id>[^"]+)":\s*(#.*)?$')
_TTL_RE = re.compile(r"^(?P<lead>    ttl:\s*)(?P<val>\d+)(?P<rest>.*)$")


# The checkout a pre-sidecar config was rendered for: ${LOCALMODELS} is
# "<checkout>/LocalModels", and the qwen-tts member puts it on PYTHONPATH.
_OWNER_RE = re.compile(r"PYTHONPATH=(?P<root>[^\s'\":]+)/LocalModels(?=[\s'\":]|$)")


class UnknownMember(ValueError):
    """An override names a member the template does not define."""


def template_path(blackbox_root: str) -> Path:
    return Path(blackbox_root, *_TEMPLATE_REL)


def substitutions(localstack_home: str, blackbox_root: str) -> "list[tuple[str, str]]":
    """The installer's six path variables, in the order its sed applied them.
    Built by string concatenation, never Path(), so the result is byte for
    byte what the shell produced. ${PORT} / ${llama-server} / ${models-dir}
    are llama-swap's own and stay literal."""
    return [
        ("${LOCALSTACK_BIN}", f"{localstack_home}/bin"),
        ("${LOCALSTACK_MODELS}", f"{localstack_home}/models"),
        ("${SPEACHES_VENV}", f"{localstack_home}/speaches-venv"),
        ("${QWEN_TTS_VENV}", f"{localstack_home}/qwen-tts-venv"),
        ("${LOCALMODELS}", f"{blackbox_root}/LocalModels"),
        ("${LOCALSTACK_HOME}", localstack_home),
    ]


def _models_lines(lines: "list[str]"):
    """Yield (index, member, match) for every member ttl line in `models:`."""
    in_models, member = False, None
    for i, line in enumerate(lines):
        if line and not line[0].isspace() and not line.startswith("#"):
            in_models, member = line.rstrip() == "models:", None
            continue
        if not in_models:
            continue
        m = _MEMBER_RE.match(line)
        if m:
            member = m.group("id")
            continue
        m = _TTL_RE.match(line)
        if m and member is not None:
            yield i, member, m


def member_ttls(text: str) -> "dict[str, int]":
    """{member: ttl} as written in a template or a rendered config."""
    return {member: int(m.group("val")) for _i, member, m in _models_lines(text.split("\n"))}


def _check_ttl(member: str, ttl) -> int:
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
        raise ValueError(f"ttl for {member!r} must be a whole number of seconds >= 0, got {ttl!r}")
    return ttl


def render(template_text: str, localstack_home: str, blackbox_root: str,
           ttl_overrides: "dict[str, int] | None" = None) -> str:
    """Template + paths + keep-warm ttl overrides -> the live config text.
    An override edits only the number on that member's ttl line (its comment
    keeps its column). ValueError for a member the template does not have
    or a ttl that is not a non-negative int."""
    out = template_text
    for token, value in substitutions(localstack_home, blackbox_root):
        out = out.replace(token, value)
    overrides = dict(ttl_overrides or {})
    known = member_ttls(out)
    for member, ttl in overrides.items():
        if member not in known:
            raise UnknownMember(f"llama-swap template has no member {member!r}")
        _check_ttl(member, ttl)
    if not overrides:
        return out
    lines = out.split("\n")
    for i, member, m in list(_models_lines(lines)):
        if member not in overrides:
            continue
        old, new = m.group("val"), str(overrides[member])
        rest = m.group("rest")
        if len(new) < len(old) and rest.startswith(" "):
            new = new.ljust(len(old))          # keep the comment's column
        elif len(new) > len(old) and rest.startswith(" " * (len(new) - len(old) + 1)):
            rest = rest[len(new) - len(old):]
        lines[i] = f"{m.group('lead')}{new}{rest}"
    return "\n".join(lines)


def overrides_path(localstack_home: str) -> Path:
    return Path(localstack_home, OVERRIDES_NAME)


def _read_sidecar(localstack_home: str) -> "dict | None":
    """{"ttl": {member: ttl}, "blackbox_root": str | None}; None when no
    sidecar exists yet. A sidecar that is not the expected JSON raises
    ValueError: silently reading it as "no overrides" would reset every
    keep-warm member."""
    path = overrides_path(localstack_home)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
        ttl = data.get("ttl", {})
        if not isinstance(ttl, dict):
            raise TypeError("ttl is not an object")
        root = data.get("blackbox_root")
        if root is not None and not (isinstance(root, str) and root.startswith("/")):
            raise TypeError("blackbox_root is not an absolute path")
        return {"ttl": {str(k): _check_ttl(str(k), v) for k, v in ttl.items()},
                "blackbox_root": root}
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"unreadable keep-warm overrides at {path} ({exc}); fix or "
                         f"delete it, then re-render") from exc


def load_ttl_overrides(localstack_home: str) -> "dict[str, int] | None":
    """The recorded keep-warm overrides; None when no sidecar exists yet.
    ValueError for a corrupt sidecar (see _read_sidecar)."""
    side = _read_sidecar(localstack_home)
    return None if side is None else side["ttl"]


def _atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def save_ttl_overrides(localstack_home: str, ttl: "dict[str, int]",
                       blackbox_root: "str | None" = None) -> None:
    """Write the sidecar. `blackbox_root` None keeps the owner already
    recorded there (a keep-warm toggle never changes the owner)."""
    if blackbox_root is None:
        side = _read_sidecar(localstack_home)
        blackbox_root = side["blackbox_root"] if side else None
    data = {"ttl": dict(sorted(ttl.items()))}
    if blackbox_root:
        data = {"blackbox_root": blackbox_root, **data}
    _atomic_write(overrides_path(localstack_home), json.dumps(data, indent=2) + "\n")


def set_ttl_override(localstack_home: str, member: str, ttl: int,
                     blackbox_root: "str | None" = None) -> None:
    """Record one keep-warm choice (a ttl equal to the template's drops the
    override), validated against `blackbox_root`'s template: pass the
    config's owner (config_owner). Does not re-render: call reconcile()
    after."""
    root = blackbox_root or str(Path(__file__).resolve().parents[1])
    defaults = member_ttls(template_path(root).read_text(encoding="utf-8"))
    if member not in defaults:
        raise UnknownMember(f"llama-swap template has no member {member!r}")
    _check_ttl(member, ttl)
    current = load_ttl_overrides(localstack_home) or {}
    if ttl == defaults[member]:
        current.pop(member, None)
    else:
        current[member] = ttl
    save_ttl_overrides(localstack_home, current)


def config_owner(localstack_home: str, live_text: "str | None" = None) -> "str | None":
    """The checkout the live config belongs to: the one recorded in the
    sidecar, else (a config rendered before the sidecar existed) the one its
    qwen-tts PYTHONPATH names, else None. `live_text` defaults to the config
    under `localstack_home`. ValueError for a corrupt sidecar."""
    side = _read_sidecar(localstack_home)
    if side and side["blackbox_root"]:
        return side["blackbox_root"]
    if live_text is None:
        try:
            live_text = Path(localstack_home, CONFIG_NAME).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
    m = _OWNER_RE.search(live_text)
    return m.group("root") if m else None


def _same_checkout(a: str, b: str) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def reconcile(localstack_home: str, blackbox_root: str,
              dest: "str | os.PathLike | None" = None, *, claim: bool = False) -> dict:
    """Converge the live config onto template + overrides, for the checkout
    that owns it (config_owner). `blackbox_root` is the caller's checkout:
      * it owns the config (or there is no config yet): render, writing only
        when the render differs from what is on disk (atomic, 0644), and
        record it as the owner;
      * another checkout owns it, or no owner can be told: write NOTHING and
        say why in "skipped" — unless `claim` (the installer: the operator
        chose this checkout), which renders for `blackbox_root` and moves the
        ownership there.
    Returns {"path", "changed", "lines_changed", "ttl_overrides", "owner",
    "skipped"}; "skipped" is None when the reconcile ran. Raises ValueError
    for a corrupt overrides sidecar or an override the template lost, and
    OSError when the template cannot be read — the live file is then left as
    it was."""
    dest = Path(dest) if dest is not None else Path(localstack_home, CONFIG_NAME)
    try:
        current = dest.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = None
    side = _read_sidecar(localstack_home)
    owner = config_owner(localstack_home, current) if current is not None else None
    if claim or current is None:
        root = blackbox_root
    elif owner is not None and _same_checkout(owner, blackbox_root):
        root = owner                    # keep the owner's spelling: no byte churn
    else:
        if owner is None:
            why = (f"cannot tell which checkout {dest} was rendered for (no "
                   f"{OVERRIDES_NAME} owner, no qwen-tts PYTHONPATH); re-run the "
                   "local-stack installer to record it")
        else:
            why = (f"{dest} belongs to the checkout at {owner}, not {blackbox_root}; "
                   "re-run the local-stack installer from this checkout to move it here")
        return {"path": str(dest), "changed": False, "lines_changed": 0,
                "ttl_overrides": side["ttl"] if side else None, "owner": owner,
                "skipped": why}
    template = template_path(root).read_text(encoding="utf-8")
    if side is None:
        # First render under this module: adopt any keep-warm choice the old
        # safe_dump path left in the live file instead of resetting it.
        defaults = member_ttls(template)
        live = _live_ttls(current) if current is not None else {}
        overrides = {m: t for m, t in live.items() if m in defaults and t != defaults[m]}
    else:
        overrides = side["ttl"]
    rendered = render(template, localstack_home, root, overrides)
    if side is None or side["blackbox_root"] != root:
        save_ttl_overrides(localstack_home, overrides, blackbox_root=root)
    result = {"path": str(dest), "changed": False, "lines_changed": 0,
              "ttl_overrides": overrides, "owner": root, "skipped": None}
    if current == rendered:
        return result
    old_lines = (current or "").split("\n")
    new_lines = rendered.split("\n")
    changed = sum(1 for a, b in zip(old_lines, new_lines) if a != b) + abs(
        len(old_lines) - len(new_lines))
    dest.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(dest, rendered)
    return {**result, "changed": True, "lines_changed": changed}


def _live_ttls(text: str) -> "dict[str, int]":
    """ttl per member from a live config: the template-shaped layout first,
    then a yaml.safe_dump-shaped one (members at two spaces, keys at four,
    unquoted ids)."""
    found = member_ttls(text)
    if found:
        return found
    out, in_models, member = {}, False, None
    for line in text.split("\n"):
        if line and not line[0].isspace():
            in_models, member = line.rstrip() == "models:", None
            continue
        if not in_models:
            continue
        m = re.match(r"^  ['\"]?(?P<id>[^'\":\s]+)['\"]?:\s*$", line)
        if m:
            member = m.group("id")
            continue
        m = re.match(r"^    ttl:\s*(?P<val>\d+)\s*$", line)
        if m and member is not None:
            out[member] = int(m.group("val"))
    return out


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Render the llama-swap config from the template.")
    ap.add_argument("--localstack-home", required=True, help="e.g. ~/.blackbox/localstack")
    ap.add_argument("--blackbox-root", required=True, help="the BlackBox checkout")
    ap.add_argument("--dest", help="default: <localstack-home>/" + CONFIG_NAME)
    args = ap.parse_args(argv)
    try:
        # The installer: the operator ran it from this checkout, so it claims
        # the config even when another checkout rendered it last.
        res = reconcile(args.localstack_home, args.blackbox_root, args.dest, claim=True)
    except (OSError, ValueError) as exc:
        print(f"[localstack-render] ERROR: {exc}", file=sys.stderr)
        return 1
    if res["changed"]:
        print(f"[localstack-render] wrote {res['path']} ({res['lines_changed']} line(s) "
              f"changed; keep-warm overrides: {res['ttl_overrides'] or 'none'})")
    else:
        print(f"[localstack-render] {res['path']} unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
