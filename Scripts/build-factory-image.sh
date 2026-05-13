#!/usr/bin/env bash
# Build a factory-ready Ubuntu 24.04 image with BlackBox pre-installed
# TODO: fill in once hardware spec is locked.
#
# Steps when ready:
#   1. Start from Ubuntu 24.04 base ISO
#   2. Mount + chroot
#   3. Pre-install: git clone blackbox-poc, run ./Scripts/install.sh
#   4. Pre-bake: Tauri setup app, autostart .desktop
#   5. Resize partition for target SSD size
#   6. Output: .img file ready to flash
echo "TODO: factory image build deferred until hardware spec locked."
exit 1
