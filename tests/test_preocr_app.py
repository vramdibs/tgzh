"""HTTP-слой preocr (FastAPI)."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    from preocr.app import app

    return TestClient(app)


def test_preocr_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_preocr_rejects_non_image(client: TestClient) -> None:
    r = client.post(
        "/v1/preocr",
        files={"image": ("x.bin", b"abc", "application/octet-stream")},
    )
    assert r.status_code == 400


def test_preocr_rejects_empty_file(client: TestClient) -> None:
    r = client.post(
        "/v1/preocr",
        files={"image": ("e.jpg", b"", "image/jpeg")},
    )
    assert r.status_code == 400


def test_preocr_ok_mocked_engine(client: TestClient) -> None:
    payload = {
        "pipeline": "ocr",
        "regions": [{"type": "text", "text": "ok", "score": 0.9, "bbox": None}],
        "merged_markdown": "ok",
        "warnings": [],
    }
    png = BytesIO()
    from PIL import Image

    Image.new("RGB", (8, 8), color="white").save(png, format="PNG")
    png.seek(0)

    with patch("preocr.app.run_preocr", return_value=payload):
        r = client.post(
            "/v1/preocr",
            files={"image": ("t.png", png.getvalue(), "image/png")},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["pipeline"] == "ocr"
    assert data["merged_markdown"] == "ok"
    assert len(data["regions"]) == 1
