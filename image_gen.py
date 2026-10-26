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
import contextvars
import io
import logging
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

import httpx

import photo_prepare

from ai_checker import CompletionUsage, add_completion_usage, usage_from_openai

logger = logging.getLogger("tgzh.image_gen")


PROMPT_MAX_LEN: Final[int] = 2_000
_DEFAULT_MODEL: Final[str] = "gpt-image-1"
_DEFAULT_SIZE: Final[str] = "1024x1024"
_DEFAULT_SOURCE_MAX_SIDE: Final[int] = 768
_DEFAULT_STYLE_REFERENCE_MAX_SIDE: Final[int] = 512
_DEFAULT_JPEG_QUALITY: Final[int] = 82
_ALLOWED_SIZES: Final[frozenset[str]] = frozenset(
    {
        "256x256",
        "512x512",
        "1024x1024",
        "1536x1024",
        "1024x1536",
    },
)
_DEFAULT_TIMEOUT_SEC: Final[float] = 120.0
# Запас под BRIDGE_IMAGE_LOCATE_TIMEOUT_SEC (до 90 с) после cursor-agent
_MULTIPART_LOCATE_BUFFER_SEC: Final[float] = 120.0
_TIMEOUT_LO: Final[float] = 10.0
_TIMEOUT_HI: Final[float] = 600.0
_MAX_SOURCE_IMAGE_BYTES: Final[int] = 8 * 1024 * 1024
_DEFAULT_STYLE_REFERENCE_PATH: Final[str] = "/app/static/imagine/style_reference.png"
# Bridge OpenAI-совместимый API требует prompt min_length=1 даже для style_redraw.
STYLE_REDRAW_DEFAULT_PROMPT: Final[str] = (
    "Перерисуй фото в живописном стиле эталона, сохрани сюжет и объекты на снимке. "
    "Лицо - строго как на исходнике, без смягчения и без усиления эмоции. "
    "Брови - самый строгий замок лица, важнее узнаваемости сцены. "
    "Скопируй линию бровей с исходника: высоту внутренних углов относительно глаз, "
    "изгиб, просвет и складку на переносице. "
    "Если внутренние углы высоко и не сведены - оставь их высоко и разведенными. "
    "Не опускай к носу, не своди, не рисуй складку, которой нет. "
    "Не подменяй брови исходника более злым нахмуренным лицом, даже если сцена узнаваема. "
    "Какая бровь выше на фото, та и остается выше. "
    "Наклон бровей не усиливать: угол от внутреннего края к внешнему как на исходнике. "
    "Внешние края не поднимать и внутренние не опускать, чтобы наклон стал круче. "
    "Глаза: тот же прищур и то же направление взгляда, не раскрывать шире и не смотреть в другую сторону. "
    "Рот и челюсть: то же открытие и то же выдвижение, не шире, не уже, не сильнее вперед или назад. "
    "Открытый рот - не улыбка, если на фото не улыбка. "
    "Если на исходнике рот почти закрыт - оставь почти закрытым: не дорисовывай крик и оскал. "
    "Стиснутые зубы остаются стиснутыми. Если между верхним и нижним рядом нет промежутка, "
    "не дорисовывай щель и не разводи челюсти в улыбку или речь. "
    "Сжатый рот не превращай в улыбку. "
    "Рука или кисть, смазанная движением на исходнике, остается сильно смазанной: "
    "сохраняй шлейф движения, не восстанавливай четкие пальцы. "
    "Эталон - только кисть и палитра. "
    "Сделай картинку ярче исходного фото: светлые тона, ясный дневной свет, "
    "без затемнения и мрачных теней."
)
IMAGE_EDIT_DEFAULT_PROMPT: Final[str] = (
    "Внеси правки по инструкции пользователя, остальное на фото не меняй."
)

_DEFAULT_BOT_QUEUE_WAIT_BUFFER_SEC: Final[float] = 900.0
_BOT_QUEUE_BUFFER_LO: Final[float] = 0.0
_BOT_QUEUE_BUFFER_HI: Final[float] = 1800.0

_active_base_url: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "image_gen_active_base_url",
    default=None,
)


def _strip_env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def image_gen_base_urls() -> list[str]:
    """OpenAI base URL (без хвостового `/`), один или несколько bridge."""
    csv = _strip_env("IMAGE_GEN_BASE_URLS")
    urls: list[str] = []
    if csv:
        urls = [u.strip().rstrip("/") for u in csv.split(",") if u.strip()]
    else:
        single = _strip_env("IMAGE_GEN_BASE_URL")
        if single:
            urls = [single.rstrip("/")]
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# Один URL = один cursor-agent. Альбом Telegram не длиннее 10 фото,
# два слота сверх альбома остаются другим пользователям.
_IMAGE_GEN_POOL_CAP: Final = 12
_TELEGRAM_ALBUM_MAX: Final = 10


