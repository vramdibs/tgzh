"""Speech-to-Text клиент для голосовых сообщений `/chat`.

Контракт: один OpenAI-совместимый эндпоинт **`POST /audio/transcriptions`**
(Whisper API — OpenAI/Groq/локальный faster-whisper-server и т.п.).

Включается env-блоком **`STT_*`** — если хотя бы `STT_BASE_URL` пуст, модуль
считается **выключенным** и бот молча сообщает пользователю, что голос пока
не распознаём. Это сделано специально: STT — опциональная фича, у нас нет
обязательного провайдера, и любой деплой без ключа должен спокойно стартовать.

Безопасность: транскрибируем только то, что пользователь сам прислал в чат
(после проверки сессии `/chat`); никаких системных промптов и инструкций
модели не передаём — Whisper их не понимает, а лишний контекст мог бы
утечь в логи провайдера. Возвращаем чистый текст распознавания.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Final

import httpx

logger = logging.getLogger("tgzh.stt")


# --- Дефолты и лимиты ---------------------------------------------------------
#
# 25 МБ — исторический лимит OpenAI Whisper API; локальные сервисы обычно тоже
# не любят сильно больше. Голосовое в Telegram (.ogg/opus) ~16 кбит/с, так что
# 25 МБ — это десятки минут речи; для нашего сценария «короткая команда в чат»
# лимит запасной.
_DEFAULT_MAX_AUDIO_BYTES: Final[int] = 25 * 1024 * 1024
_DEFAULT_MODEL: Final[str] = "whisper-1"
_DEFAULT_TIMEOUT_S: Final[float] = 60.0
_DEFAULT_LANGUAGE: Final[str] = "ru"
# Telegram voice — почти всегда `audio/ogg; codecs=opus`. Для аудио из файла
# передаваемые MIME могут отличаться (mp3, wav, m4a, flac); все они валидны
# для Whisper API.
_FALLBACK_FILENAME: Final[str] = "voice.ogg"
_FALLBACK_MIME: Final[str] = "audio/ogg"


@dataclass(frozen=True)
class SttConfig:
    base_url: str
    api_key: str
    model: str
    language: str | None
    timeout_s: float
    max_audio_bytes: int


def _read_config() -> SttConfig | None:
    base_url = (os.getenv("STT_BASE_URL") or "").strip().rstrip("/")
    if not base_url:
        return None
    api_key = (os.getenv("STT_API_KEY") or "").strip()
    model = (os.getenv("STT_MODEL") or "").strip() or _DEFAULT_MODEL
    raw_lang = (os.getenv("STT_LANGUAGE") or "").strip()
    language: str | None
    if raw_lang == "":
        language = _DEFAULT_LANGUAGE
    elif raw_lang.lower() == "auto":
        language = None
    else:
        language = raw_lang
    try:
        timeout_s = float((os.getenv("STT_TIMEOUT_S") or "").strip() or _DEFAULT_TIMEOUT_S)
    except ValueError:
        timeout_s = _DEFAULT_TIMEOUT_S
    timeout_s = max(5.0, min(600.0, timeout_s))
    try:
        max_bytes = int((os.getenv("STT_MAX_AUDIO_BYTES") or "").strip() or _DEFAULT_MAX_AUDIO_BYTES)
    except ValueError:
        max_bytes = _DEFAULT_MAX_AUDIO_BYTES
    max_bytes = max(64 * 1024, min(200 * 1024 * 1024, max_bytes))
    return SttConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        language=language,
        timeout_s=timeout_s,
        max_audio_bytes=max_bytes,
    )


def is_configured() -> bool:
    """STT включён, если задан **`STT_BASE_URL`** (остальное — с дефолтами)."""
    return _read_config() is not None


def max_audio_bytes() -> int:
    """Текущий лимит размера аудио (для предварительной проверки в боте)."""
    cfg = _read_config()
    return cfg.max_audio_bytes if cfg else _DEFAULT_MAX_AUDIO_BYTES


class SttError(RuntimeError):
    """Любая ошибка распознавания, видимая пользователю одной короткой строкой."""


class SttDisabledError(SttError):
    """STT не сконфигурирован (нет `STT_BASE_URL`)."""


async def transcribe(
    audio: bytes,
    *,
    mime: str | None = None,
    filename: str | None = None,
) -> str:
    """Распознать голосовое сообщение, вернуть текст (без обрамляющих пробелов).

    Любая ошибка — `SttError`/`SttDisabledError`. Бот ловит и показывает
    пользователю короткую фразу, не дампит трейс.
    """
    if not isinstance(audio, (bytes, bytearray)):
        raise SttError("internal: audio must be bytes")
    if not audio:
        raise SttError("Пустое аудио — нечего распознавать.")
    cfg = _read_config()
    if cfg is None:
        raise SttDisabledError("STT не сконфигурирован (нет STT_BASE_URL).")
    if len(audio) > cfg.max_audio_bytes:
        raise SttError(
            f"Аудио слишком большое для распознавания "
            f"(лимит {cfg.max_audio_bytes // (1024 * 1024)} МБ).",
        )

    fname = (filename or _FALLBACK_FILENAME).strip() or _FALLBACK_FILENAME
    fmime = (mime or _FALLBACK_MIME).strip() or _FALLBACK_MIME
    url = f"{cfg.base_url}/audio/transcriptions"
    headers: dict[str, str] = {}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    files = {"file": (fname, bytes(audio), fmime)}
    data: dict[str, str] = {
        "model": cfg.model,
        "response_format": "json",
    }
    if cfg.language:
        data["language"] = cfg.language

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout_s)) as client:
            resp = await client.post(url, headers=headers, files=files, data=data)
    except httpx.TimeoutException as e:
        logger.warning("stt timeout url=%s err=%s", url, e)
        raise SttError("Распознавание заняло слишком много времени.") from e
    except httpx.RequestError as e:
        logger.warning("stt request error url=%s err=%s", url, e)
        raise SttError(f"Ошибка связи с распознаванием: {e}") from e

    if resp.status_code != 200:
        body = resp.text[:300]
        logger.warning("stt http %s body=%r", resp.status_code, body)
        raise SttError(
            f"Сервис распознавания ответил {resp.status_code}: {body or '(пусто)'}",
        )
    try:
        payload = resp.json()
    except ValueError as e:
        logger.warning("stt invalid json body=%r", resp.text[:300])
        raise SttError("Сервис распознавания вернул не JSON.") from e
    text = ""
    if isinstance(payload, dict):
        # OpenAI/Whisper API: {"text": "..."}; некоторые сервисы возвращают
        # {"results": [...]}, но приоритет у "text", остальное — в логи.
        text = (payload.get("text") or "").strip()
    if not text:
        logger.warning("stt empty text payload=%r", str(payload)[:200])
        raise SttError("Сервис распознавания не вернул текст.")
    return text
