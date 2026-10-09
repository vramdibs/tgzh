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
import binascii
import logging
import os
from pathlib import Path
from typing import Any, Final

import httpx

logger = logging.getLogger("tgzh.image_gen")


PROMPT_MAX_LEN: Final[int] = 2_000
_DEFAULT_MODEL: Final[str] = "gpt-image-1"
_DEFAULT_SIZE: Final[str] = "1024x1024"
_DEFAULT_TIMEOUT_SEC: Final[float] = 120.0
_TIMEOUT_LO: Final[float] = 10.0
_TIMEOUT_HI: Final[float] = 600.0
_MAX_SOURCE_IMAGE_BYTES: Final[int] = 8 * 1024 * 1024
_DEFAULT_STYLE_REFERENCE_PATH: Final[str] = "/app/static/imagine/style_reference.png"


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


class ImageGenBadSource(ValueError):
    """Исходное фото для перерисовки пустое или слишком большое."""


class ImageGenStyleReferenceMissing(RuntimeError):
    """Файл эталона стиля не найден на сервере."""


def _validate_prompt(prompt: str) -> str:
    p = (prompt or "").strip()
    if not p:
        raise ImageGenBadPrompt("prompt is empty")
    if len(p) > PROMPT_MAX_LEN:
        raise ImageGenBadPrompt(f"prompt longer than {PROMPT_MAX_LEN} chars")
    return p


def _validate_optional_notes(notes: str) -> str:
    p = (notes or "").strip()
    if len(p) > PROMPT_MAX_LEN:
        raise ImageGenBadPrompt(f"prompt longer than {PROMPT_MAX_LEN} chars")
    return p


def image_gen_style_reference_path() -> Path:
    raw = _strip_env("IMAGE_GEN_STYLE_REFERENCE_PATH")
    if raw:
        return Path(raw)
    return Path(_DEFAULT_STYLE_REFERENCE_PATH)


def load_style_reference_bytes() -> bytes:
    path = image_gen_style_reference_path()
    if not path.is_file():
        raise ImageGenStyleReferenceMissing(
            f"style reference not found: {path}",
        )
    data = path.read_bytes()
    if not data or len(data) > _MAX_SOURCE_IMAGE_BYTES:
        raise ImageGenStyleReferenceMissing("style reference file is empty or too large")
    return data


def _validate_source_bytes(source: bytes) -> bytes:
    if not source:
        raise ImageGenBadSource("source image is empty")
    if len(source) > _MAX_SOURCE_IMAGE_BYTES:
        raise ImageGenBadSource(f"source image larger than {_MAX_SOURCE_IMAGE_BYTES} bytes")
    return source


def decode_source_image_b64(source_image_b64: str) -> bytes:
    raw = (source_image_b64 or "").strip()
    if not raw:
        raise ImageGenBadSource("source_image_b64 is empty")
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ImageGenBadSource("source_image_b64 is not valid base64") from e
    return _validate_source_bytes(data)


async def _parse_images_response(resp: Any) -> bytes:
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
        timeout = image_gen_timeout_sec()
        async with httpx.AsyncClient(timeout=timeout) as hc:
            r = await hc.get(url)
            r.raise_for_status()
            return r.content
    raise RuntimeError("image backend returned neither b64_json nor url")


async def _post_images_generations_json(payload: dict[str, Any]) -> bytes:
    if not is_image_gen_configured():
        raise ImageGenNotConfigured(
            "image generation backend not configured (IMAGE_GEN_BASE_URL/API_KEY)",
        )
    base_url = _strip_env("IMAGE_GEN_BASE_URL").rstrip("/")
    api_key = _strip_env("IMAGE_GEN_API_KEY")
    timeout = image_gen_timeout_sec()
    url = f"{base_url}/images/generations"
    async with httpx.AsyncClient(timeout=timeout) as hc:
        r = await hc.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"image backend {r.status_code}: {(r.text or '')[:300]}")
        body = r.json()
    data = body.get("data") or []
    if not data:
        raise RuntimeError("image backend returned empty data")
    item = data[0]
    b64 = item.get("b64_json")
    if b64:
        try:
            return base64.b64decode(b64)
        except (ValueError, TypeError) as e:
            raise RuntimeError(f"image backend returned malformed b64: {e}") from e
    img_url = item.get("url")
    if img_url:
        async with httpx.AsyncClient(timeout=timeout) as hc:
            gr = await hc.get(img_url)
            gr.raise_for_status()
            return gr.content
    raise RuntimeError("image backend returned neither b64_json nor url")


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

    return await _parse_images_response(resp)


async def generate_image_style_redraw(
    source_bytes: bytes,
    extra_prompt: str = "",
) -> bytes:
    """Перерисовать исходное фото в стиле эталона (bridge style_redraw)."""
    src = _validate_source_bytes(source_bytes)
    notes = _validate_optional_notes(extra_prompt)
    if not is_image_gen_configured():
        raise ImageGenNotConfigured(
            "image generation backend not configured (IMAGE_GEN_BASE_URL/API_KEY)",
        )
    style_bytes = load_style_reference_bytes()
    model = image_gen_model()
    size = image_gen_size()
    timeout = image_gen_timeout_sec()
    payload = {
        "model": model,
        "prompt": notes,
        "n": 1,
        "size": size,
        "source_image_b64": base64.b64encode(src).decode("ascii"),
        "style_reference_b64": base64.b64encode(style_bytes).decode("ascii"),
    }
    logger.info(
        "image_gen style_redraw model=%s size=%s source_bytes=%s notes_chars=%s",
        model,
        size,
        len(src),
        len(notes),
    )
    return await asyncio.wait_for(
        _post_images_generations_json(payload),
        timeout=timeout,
    )