def image_gen_max_concurrent() -> int:
    urls = image_gen_base_urls()
    n_urls = max(1, len(urls))
    raw = _strip_env("IMAGE_GEN_MAX_CONCURRENT")
    if raw:
        try:
            v = int(raw)
        except ValueError:
            v = n_urls
    else:
        v = n_urls
    return max(1, min(_IMAGE_GEN_POOL_CAP, v, n_urls if urls else 1))


def image_gen_style_album_limit() -> int:
    """Сколько фото одного альбома перерисовки принимаем.

    Telegram не отдает альбом длиннее 10. Если пул больше, остаток слотов
    остается другим (при 12 слотах это 2).
    """
    return max(1, min(_TELEGRAM_ALBUM_MAX, image_gen_max_concurrent()))


class _BridgePoolState:
    def __init__(self, urls: list[str]) -> None:
        self.urls = urls
        self.in_use: dict[str, bool] = {u: False for u in urls}
        self._waiters: deque[asyncio.Future[tuple[str, float]]] = deque()
        self._lock = asyncio.Lock()


_bridge_pool: _BridgePoolState | None = None


def reset_image_gen_bridge_pool() -> None:
    """Сброс пула (тесты)."""
    global _bridge_pool
    _bridge_pool = None


def _get_bridge_pool() -> _BridgePoolState:
    global _bridge_pool
    if _bridge_pool is None:
        urls = image_gen_base_urls()[: image_gen_max_concurrent()]
        if not urls:
            urls = ["http://invalid.local/v1"]
        _bridge_pool = _BridgePoolState(urls)
    return _bridge_pool


async def acquire_image_gen_bridge() -> tuple[str, float]:
    """FIFO: свободный bridge или ожидание в очереди."""
    t0 = time.monotonic()
    pool = _get_bridge_pool()
    async with pool._lock:
        for url in pool.urls:
            if not pool.in_use[url]:
                pool.in_use[url] = True
                return url, 0.0
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[str, float]] = loop.create_future()
        pool._waiters.append(fut)
    url, _assigned_at = await fut
    return url, time.monotonic() - t0


async def release_image_gen_bridge(url: str) -> None:
    pool = _get_bridge_pool()
    async with pool._lock:
        if pool._waiters:
            fut = pool._waiters.popleft()
            if not fut.done():
                fut.set_result((url, time.monotonic()))
            return
        pool.in_use[url] = False


@asynccontextmanager
async def image_gen_bridge_slot():
    """Занять слот bridge на время одного `/image/generate`."""
    url, wait_s = await acquire_image_gen_bridge()
    if wait_s >= 0.05:
        logger.info("image_gen queue wait_s=%.2f bridge=%s", wait_s, url)
    token = _active_base_url.set(url)
    try:
        yield url
    finally:
        _active_base_url.reset(token)
        await release_image_gen_bridge(url)


def image_gen_resolved_base_url() -> str:
    active = _active_base_url.get()
    if active:
        return active.rstrip("/")
    urls = image_gen_base_urls()
    if urls:
        return urls[0]
    return _strip_env("IMAGE_GEN_BASE_URL").rstrip("/")


def is_image_gen_configured() -> bool:
    """Бэкенд считается готовым, если заданы base URL и api_key."""
    return bool(image_gen_base_urls()) and bool(_strip_env("IMAGE_GEN_API_KEY"))


def image_gen_model() -> str:
    return _strip_env("IMAGE_GEN_MODEL") or _DEFAULT_MODEL


def image_gen_user_facing_label() -> str:
    """Подпись для пользователя: генерация фото не равна модели /chat (Grok)."""
    slug = image_gen_model().lower()
    if slug in ("auto", "default", ""):
        bridge_model = (_strip_env("BRIDGE_IMAGE_CURSOR_MODEL") or "composer-2.5").strip()
        if bridge_model.startswith("composer"):
            ver = bridge_model.removeprefix("composer-").strip() or "2"
            return f"Composer {ver}"
        return bridge_model or "Composer"
    if slug in ("gpt-image-1", "dall-e-3", "dall-e-2"):
        return "OpenAI Images"
    if slug.startswith("composer"):
        ver = slug.removeprefix("composer-").strip() or "2"
        return f"Composer {ver}"
    return image_gen_model()


def image_gen_size() -> str:
    return resolve_image_gen_size(None)


def image_gen_allowed_sizes() -> frozenset[str]:
    return _ALLOWED_SIZES


