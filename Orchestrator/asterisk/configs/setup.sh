#!/bin/bash
S="${BLACKBOX_ROOT:?}/Orchestrator/asterisk/configs"
D="/etc/asterisk"
cp "$S/pjsip.conf" "$D/pjsip.conf"
cp "$S/extensions.conf" "$D/extensions.conf"
cp "$S/ari.conf" "$D/ari.conf"
cp "$S/http.conf" "$D/http.conf"
chown asterisk:asterisk "$D/pjsip.conf" "$D/extensions.conf" "$D/ari.conf" "$D/http.conf"
chmod 640 "$D/pjsip.conf" "$D/extensions.conf" "$D/ari.conf" "$D/http.conf"
systemctl restart asterisk
sleep 2
echo "Done. Testing ARI..."
curl -s -u blackbox:blackbox-ari-secret-2026 http://127.0.0.1:8088/ari/asterisk/info
