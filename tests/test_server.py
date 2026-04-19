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


def test_check_rejects_unknown_engine(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    """`engine` валидируется до тяжёлой работы, чтобы опечатки не уходили в LLM."""
    monkeypatch.setenv("AI_MOCK", "1")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    r = client.post(
        "/check",
        files={"photo": ("hw.jpg", io.BytesIO(jpeg), "image/jpeg")},
        data={"paragraph": "1", "engine": "claude"},
    )
    assert r.status_code == 400
    assert "engine" in (r.json().get("detail") or "").lower()


def test_check_engine_cursor_routes_to_fallback(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """`engine=cursor` должен доходить до `check_homework(force_fallback=True)`."""
    monkeypatch.delenv("AI_MOCK", raising=False)
    monkeypatch.setenv("VLLM_BASE_URL", "http://primary:9/v1")

    captured: dict[str, object] = {}

    async def _fake_check_homework(_data: bytes, **kwargs: object) -> str:
        captured.update(kwargs)
        return "ok-from-fake"

    import server

    monkeypatch.setattr(server, "check_homework", _fake_check_homework)

    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    r = client.post(
        "/check",
        files={"photo": ("hw.jpg", io.BytesIO(jpeg), "image/jpeg")},
        data={"paragraph": "1", "engine": "cursor"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "ok-from-fake"
    assert captured.get("force_fallback") is True


def test_summarize_engine_cursor_propagates_force_fallback(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    captured: dict[str, object] = {}

    async def _fake_summarize(parts: list[str], *, force_fallback: bool = False) -> str:
        captured["parts_n"] = len(parts)
        captured["force_fallback"] = force_fallback
        return "summary-from-fake"

    import server

    monkeypatch.setattr(server, "summarize_check_parts", _fake_summarize)

    r = client.post(
        "/check/summarize",
        json={"parts": ["один", "два"], "engine": "cursor"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "summary-from-fake"
    assert captured == {"parts_n": 2, "force_fallback": True}


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


# ====================== /chat/once (sleep-вызов) ======================


def test_chat_once_rejects_empty_messages(client: TestClient) -> None:
    r = client.post("/chat/once", json={"user_id": 1, "messages": []})
    assert r.status_code == 400


def test_chat_once_rejects_too_many_messages(client: TestClient) -> None:
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(20)]
    r = client.post("/chat/once", json={"user_id": 1, "messages": msgs})
    assert r.status_code == 413


def test_chat_once_rejects_non_string_content(client: TestClient) -> None:
    r = client.post(
        "/chat/once",
        json={
            "user_id": 1,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "x"}],
                },
            ],
        },
    )
    # /chat/once запрещает multimodal — должен прийти 400.
    assert r.status_code == 400


def test_chat_once_requires_user_role(client: TestClient) -> None:
    r = client.post(
        "/chat/once",
        json={
            "user_id": 1,
            "messages": [{"role": "system", "content": "only-system"}],
        },
    )
    assert r.status_code == 400


def test_chat_once_returns_text_when_upstream_ok(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    async def _fake_once(messages, *, timeout_s=None):
        # Сервер должен прокинуть сюда нормализованные сообщения.
        assert any(m["role"] == "user" for m in messages)
        return "<<<FILE:MEMORY.md>>>\nidx\n<<<END>>>"

    import server

    monkeypatch.setattr(server, "chat_once_via_cursor", _fake_once)

    r = client.post(
        "/chat/once",
        json={
            "user_id": 1,
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "go"},
            ],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "<<<FILE:MEMORY.md>>>" in body["text"]
    assert body["reply_chars"] == len(body["text"])


def test_chat_once_maps_runtime_error_to_503(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    async def _raise_no_fb(messages, *, timeout_s=None):
        raise RuntimeError("vllm fallback not configured")

    import server

    monkeypatch.setattr(server, "chat_once_via_cursor", _raise_no_fb)

    r = client.post(
        "/chat/once",
        json={"user_id": 1, "messages": [{"role": "user", "content": "go"}]},
    )
    assert r.status_code == 503


def test_chat_once_maps_upstream_to_502(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    async def _raise(messages, *, timeout_s=None):
        raise ValueError("upstream broke")

    import server

    monkeypatch.setattr(server, "chat_once_via_cursor", _raise)

    r = client.post(
        "/chat/once",
        json={"user_id": 1, "messages": [{"role": "user", "content": "go"}]},
    )
    assert r.status_code == 502
    assert "upstream broke" in (r.json().get("detail") or "")
