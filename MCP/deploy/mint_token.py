#!/usr/bin/env python3
"""Mint a BlackBox MCP bearer token bound to a REAL, live operator.

This is the production "select which operator gets routed through" control. It
NEVER hard-codes an operator: it reads THIS box's live roster from GET /operators,
lets you pick one of YOUR operators (or auto-selects when there is exactly one),
validates the choice against that roster, generates a high-entropy token, and
writes it to the gitignored 0600 token store. The MCP server then binds every
request on that token to the validated operator.

Usage:
    python3 MCP/deploy/mint_token.py                      # interactive picker
    python3 MCP/deploy/mint_token.py --operator Alice     # non-interactive
    python3 MCP/deploy/mint_token.py --backend-url http://localhost:9091
    python3 MCP/deploy/mint_token.py --tokens-file /path/to/mcp_tokens.json

The token is printed ONCE. Store it in your MCP client config; it is not
recoverable from the server (only the token->operator map is kept).
"""
import argparse
import json
import os
import secrets
import stat
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Reuse the single source of truth for operator-selection semantics.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from operator_resolution import choose_operator
except Exception:  # pragma: no cover - fallback keeps the script standalone
    def choose_operator(provided, operators, default):
        if provided and provided.strip():
            return provided.strip(), False
        if len(operators) == 1:
            return operators[0], False
        if len(operators) > 1:
            return (default or operators[0]), True
        return (default or "Operator"), False


def _repo_root() -> Path:
    return Path(os.getenv("BLACKBOX_ROOT") or Path(__file__).resolve().parents[2])


def _default_tokens_file() -> Path:
    env = os.getenv("BLACKBOX_MCP_TOKENS_FILE")
    if env:
        return Path(env)
    return _repo_root() / "Manifest" / "mcp_tokens.json"


def _fetch_roster(backend_url: str):
    url = backend_url.rstrip("/") + "/operators"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: could not reach {url}: {e}. Is the BlackBox backend running?")
    except Exception as e:
        sys.exit(f"ERROR: invalid response from {url}: {e}")
    operators = list(data.get("operators") or [])
    default = data.get("default") or ""
    return operators, default


def _pick_operator(operators, default, requested, assume_yes):
    if not operators:
        sys.exit("ERROR: this box has NO operators (GET /operators is empty). "
                 "Configure operators in config.ini [users] first.")
    if requested:
        if requested not in operators:
            sys.exit(f"ERROR: operator {requested!r} is not in this box's live "
                     f"roster {sorted(operators)}. Pick one of those exact names.")
        return requested
    resolved, needs_selection = choose_operator(None, operators, default)
    if not needs_selection:
        print(f"Single operator on this box -> binding the token to {resolved!r}.")
        return resolved
    # Multiple operators -> interactive numbered picker (unless --yes).
    if assume_yes:
        sys.exit(f"ERROR: {len(operators)} operators on this box "
                 f"{sorted(operators)}; pass --operator <name> in non-interactive mode.")
    print("Operators on this box (the token will route through the one you pick):")
    for i, op in enumerate(operators, 1):
        tag = "  (default)" if op == default else ""
        print(f"  [{i}] {op}{tag}")
    while True:
        choice = input(f"Select operator [1-{len(operators)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(operators):
            return operators[int(choice) - 1]
        print("  not a valid choice; try again.")


def _write_token(tokens_file: Path, token: str, operator: str):
    tokens_file.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if tokens_file.exists():
        try:
            existing = json.loads(tokens_file.read_text(encoding="utf-8")) or {}
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}
    existing[token] = operator
    tokens_file.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    os.chmod(tokens_file, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def main():
    ap = argparse.ArgumentParser(description="Mint a BlackBox MCP bearer token "
                                 "bound to a live operator.")
    ap.add_argument("--backend-url", default=os.getenv("BLACKBOX_URL",
                    "http://localhost:9091"), help="BlackBox backend base URL.")
    ap.add_argument("--operator", default=None,
                    help="Operator to bind (must be in GET /operators). Omit to pick.")
    ap.add_argument("--tokens-file", default=None,
                    help="Token store path (default Manifest/mcp_tokens.json).")
    ap.add_argument("--yes", action="store_true",
                    help="Non-interactive; requires --operator when >1 operator.")
    ap.add_argument("--json", action="store_true", help="Emit JSON only.")
    args = ap.parse_args()

    operators, default = _fetch_roster(args.backend_url)
    operator = _pick_operator(operators, default, args.operator, args.yes)
    token = "bbmcp_" + secrets.token_urlsafe(48)
    tokens_file = Path(args.tokens_file) if args.tokens_file else _default_tokens_file()
    _write_token(tokens_file, token, operator)

    if args.json:
        print(json.dumps({"token": token, "operator": operator,
                          "tokens_file": str(tokens_file)}))
        return
    print()
    print("=" * 64)
    print(f"  Token minted and bound to operator: {operator}")
    print(f"  Stored (0600) in: {tokens_file}")
    print("=" * 64)
    print()
    print("  Bearer token (shown ONCE -- copy it now):")
    print(f"    {token}")
    print()
    print("  Use it in your MCP client, e.g. Claude Code:")
    print(f"    claude mcp add --transport http blackbox \\")
    print(f"      https://<your-funnel-host>:8443/mcp \\")
    print(f"      --header 'Authorization: Bearer {token}'")
    print()
    print("  Restart the MCP service so it loads the new token:")
    print("    sudo systemctl restart blackbox-mcp.service")
    print()


if __name__ == "__main__":
    main()
