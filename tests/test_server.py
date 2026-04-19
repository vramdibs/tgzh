"""HTTP API сервера проверки."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from server import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health_ok(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_check_rejects_bad_mime(client: TestClient) -> None:
    r = client.post(
        "/check",
        files={"photo": ("x.bin", b"abc", "application/octet-stream")},
        data={"paragraph": "1"},
    )
    assert r.status_code == 400


def test_summarize_requires_two_parts(client: TestClient) -> None:
    r = client.post("/check/summarize", json={"parts": ["only"]})
    # Pydantic min_length=2 на SummarizeRequest.parts — отдает 422 ещё до тела хэндлера.
    assert r.status_code == 422


def test_quip_mock(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setenv("AI_MOCK", "1")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    r = client.post("/check/quip", json={"excerpt": "тест"})
    assert r.status_code == 200
    assert "result" in r.json()
    assert len((r.json().get("result") or "").strip()) > 5


def test_summarize_mock(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setenv("AI_MOCK", "1")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    r = client.post(
        "/check/summarize",
        json={"parts": ["Фрагмент один.", "Фрагмент два."]},
    )
    assert r.status_code == 200
    body = r.json()
    assert "result" in body
    assert "[Тестовый режим]" in body["result"] or "Сводка" in body["result"]
    assert "Модель:" in body["result"]


def test_check_accepts_jpeg_mock(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setenv("AI_MOCK", "1")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    # Перечитать поведение check_homework в уже импортированном модуле не требуется:
    # check_homework читает os.environ при каждом вызове
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    r = client.post(
        "/check",
        files={"photo": ("hw.jpg", io.BytesIO(jpeg), "image/jpeg")},
        data={
            "paragraph": "1",
            "exercise": "5",
            "textbook_label": "Demo",
            "grade": "6",
            "gdz_verif_works": "Проверочная A — item 1",
            "gdz_task_condition": "Условие: найти простые числа.",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "result" in body
    assert "[Тестовый режим]" in body["result"] or "Получено" in body["result"]
    assert "Проверочная A" in body["result"]
    assert "простые" in body["result"]
    assert "Модель:" in body["result"]


