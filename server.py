"""
FastAPI-сервер: приём фото и вызов AI для проверки ДЗ.
"""

import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

load_dotenv()

from logging_config import setup_logging
from pydantic import BaseModel, Field

import preocr_client
import tgzh_metrics
from ai_checker import allowed_check_mime, check_homework, generate_check_quip, summarize_check_parts

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


class SummarizeRequest(BaseModel):
    parts: list[str] = Field(
        ...,
        min_length=2,
        max_length=_MAX_SUMMARIZE_PARTS,
    )


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
    logger.info("quip done elapsed_s=%.2f len=%s", elapsed, len(text))
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
    t0 = time.perf_counter()
    try:
        result = await summarize_check_parts(body.parts)
    except Exception:
        logger.exception("summarize_check_parts failed")
        raise
    elapsed = time.perf_counter() - t0
    logger.info("summarize done elapsed_s=%.2f parts=%s result_len=%s", elapsed, len(body.parts), len(result))
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

    logger.info(
        "check start bytes=%s content_type=%r paragraph_len=%s exercise=%r page=%s grade=%s textbook_len=%s "
        "gdz_ex_len=%s gdz_vp_len=%s gdz_vw_len=%s gdz_tc_len=%s",
        len(data),
        photo.content_type,
        len(paragraph.strip()),
        ex,
        page_val,
        grade_val,
        len(textbook_label.strip()),
        len(gdz_exercises.strip()),
        len(gdz_verif_pages.strip()),
        len(gdz_verif_works.strip()),
        len(gdz_task_condition.strip()),
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
        )
    except Exception:
        failed = True
        logger.exception("check_homework failed")
        raise
    finally:
        elapsed = time.perf_counter() - t0
        tgzh_metrics.observe_server_check(elapsed_seconds=elapsed, failed=failed)
    logger.info("check done elapsed_s=%.2f result_len=%s", elapsed, len(result))
    return CheckResponse(result=result)


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
