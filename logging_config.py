"""
Единая настройка логирования для бота и сервера.
Уровень: переменная окружения LOG_LEVEL (DEBUG, INFO, WARNING, ERROR), по умолчанию INFO.
"""

from __future__ import annotations

import logging
import os
import sys


def check_log_body_max_chars() -> int:
    """Макс. длина полей «ответ ученика» / «ответ модели» в логах `/check` (символы)."""
    raw = (os.getenv("CHECK_LOG_BODY_MAX_CHARS") or "").strip()
    if raw.isdigit():
        return max(1, min(int(raw), 256_000))
    return 16_384


def clip_check_log_body(text: str | None) -> str:
    """Обрезка одного текстового поля для логов (без исключений на None)."""
    if text is None:
        return ""
    s = str(text)
    cap = check_log_body_max_chars()
    if len(s) <= cap:
        return s
    return s[:cap] + f"\n...[truncated log body len={len(s)} cap={cap}]"


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
