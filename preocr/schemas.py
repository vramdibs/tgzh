"""
JSON-контракт HTTP API предварительного OCR (для интеграции с tgzh-server).

OpenAPI генерируется FastAPI из этих моделей (см. preocr.app).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RegionType = Literal["text", "latex", "figure"]


class PreOcrRegion(BaseModel):
    """Один логический фрагмент страницы."""

    type: RegionType = Field(description="text — строки текста; latex — формула (LaTeX); figure — рисунок/график/диаграмма")
    text: str = Field(default="", description="Распознанный текст или LaTeX; для figure — краткое описание или плейсхолдер")
    score: float | None = Field(default=None, description="Уверенность распознавания текста, 0..1, если известна")
    bbox: list[list[float]] | None = Field(
        default=None,
        description="Четырехугольник в координатах изображения [[x,y],...] или null",
    )


class PreOcrResponse(BaseModel):
    """Ответ POST /v1/preocr."""

    pipeline: str = Field(description="Использованный режим: ocr | structure")
    regions: list[PreOcrRegion] = Field(default_factory=list)
    merged_markdown: str = Field(
        default="",
        description="Склейка для подсказки VLM: текст и $$...$$ для формул (structure) или строки (ocr)",
    )
    warnings: list[str] = Field(default_factory=list, description="Нефатальные замечания (таймауты подмоделей и т.п.)")