# Размеры, которые пользователь выбирает кнопками в /imagine (не все allowlist bridge).
CHAT_IMAGINE_SIZE_ORIGINAL: Final[str] = "original_max1280"
CHAT_IMAGINE_PICK_SIZES: Final[frozenset[str]] = frozenset(
    {"256x256", "1024x1024", CHAT_IMAGINE_SIZE_ORIGINAL},
)
CHAT_IMAGINE_DEFAULT_PICK_SIZE: Final[str] = "1024x1024"
ORIGINAL_MAX_LONG_SIDE: Final[int] = 1280
_SIZE_WXH_RE = re.compile(r"^(\d{1,4})x(\d{1,4})$")
_ORIGINAL_PROMPT_SIZE_HINT: Final[str] = (
    "natural aspect ratio for the scene, longest side 1280 pixels"
)


def size_from_image_bytes_max_long_side(
    data: bytes,
    max_long: int = ORIGINAL_MAX_LONG_SIDE,
) -> str:
    """WxH с сохранением пропорций, длинная сторона не больше max_long."""
    from PIL import Image, ImageOps

    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    w, h = im.size
    if w <= 0 or h <= 0:
        return _DEFAULT_SIZE
    long_side = max(w, h)
    if long_side > max_long:
        scale = max_long / float(long_side)
        w = max(1, int(round(w * scale)))
        h = max(1, int(round(h * scale)))
    return f"{w}x{h}"


def is_api_client_image_size(size: str) -> bool:
    """Допустимое значение поля size в POST /image/generate."""
    s = (size or "").strip()
    if not s:
        return False
    if s in _ALLOWED_SIZES or s == CHAT_IMAGINE_SIZE_ORIGINAL:
        return True
    m = _SIZE_WXH_RE.match(s)
    if not m:
        return False
    w, h = int(m.group(1)), int(m.group(2))
    return 1 <= w <= ORIGINAL_MAX_LONG_SIDE and 1 <= h <= ORIGINAL_MAX_LONG_SIDE


def resolve_image_gen_size(
    override: str | None,
    *,
    source_bytes: bytes | None = None,
) -> str:
    """Размер кадра: override из запроса бота, иначе IMAGE_GEN_SIZE, иначе дефолт."""
    raw = (override or "").strip()
    if raw == CHAT_IMAGINE_SIZE_ORIGINAL:
        if source_bytes:
            return size_from_image_bytes_max_long_side(source_bytes)
        return _ORIGINAL_PROMPT_SIZE_HINT
    if raw and is_api_client_image_size(raw) and raw != CHAT_IMAGINE_SIZE_ORIGINAL:
        if raw in _ALLOWED_SIZES:
            return raw
        return raw
    env_raw = _strip_env("IMAGE_GEN_SIZE") or _DEFAULT_SIZE
    if env_raw in _ALLOWED_SIZES:
        return env_raw
    logger.warning("IMAGE_GEN_SIZE=%r not in allowlist, using %s", env_raw, _DEFAULT_SIZE)
    return _DEFAULT_SIZE


def image_gen_source_max_side() -> int:
    raw = _strip_env("IMAGE_GEN_SOURCE_MAX_SIDE")
    if not raw:
        return _DEFAULT_SOURCE_MAX_SIDE
    try:
        v = int(raw)
    except ValueError:
        return _DEFAULT_SOURCE_MAX_SIDE
    return max(256, min(1536, v))


def image_gen_style_reference_max_side() -> int:
    raw = _strip_env("IMAGE_GEN_STYLE_REFERENCE_MAX_SIDE")
    if not raw:
        return _DEFAULT_STYLE_REFERENCE_MAX_SIDE
    try:
        v = int(raw)
    except ValueError:
        return _DEFAULT_STYLE_REFERENCE_MAX_SIDE
    return max(128, min(1024, v))


def image_gen_jpeg_quality() -> int:
    raw = _strip_env("IMAGE_GEN_JPEG_QUALITY")
    if not raw:
        return _DEFAULT_JPEG_QUALITY
    try:
        v = int(raw)
    except ValueError:
        return _DEFAULT_JPEG_QUALITY
    return max(50, min(95, v))


def prepare_bridge_source_bytes(data: bytes) -> bytes:
    """Уменьшить исходник перед base64 на bridge (style_redraw / image_edit)."""
    before = len(data)
    if before == 0:
        return data
    try:
        out = photo_prepare.prepare_photo_for_upload(
            data,
            max_side=image_gen_source_max_side(),
            jpeg_quality=image_gen_jpeg_quality(),
        )
    except Exception:
        logger.warning(
            "prepare_bridge_source_bytes failed raw_bytes=%s",
            before,
            exc_info=True,
        )
        return data
    if len(out) < before:
        logger.info(
            "image_gen source prepared bytes %s -> %s max_side=%s",
            before,
            len(out),
            image_gen_source_max_side(),
        )
    return out


