"""
AI BlackBox CLI Agent module — backend selection + startup hooks.

Exposes :func:`get_backend` which returns ``"tmux"`` or ``"zellij"`` based on:

1. ``CLI_AGENT_BACKEND`` environment variable (customer ``.env`` override).
2. Code-default (locked to ``"tmux"`` until Phase 7 flips to ``"zellij"``).
3. Health-fallback: if configured as ``"zellij"`` but zellij-web is unhealthy,
   return ``"tmux"`` plus a LOUD warning (audit C4).

Also exposes :func:`startup_initialize` which the FastAPI lifespan calls
BEFORE any route handler can run, to:

- Refresh the Zellij config (``zellij_client.ensure_config``) per audit M15.
- Wait for the zellij-web daemon to come up.
- Reconcile orchestrator state ↔ Zellij ``tokens.db``
  (``zellij_state.reconcile_or_wipe``) per audit C3.

Design notes:

- Default is LOCKED IN CODE (not via env), so a missing/empty ``.env`` on
  a fresh customer install still behaves identically to today's tmux-only
  build. Phase 7 T28 will flip ``_DEFAULT_BACKEND`` and that single change
  will make zellij the global default.
- ``get_backend()`` is called per-request by endpoint dispatchers, so it
  uses a 30-second TTL cache around ``web_server_healthy()`` to avoid
  curling the daemon on every endpoint call. The TTL is short enough that
  a daemon recovery is visible within 30 s without manual intervention.
- ``startup_initialize()`` NEVER raises — if zellij is unhealthy the
  orchestrator boots in degraded mode and ``get_backend()`` keeps falling
  back to tmux until the daemon recovers.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from . import zellij_client, zellij_state

logger = logging.getLogger(__name__)

# Locked to "tmux" during coexistence; Phase 7 T28 will flip this to
# "zellij". Changing the default here is the single switch — endpoints
# should never hard-code a backend.
_DEFAULT_BACKEND = "tmux"
_VALID_BACKENDS = frozenset({"tmux", "zellij"})

# Customer/operator-facing env var. Documented in .env.example.
_ENV_VAR = "CLI_AGENT_BACKEND"

# --- health-check cache (per-request perf) -------------------------------
#
# Endpoint dispatchers call get_backend() on every request, so the
# health probe must be cheap. We cache the boolean result with a 30 s
# TTL: short enough that a daemon recovery is visible within ~30 s
# without intervention, long enough that a hot endpoint doesn't curl
# Zellij hundreds of times per minute.
#
# Lock is a plain threading.Lock; get_backend() may be called from sync
# or async contexts and FastAPI runs handlers on worker threads.

_HEALTH_TTL_SECONDS = 30.0
_health_lock = threading.Lock()
_last_health_check_at: float = 0.0
_last_health_check_result: bool = False


def _zellij_healthy_cached() -> bool:
    """Return cached zellij-web health, refreshing if TTL expired.

    First call (or call after TTL expiry) does a single-attempt curl with
    no backoff — this is the LIVE check from a hot request path, not the
    startup wait loop. If the daemon is fully up, this returns in <10 ms.
    """
    global _last_health_check_at, _last_health_check_result

    now = time.monotonic()
    with _health_lock:
        if now - _last_health_check_at < _HEALTH_TTL_SECONDS:
            return _last_health_check_result

        # TTL expired (or never checked). Probe.
        try:
            healthy = zellij_client.web_server_healthy(
                retries=1, backoff_seconds=0.0
            )
        except Exception as exc:  # noqa: BLE001 — defensive, never bubble
            logger.warning(
                "get_backend: web_server_healthy raised %s — treating as unhealthy",
                exc,
            )
            healthy = False

        _last_health_check_at = now
        _last_health_check_result = healthy
        return healthy


def _reset_health_cache_for_tests() -> None:
    """Test helper — forces the next get_backend() call to re-probe.

    Not part of the public API; underscore prefix signals "internal,
    don't call from production code."
    """
    global _last_health_check_at, _last_health_check_result
    with _health_lock:
        _last_health_check_at = 0.0
        _last_health_check_result = False


# --- public API -----------------------------------------------------------


def get_backend() -> str:
    """Return the active CLI agent backend (``"tmux"`` or ``"zellij"``).

    Resolution order:

    1. Read the ``CLI_AGENT_BACKEND`` env var. If unset, empty, or invalid,
       fall through to :data:`_DEFAULT_BACKEND`. Invalid values are NOT
       silent — they emit a WARNING so misconfigured customer installs
       show up in journalctl on the first request.
    2. If the requested backend is ``"zellij"``, probe
       :func:`zellij_client.web_server_healthy` (TTL-cached, 30 s).
    3. If unhealthy, fall back to ``"tmux"`` and log a LOUD WARNING
       (audit C4). Returning the wrong backend silently is worse than
       returning the safe default with a noisy log line.
    4. Otherwise return the requested backend.

    Performance: typical call is a dict lookup + cache hit (<1 ms). Worst
    case (cold cache, zellij-web slow to respond) is ~2 s due to the
    urllib timeout inside ``web_server_healthy(retries=1)``.
    """
    raw = os.environ.get(_ENV_VAR, "").strip().lower()

    if not raw:
        requested = _DEFAULT_BACKEND
    elif raw in _VALID_BACKENDS:
        requested = raw
    else:
        logger.warning(
            "get_backend: %s=%r is not one of %s — using default %r",
            _ENV_VAR,
            raw,
            sorted(_VALID_BACKENDS),
            _DEFAULT_BACKEND,
        )
        requested = _DEFAULT_BACKEND

    if requested == "zellij":
        if not _zellij_healthy_cached():
            logger.warning(
                "get_backend: %s requested 'zellij' but zellij-web is "
                "UNHEALTHY — FALLING BACK TO TMUX. Check `systemctl status "
                "zellij-web.service` and `journalctl -u zellij-web.service`. "
                "This warning will repeat until the daemon recovers.",
                _ENV_VAR,
            )
            return "tmux"

    return requested


def startup_initialize() -> None:
    """Run Zellij-related startup initialization.

    Called from the FastAPI startup lifecycle BEFORE the app accepts the
    first request (audit C3 + M15). Sequence (order matters):

    1. :func:`zellij_client.ensure_config` — write/refresh ``config.kdl``
       so the daemon has the fields the current orchestrator version
       expects (audit M15).
    2. Wait up to ~10 s for :func:`zellij_client.web_server_healthy`. On
       first boot the daemon may still be coming up; on warm restarts it
       answers immediately.
    3. :func:`zellij_state.reconcile_or_wipe` — only if step 2 succeeded.
       Wiping orchestrator state when we can't verify the daemon side
       would risk discarding live mappings while the daemon is just slow
       to start.

    NEVER raises. If anything fails the orchestrator boots in degraded
    mode (tmux-only) and ``get_backend()`` keeps returning ``"tmux"``
    until the daemon recovers. This matches the "coexistence-preserves
    existing behavior" guarantee for Phase 2.

    Idempotent — safe to re-run on orchestrator restart. ``ensure_config``
    is a no-op when the file already has all required fields;
    ``reconcile_or_wipe`` is a no-op when state and ``tokens.db`` agree.
    """
    # Step 1: ensure_config (filesystem only — no daemon dependency).
    try:
        zellij_client.ensure_config()
    except Exception as exc:  # noqa: BLE001 — never crash startup
        logger.error(
            "startup_initialize: ensure_config failed (%s) — continuing in "
            "degraded mode; zellij backend will be unavailable until fixed",
            exc,
            exc_info=True,
        )
        # Still attempt the rest — a stale config might still let the
        # daemon answer health checks, and reconcile is independent.

    # Step 2: wait for zellij-web. ~10 s budget (5 retries × 2 s backoff
    # default — same as the production-install audit recommendation).
    try:
        healthy = zellij_client.web_server_healthy()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "startup_initialize: web_server_healthy raised (%s) — "
            "treating as unhealthy",
            exc,
            exc_info=True,
        )
        healthy = False

    # Prime the get_backend() health cache with whatever startup
    # observed, so the first request doesn't re-probe needlessly.
    global _last_health_check_at, _last_health_check_result
    with _health_lock:
        _last_health_check_at = time.monotonic()
        _last_health_check_result = healthy

    if not healthy:
        logger.warning(
            "startup_initialize: zellij-web NOT healthy at startup — "
            "skipping reconcile_or_wipe this boot. Orchestrator is in "
            "degraded mode (tmux only). Check `systemctl status "
            "zellij-web.service`. get_backend() will retry every %.0fs.",
            _HEALTH_TTL_SECONDS,
        )
        return

    # Step 3: reconcile orchestrator state ↔ Zellij tokens.db. This MUST
    # happen before any /launch handler can mint a new token — that
    # ordering is satisfied by virtue of FastAPI deferring request
    # acceptance until all startup events complete.
    try:
        zellij_state.reconcile_or_wipe()
    except Exception as exc:  # noqa: BLE001 — never crash startup
        logger.error(
            "startup_initialize: reconcile_or_wipe failed (%s) — "
            "continuing; subsequent /launch calls may surface stale state",
            exc,
            exc_info=True,
        )
        return

    # Step 4: master-token bootstrap (Phase 5 master-token model). Two
    # parts because zellij's auth has two steps:
    #   4a. Mint (or load) the master auth_token. Persisted to disk so it
    #       survives orchestrator restarts.
    #   4b. POST /command/login with the auth_token → receive a
    #       session_token cookie. That cookie value is what the app-proxy
    #       injects on every upstream forward. The cookie value is NOT
    #       persisted to disk — re-exchange on every orchestrator boot is
    #       cheap and avoids stale-cookie issues.
    try:
        zellij_client.ensure_master_zellij_token()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "startup_initialize: ensure_master_zellij_token failed (%s) — "
            "continuing in degraded mode; app-proxy /app-proxy/9097/* "
            "requests will fail until next successful startup",
            exc,
            exc_info=True,
        )
        return

    try:
        zellij_client.ensure_master_session_cookie()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "startup_initialize: ensure_master_session_cookie failed (%s) — "
            "continuing in degraded mode; app-proxy /app-proxy/9097/* "
            "requests will fail with 401 until orchestrator restart",
            exc,
            exc_info=True,
        )
        return

    logger.info(
        "startup_initialize: zellij subsystem ready (backend=%s)",
        get_backend(),
    )


__all__ = [
    "get_backend",
    "startup_initialize",
]
