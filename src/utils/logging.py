"""
logging.py — structlog configuration for chat-summarizer.

Outputs JSON-formatted log lines to stdout and optionally to a log file.
Log level is controlled by config.logging.level.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from src.config import LoggingConfig


def configure(config: "LoggingConfig") -> None:
    level = getattr(logging, config.level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if config.log_file:
        import pathlib
        pathlib.Path(config.log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(config.log_file))

    logging.basicConfig(level=level, handlers=handlers, format="%(message)s")

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
