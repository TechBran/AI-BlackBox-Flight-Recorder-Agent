#!/usr/bin/env python3
"""
provisioner.py - Runtime Asterisk config provisioning for TG gateways.

Task 5.1 (install-time) created a writable ``/etc/asterisk/blackbox.d/`` that is
``#include``d by ``pjsip.conf`` + ``extensions.conf``, plus a scoped sudoers rule
allowing the service user to run ``asterisk -rx "... reload"``.

This module is the RUNTIME piece: it writes a gateway's PJSIP trunk into that
directory and reloads Asterisk so that "add a gateway in the Portal -> it just
works" becomes real.

Layout written into ``BLACKBOX_D``:
  - ``_shared.conf``        : the gateway-INDEPENDENT dialplan contexts
                              (``from-tg200`` / ``to-tg200`` / ``blackbox-audiosocket``).
                              Written ONCE (idempotent overwrite). If each
                              per-gateway file repeated these contexts, a
                              ``dialplan reload`` would error on duplicate
                              context definitions across files — so the shared
                              dialplan lives in exactly one file.
  - ``<trunk_name>.conf``   : the PER-gateway PJSIP endpoint/aor/identify
                              (unique per trunk). Safe to have many of these.

All render functions are pure string builders (no I/O) so they are trivially
unit-testable. File paths are overridable via ``ASTERISK_INCLUDE_DIR`` and the
binary via ``ASTERISK_BIN`` so tests run hermetically against a tmp dir with a
monkeypatched ``subprocess.run``.
"""

import logging
import os
import subprocess

from Orchestrator.asterisk import gateway_manager
from Orchestrator.asterisk.config import (
    ASTERISK_AUDIOSOCKET_PORT,
    ASTERISK_INBOUND_CONTEXT,
    ASTERISK_OUTBOUND_CONTEXT,
)

logger = logging.getLogger(__name__)

# Writable include dir created at install time (Task 5.1). Overridable for tests.
BLACKBOX_D = os.getenv("ASTERISK_INCLUDE_DIR", "/etc/asterisk/blackbox.d")

# Asterisk CLI binary. Overridable for tests / non-standard installs.
ASTERISK_BIN = os.getenv("ASTERISK_BIN", "/usr/sbin/asterisk")

SHARED_FILENAME = "_shared.conf"


# ---------------------------------------------------------------------------
# Pure renderers (no I/O)
# ---------------------------------------------------------------------------
def render_pjsip(gw: dict) -> str:
    """Return the per-gateway PJSIP block (endpoint/aor/identify).

    This is trunk-specific and unique per gateway. Delegates to the existing,
    already-tested ``gateway_manager.generate_pjsip_trunk_config`` so there is a
    single source of truth for the PJSIP shape.
    """
    return gateway_manager.generate_pjsip_trunk_config(gw)


def render_shared_dialplan() -> str:
    """Return the gateway-INDEPENDENT dialplan contexts.

    These contexts are identical regardless of which gateway is configured, so
    they are written once to ``_shared.conf`` to avoid duplicate-context errors
    on ``dialplan reload`` when multiple gateways are present.

    Contexts:
      - ``[from-tg200]``          inbound: hand any incoming call to the Stasis
                                  app ``blackbox``.
      - ``[blackbox-audiosocket]`` bridge a channel to the Orchestrator's
                                  AudioSocket TCP server.
      - ``[to-tg200]``            minimal outbound convenience (PJSIP dialing is
                                  otherwise implicit via ``PJSIP/<num>@<trunk>``).
    """
    inbound = ASTERISK_INBOUND_CONTEXT       # "from-tg200"
    outbound = ASTERISK_OUTBOUND_CONTEXT     # "to-tg200"
    audiosocket_port = ASTERISK_AUDIOSOCKET_PORT

    return f"""; === BlackBox shared dialplan (gateway-independent) ===
; Written once by Orchestrator/asterisk/provisioner.py. Do not duplicate these
; contexts in per-gateway files or `dialplan reload` will error.

[{inbound}]
; Inbound from any TG trunk. `_.` catches any DID (the gateway may present `s`
; or the full DID); hand the call to the Stasis app for BlackBox to drive.
exten => _.,1,NoOp(Inbound via ${{CHANNEL(endpoint)}})
 same => n,Stasis(blackbox,inbound)
 same => n,Hangup()

[{outbound}]
; Outbound convenience. PJSIP dialing is normally PJSIP/<num>@<trunk>; this lets
; channels originate into a trunk context generically.
exten => _.,1,Dial(PJSIP/${{EXTEN}})
 same => n,Hangup()

[blackbox-audiosocket]
; Bridge the channel's audio to the Orchestrator AudioSocket server over TCP.
exten => s,1,Answer()
 same => n,AudioSocket(${{CALL_UUID}},127.0.0.1:{audiosocket_port})
 same => n,Hangup()
"""


# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------
def write_gateway_config(gw: dict) -> str:
    """Write the shared dialplan + this gateway's PJSIP block into BLACKBOX_D.

    - Ensures ``BLACKBOX_D`` exists.
    - Writes (overwrites idempotently) ``_shared.conf`` with the shared dialplan.
    - Writes ``<trunk_name>.conf`` with this gateway's PJSIP block.

    Returns the per-gateway file path. Never raises on a benign re-write.
    """
    os.makedirs(BLACKBOX_D, exist_ok=True)

    shared_path = os.path.join(BLACKBOX_D, SHARED_FILENAME)
    try:
        with open(shared_path, "w") as f:
            f.write(render_shared_dialplan())
    except OSError as e:
        logger.warning("[Provisioner] Could not write shared dialplan %s: %s", shared_path, e)

    trunk = gw["trunk_name"]
    per_gw_path = os.path.join(BLACKBOX_D, f"{trunk}.conf")
    try:
        with open(per_gw_path, "w") as f:
            f.write(render_pjsip(gw) + "\n")
    except OSError as e:
        logger.warning("[Provisioner] Could not write gateway config %s: %s", per_gw_path, e)

    return per_gw_path


def remove_gateway_config(trunk_name: str) -> bool:
    """Delete ``<trunk_name>.conf`` if present. Return whether it existed."""
    path = os.path.join(BLACKBOX_D, f"{trunk_name}.conf")
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            logger.warning("[Provisioner] Could not remove %s: %s", path, e)
            return False
        return True
    return False


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------
def _asterisk_cmd(cli_arg: str) -> list:
    """Build the argv for ``asterisk -rx "<cli_arg>"`` (with sudo unless told not to)."""
    cmd = [ASTERISK_BIN, "-rx", cli_arg]
    if not os.getenv("ASTERISK_NO_SUDO"):
        cmd = ["sudo"] + cmd
    return cmd


def reload_asterisk() -> dict:
    """Reload PJSIP + dialplan via the Asterisk CLI.

    Runs ``asterisk -rx "pjsip reload"`` then ``asterisk -rx "dialplan reload"``.
    Returns ``{"pjsip": rc, "dialplan": rc, "ok": bool}``. On a missing binary or
    timeout, returns ``{"ok": False, "error": "..."}`` rather than raising.
    """
    try:
        pjsip = subprocess.run(
            _asterisk_cmd("pjsip reload"),
            capture_output=True, text=True, timeout=15,
        )
        dialplan = subprocess.run(
            _asterisk_cmd("dialplan reload"),
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.warning("[Provisioner] Asterisk reload failed: %s", e)
        return {"ok": False, "error": str(e)}

    ok = pjsip.returncode == 0 and dialplan.returncode == 0
    if not ok:
        logger.warning(
            "[Provisioner] reload non-zero: pjsip rc=%s stderr=%s; dialplan rc=%s stderr=%s",
            pjsip.returncode, pjsip.stderr, dialplan.returncode, dialplan.stderr,
        )
    return {
        "pjsip": pjsip.returncode,
        "dialplan": dialplan.returncode,
        "ok": ok,
    }


# ---------------------------------------------------------------------------
# High-level apply
# ---------------------------------------------------------------------------
def apply_gateway(gw: dict) -> dict:
    """Write a gateway's config then reload Asterisk.

    Returns ``{"written": <per-gateway path>, "reload": <reload_asterisk result>}``.
    """
    written = write_gateway_config(gw)
    reload_result = reload_asterisk()
    return {"written": written, "reload": reload_result}
