#!/bin/bash
# setup.sh — kept as an alias of install.sh (same arguments).
#
# It used to copy a hand-written ari.conf and test ARI with a password written
# into this file. install.sh now renders ari.conf from ASTERISK_ARI_PASSWORD in
# .env (generating one when empty), so no secret lives in the repo.
#
# Run with: sudo bash Orchestrator/asterisk/configs/setup.sh [--rotate-ari-password]
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh" "$@"