def prepare_style_reference_bytes(data: bytes) -> bytes:
    before = len(data)
    if before == 0:
        return data
    try:
        out = photo_prepare.prepare_photo_for_upload(
            data,
            max_side=image_gen_style_reference_max_side(),
            jpeg_quality=image_gen_jpeg_quality(),
        )
    except Exception:
        logger.warning(
            "prepare_style_reference_bytes failed raw_bytes=%s",
            before,
            exc_info=True,
        )
        return data
    if len(out) < before:
        logger.info(
            "image_gen style_reference prepared bytes %s -> %s max_side=%s",
            before,
            len(out),
            image_gen_style_reference_max_side(),
        )
    return out


def image_gen_timeout_sec() -> float:
    raw = _strip_env("IMAGE_GEN_TIMEOUT_SEC")
    if not raw:
        return _DEFAULT_TIMEOUT_SEC
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_SEC
    return max(_TIMEOUT_LO, min(_TIMEOUT_HI, v))


def image_gen_multipart_timeout_sec() -> float:
    """Таймаут для style_redraw / image_edit (агент bridge + поиск PNG на диске)."""
    raw = _strip_env("IMAGE_GEN_MULTIPART_TIMEOUT_SEC")
    if raw:
        try:
            v = float(raw)
        except ValueError:
            v = image_gen_timeout_sec() + _MULTIPART_LOCATE_BUFFER_SEC
        return max(_TIMEOUT_LO, min(_TIMEOUT_HI, v))
    base = image_gen_timeout_sec()
    return max(_TIMEOUT_LO, min(_TIMEOUT_HI, base + _MULTIPART_LOCATE_BUFFER_SEC))


def image_gen_bot_queue_wait_buffer_sec() -> float:
    raw = _strip_env("IMAGE_GEN_BOT_QUEUE_WAIT_BUFFER_SEC")
    if not raw:
        return _DEFAULT_BOT_QUEUE_WAIT_BUFFER_SEC
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_BOT_QUEUE_WAIT_BUFFER_SEC
    return max(_BOT_QUEUE_BUFFER_LO, min(_BOT_QUEUE_BUFFER_HI, v))


def image_gen_bot_client_timeout_sec(*, multipart: bool = False) -> float:
    """HTTP-таймаут бота на POST /image/generate (+ сеть + очередь на bridge)."""
    base = image_gen_multipart_timeout_sec() if multipart else image_gen_timeout_sec()
    return max(
        60.0,
        base + 30.0 + image_gen_bot_queue_wait_buffer_sec(),
    )


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
    return prepare_style_reference_bytes(data)


def _validate_source_bytes(source: bytes) -> bytes:
    if not source:
        raise ImageGenBadSource("source image is empty")
    if len(source) > _MAX_SOURCE_IMAGE_BYTES:
        raise ImageGenBadSource(f"source image larger than {_MAX_SOURCE_IMAGE_BYTES} bytes")
    return source


