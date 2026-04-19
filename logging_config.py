"""
Единая настройка логирования для бота и сервера.
Уровень: переменная окружения LOG_LEVEL (DEBUG, INFO, WARNING, ERROR), по умолчанию INFO.
"""

from __future__ import annotations

import logging
import os
import sys


def setup_logging(name: str) -> logging.Logger:
    level_str = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_str, logging.INFO)

    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(handler)
    root.setLevel(level)

    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(
            logging.DEBUG if level <= logging.DEBUG else logging.WARNING
        )

    return logging.getLogger(name)
