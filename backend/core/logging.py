"""Logging setup for ZLAgent.

Uses loguru for nicer structured logs while remaining compatible with
uvicorn's default log handlers.
"""
from __future__ import annotations

import logging
import sys

from loguru import logger

from .config import get_settings


class _InterceptHandler(logging.Handler):
    """Redirect stdlib logging records into loguru."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - passthrough
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def configure_logging() -> None:
    """Install the loguru-based logging configuration."""
    settings = get_settings()

    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
            "<level>{level: <8}</level> "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
            "- <level>{message}</level>"
        ),
        enqueue=False,
        backtrace=False,
        diagnose=False,
    )

    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        logging.getLogger(name).handlers = [_InterceptHandler()]
