import logging
from .constants import config

import os
import sys
import contextlib

@contextlib.contextmanager
def suppress_alsa_warnings():
    stderr_fileno = sys.stderr.fileno()
    with open(os.devnull, 'w') as devnull:
        old_stderr = os.dup(stderr_fileno)
        os.dup2(devnull.fileno(), stderr_fileno)
        try:
            yield
        finally:
            os.dup2(old_stderr, stderr_fileno)


def setup_logger():
    """Configure and return a shared logger."""
    level_name = config.get("logging", "level", fallback="INFO").upper()
    log_to_file = config.getboolean("logging", "log_to_file", fallback=False)
    log_file = config.get("logging", "log_file", fallback="beatsaber.log")

    logger = logging.getLogger("firebeat_show")
    logger.setLevel(level_name)

    if not logger.handlers:
        console_handler = logging.StreamHandler(sys.stdout)
        console_formatter = logging.Formatter(
            "\033[1;34m[%(asctime)s]\033[0m %(levelname)-8s | %(name)s: %(message)s",
            "%H:%M:%S"
        )
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)

        if log_to_file:
            file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
            file_formatter = logging.Formatter(
                "[%(asctime)s] %(levelname)-8s | %(name)s: %(message)s",
                "%Y-%m-%d %H:%M:%S"
            )
            file_handler.setFormatter(file_formatter)
            logger.addHandler(file_handler)

    return logger

logger = setup_logger()
