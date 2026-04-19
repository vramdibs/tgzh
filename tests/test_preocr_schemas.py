"""Контракт Pydantic preocr API."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from preocr.schemas import PreOcrRegion, PreOcrResponse


def test_preocr_region_valid_types() -> None:
    for t in ("text", "latex", "figure"):
        r = PreOcrRegion(type=t, text="x", score=0.5, bbox=[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
        assert r.type == t


def test_preocr_region_invalid_type() -> None:
    with pytest.raises(ValidationError):
        PreOcrRegion(type="table", text="")  # type: ignore[arg-type]


def test_preocr_response_roundtrip() -> None:
    raw = {
        "pipeline": "ocr",
        "regions": [{"type": "text", "text": "hi", "score": None, "bbox": None}],
        "merged_markdown": "hi",
        "warnings": [],
    }
    m = PreOcrResponse.model_validate(raw)
    d = m.model_dump()
    assert d["pipeline"] == "ocr"
    assert len(d["regions"]) == 1
    assert d["merged_markdown"] == "hi"
