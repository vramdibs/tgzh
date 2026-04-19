"""
HTTP API предварительного распознавания (кириллица, формулы, графики в режиме structure).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

# Снижает задержку старта в контейнерах без доступа к зеркалам проверки моделей
os.environ.setdefault("DISABLE_MODEL_SOURCE_CHECK", "True")

from fastapi import FastAPI, File, HTTPException, UploadFile

from preocr.engine import run_preocr, warm_up
from preocr.schemas import PreOcrResponse

logger = logging.getLogger(__name__)


def _max_upload_bytes() -> int:
    raw = (os.getenv("PREOCR_MAX_UPLOAD_BYTES") or "").strip()
    if raw.isdigit():
        return max(64 * 1024, min(64 * 1024 * 1024, int(raw)))
    return 16 * 1024 * 1024


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        await asyncio.to_thread(warm_up)
        logger.info("preocr warm_up ok")
    except Exception:
        logger.exception("preocr warm_up failed (will retry on first request)")
    yield


app = FastAPI(
    title="tgzh-preocr",
    version="1.0.0",
    description="Предварительное OCR: PaddleOCR (ru) и опционально PP-StructureV3 (формулы, диаграммы). "
    "Контракт ответа — модель PreOcrResponse (regions, merged_markdown).",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/preocr", response_model=PreOcrResponse)
async def preocr_endpoint(image: UploadFile = File(...)) -> PreOcrResponse:
    ct = (image.content_type or "").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail="Ожидается изображение (image/jpeg, image/png, image/webp)",
        )
    cap = _max_upload_bytes()
    data = await image.read(cap + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Пустой файл")
    if len(data) > cap:
        raise HTTPException(status_code=413, detail=f"Файл больше лимита {cap} байт")
    try:
        payload = await asyncio.to_thread(run_preocr, data)
    except Exception:
        logger.exception("run_preocr failed")
        raise HTTPException(status_code=500, detail="preocr engine failed")
    return PreOcrResponse.model_validate(payload)
