"""Logging for daemon and hooks: one rotating file, no sensitive content.

Only token counts, session ids, paths and error strings are ever logged.
Prompts and transcript content are not (the guard stores a SHA-256 of the
blocked prompt, nothing else).
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from keepwarm.state import ensure_dir

_MAX_BYTES = 1_000_000
_BACKUPS = 3


def get_logger(name: str, state_dir: Path) -> logging.Logger:
    logger = logging.getLogger(f"keepwarm.{name}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    ensure_dir(state_dir)
    handler = RotatingFileHandler(
        state_dir / "daemon.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(handler)
    return logger
