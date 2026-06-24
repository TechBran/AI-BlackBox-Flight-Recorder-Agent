#!/usr/bin/env bash
# AI BlackBox installer — Ubuntu 24.04
set -euo pipefail

# ── Step 0: detect sudo, resolve real user/home (audit M6) ──
if [[ $EUID -eq 0 ]]; then
    if [[ -z "${SUDO_USER:-}" ]]; then
        echo "[install] ERROR: do not run as direct root. Run as your user (sudo invoked as needed)."
        exit 1
    fi
    REAL_USER="$SUDO_USER"
    REAL_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
else
    REAL_USER="$USER"
    REAL_HOME="$HOME"
fi

# Determine BLACKBOX_ROOT: parent of the directory holding this script
BLACKBOX_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "[install] BLACKBOX_ROOT=$BLACKBOX_ROOT"
echo "[install] REAL_USER=$REAL_USER  REAL_HOME=$REAL_HOME"

# Audit: REAL_USER drives sudoers grants — defend against weird envvar injection
if ! [[ "$REAL_USER" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
    echo "[install] ERROR: REAL_USER='$REAL_USER' contains invalid characters (POSIX usernames only)" >&2
    exit 1
fi

# ── Step 0a: git-clone bootstrap (audit E23 / T3) ──
# Update pipeline requires $BLACKBOX_ROOT to be a git checkout so the wizard's
# Updates panel can `git fetch + reset --hard origin/main` against it. Two
# cases:
#   1. Empty $BLACKBOX_ROOT → fresh clone from public repo
#   2. ZIP install populated $BLACKBOX_ROOT but never ran git → lazy-init:
#      create .git/ in place with origin pointing at the repo. No checkout —
#      customer's ZIP files stay in place. The wizard's FIRST update will
#      detect divergence and ask before stomping (audit M2).
#
# Repo is public (Brandon's T10 decision) so HTTPS clone needs no credentials.
if [[ ! -d "$BLACKBOX_ROOT/.git" ]]; then
    if [[ -z "$(ls -A "$BLACKBOX_ROOT" 2>/dev/null)" ]]; then
        echo "[install] $BLACKBOX_ROOT is empty — cloning blackbox-poc..."
        sudo -u "$REAL_USER" git clone https://github.com/TechBran/blackbox-poc.git "$BLACKBOX_ROOT"
    else
        echo "[install] $BLACKBOX_ROOT has ZIP install content — lazy-initializing git..."
        sudo -u "$REAL_USER" bash -c "
            set -e
            cd '$BLACKBOX_ROOT'
            git init -q
            git remote add origin https://github.com/TechBran/blackbox-poc.git
            git fetch -q origin main
            # Mark main as the working branch but DON'T checkout. Customer ZIP
            # files stay in place. First update via the wizard does the diff
            # + consent dance before any reset --hard.
            git update-ref refs/heads/main FETCH_HEAD
            git symbolic-ref HEAD refs/heads/main
            git branch --set-upstream-to=origin/main main 2>/dev/null || true
            echo '[install] Git initialized; tracking origin/main'
        "
    fi
else
    echo "[install] $BLACKBOX_ROOT already a git repo (no bootstrap needed)"
fi

# ── Pre-flight (Phase 4.0) ──
"$BLACKBOX_ROOT/Scripts/install-preflight.sh"

# ── Step 1: apt deps (audit C1 — corrected pipeline) ──
# E16 fix: install MUST_HAVE + SHOULD_HAVE buckets. SHOULD_HAVE packages
# (scrot, xdotool, openbox, x11vnc, mpg123, alsa-utils, chromium-browser)
# back customer-facing features like Computer Use screenshots + audio playback;
# previously only MUST_HAVE installed so those features silently failed.
echo "[install] Installing system packages (MUST_HAVE + SHOULD_HAVE)..."
sudo apt update
grep -E '^[a-zA-Z0-9._+-]+\s+#\s+(MUST_HAVE|SHOULD_HAVE)' \
    "$BLACKBOX_ROOT/Scripts/onboarding/system-packages.txt" \
  | awk '{print $1}' \
  | xargs sudo apt install -y

# ── Step 1b: Tailscale install (audit E1 — official installer adds apt repo + signing key + package) ──
# Pre-installs Tailscale on every BlackBox. Wizard onboarding step then only
# needs to handle authentication (not install). Idempotent on re-run.
if [[ ! -x /usr/bin/tailscale ]]; then
    echo "[install] Installing Tailscale..."
    curl -fsSL https://tailscale.com/install.sh | sh
else
    echo "[install] Tailscale already installed (skipping)"
fi

# Audit: fail fast if tailscale binary not at the path sudoers grants
if ! [[ -x /usr/bin/tailscale ]]; then
    echo "[install] ERROR: tailscale binary not at /usr/bin/tailscale after install" >&2
    echo "[install] Found at: $(command -v tailscale 2>/dev/null || echo 'nowhere on PATH')" >&2
    exit 1
fi

# ── Step 1d: Ollama install (pluggable embeddings — local model runtime) ──
# Pre-installs the Ollama daemon on every BlackBox so the onboarding wizard's
# "Memory & Search" step only needs one-click model pulls (POST
# /embeddings/ollama/pull streams progress), never a terminal. The orchestrator
# itself cannot install it at runtime (no root; ProtectSystem). Models are NOT
# pre-pulled here — the 0.6B/8B downloads (0.7-6 GB) only happen if the
# customer picks a local model in the wizard. Idempotent on re-run; non-fatal
# on failure (cloud embedding models still work; wizard shows the remediation).
if ! command -v ollama >/dev/null 2>&1; then
    echo "[install] Installing Ollama (local embedding runtime)..."
    if curl -fsSL https://ollama.com/install.sh | sh; then
        systemctl enable --now ollama 2>/dev/null || true
        echo "[install] Ollama installed + service enabled"
    else
        echo "[install] WARN: Ollama install failed — local embedding models will"
        echo "[install]       show an install blocker in the wizard (cloud models unaffected)"
    fi
else
    echo "[install] Ollama already installed (skipping)"
    systemctl enable --now ollama 2>/dev/null || true
fi

# ── Step 1c: nvm + Node.js + CLI agent binaries (audit E20) ──
# CLI Agent feature spawns claude / gemini / codex via tmux PTY bridge
# (Orchestrator/routes/cli_agent_routes.py PROVIDER_BIN). Binaries are
# provided as npm globals — install nvm (matches dev-box pattern per
# CLAUDE.md memory: nvm-aware bin resolution in path_extension.py),
# install latest LTS Node, then npm install -g the three provider CLIs.
# All as $REAL_USER so binaries land in ~/.nvm/versions/node/<ver>/bin/
# which the orchestrator's path_extension auto-discovers via glob.
echo "[install] Installing nvm + Node.js + CLI agent binaries..."
sudo -u "$REAL_USER" bash -c '
    export NVM_DIR="$HOME/.nvm"
    if [[ ! -d "$NVM_DIR" ]]; then
        echo "[install]   Installing nvm..."
        curl -fsSL -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.0/install.sh | bash
    else
        echo "[install]   nvm already installed (skipping)"
    fi
    . "$NVM_DIR/nvm.sh"
    if ! command -v node > /dev/null 2>&1; then
        echo "[install]   Installing latest LTS Node..."
        nvm install --lts
        nvm alias default "lts/*"
    else
        echo "[install]   Node already installed: $(node --version)"
    fi
    echo "[install]   Installing CLI agent npm globals: @anthropic-ai/claude-code, @google/gemini-cli, @openai/codex..."
    npm install -g @anthropic-ai/claude-code @google/gemini-cli @openai/codex 2>&1 | tail -3
    echo "[install]   CLI agent binaries resolved to: $(which claude gemini codex 2>&1 | tr "\n" " ")"
'

# ── Step 2: Python venv (audit I1 — run as $REAL_USER so files are user-owned, not root-owned) ──
echo "[install] Creating Python venv..."
sudo -u "$REAL_USER" python3.12 -m venv "$BLACKBOX_ROOT/Orchestrator/venv"
sudo -u "$REAL_USER" "$BLACKBOX_ROOT/Orchestrator/venv/bin/pip" install --upgrade pip
sudo -u "$REAL_USER" "$BLACKBOX_ROOT/Orchestrator/venv/bin/pip" install -r "$BLACKBOX_ROOT/requirements.txt"

# ── Step 2b: BlackBox MCP server venv + per-CLI registration (audit E21) ──
# MCP server lives in a SEPARATE venv from the Orchestrator because mcp's
# transitive starlette>=0.49 conflicts with fastapi 0.118's starlette<0.49
# upper bound — sharing the venv would brick the Orchestrator. Cheap cost:
# MCP/venv only needs 4 packages (mcp, httpx, requests, beautifulsoup4).
# Server imports from Orchestrator/web_tools.py + Orchestrator/tools/
# tool_registry.py — those only need stdlib + requests + bs4.
#
# Brandon 2026-05-17 ("MCP Tools server should start on every boot, you know,
# every new install as well"): MCP is a stdio subprocess spawned on-demand by
# the CLI when a session starts — not a long-running service. "Starts on every
# install" really means "registered in every CLI's user-scoped config so it's
# available in every project, in every session, on every fresh BlackBox".
#
# Use each CLI's `mcp add -s user` subcommand so we don't track schema drift.
# Remove-first (ignore-missing) for idempotent re-install / upgrade semantics.
echo "[install] Building BlackBox MCP venv + registering server with CLI agents..."
sudo -u "$REAL_USER" bash -c "
    set -e
    BB='${BLACKBOX_ROOT}'
    MCP_VENV=\"\${BB}/MCP/venv\"
    MCP_PY=\"\${MCP_VENV}/bin/python\"
    MCP_SERVER=\"\${BB}/MCP/blackbox_mcp_server.py\"

    if [[ ! -x \"\${MCP_PY}\" ]]; then
        echo '[install]   Creating MCP venv...'
        python3 -m venv \"\${MCP_VENV}\"
    fi
    \"\${MCP_VENV}/bin/pip\" install --quiet --upgrade pip
    \"\${MCP_VENV}/bin/pip\" install --quiet -r \"\${BB}/MCP/requirements.txt\"
    echo \"[install]   MCP venv ready at \${MCP_PY}\"

    # Load nvm so claude/gemini/codex are on PATH
    export NVM_DIR=\"\$HOME/.nvm\"
    [[ -s \"\$NVM_DIR/nvm.sh\" ]] && . \"\$NVM_DIR/nvm.sh\"

    # Claude Code: stdio server, user scope, BLACKBOX_URL+ROOT env
    if command -v claude > /dev/null 2>&1; then
        claude mcp remove blackbox -s user > /dev/null 2>&1 || true
        claude mcp add blackbox -s user \
            -e BLACKBOX_URL=http://localhost:9091 \
            -e BLACKBOX_ROOT=\"\${BB}\" \
            -- \"\${MCP_PY}\" \"\${MCP_SERVER}\" > /dev/null \
          && echo '[install]   claude: registered blackbox MCP (user scope)' \
          || echo '[install]   claude: registration failed (non-fatal)'
    fi

    # Gemini CLI: -s user, -e env, command + args positional (no -- needed)
    if command -v gemini > /dev/null 2>&1; then
        gemini mcp remove blackbox > /dev/null 2>&1 || true
        gemini mcp add blackbox -s user \
            -e BLACKBOX_URL=http://localhost:9091 \
            -e BLACKBOX_ROOT=\"\${BB}\" \
            \"\${MCP_PY}\" \"\${MCP_SERVER}\" > /dev/null \
          && echo '[install]   gemini: registered blackbox MCP (user scope)' \
          || echo '[install]   gemini: registration failed (non-fatal)'
    fi

    # Codex: --env not -e, requires -- separator before stdio command. Codex
    # has only global config (no per-project), so no scope flag.
    if command -v codex > /dev/null 2>&1; then
        codex mcp remove blackbox > /dev/null 2>&1 || true
        codex mcp add blackbox \
            --env BLACKBOX_URL=http://localhost:9091 \
            --env BLACKBOX_ROOT=\"\${BB}\" \
            -- \"\${MCP_PY}\" \"\${MCP_SERVER}\" > /dev/null \
          && echo '[install]   codex: registered blackbox MCP (global)' \
          || echo '[install]   codex: registration failed (non-fatal)'
    fi
"

# ── Step 2c: install zellij (Phase 1 T3 + T1.5 fused) ──
# Stands up the Zellij web client daemon that backs the CLI Agent terminal
# bridge. Eight ordered sub-steps inside step_2c_install_zellij():
#   1. Read pinned version from installer/templates/zellij-version
#   2. Generate TLS cert at /etc/blackbox/zellij/{cert,key}.pem (T1.5 fused —
#      idempotent skip if present+valid+non-expired; refuse to leave HTTPS
#      enforcement disabled if cert generation fails)
#   3. Reconcile ~/.local/share/zellij/tokens.db against orchestrator session
#      state file (audit C3 — wipe both if one-sided, leave alone otherwise)
#   4. Download + sha256-verify + atomic-install zellij binary (extract-then-
#      verify per Phase 0 finding #4; skip download if already at pinned ver)
#   5. Install the dispatcher script at /usr/local/sbin/blackbox-install-zellij-binary
#      for future updates (sudoers grant points at this exact path)
#   6. Write ~/.config/zellij/config.kdl (port 9097, enforce_https=true,
#      cert paths pointing at step 2's artifacts)
#   7. Install + daemon-reload + enable + restart zellij-web.service (template
#      lives at installer/templates/zellij-web.service; substitute REAL_USER)
#   8. HTTP sanity check via curl (warning-only — T5 catches
#      persistent issues)
step_2c_install_zellij() {
    # Requires from install.sh preamble:
    #   $REAL_USER       — the user that owns blackbox.service (== $REAL_USER in unit)
    #   $REAL_HOME       — that user's home directory (where ~/.config/zellij lives)
    #   $BLACKBOX_ROOT   — the project root (where installer/templates/ lives)
    local ZELLIJ_TEMPLATE_DIR="$BLACKBOX_ROOT/installer/templates"
    local CERT_DIR="/etc/blackbox/zellij"
    local CERT_FILE="$CERT_DIR/cert.pem"
    local KEY_FILE="$CERT_DIR/key.pem"
    local ZELLIJ_BIN="/usr/local/bin/zellij"
    local ZELLIJ_PORT="9097"

    # ─── 1. Read pinned version ───
    local ZELLIJ_VERSION
    ZELLIJ_VERSION=$(grep -vE '^[[:space:]]*(#|$)' "$ZELLIJ_TEMPLATE_DIR/zellij-version" \
                       | head -n 1 | tr -d '[:space:]')
    if [[ -z "$ZELLIJ_VERSION" ]]; then
        echo "[install] ERROR: could not parse pinned zellij version from $ZELLIJ_TEMPLATE_DIR/zellij-version" >&2
        exit 1
    fi
    echo "[install] Pinned zellij version: $ZELLIJ_VERSION"

    # ─── 2. TLS posture (T5 walk-back of audit C1) ───
    # NOTE: original C1 lock required generating a self-signed cert here +
    # enforcing HTTPS on localhost. Zellij 0.44.3's web_server_cert /
    # web_server_key config keys (and the --cert/--key CLI flags) refuse to
    # bind with "Cannot bind without an SSL certificate" regardless of cert
    # paths/format — see T5 diagnostic in commit history. Pragmatic v1
    # decision: serve plain HTTP on 127.0.0.1:9097 and rely on the
    # orchestrator-fronted Tailscale-funnel TLS termination at port 9091
    # for customer-facing crypto. Internal localhost-to-localhost connection
    # between orchestrator and zellij-web becomes the new trust boundary —
    # documented in plan AC2. Defense-in-depth TLS at this internal hop is
    # deferred to v1.1 once Zellij's HTTPS cert config is understood.
    echo "[install] TLS posture: HTTP on localhost (TLS terminated at orchestrator edge — see plan AC2)"

    # ─── 3. Reconcile tokens.db (audit C3) ───
    # Four cases:
    #   neither present → no-op (fresh install)
    #   both present    → no-op (consistent state)
    #   one-sided       → wipe both (reinstall partial-state reconciliation)
    local TOKENS_DB="$REAL_HOME/.local/share/zellij/tokens.db"
    local SESSIONS_JSON="$BLACKBOX_ROOT/Orchestrator/cli_agent/state/zellij_sessions.json"
    local have_tokens=0 have_sessions=0
    [[ -f "$TOKENS_DB" ]]      && have_tokens=1
    [[ -f "$SESSIONS_JSON" ]]  && have_sessions=1
    if [[ $have_tokens -eq 0 && $have_sessions -eq 0 ]]; then
        echo "[install] tokens.db reconciliation: clean (neither tokens.db nor zellij_sessions.json present)"
    elif [[ $have_tokens -eq 1 && $have_sessions -eq 1 ]]; then
        echo "[install] tokens.db reconciliation: consistent (both present, leaving alone)"
    else
        echo "[install] tokens.db reconciliation: WIPING — one-sided state detected (tokens=$have_tokens sessions=$have_sessions)"
        # Permission errors are possible if tokens.db is owned by another user (e.g., a leftover
        # from a previous install with a different REAL_USER); '|| true' keeps reconciliation
        # from breaking install.
        sudo -u "$REAL_USER" rm -f "$TOKENS_DB"      2>/dev/null || true
        sudo -u "$REAL_USER" rm -f "$SESSIONS_JSON"  2>/dev/null || true
    fi

    # ─── 4. Download + install zellij binary ───
    # Direct install (NOT via dispatcher — dispatcher is for UPDATES). Skip
    # download if already at pinned version. Extract-then-verify per Phase 0
    # finding #4 (published .sha256sum hashes the EXTRACTED binary, not the
    # tarball). Atomic install via `install -m 0755`.
    local need_download=1
    if [[ -x "$ZELLIJ_BIN" ]]; then
        local current
        current=$("$ZELLIJ_BIN" --version 2>/dev/null | awk '{print $2}' || true)
        if [[ "$current" == "$ZELLIJ_VERSION" ]]; then
            echo "[install] zellij $ZELLIJ_VERSION already installed, skipping download"
            need_download=0
        fi
    fi
    if [[ $need_download -eq 1 ]]; then
        local TMPDIR
        TMPDIR=$(mktemp -d /tmp/zellij-install-XXXXXX)
        # Trap covers the five exit-1 paths below (download/extract/hash-parse/
        # hash-mismatch failures) so we never leak /tmp/zellij-install-* dirs.
        # The explicit `rm -rf "$TMPDIR"` at the end of the happy path is kept
        # as documented redundancy — the trap handles both happy + sad paths.
        # ${TMPDIR:-} default protects against `local` going out of scope when
        # the trap fires AFTER function-exit (set -u would otherwise kill the
        # trap with "TMPDIR: unbound variable").
        trap 'rm -rf "${TMPDIR:-}"' EXIT

        local TARBALL_URL="https://github.com/zellij-org/zellij/releases/download/v${ZELLIJ_VERSION}/zellij-x86_64-unknown-linux-musl.tar.gz"
        local SHA_URL="https://github.com/zellij-org/zellij/releases/download/v${ZELLIJ_VERSION}/zellij-x86_64-unknown-linux-musl.sha256sum"

        echo "[install] Downloading zellij v${ZELLIJ_VERSION} (x86_64-linux-musl)..."
        if ! curl --fail --location --silent --show-error \
                --output "$TMPDIR/zellij.tar.gz" "$TARBALL_URL"; then
            echo "[install] ERROR: zellij tarball download failed: $TARBALL_URL" >&2
            exit 1
        fi
        if ! curl --fail --location --silent --show-error \
                --output "$TMPDIR/zellij.sha256sum" "$SHA_URL"; then
            echo "[install] ERROR: zellij sha256sum download failed: $SHA_URL" >&2
            exit 1
        fi

        echo "[install] Extracting zellij tarball..."
        if ! tar -xzf "$TMPDIR/zellij.tar.gz" -C "$TMPDIR"; then
            echo "[install] ERROR: zellij tarball extraction failed" >&2
            exit 1
        fi
        if [[ ! -f "$TMPDIR/zellij" ]]; then
            echo "[install] ERROR: expected zellij binary not found at $TMPDIR/zellij after extract" >&2
            exit 1
        fi

        # Phase 0 finding #4: hash the EXTRACTED binary, not the tarball.
        local EXPECTED ACTUAL
        EXPECTED=$(awk '{print $1}' "$TMPDIR/zellij.sha256sum")
        ACTUAL=$(sha256sum "$TMPDIR/zellij" | awk '{print $1}')
        if [[ -z "$EXPECTED" ]]; then
            echo "[install] ERROR: could not parse expected hash from $TMPDIR/zellij.sha256sum" >&2
            exit 1
        fi
        if [[ "$EXPECTED" != "$ACTUAL" ]]; then
            echo "[install] ERROR: zellij sha256 mismatch" >&2
            echo "[install]   expected: $EXPECTED" >&2
            echo "[install]   actual:   $ACTUAL" >&2
            exit 1
        fi
        echo "[install] zellij sha256 verified ($ACTUAL)"

        # Atomic install + perms + ownership in one call.
        sudo install -m 0755 -o root -g root "$TMPDIR/zellij" "$ZELLIJ_BIN"
        echo "[install] Installed $ZELLIJ_BIN (version $ZELLIJ_VERSION)"
        rm -rf "$TMPDIR"
    fi

    # ─── 5. Install the dispatcher script (for future updates) ───
    # Sudoers grant authorizes `/usr/local/sbin/blackbox-install-zellij-binary *`
    # (no .sh extension — see installer/templates/sudoers-blackbox-system).
    sudo install -m 0755 -o root -g root \
        "$ZELLIJ_TEMPLATE_DIR/blackbox-install-zellij-binary.sh" \
        "/usr/local/sbin/blackbox-install-zellij-binary"
    echo "[install] Installed dispatcher: /usr/local/sbin/blackbox-install-zellij-binary"

    # ─── 6. Write ~/.config/zellij/config.kdl ───
    # Port 9097 hardcoded here (matches comment in installer/templates/zellij-version).
    # Idempotent: skip write if file is byte-identical to what we'd emit.
    local ZELLIJ_CFG_DIR="$REAL_HOME/.config/zellij"
    local ZELLIJ_CFG="$ZELLIJ_CFG_DIR/config.kdl"
    local ZELLIJ_LAYOUTS_DIR="$ZELLIJ_CFG_DIR/layouts"
    sudo -u "$REAL_USER" mkdir -p "$ZELLIJ_CFG_DIR" "$ZELLIJ_LAYOUTS_DIR"

    # Custom "blackbox" layout — just a single pane, no tab_bar / status_bar
    # plugins. Result: the iframe renders only the terminal content with zero
    # Zellij chrome. Full "AI BlackBox Flight Recorder" rebrand of the
    # remaining plugin text is deferred (would require a custom Zellij
    # plugin); this gets ~95% of the UX win for ~0% of the work.
    local BLACKBOX_LAYOUT="$ZELLIJ_LAYOUTS_DIR/blackbox.kdl"
    local TMP_LAYOUT
    TMP_LAYOUT=$(mktemp)
    cat > "$TMP_LAYOUT" <<'LAYOUT_EOF'
// Generated by BlackBox install.sh — DO NOT EDIT BY HAND.
// Minimal chrome-less layout: a single pane, no tab_bar, no status_bar.
layout {
    pane
}
LAYOUT_EOF
    sudo install -m 0644 -o "$REAL_USER" -g "$REAL_USER" "$TMP_LAYOUT" "$BLACKBOX_LAYOUT"
    rm -f "$TMP_LAYOUT"

    local TMP_CFG
    TMP_CFG=$(mktemp)
    cat > "$TMP_CFG" <<KDL_EOF
// $ZELLIJ_CFG
// Generated by BlackBox install.sh — DO NOT EDIT BY HAND.
// Zellij web server config for AI BlackBox CLI Agent.
// HTTP on 127.0.0.1 only — TLS terminated at orchestrator edge (plan AC2).
web_server true
web_server_ip "127.0.0.1"
web_server_port $ZELLIJ_PORT
web_sharing "on"
enforce_https_for_localhost false

// ── UX cleanup (T15, 2026-05-25) ─────────────────────────────────────────
// Drop the auto-shown tips/release-notes plugins. (Earlier experiments
// with simplified_ui/pane_frames/default_layout "blackbox" empirically
// BROKE claude's TUI rendering in WS-attached clients — root cause is
// zellij-web's serialization bug per PR #5156, but the chromeless
// layout exacerbated it. Keep Zellij's default layout for now.)
show_startup_tips false
show_release_notes false
KDL_EOF
    if [[ -f "$ZELLIJ_CFG" ]] && cmp -s "$TMP_CFG" "$ZELLIJ_CFG"; then
        echo "[install] zellij config.kdl already current, skipping write"
        rm -f "$TMP_CFG"
    else
        if [[ -f "$ZELLIJ_CFG" ]]; then
            echo "[install] config.kdl: overwrote previous version (operator edits to this file are NOT preserved; customize via /etc/blackbox/zellij/ overrides if needed)"
        else
            echo "[install] config.kdl: writing (first install)"
        fi
        # Atomic write with correct ownership in one root-side `install` call.
        # `sudo -u $REAL_USER cp` was the original approach but breaks because
        # mktemp creates root-owned mode-0600 files that REAL_USER can't read.
        sudo install -m 0644 -o "$REAL_USER" -g "$REAL_USER" "$TMP_CFG" "$ZELLIJ_CFG"
        rm -f "$TMP_CFG"
        echo "[install] Wrote $ZELLIJ_CFG (port $ZELLIJ_PORT, HTTP localhost)"
    fi

    # ─── 7. Install zellij-web.service unit ───
    # Substitute REAL_USER_PLACEHOLDER at install time. daemon-reload first
    # so systemd notices the new unit, then enable + restart (restart handles
    # both "not running yet" and "already running" cases).
    sed "s/REAL_USER_PLACEHOLDER/$REAL_USER/g" \
        "$ZELLIJ_TEMPLATE_DIR/zellij-web.service" \
        | sudo tee /etc/systemd/system/zellij-web.service > /dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable zellij-web.service > /dev/null 2>&1
    sudo systemctl restart zellij-web.service
    # 2-second settle gives systemd's start-job → active transition AND
    # zellij's port-bind both time to complete before the curl sanity check
    # below (otherwise slow boxes see is-active=true but port not yet open,
    # producing a spurious "returned 000" WARNING).
    sleep 2
    if ! sudo systemctl is-active --quiet zellij-web.service; then
        echo "[install] ERROR: zellij-web.service is not active after restart" >&2
        echo "[install] (Check 'journalctl -u zellij-web.service' for details)" >&2
        exit 1
    fi
    echo "[install] zellij-web.service installed + active"

    # ─── 8. HTTP sanity check ───
    # --insecure: self-signed cert, expected. Warning only — T5 smoke-test
    # catches persistent issues; transient timing on first start shouldn't
    # block the rest of install.sh.
    local http_code
    http_code=$(curl --silent --output /dev/null --write-out "%{http_code}" \
                    --max-time 5 "http://127.0.0.1:$ZELLIJ_PORT/" 2>/dev/null || echo "000")
    if [[ "$http_code" == "200" ]]; then
        echo "[install] zellij-web HTTP sanity check: 200 OK"
    else
        echo "[install] WARNING: zellij-web HTTP sanity check returned $http_code (expected 200) — T5 will verify"
    fi
}
step_2c_install_zellij

# ── Step 3: .env from template (audit I2 — created as $REAL_USER, mode 0600 since it holds API keys) ──
if [[ ! -f "$BLACKBOX_ROOT/.env" ]]; then
    sudo -u "$REAL_USER" cp "$BLACKBOX_ROOT/.env.template" "$BLACKBOX_ROOT/.env"
    sudo -u "$REAL_USER" bash -c "echo 'BLACKBOX_ROOT=$BLACKBOX_ROOT' >> '$BLACKBOX_ROOT/.env'"
    chmod 0600 "$BLACKBOX_ROOT/.env"
    echo "[install] Created .env from template (mode 0600)"
fi

# Step 3a: ensure CLI_AGENT_BACKEND=zellij is set on existing customers'
# .env files too (idempotent — appended only if missing, never overrides
# an existing assignment). Without this, customers who installed before
# Phase 3 keep defaulting to the legacy tmux backend and never see the
# launch-buttons UI even after pulling the new code. MSO2 hit this on
# the 2026-05-25 update.
if ! grep -qE "^CLI_AGENT_BACKEND=" "$BLACKBOX_ROOT/.env" 2>/dev/null; then
    sudo -u "$REAL_USER" bash -c "echo 'CLI_AGENT_BACKEND=zellij' >> '$BLACKBOX_ROOT/.env'"
    echo "[install] Appended CLI_AGENT_BACKEND=zellij to .env (was missing)"
fi

# CLI_AGENT_IDLE_DAYS: optional override for the zellij idle-session reaper
# (default 7d in code). Appended for parity with CLI_AGENT_BACKEND so an
# upgraded box has a knob to tune; harmless if left at the default.
if ! grep -qE "^CLI_AGENT_IDLE_DAYS=" "$BLACKBOX_ROOT/.env" 2>/dev/null; then
    sudo -u "$REAL_USER" bash -c "echo 'CLI_AGENT_IDLE_DAYS=7' >> '$BLACKBOX_ROOT/.env'"
    echo "[install] Appended CLI_AGENT_IDLE_DAYS=7 to .env (was missing)"
fi

# ── Step 3b: config.ini from template (per-customer state — operators + pairing) ──
# Customer ZIP doesn't ship config.ini (gitignored to prevent shipping the
# author's operator roster + tailnet hostname). Wizard's operator + tailscale
# steps populate [users] + [pairing] sections via /onboarding/config writes.
if [[ ! -f "$BLACKBOX_ROOT/config.ini" ]]; then
    sudo -u "$REAL_USER" cp "$BLACKBOX_ROOT/config.ini.template" "$BLACKBOX_ROOT/config.ini"
    echo "[install] Created config.ini from template"
fi

# ── Step 3c: device_registry/devices.json from template (per-install state — tracked devices on this BlackBox) ──
# Customer ZIP doesn't ship devices.json (gitignored to prevent shipping the
# author's Tailscale device list — phones, dev boxes from a different tailnet).
# /devices endpoints + tailscale sync repopulate from the customer's actual
# tailnet on first use.
if [[ ! -f "$BLACKBOX_ROOT/Orchestrator/device_registry/devices.json" ]]; then
    sudo -u "$REAL_USER" cp "$BLACKBOX_ROOT/Orchestrator/device_registry/devices.json.template" \
        "$BLACKBOX_ROOT/Orchestrator/device_registry/devices.json"
    echo "[install] Bootstrapped Orchestrator/device_registry/devices.json from template"
fi

# ── Step 4: systemd unit (audit M2 + M3 + Q3 + Q4) ──
echo "[install] Installing blackbox.service..."
sudo tee /etc/systemd/system/blackbox.service > /dev/null <<EOF
[Unit]
Description=AI BlackBox Orchestrator
Documentation=https://github.com/TechBran/blackbox-poc
After=network-online.target zellij-web.service
Wants=network-online.target zellij-web.service
# Restart rate limiting (audit empirical fix: these belong in [Unit], not [Service]
# — systemd silently ignores them in [Service] and warns. Without them, Restart=always
# loops forever on a broken install at ~6 attempts/min instead of bounding to 5 per 600s.)
StartLimitBurst=5
StartLimitIntervalSec=600

[Service]
Type=simple
User=$REAL_USER
WorkingDirectory=$BLACKBOX_ROOT
EnvironmentFile=$BLACKBOX_ROOT/.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
# Free port 9092 before start. KillMode=process (cli-agent-overrides.conf) keeps
# children alive across restarts so CLI-agent PTY sessions survive — but it also
# orphans the Asterisk audio_subprocess, which keeps squatting 127.0.0.1:9092 and
# makes the next start crash "address already in use" (Errno 98). SIGKILL is
# required (uncatchable): the subprocess installs a SIGTERM handler whose graceful
# path hangs once orphaned, so SIGTERM/pkill leaves it alive. '-' ignores
# "nothing to kill"; pkill skips its own PID. (Caught 2026-06-05.)
ExecStartPre=-/usr/bin/pkill -9 -f audio_subprocess.py
ExecStart=$BLACKBOX_ROOT/Orchestrator/venv/bin/python -m uvicorn Orchestrator.app:app \\
    --host 0.0.0.0 --port 9091 \\
    --timeout-keep-alive 120 --limit-max-requests 100000 --timeout-graceful-shutdown 30 --loop uvloop
Restart=always
RestartSec=10

# Memory pressure (audit Q4) — soft cap at 70 % of system RAM
MemoryHigh=70%

# Security hardening (audit M2 — preserved from existing unit)
# NOTE: ProtectHome=read-only (NOT true) because BLACKBOX_ROOT lives in /home
# (audit Q2=A install location). ProtectHome=true masks /home entirely so the
# sandboxed process cannot exec \$BLACKBOX_ROOT/Orchestrator/venv/bin/python
# → status=203/EXEC. read-only allows visibility; ReadWritePaths punches through
# for the install dir's write needs (Volume/, Manifest/, Fossils/, etc.).
#
# NoNewPrivileges=false (audit empirical T4 finding): the Tailscale wizard
# actuator invokes \`sudo -n /usr/bin/tailscale up\` via the NOPASSWD grant
# from Step 4e. NoNewPrivileges=true would block sudo's setuid escalation
# regardless of sudoers config ("sudo: The 'no new privileges' flag is set,
# which prevents sudo from running as root"). The bounded NOPASSWD sudoers
# entry remains the security boundary — only specific tailscale subcommands
# with literal-arg matching are permitted.
NoNewPrivileges=false
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$BLACKBOX_ROOT
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=blackbox

[Install]
WantedBy=multi-user.target
EOF

# ── Step 4b: override.conf scaffold (audit M3 carry-forward) ──
sudo mkdir -p /etc/systemd/system/blackbox.service.d
sudo tee /etc/systemd/system/blackbox.service.d/override.conf > /dev/null <<EOF
# BlackBox service override — customize without modifying the main unit.
# Uncomment + edit, then run:
#   sudo systemctl daemon-reload && sudo systemctl restart blackbox

[Service]
# Change port (default 9091):
# ExecStart=
# ExecStart=$BLACKBOX_ROOT/Orchestrator/venv/bin/python -m uvicorn Orchestrator.app:app --host 0.0.0.0 --port 8000

# Override memory pressure (default 70 %):
# MemoryHigh=50%

# Override CPU priority:
# Nice=-5
EOF

# ── Step 4b1: CLI Agent compatibility drop-in (audit E22) ──
# Brandon 2026-05-17: Android portal Tmux bridge showed blank screen on
# every connect. Root cause: three systemd-hardening settings from Step 4
# silently broke the CLI Agent feature on customer hardware:
#
#   1. ProtectHome=read-only blocked claude/gemini/codex from writing
#      their session state, history, and auth tokens to ~/.claude,
#      ~/.gemini, ~/.codex — CLI agents crashed on startup, leaving an
#      empty tmux pane that the bridge attached to with nothing to show.
#   2. PrivateTmp=true put tmux's socket in a per-service-instance
#      namespace; every service restart destroyed the namespace and the
#      sessions inside it.
#   3. KillMode defaulted to control-group; restart killed tmux server
#      itself. Code in Orchestrator/cli_agent/session_manager.py expected
#      a drop-in setting KillMode=process — but install.sh never made it.
#
# Dev box never hit this because it runs uvicorn directly in a shell
# (no systemd unit = no hardening sandbox).

# Pre-create every dir referenced in ReadWritePaths below. systemd refuses
# to set up the mount namespace if any ReadWritePaths entry doesn't
# resolve at start time, so a service that listed ~/.local/share/zellij
# would fail with status=226/NAMESPACE on any host where zellij had
# never run interactively (MSO2 customer flash, 2026-05-25 incident).
# Files don't need pre-creation (systemd handles missing files fine);
# only directories.
sudo -u "$REAL_USER" mkdir -p \
    "$REAL_HOME/.claude" \
    "$REAL_HOME/.gemini" \
    "$REAL_HOME/.codex" \
    "$REAL_HOME/.config" \
    "$REAL_HOME/.cache" \
    "$REAL_HOME/.npm" \
    "$REAL_HOME/.local/share/zellij" \
    "$REAL_HOME/.local/share/blackbox"
sudo tee /etc/systemd/system/blackbox.service.d/cli-agent-overrides.conf > /dev/null <<EOF
# CLI Agent compatibility drop-in (E22). DO NOT EDIT — install.sh manages
# this file. To customize service behavior, edit override.conf instead.
[Service]
# Punch holes through ProtectHome=read-only for each CLI agent's config
# dir + standard user dirs they write to during normal operation. Also
# punch /tmp through ProtectSystem=strict (E22a) so tmux's socket dir
# /tmp/tmux-\$UID/ is writable. /tmp is 1777 sticky-bit world-writable
# already so this doesn't weaken security.
#
# E22b (Phase 2 T8, 2026-05-25): added \$REAL_HOME/.local/share/zellij so
# the orchestrator can mint+revoke Zellij tokens (write to tokens.db) via
# the cli-agent zellij endpoints. Without this, every
# /cli-agent/zellij/launch returns 500 "attempt to write a readonly
# database" once CLI_AGENT_BACKEND=zellij.
ReadWritePaths=$REAL_HOME/.claude $REAL_HOME/.claude.json $REAL_HOME/.gemini $REAL_HOME/.codex $REAL_HOME/.config $REAL_HOME/.cache $REAL_HOME/.npm $REAL_HOME/.local/share/zellij $REAL_HOME/.local/share/blackbox /tmp
# Disable PrivateTmp so tmux's socket lives in real /tmp and survives
# service restarts (combined with KillMode=process below).
PrivateTmp=false
# Restart only kills the main uvicorn process; tmux server + CLI agents
# persist across restarts. See session_manager.py _new_session_cmd comment.
KillMode=process
# Color terminal support (T11c, 2026-05-25): systemd services start with
# no TERM set; subprocess.Popen children (Zellij + the CLI binary it runs)
# then inherit empty TERM and fall back to dumb-terminal output. claude /
# gemini / codex / antigravity all probe TERM + COLORTERM to decide color
# capability. xterm-256color is the safest 'works everywhere' baseline;
# COLORTERM=truecolor unlocks 24-bit where the tool supports it.
Environment=TERM=xterm-256color COLORTERM=truecolor
# Claude needs writable home (T16, 2026-05-25): claude-cli's interactive
# TUI writes to many files at \$HOME root (~/.claude.json, ~/.npm cache,
# ~/.local/state, plugin marketplaces). The original whitelist missed
# enough of them that claude silently hung in startup with no output to
# its PTY — pane buffer stayed at 0 bytes for minutes. Enumerating every
# path is brittle (changes per claude version), so we disable ProtectHome
# entirely. ProtectSystem=strict still protects /etc /usr /run.
ProtectHome=no
EOF

# ── Step 4b2: time-sync boot gate for the cron scheduler (cron M3.3) ──
# The cron scheduler computes every job's next fire — and decides on cold
# restart whether a job was "due during downtime" (the catch-up path) — against
# the box-local wall clock. On a no-RTC / RTC-skewed cold boot the clock can be
# wrong (the classic "1970" or month-stale clock) for a few seconds until NTP
# converges. If the orchestrator starts BEFORE that, fires are computed against
# a bogus clock and missed-run catch-up misfires.
#
# Gate startup on time-sync so a fresh box always schedules against a synced
# clock. After=time-sync.target orders us after systemd's time-sync milestone;
# Wants=systemd-timesyncd pulls in the SNTP client that reaches that milestone
# on a stock Ubuntu/Debian box (a no-op where the host runs chrony/ntpd instead
# — those reach time-sync.target themselves). Lives in its own drop-in so it is
# independent of the main unit + the cli-agent drop-in.
sudo tee /etc/systemd/system/blackbox.service.d/time-sync.conf > /dev/null <<EOF
# Time-sync boot gate for the cron scheduler (cron M3.3). DO NOT EDIT —
# install.sh manages this file. Ensures a no-RTC cold boot computes cron
# fires (and the cold-restart catch-up) against a synced wall clock.
[Unit]
After=time-sync.target
Wants=systemd-timesyncd
EOF

# ── Step 4c: log rotation (audit M3 carry-forward) ──
sudo tee /etc/logrotate.d/blackbox > /dev/null <<EOF
/var/log/blackbox/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0640 $REAL_USER $REAL_USER
    sharedscripts
}
EOF

# ── Step 4d: helper script (T3 — now sourced from tracked file) ──
# Previously emitted inline via heredoc (audit M3 flagged the resulting
# $BLACKBOX_ROOT/blackbox-status.sh as untracked + collision-prone).
# Now lives at Scripts/blackbox-status.sh in repo; install.sh just symlinks
# so customers can still run `./blackbox-status.sh` from BLACKBOX_ROOT.
if [[ -L "$BLACKBOX_ROOT/blackbox-status.sh" || -f "$BLACKBOX_ROOT/blackbox-status.sh" ]]; then
    rm -f "$BLACKBOX_ROOT/blackbox-status.sh"
fi
ln -sf "Scripts/blackbox-status.sh" "$BLACKBOX_ROOT/blackbox-status.sh"
echo "[install] Symlinked blackbox-status.sh → Scripts/blackbox-status.sh"

# ── Step 4e: sudoers grant for runtime BlackBox operations (T5) ──
# Bounded NOPASSWD entries covering: Tailscale wizard actuator, service
# restart + journal access, update-pipeline dispatch helpers. install -m
# 0440 atomic-replaces existing file; visudo-check aborts if syntax broken.
#
# Renamed from blackbox-tailscale → blackbox-system in T5 (scope grew
# beyond tailscale). Remove the old file if it exists (upgrade-in-place).
if [[ -f /etc/sudoers.d/blackbox-tailscale ]]; then
    sudo rm -f /etc/sudoers.d/blackbox-tailscale
    echo "[install] Removed legacy /etc/sudoers.d/blackbox-tailscale (renamed to -system)"
fi
sed -e "s|REAL_USER_PLACEHOLDER|$REAL_USER|g" \
    -e "s|BLACKBOX_ROOT_PLACEHOLDER|$BLACKBOX_ROOT|g" \
    "$BLACKBOX_ROOT/installer/templates/sudoers-blackbox-system" \
    | sudo install -m 0440 -o root -g root /dev/stdin /etc/sudoers.d/blackbox-system
if ! sudo visudo -c -f /etc/sudoers.d/blackbox-system > /dev/null; then
    echo "[install] ERROR: sudoers file syntax check failed" >&2
    sudo rm -f /etc/sudoers.d/blackbox-system
    exit 1
fi
echo "[install] Sudoers grant written for $REAL_USER (tailscale + service + update helpers)"

# ── Step 4f1: install root-owned dispatch helpers for update pipeline (T2 / T3) ──
# Two bounded helper scripts that the update flow's sudoers grants will point at.
# Replaces wildcard sudo grants for apt-get install + tee /etc/sudoers.d that
# would otherwise be priv-esc primitives via any MCP-tool prompt injection.
# T5 lands the actual sudoers grants that point at these.
for HELPER in blackbox-apt-install blackbox-write-systemd; do
    sudo install -m 0755 -o root -g root \
        "$BLACKBOX_ROOT/installer/templates/${HELPER}.sh" \
        "/usr/local/sbin/${HELPER}"
    echo "[install] Installed helper: /usr/local/sbin/${HELPER}"
done

# ── Step 4g: Asterisk blackbox.d include + ReadWritePaths + scoped reload sudoers (T5.1) ──
# The telephony production pass auto-configures OUR local Asterisk at runtime by
# writing trunk/dialplan files into a dedicated include dir and reloading. But
# ProtectSystem=strict makes /etc read-only in the service's mount namespace, so
# the runtime can't create that dir or write the configs unless we punch a narrow
# hole here, ONCE, at install time (as root). This function:
#   1. creates /etc/asterisk/blackbox.d, owned by the service user;
#   2. #includes blackbox.d/*.conf from pjsip.conf + extensions.conf (once);
#   3. punches a ReadWritePaths hole for JUST that dir via a systemd drop-in;
#   4. grants the service user a scoped NOPASSWD sudoers rule for the two
#      `asterisk -rx ... reload` commands only.
# The runtime NEVER writes sudoers / systemd config itself — only the trunk +
# dialplan .conf files inside blackbox.d, then `sudo asterisk -rx ... reload`.
#
# Idempotent: safe to re-run. Paths are overridable via env vars purely so the
# bash test (scripts/tests/test_install_asterisk_block.sh) can redirect them
# into a temp sandbox; production always uses the /etc defaults below.
setup_asterisk_blackbox_include() {
    local ASTERISK_ETC="${ASTERISK_ETC:-/etc/asterisk}"
    local BLACKBOX_D="${BLACKBOX_D:-$ASTERISK_ETC/blackbox.d}"
    local SYSTEMD_DROPIN_DIR="${SYSTEMD_DROPIN_DIR:-/etc/systemd/system/blackbox.service.d}"
    local SUDOERS_FILE="${SUDOERS_FILE:-/etc/sudoers.d/blackbox-asterisk}"
    local SERVICE_USER="${SERVICE_USER:-$REAL_USER}"
    local INCLUDE_LINE='#include "blackbox.d/*.conf"'

    # ── 1. include dir, owned by the service user ──
    mkdir -p "$BLACKBOX_D"
    if [[ -d "$BLACKBOX_D" ]]; then
        chown "$SERVICE_USER" "$BLACKBOX_D" 2>/dev/null \
            || echo "[install] WARN: could not chown $BLACKBOX_D to $SERVICE_USER (continuing)"
    fi
    echo "[install] Asterisk include dir ready: $BLACKBOX_D (owner $SERVICE_USER)"

    # ── 2. #include the dir from pjsip.conf + extensions.conf, once each ──
    local conf
    for conf in pjsip.conf extensions.conf; do
        local target="$ASTERISK_ETC/$conf"
        if [[ ! -f "$target" ]]; then
            echo "[install] WARN: $target not present — skipping include (Asterisk not configured?)"
            continue
        fi
        if grep -qF "$INCLUDE_LINE" "$target"; then
            echo "[install] $conf already includes blackbox.d/*.conf (skipping)"
        else
            printf '\n; Added by BlackBox installer — auto-managed telephony config\n%s\n' \
                "$INCLUDE_LINE" >> "$target"
            echo "[install] Appended blackbox.d include to $conf"
        fi
    done

    # ── 3. systemd drop-in: ReadWritePaths hole for JUST the include dir ──
    # The drop-in content uses the LITERAL /etc path the live service needs; only
    # WHERE the drop-in is written is overridable (for the test sandbox).
    mkdir -p "$SYSTEMD_DROPIN_DIR"
    cat > "$SYSTEMD_DROPIN_DIR/asterisk.conf" <<'DROPIN'
# Asterisk telephony drop-in (T5.1). DO NOT EDIT — install.sh manages this file.
# Punches a narrow hole through ProtectSystem=strict so the runtime can write
# auto-generated trunk + dialplan configs into Asterisk's blackbox.d include dir.
[Service]
ReadWritePaths=/etc/asterisk/blackbox.d
DROPIN
    echo "[install] Wrote systemd drop-in: $SYSTEMD_DROPIN_DIR/asterisk.conf"

    # ── 4. scoped NOPASSWD sudoers rule for the two reload commands only ──
    cat > "$SUDOERS_FILE" <<SUDOERS
# Asterisk reload grant (T5.1). DO NOT EDIT — install.sh manages this file.
# Bounded NOPASSWD for ONLY the two reload subcommands the runtime needs after
# rewriting blackbox.d configs. No wildcard — literal-arg matching is the
# security boundary.
$SERVICE_USER ALL=(root) NOPASSWD: /usr/sbin/asterisk -rx pjsip reload, /usr/sbin/asterisk -rx dialplan reload
SUDOERS
    chmod 0440 "$SUDOERS_FILE"
    # Validate syntax. Skipped in the test sandbox (SKIP_VISUDO=1) so we never
    # run visudo against temp files. Never leave an invalid sudoers file behind.
    if [[ "${SKIP_VISUDO:-0}" != "1" ]]; then
        if ! visudo -cf "$SUDOERS_FILE" >/dev/null; then
            echo "[install] ERROR: $SUDOERS_FILE failed visudo syntax check — removing" >&2
            rm -f "$SUDOERS_FILE"
            return 1
        fi
    fi
    echo "[install] Wrote scoped Asterisk reload sudoers: $SUDOERS_FILE"

    # ── 5. reload systemd so the drop-in takes effect — ONLY for the real /etc ──
    # Guarded so the test (which redirects SYSTEMD_DROPIN_DIR into a temp dir)
    # never invokes systemctl.
    if [[ "$SYSTEMD_DROPIN_DIR" = "/etc/systemd/system/blackbox.service.d" ]]; then
        systemctl daemon-reload
        echo "[install] systemctl daemon-reload (Asterisk drop-in active)"
    fi
}

# Only enable the include plumbing if Asterisk is actually installed (it's a
# FEATURE_OPTIONAL package — see Scripts/onboarding/system-packages.txt).
if [[ -x /usr/sbin/asterisk ]]; then
    echo "[install] Asterisk detected — enabling blackbox.d include plumbing"
    sudo SKIP_VISUDO="${SKIP_VISUDO:-0}" bash -c "REAL_USER='$REAL_USER'; $(declare -f setup_asterisk_blackbox_include); setup_asterisk_blackbox_include"
else
    echo "[install] Asterisk not installed (/usr/sbin/asterisk absent) — skipping telephony include setup"
fi

# ── Step 4h: force X11 session via GDM (audit E18b — Computer Use input on Wayland) ──
# Wayland's Mutter compositor silently drops uinput events for cursor/click from
# untrusted processes (including ydotool). xdotool similarly only sees XWayland
# windows, not native Wayland surfaces. Computer Use therefore cannot inject
# input into native apps on a Wayland session — clicks "succeed" (exit 0) but
# never reach the GUI. Forcing X11 session restores full xdotool functionality
# (X server processes uinput events normally). This matches the dev-box config
# pattern: WaylandEnable=false in /etc/gdm3/custom.conf. Customers logging in
# next will get an X11 session automatically.
if [[ -f /etc/gdm3/custom.conf ]]; then
    if sudo grep -q "^WaylandEnable=false" /etc/gdm3/custom.conf; then
        echo "[install] GDM already configured for X11 session"
    elif sudo grep -q "^#WaylandEnable=false" /etc/gdm3/custom.conf; then
        sudo sed -i 's/^#WaylandEnable=false/WaylandEnable=false/' /etc/gdm3/custom.conf
        echo "[install] Switched GDM to X11 session (uncommented WaylandEnable=false)"
    else
        sudo sed -i '/^\[daemon\]/a WaylandEnable=false' /etc/gdm3/custom.conf
        echo "[install] Switched GDM to X11 session (inserted WaylandEnable=false under [daemon])"
    fi
    echo "[install] X11 session takes effect on next login. Reboot or log out + back in to activate."
else
    echo "[install] /etc/gdm3/custom.conf not present (non-GDM display manager?) — skipping X11 switch"
fi

# ── Step 6c: CU display resolution autostart (audit E19) ──
# Codify 1280x720 (16:9) as the v1 BlackBox default display resolution.
# Anthropic Computer Use models are trained on 16:9 / 4:3 in the 1024x768
# to 1280x800 range — 1280x720 is the precision sweet spot. Customer-class
# machines are AI-first (not human aesthetics), so we set this automatically.
# Higher resolutions or ultrawide aspect ratios degrade model click accuracy
# (model picks coordinates that drift 5-15% due to aspect-ratio bias in its
# training set). Brandon's MSO2 Ultra testing confirmed: 3440x1440 = 4-5
# inches click drift; 1280x720 = pinpoint accuracy.
#
# Autostart .desktop file runs xrandr at every login (X11 session required —
# see Step 4h). Iterates connected outputs (HDMI-1, DP-1, etc.) and applies
# the mode to the first one that accepts it. Sleeps 5s to let the session
# fully initialize before changing resolution.
sudo -u "$REAL_USER" mkdir -p "$REAL_HOME/.config/autostart"
sudo -u "$REAL_USER" cp "$BLACKBOX_ROOT/installer/templates/set-cu-resolution.desktop" \
    "$REAL_HOME/.config/autostart/blackbox-cu-resolution.desktop"
echo "[install] Installed autostart entry: set display to 1280x720 on next login (Anthropic CU sweet spot)"

# ── Step 4f: ydotool 1.x for Wayland input injection (E18) ──
# Ubuntu 24.04's apt ydotool is v0.1.8 which lacks --absolute mousemove
# (Computer Use sends absolute coords, so we can't use 0.1.8). Build v1.0.4
# from source; it's a tiny C project (<5s compile). The daemon writes to
# /dev/uinput at the kernel layer — both X11 AND Wayland apps receive events
# (xdotool only reaches XWayland windows; native Wayland apps were silent
# before E18).
build_ydotool() {
    if [[ -x /usr/local/bin/ydotool && -x /usr/local/bin/ydotoold ]]; then
        echo "[install] ydotool 1.x already installed at /usr/local/bin/"
        return 0
    fi
    echo "[install] Building ydotool 1.0.4 from source..."
    local BUILD_DIR=/tmp/ydotool-build-$$
    rm -rf "$BUILD_DIR"
    git clone --depth 1 --branch v1.0.4 https://github.com/ReimuNotMoe/ydotool.git "$BUILD_DIR"
    (
        cd "$BUILD_DIR"
        mkdir -p build && cd build
        cmake .. -DCMAKE_BUILD_TYPE=Release
        make -j"$(nproc)"
        sudo make install
    )
    rm -rf "$BUILD_DIR"
    if [[ ! -x /usr/local/bin/ydotool || ! -x /usr/local/bin/ydotoold ]]; then
        echo "[install] ERROR: ydotool build/install did not produce expected binaries" >&2
        exit 1
    fi
    echo "[install] ydotool 1.0.4 installed to /usr/local/bin/"
}
build_ydotool

# REAL_USER needs /dev/uinput access (input group). For the running session,
# the systemd service runs ydotoold as root and hands ownership of the socket
# to REAL_USER's uid:gid via --socket-own, so this group membership is mostly
# defensive (helps if someone tries to run ydotool directly outside the service).
USER_UID=$(id -u "$REAL_USER")
USER_GID=$(id -g "$REAL_USER")
sudo usermod -aG input "$REAL_USER"
echo "[install] Added $REAL_USER to 'input' group (effective next login)"

# Install ydotoold systemd unit. Daemon owns /dev/uinput access (root needed),
# but the socket gets chowned to REAL_USER so blackbox.service can talk to it
# without privilege escalation.
sudo tee /etc/systemd/system/ydotoold.service > /dev/null <<EOF
[Unit]
Description=ydotool daemon (Wayland-compatible input injection for Computer Use)
Documentation=https://github.com/ReimuNotMoe/ydotool
After=multi-user.target

[Service]
Type=simple
# Socket path uses /run/user/<uid>/ — survives blackbox.service's
# PrivateTmp=true sandbox (PrivateTmp masks /tmp but leaves /run/user/* alone).
# REAL_USER's uid:gid owns the socket so the BlackBox process can write to it
# without root.
ExecStart=/usr/local/bin/ydotoold --socket-path=/run/user/${USER_UID}/.ydotool_socket --socket-own=${USER_UID}:${USER_GID}
# Make sure /run/user/<uid> exists before we try to bind there. systemd creates
# it on user login, but if ydotoold starts at boot before login we need it now.
ExecStartPre=/usr/bin/install -d -o ${USER_UID} -g ${USER_GID} -m 700 /run/user/${USER_UID}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ydotoold.service
echo "[install] ydotoold.service enabled and running"

# ── Step 4g: GNOME 46 screenshot flash suppression (E18) ──
# GNOME 46 fires a full-screen flash on every XDG Portal Screenshot — annoying
# during Computer Use (Portal screenshots run at 1Hz from the live viewer).
# org.gnome.Shell.Screenshot D-Bus is blocked by GNOME 46 ("Screenshot is not
# allowed") so we can't use the older flash=false API. The only working knob
# is the global animation toggle. Trade-off: customer loses all GNOME UI
# transitions (window minimize/maximize/workspace switch animations), but this
# is desirable on a CU kiosk anyway — animations confuse the model and waste
# CPU. Set via dbus-launch wrapper so dconf finds the right session.
sudo -u "$REAL_USER" bash -c '
    if [[ -e "/run/user/$(id -u)/bus" ]]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
        gsettings set org.gnome.desktop.interface enable-animations false 2>/dev/null \
            && echo "[install] Disabled GNOME animations (suppresses screenshot flash)" \
            || echo "[install] (skipping animation disable — gsettings unavailable)"
    else
        echo "[install] (skipping animation disable — no user dbus, will apply on next login)"
    fi
' || true

# ── Step 4i: default browser handler — chromium/firefox xdg-settings ──
# Fresh Ubuntu installs leave all MIME handlers EMPTY out-of-the-box:
#   xdg-settings get default-web-browser    → (blank)
#   xdg-mime query default text/html        → (blank)
#   xdg-mime query default x-scheme-handler/https → (blank)
# When a CLI agent (Antigravity `agy` in particular — issued via PTY-bridge
# from the CLI Agents modal) calls `xdg-open https://...` for OAuth, xdg-open
# finds no registered handler and falls back to whatever default exists —
# which on Ubuntu Desktop often routes to gedit/text-editor/Notepad-equivalent.
# Result: agy auto-pops a text editor with HTML content instead of a browser.
# Brandon hit this on MSO2 2026-05-22.
# Fix: register the first available browser as default for all relevant MIME
# types. Prefer chromium-browser.desktop (apt-installed, deterministic launch);
# fall back to firefox_firefox.desktop (snap-managed) if chromium absent.
# Idempotent: re-running with the default already set is a no-op.
sudo -u "$REAL_USER" bash -c '
    DESKTOP_DIRS="/usr/share/applications /var/lib/snapd/desktop/applications"
    chosen=""
    # Priority order: chromium-browser (apt) → firefox_firefox (snap) → google-chrome
    for desktop in chromium-browser.desktop firefox_firefox.desktop firefox.desktop google-chrome.desktop; do
        for dir in $DESKTOP_DIRS; do
            if [[ -f "$dir/$desktop" ]]; then
                chosen="$desktop"
                break 2
            fi
        done
    done
    if [[ -n "$chosen" ]]; then
        # xdg-settings + xdg-mime need DBUS_SESSION_BUS_ADDRESS to talk to
        # the user keyring/desktop layer. /run/user/<uid>/bus is the systemd
        # logind path; populated for any user with an active graphical or
        # PAM-spawned session.
        if [[ -e "/run/user/$(id -u)/bus" ]]; then
            export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
        fi
        xdg-settings set default-web-browser "$chosen" 2>/dev/null \
            && echo "[install] Default browser set: $chosen" \
            || echo "[install] (xdg-settings failed for $chosen — re-run after login)"
        xdg-mime default "$chosen" text/html x-scheme-handler/http x-scheme-handler/https x-scheme-handler/about x-scheme-handler/unknown 2>/dev/null \
            && echo "[install] MIME handlers registered for $chosen (text/html + http/https schemes)" \
            || echo "[install] (xdg-mime failed for $chosen — re-run after login)"
    else
        echo "[install] WARNING: no browser .desktop file found (looked in $DESKTOP_DIRS for chromium/firefox/chrome). CLI agents that auto-open browsers (Antigravity, etc.) will fall through to xdg-open default — likely a text editor. Install chromium-browser or firefox and re-run."
    fi
' || true

# ── Step 5: Build + install Tauri setup app (audit C2 / Q1=A) ──
build_tauri_setup() {
    echo "[install] Building BlackBox Setup (Tauri .deb)..."
    sudo apt install -y \
        libwebkit2gtk-4.1-dev libsoup-3.0-dev librsvg2-dev libxdo-dev \
        libssl-dev libayatana-appindicator3-dev pkg-config build-essential
    if ! command -v cargo > /dev/null; then
        echo "[install] Installing Rust toolchain via rustup..."
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
            | sh -s -- -y --default-toolchain stable
        # shellcheck disable=SC1091
        source "$HOME/.cargo/env"
    fi
    if ! cargo tauri --version 2>/dev/null | grep -q "^tauri-cli"; then
        cargo install tauri-cli --locked --version "^2.0"
    fi
    # Pre-clean bundle dir so we always install the freshly-built .deb (audit I4)
    rm -f "$BLACKBOX_ROOT/installer/src-tauri/target/release/bundle/deb/"*.deb 2>/dev/null || true
    cd "$BLACKBOX_ROOT/installer"
    npm install --no-audit --no-fund > /dev/null 2>&1 || true
    cargo tauri build --bundles deb
    DEB_FILE=$(ls "$BLACKBOX_ROOT/installer/src-tauri/target/release/bundle/deb/"*.deb | head -1)
    if [[ ! -f "$DEB_FILE" ]]; then
        echo "[install] ERROR: cargo tauri build did not produce a .deb" >&2
        exit 1
    fi
    echo "[install] Built: $DEB_FILE"
    cd "$BLACKBOX_ROOT"
}
build_tauri_setup
DEB_FILE=$(ls "$BLACKBOX_ROOT/installer/src-tauri/target/release/bundle/deb/"*.deb | head -1)
echo "[install] Installing $DEB_FILE..."
sudo apt install -y "$DEB_FILE"   # apt 1.1+ resolves deps + installs in one step (audit N7)

# ── Step 6a: autostart .desktop — first-boot wizard launch (audit M6) ──
sudo -u "$REAL_USER" mkdir -p "$REAL_HOME/.config/autostart"
sudo -u "$REAL_USER" cp "$BLACKBOX_ROOT/installer/dist/blackbox-setup-autostart.desktop" \
    "$REAL_HOME/.config/autostart/blackbox-setup.desktop"

# ── Step 6b: persistent .desktop — manage-mode launcher (audit M6) ──
sudo -u "$REAL_USER" mkdir -p "$REAL_HOME/.local/share/applications"
sudo -u "$REAL_USER" cp "$BLACKBOX_ROOT/installer/dist/blackbox-setup.desktop" \
    "$REAL_HOME/.local/share/applications/blackbox-setup.desktop"
sudo -u "$REAL_USER" update-desktop-database "$REAL_HOME/.local/share/applications" 2>/dev/null || true

# ── Step 7: enable + restart (audit M5 — restart works whether running or stopped) ──
sudo systemctl daemon-reload
sudo systemctl enable blackbox.service
sudo systemctl restart blackbox.service

# ── Step 8: Final user message (audit C3 — /usr/bin not /usr/local/bin) ──
echo
echo "[install] Done. Reboot to launch BlackBox Setup, or run /usr/bin/blackbox-setup --first-run now."
echo "[install] Find 'BlackBox Setup' in your applications menu later for maintenance/manage mode."
