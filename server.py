"""
FastAPI-сервер: приём фото и вызов AI для проверки ДЗ.
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager, suppress

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

load_dotenv()

from logging_config import clip_check_log_body, setup_logging
from pydantic import BaseModel, Field

import preocr_client
import tgzh_metrics
from motok_jwt import HubJwtError, verify_homework_jwt
from ai_checker import (
    allowed_check_mime,
    check_homework,
    chat_default_system_prompt,
    chat_max_history_turns,
    chat_resolve_cursor_model,
    generate_check_quip,
    stream_chat_via_cursor,
    summarize_check_parts,
)
from photo_check import (
    ImageRole,
    PhotoCheckMode,
    photo_check_enabled,
    photo_check_max_images,
    run_photo_check,
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
    subject_slug: str = Field(default="matematika")


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
async def check_summarize(request: Request, body: SummarizeRequest) -> CheckResponse:
    """Текстовая сводка нескольких результатов проверки (второй вызов LLM)."""
    person_id = _hub_person_id(request)
    if person_id:
        logger.info("summarize person_id=%s", person_id)
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
        result = await summarize_check_parts(
            body.parts,
            force_fallback=engine == "cursor",
            subject_slug=body.subject_slug,
        )
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


def _hub_person_id(request: Request) -> str | None:
    secret = (os.getenv("MOTOK_HUB_TOKEN_SECRET") or "").strip()
    if not secret:
        return None
    raw = request.headers.get("authorization") or ""
    if not raw.lower().startswith("bearer "):
        raise HTTPException(401, "Нужен токен хаба")
    token = raw.split(" ", 1)[1].strip()
    try:
        payload = verify_homework_jwt(token, secret=secret)
    except HubJwtError:
        raise HTTPException(401, "Токен хаба недействителен") from None
    return str(payload["sub"])


def _max_check_upload_bytes() -> int:
    raw = (os.getenv("CHECK_MAX_UPLOAD_BYTES") or "").strip()
    if raw.isdigit():
        return max(64 * 1024, min(64 * 1024 * 1024, int(raw)))
    return 25 * 1024 * 1024


@app.post("/check", response_model=CheckResponse)
async def check_photo(
    request: Request,
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
    subject_slug: str = Form("matematika"),
    engine: str = Form("auto"),
    check_id: str = Form(""),
) -> CheckResponse:
    """Принимает фото или документ (см. allowed_check_mime), возвращает результат проверки."""
    person_id = _hub_person_id(request)
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
        "check start bytes=%s content_type=%r engine=%s person_id=%s check_id=%s paragraph=%r exercise=%r page=%s grade=%s "
        "textbook_label=%r gdz_ex_len=%s gdz_vp_len=%s gdz_vw_len=%s gdz_tc_len=%s submission=%s",
        len(data),
        photo.content_type,
        engine_norm,
        person_id or "-",
        (check_id or "").strip() or "-",
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
            subject_slug=subject_slug.strip() or "matematika",
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


_VALID_PHOTO_ROLES: frozenset[str] = frozenset(
    {"condition", "solution", "mixed", "unspecified"}
)


def _normalize_photo_check_mode(raw: str) -> PhotoCheckMode:
    m = (raw or "").strip().lower()
    if m == "two_step":
        return "two_step"
    return "single_album"


def _normalize_image_roles(count: int, raw_roles: list[str]) -> list[ImageRole]:
    out: list[ImageRole] = []
    for i in range(count):
        if i < len(raw_roles):
            r = (raw_roles[i] or "").strip().lower()
            if r in _VALID_PHOTO_ROLES:
                out.append(r)  # type: ignore[arg-type]
                continue
        out.append("unspecified")
    return out


@app.post("/photo/check", response_model=CheckResponse)
async def photo_check_multipart(
    request: Request,
    mode: str = Form("single_album"),
    images: list[UploadFile] = File(...),
    image_roles: list[str] = Form(default=[]),
) -> CheckResponse:
    """Multimodal проверка по нескольким фото без OCR и ГДЗ (/photo)."""
    if not photo_check_enabled():
        raise HTTPException(503, "photo check disabled")
    if not images:
        raise HTTPException(400, "expected at least one image")
    cap_n = photo_check_max_images()
    if len(images) > cap_n:
        raise HTTPException(413, f"too many images (max {cap_n})")

    cap_bytes = _max_check_upload_bytes()
    blobs: list[bytes] = []
    for idx, up in enumerate(images):
        if not allowed_check_mime(up.content_type):
            raise HTTPException(
                400,
                f"images[{idx}]: expected jpeg, png, webp or gif",
            )
        data = await up.read(cap_bytes + 1)
        if len(data) > cap_bytes:
            raise HTTPException(413, f"images[{idx}] larger than {cap_bytes} bytes")
        blobs.append(data)

    mode_norm = _normalize_photo_check_mode(mode)
    roles = _normalize_image_roles(len(blobs), image_roles)
    person_id = _hub_person_id(request)
    logger.info(
        "photo/check start images=%s mode=%s person_id=%s roles=%s",
        len(blobs),
        mode_norm,
        person_id or "-",
        roles,
    )
    tgzh_metrics.record_server_check_start()
    t0 = time.perf_counter()
    failed = False
    try:
        result = await run_photo_check(images=blobs, roles=roles, mode=mode_norm)
    except Exception:
        failed = True
        logger.exception("photo/check failed")
        raise
    finally:
        elapsed = time.perf_counter() - t0
        tgzh_metrics.observe_server_check(elapsed_seconds=elapsed, failed=failed)
    logger.info(
        "photo/check done elapsed_s=%.2f result_len=%s",
        elapsed,
        len(result),
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
    model: str | None = None


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
        "chat stream start user_id=%s msgs=%s model=%s last_user_chars=%s last_user_images=%s",
        req.user_id,
        len(messages),
        chat_resolve_cursor_model(req.model),
        last_text_chars,
        last_image_count,
    )
    t0 = time.perf_counter()

    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def on_delta(piece: str) -> None:
        await queue.put(piece.encode("utf-8"))

    async def runner() -> None:
        try:
            full = await stream_chat_via_cursor(
                messages,
                on_delta=on_delta,
                model=req.model,
            )
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
