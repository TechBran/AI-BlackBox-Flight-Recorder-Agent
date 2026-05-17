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

# ── Step 3: .env from template (audit I2 — created as $REAL_USER, mode 0600 since it holds API keys) ──
if [[ ! -f "$BLACKBOX_ROOT/.env" ]]; then
    sudo -u "$REAL_USER" cp "$BLACKBOX_ROOT/.env.template" "$BLACKBOX_ROOT/.env"
    sudo -u "$REAL_USER" bash -c "echo 'BLACKBOX_ROOT=$BLACKBOX_ROOT' >> '$BLACKBOX_ROOT/.env'"
    chmod 0600 "$BLACKBOX_ROOT/.env"
    echo "[install] Created .env from template (mode 0600)"
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
After=network-online.target
Wants=network-online.target
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
ExecStart=$BLACKBOX_ROOT/Orchestrator/venv/bin/python -m uvicorn Orchestrator.app:app \\
    --host 0.0.0.0 --port 9091 \\
    --timeout-keep-alive 120 --limit-max-requests 10000 --loop uvloop
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

# ── Step 4d: helper script (audit M3 carry-forward) ──
cat > "$BLACKBOX_ROOT/blackbox-status.sh" <<'STATUSEOF'
#!/usr/bin/env bash
echo "=== BlackBox Service ==="
systemctl status blackbox.service --no-pager | head -15
echo
echo "=== Recent Logs ==="
journalctl -u blackbox.service -n 20 --no-pager
echo
echo "=== Health ==="
curl -fsS http://localhost:9091/health 2>&1 | head -5
STATUSEOF
chmod +x "$BLACKBOX_ROOT/blackbox-status.sh"

# ── Step 4e: sudoers grant for runtime tailscale operations ──
# (Tailscale wizard actuator: bounded NOPASSWD for the specific commands
# the onboarding step needs. install -m 0440 atomic-replaces existing
# file; visudo-check aborts if syntax broken.)
sed "s|REAL_USER_PLACEHOLDER|$REAL_USER|g" \
    "$BLACKBOX_ROOT/installer/templates/sudoers-blackbox-tailscale" \
    | sudo install -m 0440 -o root -g root /dev/stdin /etc/sudoers.d/blackbox-tailscale
if ! sudo visudo -c -f /etc/sudoers.d/blackbox-tailscale > /dev/null; then
    echo "[install] ERROR: sudoers file syntax check failed" >&2
    sudo rm -f /etc/sudoers.d/blackbox-tailscale
    exit 1
fi
echo "[install] Sudoers grant written for $REAL_USER (tailscale operations)"

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
