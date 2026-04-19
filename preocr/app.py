"""
HTTP API предварительного распознавания (кириллица, формулы, графики в режиме structure).
"""

from __future__ import annotations

import os

# Снижает задержку старта в контейнерах без доступа к зеркалам проверки моделей
os.environ.setdefault("DISABLE_MODEL_SOURCE_CHECK", "True")

from fastapi import FastAPI, File, HTTPException, UploadFile

from preocr.engine import run_preocr
from preocr.schemas import PreOcrResponse

app = FastAPI(
    title="tgzh-preocr",
    version="1.0.0",
    description="Предварительное OCR: PaddleOCR (ru) и опционально PP-StructureV3 (формулы, диаграммы). "
    "Контракт ответа — модель PreOcrResponse (regions, merged_markdown).",
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
    data = await image.read()
    if not data:
        raise HTTPException(status_code=400, detail="Пустой файл")
    payload = run_preocr(data)
    return PreOcrResponse.model_validate(payload)
