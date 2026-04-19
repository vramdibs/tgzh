"""Юнит-тесты preocr.engine без загрузки PaddleOCR."""

from __future__ import annotations

import io
import os
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from preocr.engine import (
    _bbox_from_poly,
    _map_block_label,
    _max_side,
    _pipeline_name,
    _prepare_image_bytes,
    run_ocr,
    run_preocr,
    run_structure,
)


def test_max_side_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PREOCR_MAX_SIDE", raising=False)
    assert _max_side() == 1024


def test_max_side_custom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREOCR_MAX_SIDE", "1536")
    assert _max_side() == 1536


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("100", 256),
        ("5000", 4096),
        ("abc", 1024),
    ],
)
def test_max_side_clamp_and_invalid(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    monkeypatch.setenv("PREOCR_MAX_SIDE", raw)
    assert _max_side() == expected


def test_pipeline_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PREOCR_PIPELINE", raising=False)
    assert _pipeline_name() == "ocr"
    monkeypatch.setenv("PREOCR_PIPELINE", "STRUCTURE")
    assert _pipeline_name() == "structure"
    monkeypatch.setenv("PREOCR_PIPELINE", "  structure  ")
    assert _pipeline_name() == "structure"


def test_map_block_label() -> None:
    assert _map_block_label("formula") == "latex"
    assert _map_block_label("image") == "figure"
    assert _map_block_label("chart") == "figure"
    assert _map_block_label("text") == "text"
    assert _map_block_label("paragraph_title") == "text"


def test_bbox_from_poly_none_and_valid() -> None:
    assert _bbox_from_poly(None) is None
    poly = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=float)
    out = _bbox_from_poly(poly)
    assert out is not None
    assert len(out) == 4
    assert all(len(p) == 2 for p in out)


def test_bbox_from_poly_too_few_points() -> None:
    tri = np.array([[0, 0], [1, 0], [1, 1]], dtype=float)
    assert _bbox_from_poly(tri) is None


def test_bbox_from_poly_invalid() -> None:
    assert _bbox_from_poly("bad") is None


def test_prepare_image_bytes_respects_max_side(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREOCR_MAX_SIDE", "400")
    im = Image.new("RGB", (800, 200), color=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    path, w, h = _prepare_image_bytes(buf.getvalue())
    try:
        assert max(w, h) <= 400
        assert os.path.isfile(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_run_ocr_empty_predict() -> None:
    class _Fake:
        def predict(self, *_a, **_k):
            return []

    with patch("preocr.engine._get_ocr", return_value=_Fake()):
        d = run_ocr("/tmp/nonexistent.png")
    assert d["pipeline"] == "ocr"
    assert d["merged_markdown"] == ""
    assert d["warnings"]


def test_run_ocr_parses_lines() -> None:
    class _Row(dict):
        pass

    row = _Row(rec_texts=["  a  ", "", "b"], rec_scores=[0.9, 0.8], rec_polys=[])

    class _Fake:
        def predict(self, *_a, **_k):
            return [row]

    with patch("preocr.engine._get_ocr", return_value=_Fake()):
        d = run_ocr("/x.png")
    assert d["merged_markdown"] == "a\nb"
    assert len(d["regions"]) == 2
    assert d["regions"][0]["text"] == "a"
    assert d["regions"][1]["text"] == "b"


def test_run_ocr_bad_result_shape() -> None:
    class _Fake:
        def predict(self, *_a, **_k):
            return [{"not_rec": True}]

    with patch("preocr.engine._get_ocr", return_value=_Fake()):
        d = run_ocr("/x.png")
    assert d["regions"] == []
    assert d["warnings"]


def test_run_structure_predict_raises() -> None:
    def _boom() -> object:
        raise RuntimeError("no gpu")

    with patch("preocr.engine._get_structure", side_effect=_boom):
        d = run_structure("/x.png")
    assert d["pipeline"] == "structure"
    assert "structure" in d["warnings"][0].lower() or "no gpu" in d["warnings"][0]


def test_run_preocr_routes_to_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREOCR_PIPELINE", "structure")
    calls: list[str] = []

    def _prep(_b: bytes) -> tuple[str, int, int]:
        path = "/fake/path.png"
        calls.append("prep")
        return path, 10, 10

    def _rs(p: str) -> dict:
        calls.append(f"structure:{p}")
        return {"pipeline": "structure", "regions": [], "merged_markdown": "m", "warnings": []}

    im = Image.new("RGB", (50, 50), color="white")
    buf = io.BytesIO()
    im.save(buf, format="PNG")

    with (
        patch("preocr.engine._prepare_image_bytes", _prep),
        patch("preocr.engine.run_structure", _rs),
        patch("preocr.engine.os.unlink") as mock_unlink,
    ):
        d = run_preocr(buf.getvalue())

    assert d["merged_markdown"] == "m"
    assert any("structure:/fake/path.png" == c for c in calls)
    mock_unlink.assert_called_once_with("/fake/path.png")


def test_run_preocr_routes_to_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PREOCR_PIPELINE", raising=False)

    def _prep(_b: bytes) -> tuple[str, int, int]:
        return "/p.png", 1, 1

    def _ro(p: str) -> dict:
        return {"pipeline": "ocr", "regions": [], "merged_markdown": "o", "warnings": []}

    im = Image.new("RGB", (10, 10), color="white")
    buf = io.BytesIO()
    im.save(buf, format="PNG")

    with (
        patch("preocr.engine._prepare_image_bytes", _prep),
        patch("preocr.engine.run_ocr", _ro),
        patch("preocr.engine.os.unlink"),
    ):
        d = run_preocr(buf.getvalue())
    assert d["merged_markdown"] == "o"
