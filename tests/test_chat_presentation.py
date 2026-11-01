"""Тесты chat_presentation и извлечения xlsx/pptx."""

from __future__ import annotations

from pathlib import Path

import chat_documents
import chat_presentation

_FIX = Path(__file__).resolve().parent / "fixtures"


def test_extract_xlsx_fixture() -> None:
    data = (_FIX / "minimal_1fk.xlsx").read_bytes()
    text = chat_documents.extract_xlsx_text(data)
    assert "Раздел3" in text
    assert "41" in text


def test_extract_pptx_fixture() -> None:
    data = (_FIX / "minimal_template.pptx").read_bytes()
    text = chat_documents.extract_pptx_text(data)
    assert "Title slide" in text
    assert chat_documents.pptx_slide_count(data) == 1


def test_parse_1fk_metrics() -> None:
    data = (_FIX / "minimal_1fk.xlsx").read_bytes()
    metrics = chat_presentation.parse_1fk_workbook(data)
    assert metrics.get("facility_efficiency_pct") is not None
    assert metrics["sportspeople_total"] == 31553
    summary = chat_presentation.format_appendix1_metrics(metrics)
    assert "[сумма позже" in summary


def test_parse_slides_json_fenced() -> None:
    raw = '```json\n{"slides":[{"title":"A","bullets":["b"]}]}\n```'
    slides = chat_presentation.parse_slides_json(raw)
    assert slides[0]["title"] == "A"


def test_apply_slides_to_pptx() -> None:
    data = (_FIX / "minimal_template.pptx").read_bytes()
    out = chat_presentation.apply_slides_to_pptx(
        data,
        [{"title": "New title", "bullets": ["Line 1", "Line 2"]}],
    )
    text = chat_documents.extract_pptx_text(out)
    assert "New title" in text
    assert "Line 1" in text
