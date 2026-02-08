"""File logging for adbui. Writes to /tmp/adbui/."""

import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = "/tmp/adbui"
LOG_FILE = os.path.join(LOG_DIR, "adbui.log")

logger = logging.getLogger("adbui")


def setup_logging() -> None:
    """Initialize file logging to /tmp/adbui/adbui.log."""
    os.makedirs(LOG_DIR, exist_ok=True)

    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
