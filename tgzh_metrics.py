"""
Опциональный экспорт метрик Prometheus (HTTP /metrics) для бота и server.py.
Включение: задать METRICS_PORT (положительное число). Привязка: METRICS_BIND (по умолчанию 127.0.0.1).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

logger = logging.getLogger("tgzh.metrics")

if TYPE_CHECKING:
    from telegram import Update

try:
    from prometheus_client import Counter, Histogram, start_http_server
except ImportError:  # pragma: no cover - optional dependency
    start_http_server = None  # type: ignore[misc, assignment]
    Counter = Histogram = None  # type: ignore[misc, assignment]

_bot_updates_total = None
_bot_handler_errors_total = None
_poll_completed_total = None
_server_check_requests_total = None
_server_check_errors_total = None
_server_check_duration_seconds = None
_check_feedback_total = None


def _ensure_metrics() -> None:
    global _bot_updates_total, _bot_handler_errors_total, _poll_completed_total
    global _server_check_requests_total, _server_check_errors_total, _server_check_duration_seconds
    global _check_feedback_total
    if Counter is None:
        return
    if _bot_updates_total is None:
        _bot_updates_total = Counter(
            "tgzh_bot_updates_total",
            "Входящие обновления Telegram",
            ["update_type"],
        )
    if _bot_handler_errors_total is None:
        _bot_handler_errors_total = Counter(
            "tgzh_bot_handler_errors_total",
            "Необработанные исключения в обработчиках бота",
        )
    if _poll_completed_total is None:
        _poll_completed_total = Counter(
            "tgzh_poll_completed_total",
            "Завершенные опросы /polling (сохранено в БД)",
        )
    if _server_check_requests_total is None:
        _server_check_requests_total = Counter(
            "tgzh_server_check_requests_total",
            "Запросы POST /check",
        )
    if _server_check_errors_total is None:
        _server_check_errors_total = Counter(
            "tgzh_server_check_errors_total",
            "Ошибки при выполнении POST /check",
        )
    if _server_check_duration_seconds is None:
        _server_check_duration_seconds = Histogram(
            "tgzh_server_check_duration_seconds",
            "Длительность check_homework (сек)",
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
        )
    if _check_feedback_total is None:
        _check_feedback_total = Counter(
            "tgzh_check_feedback_total",
            "Оценка результата проверки (кнопки под сообщением)",
            ["vote"],
        )


def maybe_start_http_server() -> None:
    if start_http_server is None:
        return
    raw = (os.getenv("METRICS_PORT") or "").strip()
    if not raw:
        return
    try:
        port = int(raw)
    except ValueError:
        logger.warning("METRICS_PORT ignored (not an integer): %r", raw)
        return
    if port <= 0:
        return
    bind = (os.getenv("METRICS_BIND") or "127.0.0.1").strip() or "127.0.0.1"
    start_http_server(port, addr=bind)
    logger.info("prometheus metrics on http://%s:%s/metrics", bind, port)


def record_bot_update(update: "Update") -> None:
    _ensure_metrics()
    if _bot_updates_total is None:
        return
    ut = "unknown"
    if update.message:
        ut = "message"
    elif update.edited_message:
        ut = "edited_message"
    elif update.callback_query:
        ut = "callback_query"
    elif update.channel_post:
        ut = "channel_post"
    _bot_updates_total.labels(ut).inc()


def record_bot_handler_error() -> None:
    _ensure_metrics()
    if _bot_handler_errors_total is None:
        return
    _bot_handler_errors_total.inc()


def record_poll_completed() -> None:
    _ensure_metrics()
    if _poll_completed_total is None:
        return
    _poll_completed_total.inc()


def record_check_feedback(*, vote: str) -> None:
    """vote: like | dislike (новое подтвержденное нажатие, в т.ч. смена мнения)."""
    _ensure_metrics()
    if _check_feedback_total is None:
        return
    if vote not in ("like", "dislike"):
        return
    _check_feedback_total.labels(vote).inc()


def record_server_check_start() -> None:
    _ensure_metrics()
    if _server_check_requests_total is None:
        return
    _server_check_requests_total.inc()


def observe_server_check(*, elapsed_seconds: float, failed: bool) -> None:
    _ensure_metrics()
    if _server_check_duration_seconds is None:
        return
    _server_check_duration_seconds.observe(elapsed_seconds)
    if failed and _server_check_errors_total is not None:
        _server_check_errors_total.inc()
