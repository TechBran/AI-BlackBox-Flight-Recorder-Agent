"""Centralized logging configuration for BlackBox Orchestrator."""
import logging
import sys

from Orchestrator.secret_redaction import install_redaction


def setup_logging(level=logging.INFO):
    """Configure structured logging for the Orchestrator."""
    fmt = "[%(asctime)s] %(levelname)-7s %(name)-25s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=level,
        format=fmt,
        datefmt=datefmt,
        stream=sys.stdout,
        force=True,
    )

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    # basicConfig(force=True) just replaced the root handler: put the secret
    # redaction filter on it (and on uvicorn's handlers, configured earlier).
    install_redaction()
