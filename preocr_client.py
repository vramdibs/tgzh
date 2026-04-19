"""
Клиент HTTP-сервиса предварительного OCR (tgzh-preocr).

Вызывается из ai_checker при заданном PREOCR_URL.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Singleton-клиент: пересоздаём AsyncClient только при смене таймаута (через env).
# Иначе на каждый /check открывалось новое TCP/HTTP-соединение к tgzh-preocr,
# что под нагрузкой давало лишние коннекты и тормоза.
_client: httpx.AsyncClient | None = None
_client_timeout: float | None = None
_client_lock = asyncio.Lock()


async def _get_client(timeout_s: float) -> httpx.AsyncClient:
    global _client, _client_timeout
    if _client is not None and _client_timeout == timeout_s:
        return _client
    async with _client_lock:
        if _client is not None and _client_timeout == timeout_s:
            return _client
        if _client is not None:
            try:
                await _client.aclose()
            except Exception:
                logger.debug("preocr client aclose failed", exc_info=True)
        _client = httpx.AsyncClient(timeout=timeout_s)
        _client_timeout = timeout_s
        return _client


async def aclose_client() -> None:
    """Корректно закрыть глобальный AsyncClient (вызывать на shutdown сервера)."""
    global _client, _client_timeout
    if _client is None:
        return
    try:
        await _client.aclose()
    finally:
        _client = None
        _client_timeout = None


def _preocr_url() -> str:
    return (os.getenv("PREOCR_URL") or "").strip().rstrip("/")


def _timeout_s() -> float:
    raw = (os.getenv("PREOCR_TIMEOUT_SEC") or "60").strip()
    try:
        v = float(raw)
        return max(5.0, min(120.0, v))
    except ValueError:
        return 60.0


def _max_chars() -> int:
    raw = (os.getenv("PREOCR_PROMPT_MAX_CHARS") or "8000").strip()
    if raw.isdigit():
        return max(500, min(32000, int(raw)))
    return 8000


async def fetch_preocr_block(*, image_bytes: bytes, content_type: str) -> str:
    """
    POST /v1/preocr, возвращает готовый блок для вставки в промпт или пустую строку.
    """
    base = _preocr_url()
    if not base:
        return ""
    ct = (content_type or "image/jpeg").split(";")[0].strip() or "image/jpeg"
    filename = "photo.jpg"
    if "png" in ct:
        filename = "photo.png"
    elif "webp" in ct:
        filename = "photo.webp"

    tpl = (
        os.getenv("VLLM_PROMPT_PREOCR_BLOCK", "").strip()
        or (
            "Ниже — автоматическое предварительное распознавание листа (OCR). "
            "Оно может содержать ошибки; опирайся в первую очередь на изображение. "
            "Используй текст как подсказку для символов и формул.\n\n{body}"
        )
    )

    url = f"{base}/v1/preocr"
    try:
        client = await _get_client(_timeout_s())
        r = await client.post(
            url,
            files={"image": (filename, image_bytes, ct)},
        )
    except httpx.HTTPError as e:
        logger.warning("preocr request failed: %s", e)
        return ""

    if r.status_code != 200:
        logger.warning("preocr HTTP %s: %s", r.status_code, r.text[:200])
        return ""

    try:
        data: dict[str, Any] = r.json()
    except ValueError:
        logger.warning("preocr invalid JSON")
        return ""

    body = (data.get("merged_markdown") or "").strip()
    if not body:
        return ""
    lim = _max_chars()
    if len(body) > lim:
        body = body[: lim - 3] + "..."

    if "{body}" in tpl:
        return tpl.replace("{body}", body)
    return f"{tpl}\n\n{body}" if tpl else body