def _env_positive_int(name: str, default: int) -> int:
    raw = _strip_env(name)
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def estimated_image_gen_usage(
    prompt: str,
    *,
    includes_source_image: bool = False,
) -> CompletionUsage:
    """Оценка токенов, если бэкенд (bridge) не вернул `usage` в JSON."""
    chars = len((prompt or "").strip())
    chars_per_token = max(1, _env_positive_int("IMAGE_GEN_ESTIMATE_CHARS_PER_TOKEN", 4))
    prompt_tokens = max(1, chars // chars_per_token) if chars else 1
    if includes_source_image:
        prompt_tokens += _env_positive_int("IMAGE_GEN_ESTIMATE_SOURCE_IMAGE_TOKENS", 2048)
    completion_tokens = _env_positive_int("IMAGE_GEN_ESTIMATE_OUTPUT_TOKENS", 4096)
    return CompletionUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def record_image_gen_usage(
    response: Any | None,
    *,
    prompt: str,
    includes_source_image: bool = False,
) -> CompletionUsage:
    """Учесть токены images API в contextvar для `_record_llm_spend` на server."""
    extra = usage_from_openai(response) if response is not None else CompletionUsage()
    if extra.prompt_tokens == 0 and extra.completion_tokens == 0:
        extra = estimated_image_gen_usage(
            prompt,
            includes_source_image=includes_source_image,
        )
    add_completion_usage(
        {
            "usage": {
                "prompt_tokens": extra.prompt_tokens,
                "completion_tokens": extra.completion_tokens,
            },
        },
    )
    return extra


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


async def _post_images_generations_json(
    payload: dict[str, Any],
    *,
    timeout: float | None = None,
) -> bytes:
    if not is_image_gen_configured():
        raise ImageGenNotConfigured(
            "image generation backend not configured (IMAGE_GEN_BASE_URL/API_KEY)",
        )
    base_url = image_gen_resolved_base_url()
    api_key = _strip_env("IMAGE_GEN_API_KEY")
    if timeout is None:
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
    record_image_gen_usage(
        body,
        prompt=str(payload.get("prompt") or ""),
        includes_source_image=bool(payload.get("source_image_b64")),
    )
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


async def generate_image(prompt: str, *, size: str | None = None) -> bytes:
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

    base_url = image_gen_resolved_base_url()
    api_key = _strip_env("IMAGE_GEN_API_KEY")
    model = image_gen_model()
    resolved_size = resolve_image_gen_size(size)
    timeout = image_gen_timeout_sec()

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
    logger.info(
        "image_gen request model=%s size=%s prompt_chars=%s base_url=%s",
        model,
        resolved_size,
        len(p),
        base_url,
    )

    # `gpt-image-1` отвергает аргумент `response_format` (всегда отдаёт b64_json),
    # `dall-e-3` принимает; чтобы поддержать оба бэкенда без if-else, не передаём
    # response_format и далее принимаем и `b64_json`, и `url` в ответе.
    resp = await asyncio.wait_for(
        client.images.generate(
            model=model,
            prompt=p,
            n=1,
            size=resolved_size,
        ),
        timeout=timeout,
    )

    record_image_gen_usage(resp, prompt=p, includes_source_image=False)
    return await _parse_images_response(resp)


async def generate_image_edit(
    source_bytes: bytes,
    instruction: str = "",
    *,
    size: str | None = None,
) -> bytes:
    """Правка исходного фото по текстовой инструкции (bridge image_edit)."""
    raw = _validate_source_bytes(source_bytes)
    src = prepare_bridge_source_bytes(raw)
    notes = _validate_optional_notes(instruction)
    api_prompt = notes or IMAGE_EDIT_DEFAULT_PROMPT
    if not is_image_gen_configured():
        raise ImageGenNotConfigured(
            "image generation backend not configured (IMAGE_GEN_BASE_URL/API_KEY)",
        )
    model = image_gen_model()
    resolved_size = resolve_image_gen_size(size, source_bytes=raw)
    timeout = image_gen_multipart_timeout_sec()
    payload = {
        "model": model,
        "prompt": api_prompt,
        "n": 1,
        "size": resolved_size,
        "source_image_b64": base64.b64encode(src).decode("ascii"),
        "image_edit": True,
    }
    logger.info(
        "image_gen image_edit model=%s size=%s source_bytes=%s instruction_chars=%s",
        model,
        resolved_size,
        len(src),
        len(notes),
    )
    return await asyncio.wait_for(
        _post_images_generations_json(payload, timeout=timeout),
        timeout=timeout,
    )


async def generate_image_style_redraw(
    source_bytes: bytes,
    extra_prompt: str = "",
    *,
    size: str | None = None,
) -> bytes:
    """Перерисовать исходное фото в стиле эталона (bridge style_redraw)."""
    raw = _validate_source_bytes(source_bytes)
    src = prepare_bridge_source_bytes(raw)
    notes = _validate_optional_notes(extra_prompt)
    api_prompt = notes or STYLE_REDRAW_DEFAULT_PROMPT
    if not is_image_gen_configured():
        raise ImageGenNotConfigured(
            "image generation backend not configured (IMAGE_GEN_BASE_URL/API_KEY)",
        )
    style_bytes = load_style_reference_bytes()
    model = image_gen_model()
    resolved_size = resolve_image_gen_size(size, source_bytes=raw)
    timeout = image_gen_multipart_timeout_sec()
    payload = {
        "model": model,
        "prompt": api_prompt,
        "n": 1,
        "size": resolved_size,
        "source_image_b64": base64.b64encode(src).decode("ascii"),
        "style_reference_b64": base64.b64encode(style_bytes).decode("ascii"),
    }
    logger.info(
        "image_gen style_redraw model=%s size=%s source_bytes=%s notes_chars=%s api_prompt_chars=%s",
        model,
        resolved_size,
        len(src),
        len(notes),
        len(api_prompt),
    )
    return await asyncio.wait_for(
        _post_images_generations_json(payload, timeout=timeout),
        timeout=timeout,
    )
