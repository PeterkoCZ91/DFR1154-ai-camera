"""Logging configuration with colored console output and rotating file handlers."""

import logging
import sys
import os
from logging.handlers import RotatingFileHandler


class ColoredFormatter(logging.Formatter):
    """Custom formatter for colored console output."""

    GREY = "\x1b[38;20m"
    GREEN = "\x1b[32;20m"
    YELLOW = "\x1b[33;20m"
    RED = "\x1b[31;20m"
    BOLD_RED = "\x1b[31;1m"
    RESET = "\x1b[0m"

    FORMAT = "%(asctime)s - %(levelname)s - %(message)s"

    FORMATS = {
        logging.DEBUG: GREY + FORMAT + RESET,
        logging.INFO: GREEN + FORMAT + RESET,
        logging.WARNING: YELLOW + FORMAT + RESET,
        logging.ERROR: RED + FORMAT + RESET,
        logging.CRITICAL: BOLD_RED + FORMAT + RESET,
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)


def setup_logging(config: dict, script_dir: str) -> None:
    """Configure logging with rotating file handlers and colored console."""
    if os.environ.get("LOG_LEVEL"):
        config["logging"]["level"] = os.environ.get("LOG_LEVEL").upper()

    log_level_str = config["logging"].get("level", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    # Console handler with colors
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColoredFormatter())

    log_handlers = [console_handler]

    # Main log file (rotating, 10MB, 3 backups)
    log_file_name = config["logging"].get("file", "a12.log")
    log_file_path = os.path.join(script_dir, log_file_name)

    file_formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(log_file_path, maxBytes=10 * 1024 * 1024, backupCount=3)
    file_handler.setFormatter(file_formatter)
    log_handlers.append(file_handler)

    # Events log (WARNING+ only, 5MB, 2 backups)
    events_file_path = os.path.join(script_dir, "events.log")
    events_handler = RotatingFileHandler(events_file_path, maxBytes=5 * 1024 * 1024, backupCount=2)
    events_handler.setFormatter(file_formatter)
    events_handler.setLevel(logging.WARNING)
    log_handlers.append(events_handler)

    logging.basicConfig(level=log_level, handlers=log_handlers)
