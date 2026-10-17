#!/bin/bash
# =============================================================================
# install.sh — Install Asterisk configs for AI BlackBox TG200 integration
#
# Run with: sudo bash Orchestrator/asterisk/configs/install.sh [--rotate-ari-password]
#
# No password lives in this repo. The ARI password's single source of truth is
# ASTERISK_ARI_PASSWORD in $BLACKBOX_ROOT/.env (what the Orchestrator logs in
# with). It is generated there when empty, or replaced with
# --rotate-ari-password, and /etc/asterisk/ari.conf is rendered from it so the
# two cannot drift. pjsip.conf, extensions.conf and http.conf are per-box
# (gitignored) and are copied from this directory when present, except on
# --rotate-ari-password, which touches ari.conf only. A full install also
# installs the IVR prompts (install_prompts.py); blackbox.service cannot.
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASTERISK_DIR="${ASTERISK_ETC:-/etc/asterisk}"   # overridable for scripts/tests/
BLACKBOX_ROOT="${BLACKBOX_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
ENV_FILE="$BLACKBOX_ROOT/.env"
ENV_KEY="$BLACKBOX_ROOT/Scripts/env_key.py"

ROTATE=""
case "${1:-}" in
    --rotate-ari-password) ROTATE="--force" ;;
    "") ;;
    *) echo "Usage: sudo bash $0 [--rotate-ari-password]" >&2; exit 2 ;;
esac

echo "=== AI BlackBox Asterisk Configuration Installer ==="

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found — run Scripts/install.sh first." >&2
    exit 1
fi

# ARI rotation touches ari.conf only; a full install also copies the per-box confs.
if [ -n "$ROTATE" ]; then
    CONFS="ari.conf"
    PER_BOX=""
else
    CONFS="pjsip.conf extensions.conf ari.conf http.conf"
    PER_BOX="pjsip.conf extensions.conf http.conf"
fi

# Backup existing configs
echo "[1/5] Backing up existing configs..."
for f in $CONFS; do
    if [ -f "$ASTERISK_DIR/$f" ]; then
        cp "$ASTERISK_DIR/$f" "$ASTERISK_DIR/$f.bak.$(date +%Y%m%d%H%M%S)"
        echo "  Backed up: $f"
    fi
done

# Copy per-box configs; render ari.conf from .env
echo "[2/5] Installing new configs..."
for f in $PER_BOX; do
    if [ -f "$SCRIPT_DIR/$f" ]; then
        cp "$SCRIPT_DIR/$f" "$ASTERISK_DIR/$f"
    else
        echo "  WARNING: $SCRIPT_DIR/$f not found — keeping the installed $f"
    fi
done

python3 -I "$ENV_KEY" generate $ROTATE "$ENV_FILE" ASTERISK_ARI_PASSWORD
ARI_USER="$(python3 -I "$ENV_KEY" get "$ENV_FILE" ASTERISK_ARI_USER)"
ARI_USER="${ARI_USER:-blackbox}"
ARI_PASSWORD="$(python3 -I "$ENV_KEY" get "$ENV_FILE" ASTERISK_ARI_PASSWORD)"
(
    umask 027
    cat > "$ASTERISK_DIR/ari.conf" <<ARI
; =============================================================================
; ari.conf — Asterisk REST Interface for AI BlackBox
; Rendered by Orchestrator/asterisk/configs/install.sh from ASTERISK_ARI_* in
; .env. Do not edit the password here: change .env and re-run install.sh.
; =============================================================================

[general]
enabled=yes
pretty=yes
allowed_origins=*

[$ARI_USER]
type=user
read_only=no
password=$ARI_PASSWORD
ARI
)
echo "  Rendered ari.conf for ARI user '$ARI_USER' (password from .env)"

# Fix ownership
echo "[3/5] Setting permissions..."
for f in $CONFS; do
    if [ -f "$ASTERISK_DIR/$f" ]; then
        chown asterisk:asterisk "$ASTERISK_DIR/$f"
        chmod 640 "$ASTERISK_DIR/$f"
    fi
done

# IVR prompts: the service cannot write Asterisk's sound dir (ProtectSystem=strict)
if [ -z "$ROTATE" ]; then
    python3 -I "$SCRIPT_DIR/install_prompts.py" \
        || echo "  WARNING: IVR prompts not installed; callers will hear silence. Re-run: sudo python3 -I $SCRIPT_DIR/install_prompts.py"
fi

# Enable and start Asterisk
echo "[4/5] Enabling Asterisk service..."
systemctl enable asterisk
systemctl restart asterisk
sleep 2

# Verify
echo "[5/5] Verifying..."
if systemctl is-active --quiet asterisk; then
    echo "  Asterisk is running"
    asterisk -rx "core show version"
    echo ""
    echo "  Checking key modules:"
    asterisk -rx "module show like audiosocket" 2>/dev/null | grep -v "^$" || echo "  WARNING: app_audiosocket not loaded"
    asterisk -rx "module show like res_ari" 2>/dev/null | grep -c "res_ari" | xargs -I{} echo "  ARI modules loaded: {}"
    asterisk -rx "module show like codec_g722" 2>/dev/null | grep -v "^$" || echo "  WARNING: codec_g722 not loaded"
    echo ""
    echo "  Testing ARI..."
    # Credentials go to curl on stdin (-K -), never on its command line.
    HTTP_CODE=$(printf 'user = "%s:%s"\n' "$ARI_USER" "$ARI_PASSWORD" \
        | curl -s -o /dev/null -w "%{http_code}" -K - http://127.0.0.1:8088/ari/asterisk/info)
    if [ "$HTTP_CODE" = "200" ]; then
        echo "  ARI is responding (HTTP 200)"
    else
        echo "  WARNING: ARI returned HTTP $HTTP_CODE"
    fi
else
    echo "  ERROR: Asterisk failed to start!"
    journalctl -u asterisk --no-pager -n 20
fi

echo ""
echo "=== Installation complete ==="
echo "Next: restart the BlackBox (sudo systemctl restart blackbox.service) if the"
echo "      ARI password was generated or rotated, so the Orchestrator logs in with it."
echo "Test: curl -u \"$ARI_USER:<ASTERISK_ARI_PASSWORD from .env>\" http://127.0.0.1:8088/ari/asterisk/info"
