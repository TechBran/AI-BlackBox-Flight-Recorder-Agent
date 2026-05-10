#!/bin/bash
# Setup polkit rule to allow BlackBox service to control NetworkManager
# Run this once with: sudo bash Scripts/setup-cellular-polkit.sh

set -e

RULES_DIR="/etc/polkit-1/rules.d"
RULES_FILE="$RULES_DIR/10-blackbox-networkmanager.rules"

# Get the service user
SERVICE_USER=$(systemctl show blackbox.service -p User --value 2>/dev/null || id -un)

echo "Creating polkit rule for user: $SERVICE_USER"
echo "File: $RULES_FILE"

mkdir -p "$RULES_DIR"

cat > "$RULES_FILE" << POLKIT_EOF
// Allow BlackBox service user to control NetworkManager and ModemManager
// (needed for cellular internet failover from systemd service)
polkit.addRule(function(action, subject) {
    if (subject.user !== "$SERVICE_USER") return undefined;

    // NetworkManager: connection activate/deactivate/modify
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0) {
        return polkit.Result.YES;
    }
    // systemd: restart ModemManager
    if (action.id === "org.freedesktop.systemd1.manage-units") {
        return polkit.Result.YES;
    }
    return undefined;
});
POLKIT_EOF

chmod 644 "$RULES_FILE"
echo "Polkit rule created successfully."
echo "Restarting polkit..."
systemctl restart polkit
echo "Done. The BlackBox service can now control NetworkManager and ModemManager."
