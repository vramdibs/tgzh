"""
FastAPI-сервер: приём фото и вызов AI для проверки ДЗ.
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager, suppress

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse

load_dotenv()

from logging_config import clip_check_log_body, setup_logging
from pydantic import BaseModel, Field

import image_gen
import preocr_client
import tgzh_metrics
from ai_checker import (
    allowed_check_mime,
    check_homework,
    chat_default_system_prompt,
    chat_max_history_turns,
    chat_once_via_cursor,
    generate_check_quip,
    stream_chat_via_cursor,
    summarize_check_parts,
)

logger = setup_logging("tgzh.server")

# Лимиты для входов LLM-эндпоинтов: страховка против гигантских payload'ов и
# разогнанного потребления токенов. Значения с запасом по сравнению с реальными вызовами от бота.
_MAX_SUMMARIZE_PARTS = 16
_MAX_SUMMARIZE_PART_LEN = 16_000
_MAX_QUIP_EXCERPT_LEN = 8_000


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info(
        "server startup host=0.0.0.0 port=%s AI_MOCK=%s",
        os.getenv("PORT", "8000"),
        os.getenv("AI_MOCK", "?"),
    )
    try:
        yield
    finally:
        await preocr_client.aclose_client()


app = FastAPI(title="Homework Checker API", lifespan=lifespan)


class CheckResponse(BaseModel):
    result: str


_ALLOWED_CHECK_ENGINES = frozenset({"auto", "cursor"})


def _resolve_engine(value: str | None) -> str:
    """Нормализует Form-параметр `engine` в `/check`-эндпоинтах."""
    raw = (value or "").strip().lower() or "auto"
    if raw not in _ALLOWED_CHECK_ENGINES:
        raise HTTPException(
            400,
            f"Unsupported engine={raw!r} (allowed: {sorted(_ALLOWED_CHECK_ENGINES)})",
        )
    return raw


class SummarizeRequest(BaseModel):
    parts: list[str] = Field(
        ...,
        min_length=2,
        max_length=_MAX_SUMMARIZE_PARTS,
    )
    engine: str = Field(default="auto")


class QuipRequest(BaseModel):
    excerpt: str = Field(default="", max_length=_MAX_QUIP_EXCERPT_LEN)


@app.post("/check/quip", response_model=CheckResponse)
async def check_quip(body: QuipRequest) -> CheckResponse:
    """Короткая ироничная фраза после низкой оценки проверки (текстовый вызов LLM)."""
    t0 = time.perf_counter()
    try:
        text = await generate_check_quip(excerpt=body.excerpt)
    except Exception:
        logger.exception("generate_check_quip failed")
        raise HTTPException(500, "quip failed")
    elapsed = time.perf_counter() - t0
    logger.info(
        "quip done elapsed_s=%.2f len=%s excerpt=%s quip=%s",
        elapsed,
        len(text),
        clip_check_log_body(body.excerpt),
        clip_check_log_body(text),
    )
    return CheckResponse(result=text)


@app.post("/check/summarize", response_model=CheckResponse)
async def check_summarize(body: SummarizeRequest) -> CheckResponse:
    """Текстовая сводка нескольких результатов проверки (второй вызов LLM)."""
    if len(body.parts) < 2:
        raise HTTPException(400, "Нужно минимум два фрагмента для сводки")
    too_long = next((i for i, p in enumerate(body.parts) if len(p) > _MAX_SUMMARIZE_PART_LEN), -1)
    if too_long >= 0:
        raise HTTPException(
            413,
            f"Часть {too_long} длиннее лимита {_MAX_SUMMARIZE_PART_LEN} символов",
        )
    engine = _resolve_engine(body.engine)
    t0 = time.perf_counter()
    try:
        result = await summarize_check_parts(body.parts, force_fallback=engine == "cursor")
    except Exception:
        logger.exception("summarize_check_parts failed")
        raise
    elapsed = time.perf_counter() - t0
    for i, p in enumerate(body.parts):
        logger.info(
            "summarize part idx=%s/%s len=%s text=%s",
            i + 1,
            len(body.parts),
            len(p),
            clip_check_log_body(p),
        )
    logger.info(
        "summarize done elapsed_s=%.2f parts=%s result_len=%s engine=%s merged_result=%s",
        elapsed,
        len(body.parts),
        len(result),
        engine,
        clip_check_log_body(result),
    )
    return CheckResponse(result=result)


def _max_check_upload_bytes() -> int:
    raw = (os.getenv("CHECK_MAX_UPLOAD_BYTES") or "").strip()
    if raw.isdigit():
        return max(64 * 1024, min(64 * 1024 * 1024, int(raw)))
    return 25 * 1024 * 1024


@app.post("/check", response_model=CheckResponse)
async def check_photo(
    photo: UploadFile = File(...),
    paragraph: str = Form(""),
    exercise: str = Form(""),
    page: str = Form(""),
    textbook_label: str = Form(""),
    grade: str = Form(""),
    gdz_exercises: str = Form(""),
    gdz_verif_pages: str = Form(""),
    gdz_verif_works: str = Form(""),
    gdz_task_condition: str = Form(""),
    engine: str = Form("auto"),
) -> CheckResponse:
    """Принимает фото или документ (см. allowed_check_mime), возвращает результат проверки."""
    if not allowed_check_mime(photo.content_type):
        logger.warning("check reject bad mime content_type=%r", photo.content_type)
        raise HTTPException(
            400,
            "Ожидается изображение или документ: "
            "jpeg, png, webp, gif, pdf, txt, html, markdown, doc, docx, rtf",
        )

    cap = _max_check_upload_bytes()
    data = await photo.read(cap + 1)
    if len(data) > cap:
        logger.warning("check reject too large bytes=%s cap=%s", len(data), cap)
        raise HTTPException(413, f"Файл больше лимита {cap} байт")
    page_val: int | None = None
    if page.strip().isdigit():
        page_val = int(page.strip())
    grade_val: int | None = None
    if grade.strip().isdigit():
        grade_val = int(grade.strip())
    ex = exercise.strip() or None
    engine_norm = _resolve_engine(engine)
    para_s = paragraph.strip()
    ct = (photo.content_type or "").lower()
    if "text/plain" in ct:
        # Текст ученика никогда не пишем в лог: персональные данные / личный труд.
        # Длина оставлена для диагностики: если 0 — клиент шлёт пустоту.
        student_excerpt = f"<text submission {len(data)} bytes>"
    else:
        student_excerpt = f"<non-text submission {len(data)} bytes>"

    logger.info(
        "check start bytes=%s content_type=%r engine=%s paragraph=%r exercise=%r page=%s grade=%s "
        "textbook_label=%r gdz_ex_len=%s gdz_vp_len=%s gdz_vw_len=%s gdz_tc_len=%s submission=%s",
        len(data),
        photo.content_type,
        engine_norm,
        para_s[:200] + ("…" if len(para_s) > 200 else ""),
        ex,
        page_val,
        grade_val,
        (textbook_label.strip() or "")[:200],
        len(gdz_exercises.strip()),
        len(gdz_verif_pages.strip()),
        len(gdz_verif_works.strip()),
        len(gdz_task_condition.strip()),
        student_excerpt,
    )
    tgzh_metrics.record_server_check_start()
    t0 = time.perf_counter()
    failed = False
    try:
        result = await check_homework(
            data,
            content_type=photo.content_type,
            paragraph=paragraph.strip(),
            exercise=ex,
            page=page_val,
            textbook_label=textbook_label.strip(),
            grade=grade_val,
            gdz_exercises=gdz_exercises.strip(),
            gdz_verif_pages=gdz_verif_pages.strip(),
            gdz_verif_works=gdz_verif_works.strip(),
            gdz_task_condition=gdz_task_condition.strip(),
            force_fallback=engine_norm == "cursor",
        )
    except Exception:
        failed = True
        logger.exception("check_homework failed")
        raise
    finally:
        elapsed = time.perf_counter() - t0
        tgzh_metrics.observe_server_check(elapsed_seconds=elapsed, failed=failed)
    logger.info(
        "check done elapsed_s=%.2f result_len=%s paragraph=%r exercise=%r page=%s engine=%s model_result=%s",
        elapsed,
        len(result),
        para_s[:200] + ("…" if len(para_s) > 200 else ""),
        ex,
        page_val,
        engine_norm,
        clip_check_log_body(result),
    )
    return CheckResponse(result=result)


_CHAT_MSG_CONTENT_MAX_LEN = 8_000
_CHAT_MAX_TOTAL_MESSAGES = 64
# Размер `data:`-URL картинки в multimodal-сообщении (base64 + префикс). 8 МБ
# с запасом покрывают сжатые фото из Telegram (`prepare_photo_for_upload`),
# при этом не пускают мегабайтные PDF/PNG в качестве chat-вложения.
_CHAT_IMAGE_DATA_URL_MAX_LEN = 8_000_000
# Сколько image-частей допускаем в одном сообщении: один альбом из Telegram
# приходит по фото за раз, multipart-кейс маловероятен — берём 4 на всякий.
_CHAT_MAX_IMAGES_PER_MESSAGE = 4


class ChatMessageContentPart(BaseModel):
    """Одна «часть» multimodal-сообщения OpenAI chat.completions.

    Поддерживаем text и image_url — этого достаточно, чтобы прокинуть фото
    из Telegram в Cursor (`/chat` для админа: исключение из общего правила
    «Cursor — текстовый», pre-OCR пропускаем — см. `_handle_chat_photo`).
    """

    type: str
    text: str | None = None
    image_url: dict[str, str] | None = None


class ChatMessage(BaseModel):
    role: str
    # Либо обычная строка (текстовое сообщение), либо список частей OpenAI-формата.
    content: str | list[ChatMessageContentPart]


class ChatStreamRequest(BaseModel):
    user_id: int = 0
    messages: list[ChatMessage] = Field(default_factory=list)
    system_prompt: str | None = None


def _content_text_chars(content: str | list[ChatMessageContentPart]) -> int:
    """Сумма длин text-частей (image_url НЕ считаем — лимитим отдельно)."""
    if isinstance(content, str):
        return len(content)
    total = 0
    for part in content:
        if part.type == "text":
            total += len(part.text or "")
    return total


def _validate_multimodal_content(
    msg_idx: int,
    content: list[ChatMessageContentPart],
) -> list[dict]:
    """Превратить multimodal-список в OpenAI-формат, попутно проверив лимиты."""
    if not content:
        raise HTTPException(400, f"messages[{msg_idx}].content is empty list")
    image_count = 0
    out: list[dict] = []
    for j, part in enumerate(content):
        kind = (part.type or "").strip().lower()
        if kind == "text":
            text = (part.text or "").strip()
            if not text:
                continue
            out.append({"type": "text", "text": text})
            continue
        if kind == "image_url":
            image_count += 1
            if image_count > _CHAT_MAX_IMAGES_PER_MESSAGE:
                raise HTTPException(
                    413,
                    f"messages[{msg_idx}] has more than {_CHAT_MAX_IMAGES_PER_MESSAGE} images",
                )
            url = (part.image_url or {}).get("url", "")
            if not url:
                raise HTTPException(
                    400,
                    f"messages[{msg_idx}].content[{j}].image_url.url is required",
                )
            if len(url) > _CHAT_IMAGE_DATA_URL_MAX_LEN:
                raise HTTPException(
                    413,
                    f"messages[{msg_idx}] image_url.url longer than "
                    f"{_CHAT_IMAGE_DATA_URL_MAX_LEN} bytes",
                )
            # http(s) и data: оба допустимы; первый отдаём как есть, второй — тоже.
            if not (url.startswith("http://") or url.startswith("https://") or url.startswith("data:")):
                raise HTTPException(
                    400,
                    f"messages[{msg_idx}].content[{j}].image_url.url must be http(s) or data:",
                )
            out.append({"type": "image_url", "image_url": {"url": url}})
            continue
        raise HTTPException(
            400,
            f"messages[{msg_idx}].content[{j}].type must be 'text' or 'image_url'",
        )
    if not out:
        raise HTTPException(400, f"messages[{msg_idx}] has no usable content parts")
    return out


def _normalize_chat_messages(req: ChatStreamRequest) -> list[dict]:
    """Срез истории + опциональная подмена system. Жёсткие лимиты против abuse.

    Поддерживает multimodal user-сообщение в **последней** реплике (фото из
    `/chat`): для него `content` — список `[{type: text, ...}, {type: image_url, ...}]`.
    История (assistant/user в начале) — только строковая; multimodal-история
    бот сам не присылает (фото в RAM/DB лежат как текстовый плейсхолдер).
    """
    if not req.messages:
        raise HTTPException(400, "messages must be non-empty")
    if len(req.messages) > _CHAT_MAX_TOTAL_MESSAGES:
        raise HTTPException(413, f"too many messages (max {_CHAT_MAX_TOTAL_MESSAGES})")
    too_long = next(
        (
            i
            for i, m in enumerate(req.messages)
            if _content_text_chars(m.content) > _CHAT_MSG_CONTENT_MAX_LEN
        ),
        -1,
    )
    if too_long >= 0:
        raise HTTPException(
            413,
            f"messages[{too_long}] text longer than {_CHAT_MSG_CONTENT_MAX_LEN} chars",
        )

    system_text = (req.system_prompt or "").strip() or chat_default_system_prompt()
    history: list[dict] = []
    keep = max(2, chat_max_history_turns())
    tail = req.messages[-keep:]
    for m in tail:
        role = (m.role or "").strip().lower()
        if role not in ("user", "assistant", "system"):
            continue
        if role == "system":
            continue
        if isinstance(m.content, str):
            history.append({"role": role, "content": (m.content or "").strip()})
        else:
            parts = _validate_multimodal_content(req.messages.index(m), m.content)
            history.append({"role": role, "content": parts})
    if not history or history[-1]["role"] != "user":
        raise HTTPException(400, "last message must have role=user")
    return [{"role": "system", "content": system_text}, *history]


@app.post("/chat/stream")
async def chat_stream(req: ChatStreamRequest):
    """Стрим chat-ответа Cursor-bridge как **plain text** (поток токенов).

    Не SSE: бот читает байты по мере поступления (FastAPI отдаёт `StreamingResponse`),
    разделение токенов внутри потока не нужно — на стороне бота буфер собирается
    конкатенацией. Авторизация — на стороне бота (хранит `chat_session`); сервер
    лимитирует только размер и количество сообщений.
    """
    messages = _normalize_chat_messages(req)
    last_user = next((m for m in reversed(messages) if m["role"] == "user"), None)
    last_text_chars = 0
    last_image_count = 0
    if last_user:
        c = last_user["content"]
        if isinstance(c, str):
            last_text_chars = len(c)
        else:
            for part in c:
                if part.get("type") == "text":
                    last_text_chars += len(part.get("text") or "")
                elif part.get("type") == "image_url":
                    last_image_count += 1
    logger.info(
        "chat stream start user_id=%s msgs=%s last_user_chars=%s last_user_images=%s",
        req.user_id,
        len(messages),
        last_text_chars,
        last_image_count,
    )
    t0 = time.perf_counter()

    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def on_delta(piece: str) -> None:
        await queue.put(piece.encode("utf-8"))

    async def runner() -> None:
        try:
            full = await stream_chat_via_cursor(messages, on_delta=on_delta)
            elapsed = time.perf_counter() - t0
            logger.info(
                "chat stream done user_id=%s elapsed_s=%.2f reply_chars=%s",
                req.user_id,
                elapsed,
                len(full),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("chat stream failed user_id=%s", req.user_id)
        finally:
            await queue.put(None)

    task = asyncio.create_task(runner())

    async def gen():
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


# Лимиты для /chat/once: одноразовый sleep-вызов может прислать большой
# snapshot памяти + транскрипт; даём более широкий потолок, чем /chat/stream,
# но всё равно ограниченный — иначе Cursor-bridge просто отвергнет.
_CHAT_ONCE_MAX_MESSAGES: int = 8
_CHAT_ONCE_MAX_TOTAL_CHARS: int = 200_000
_CHAT_ONCE_DEFAULT_TIMEOUT_S: float = 240.0
_CHAT_ONCE_MAX_TIMEOUT_S: float = 600.0


class ChatOnceRequest(BaseModel):
    user_id: int = 0
    messages: list[ChatMessage] = Field(default_factory=list)
    timeout_s: float | None = None


@app.post("/chat/once")
async def chat_once(req: ChatOnceRequest) -> dict:
    """Одноразовый (нестримовый) чат-вызов через Cursor-bridge.

    Используется ботом для **«сна памяти»**: на вход — system + user (sleep-промпт
    с snapshot'ом памяти и транскриптом), на выход — финальный текст модели в
    `{"text": "..."}`. Без стриминга, без правок истории, никакой рендерринг
    в Telegram не нужен.

    Авторизация — на стороне бота. Сервер делает санитари-валидацию:
    максимум `_CHAT_ONCE_MAX_MESSAGES` сообщений, суммарно ≤ `_CHAT_ONCE_MAX_TOTAL_CHARS`
    символов текста. Картинки в `/chat/once` запрещены — сон работает с
    plain-текстом памяти и транскрипта.
    """
    if not req.messages:
        raise HTTPException(400, "messages must be non-empty")
    if len(req.messages) > _CHAT_ONCE_MAX_MESSAGES:
        raise HTTPException(413, f"too many messages (max {_CHAT_ONCE_MAX_MESSAGES})")

    out_msgs: list[dict] = []
    total_chars = 0
    for i, m in enumerate(req.messages):
        role = (m.role or "").strip().lower()
        if role not in ("system", "user", "assistant"):
            raise HTTPException(400, f"messages[{i}].role must be system|user|assistant")
        if not isinstance(m.content, str):
            raise HTTPException(400, f"messages[{i}].content must be a string for /chat/once")
        text = (m.content or "").strip()
        if not text:
            continue
        total_chars += len(text)
        if total_chars > _CHAT_ONCE_MAX_TOTAL_CHARS:
            raise HTTPException(
                413,
                f"total text exceeds {_CHAT_ONCE_MAX_TOTAL_CHARS} chars",
            )
        out_msgs.append({"role": role, "content": text})
    if not out_msgs:
        raise HTTPException(400, "messages have no usable content")
    if not any(m["role"] == "user" for m in out_msgs):
        raise HTTPException(400, "messages must include a user-role entry")

    timeout = float(req.timeout_s or 0) or _CHAT_ONCE_DEFAULT_TIMEOUT_S
    timeout = min(max(30.0, timeout), _CHAT_ONCE_MAX_TIMEOUT_S)

    logger.info(
        "chat once start user_id=%s msgs=%s total_chars=%s timeout_s=%.1f",
        req.user_id,
        len(out_msgs),
        total_chars,
        timeout,
    )
    t0 = time.perf_counter()
    try:
        text = await chat_once_via_cursor(out_msgs, timeout_s=timeout)
    except RuntimeError as e:
        # fallback не сконфигурирован — это конфигурационная, не upstream-ошибка.
        raise HTTPException(503, str(e)) from e
    except asyncio.TimeoutError as e:
        logger.warning("chat once timeout user_id=%s", req.user_id)
        raise HTTPException(504, "chat once timeout") from e
    except Exception as e:
        logger.exception("chat once upstream failure user_id=%s", req.user_id)
        raise HTTPException(502, f"chat upstream error: {e}") from e
    elapsed = time.perf_counter() - t0
    reply_chars = len(text or "")
    logger.info(
        "chat once done user_id=%s elapsed_s=%.2f reply_chars=%s",
        req.user_id,
        elapsed,
        reply_chars,
    )
    return {"text": text or "", "reply_chars": reply_chars}


class ImageGenerateRequest(BaseModel):
    user_id: int = 0
    prompt: str


@app.post("/image/generate")
async def image_generate(req: ImageGenerateRequest) -> Response:
    """Сгенерировать одну картинку по текстовому промпту.

    OpenAI-совместимый бэкенд (`IMAGE_GEN_*`). Возвращает PNG-байты (`image/png`).
    Бот вызывает этот эндпоинт из админ-`/chat` (`/imagine` или кнопка), пробрасывает
    результат в Telegram через `send_photo`.
    """
    if not image_gen.is_image_gen_configured():
        raise HTTPException(
            503,
            "image generation backend not configured (set IMAGE_GEN_BASE_URL/API_KEY)",
        )
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is empty")
    if len(prompt) > image_gen.PROMPT_MAX_LEN:
        raise HTTPException(400, f"prompt longer than {image_gen.PROMPT_MAX_LEN} chars")

    t0 = time.perf_counter()
    logger.info(
        "image_generate start user_id=%s prompt_chars=%s model=%s size=%s",
        req.user_id,
        len(prompt),
        image_gen.image_gen_model(),
        image_gen.image_gen_size(),
    )
    try:
        png_bytes = await image_gen.generate_image(prompt)
    except image_gen.ImageGenBadPrompt as e:
        raise HTTPException(400, str(e)) from e
    except image_gen.ImageGenNotConfigured as e:
        raise HTTPException(503, str(e)) from e
    except asyncio.TimeoutError as e:
        logger.warning("image_generate timeout user_id=%s", req.user_id)
        raise HTTPException(504, "image generation timeout") from e
    except Exception as e:
        logger.exception("image_generate upstream failure user_id=%s", req.user_id)
        raise HTTPException(502, f"image backend error: {e}") from e

    elapsed = time.perf_counter() - t0
    logger.info(
        "image_generate done user_id=%s elapsed_s=%.2f bytes=%s",
        req.user_id,
        elapsed,
        len(png_bytes),
    )
    return Response(content=png_bytes, media_type="image/png")


@app.get("/health")
async def health() -> dict:
    """Проверка доступности сервера."""
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    tgzh_metrics.maybe_start_http_server()
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
