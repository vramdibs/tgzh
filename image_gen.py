"""Генерация изображений по текстовому промпту через OpenAI-совместимый
`/v1/images/generations`.

Бэкенд настраивается переменными окружения **`IMAGE_GEN_*`** независимо от
основного **`VLLM_*`** и **`VLLM_FALLBACK_*`** — например, основной чат может
ходить через cursor-bridge (он картинки не генерирует), а сюда подключён
OpenAI/together.ai/локальный SDXL-прокси. Если переменные не заданы или
неполные, фича считается выключенной (сервер вернёт 503).

Используется только из скрытого админ-`/chat` (`/imagine <prompt>` или кнопка
«Сгенерировать фото»). Никаких ученических сценариев тут нет.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Final

import httpx

logger = logging.getLogger("tgzh.image_gen")


PROMPT_MAX_LEN: Final[int] = 2_000
_DEFAULT_MODEL: Final[str] = "gpt-image-1"
_DEFAULT_SIZE: Final[str] = "1024x1024"
_DEFAULT_TIMEOUT_SEC: Final[float] = 120.0
_TIMEOUT_LO: Final[float] = 10.0
_TIMEOUT_HI: Final[float] = 600.0


def _strip_env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def is_image_gen_configured() -> bool:
    """Бэкенд считается готовым, если заданы и base_url, и api_key."""
    return bool(_strip_env("IMAGE_GEN_BASE_URL")) and bool(_strip_env("IMAGE_GEN_API_KEY"))


def image_gen_model() -> str:
    return _strip_env("IMAGE_GEN_MODEL") or _DEFAULT_MODEL


def image_gen_size() -> str:
    return _strip_env("IMAGE_GEN_SIZE") or _DEFAULT_SIZE


def image_gen_timeout_sec() -> float:
    raw = _strip_env("IMAGE_GEN_TIMEOUT_SEC")
    if not raw:
        return _DEFAULT_TIMEOUT_SEC
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_SEC
    return max(_TIMEOUT_LO, min(_TIMEOUT_HI, v))


class ImageGenNotConfigured(RuntimeError):
    """Бэкенд не настроен: нужны IMAGE_GEN_BASE_URL и IMAGE_GEN_API_KEY."""


class ImageGenBadPrompt(ValueError):
    """Промпт пуст или превышает лимит длины."""


def _validate_prompt(prompt: str) -> str:
    p = (prompt or "").strip()
    if not p:
        raise ImageGenBadPrompt("prompt is empty")
    if len(p) > PROMPT_MAX_LEN:
        raise ImageGenBadPrompt(f"prompt longer than {PROMPT_MAX_LEN} chars")
    return p


async def generate_image(prompt: str) -> bytes:
    """Сгенерировать одну картинку по промпту. Возвращает байты PNG.

    Raises:
        ImageGenNotConfigured: бэкенд не сконфигурирован.
        ImageGenBadPrompt: пустой/слишком длинный промпт.
        asyncio.TimeoutError: бэкенд не уложился в `IMAGE_GEN_TIMEOUT_SEC`.
        RuntimeError: пустой/нечитаемый ответ бэкенда.
    """
    p = _validate_prompt(prompt)
    if not is_image_gen_configured():
        raise ImageGenNotConfigured(
            "image generation backend not configured (IMAGE_GEN_BASE_URL/API_KEY)",
        )

    from openai import AsyncOpenAI

    base_url = _strip_env("IMAGE_GEN_BASE_URL")
    api_key = _strip_env("IMAGE_GEN_API_KEY")
    model = image_gen_model()
    size = image_gen_size()
    timeout = image_gen_timeout_sec()

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
    logger.info(
        "image_gen request model=%s size=%s prompt_chars=%s",
        model,
        size,
        len(p),
    )

    # `gpt-image-1` отвергает аргумент `response_format` (всегда отдаёт b64_json),
    # `dall-e-3` принимает; чтобы поддержать оба бэкенда без if-else, не передаём
    # response_format и далее принимаем и `b64_json`, и `url` в ответе.
    resp = await asyncio.wait_for(
        client.images.generate(
            model=model,
            prompt=p,
            n=1,
            size=size,
        ),
        timeout=timeout,
    )

    data = getattr(resp, "data", None) or []
    if not data:
        raise RuntimeError("image backend returned empty data")
    item = data[0]
    b64 = getattr(item, "b64_json", None)
    if b64:
        try:
            return base64.b64decode(b64)
        except (ValueError, TypeError) as e:
            raise RuntimeError(f"image backend returned malformed b64: {e}") from e
    url = getattr(item, "url", None)
    if url:
        # На случай DALL-E-стиля ответа: качаем картинку отдельным httpx-запросом.
        async with httpx.AsyncClient(timeout=timeout) as hc:
            r = await hc.get(url)
            r.raise_for_status()
            return r.content
    raise RuntimeError("image backend returned neither b64_json nor url")
