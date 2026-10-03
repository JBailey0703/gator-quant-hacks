"""Unified logging utilities.

All loggers in this project hang under the root logger ``webull`` (e.g.
``webull.feed``, ``webull.broker``, ``webull.main``, ``webull.strategy``).
Call ``setup_logging()`` once at the entry point to configure the root logger;
child loggers inherit its handler and level automatically.

Conventions:
  - Use ``get_logger(__name__ or short name)`` to obtain a logger; the name is
    automatically nested under the ``webull`` root.
  - Log messages start with a ``[module tag]`` prefix (e.g. ``[Backtest]``,
    ``[Live-Poll]``, ``[Broker]``) for easy filtering by source.
"""

from __future__ import annotations

import logging
import os

ROOT_LOGGER_NAME = "webull"

_LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str) -> logging.Logger:
    """Return a logger nested under the ``webull`` root.

    :param name: Short module name (e.g. "feed"/"broker"/"main") or ``__name__``.
        Names already prefixed with "webull." are used as-is.
    """
    if name == ROOT_LOGGER_NAME or name.startswith(ROOT_LOGGER_NAME + "."):
        return logging.getLogger(name)
    # Take the last segment as the short name to avoid embedding package paths.
    short = name.rsplit(".", 1)[-1]
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{short}")


def setup_logging(level: str | None = None) -> None:
    """Configure the ``webull`` root logger.

    Level defaults to the ``LOG_LEVEL`` environment variable (else INFO).
    Call once at the program entry point; all ``webull.*`` child loggers inherit
    this configuration. The log format carries a millisecond timestamp (local
    time).
    """
    resolved = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.setLevel(resolved)

    # Avoid adding duplicate handlers (when called more than once).
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))
        root.addHandler(handler)

    # Do not propagate to the Python root logger to avoid duplicate/interleaved
    # output with the SDK's own logging.
    root.propagate = False
