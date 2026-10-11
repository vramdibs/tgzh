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

    async def _fake_summarize(
        parts: list[str],
        *,
        force_fallback: bool = False,
        subject_slug: str = "matematika",
    ) -> str:
        captured["parts_n"] = len(parts)
        captured["force_fallback"] = force_fallback
        captured["subject_slug"] = subject_slug
        return "summary-from-fake"

    import server

    monkeypatch.setattr(server, "summarize_check_parts", _fake_summarize)

    r = client.post(
        "/check/summarize",
        json={"parts": ["один", "два"], "engine": "cursor"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "summary-from-fake"
    assert captured == {
        "parts_n": 2,
        "force_fallback": True,
        "subject_slug": "matematika",
    }


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


def test_photo_check_requires_images(client: TestClient) -> None:
    r = client.post("/photo/check", data={"mode": "single_album"})
    assert r.status_code == 422


def test_image_generate_returns_503_when_not_configured(client: TestClient) -> None:
    r = client.post("/image/generate", json={"user_id": 1, "prompt": "cat"})
    assert r.status_code == 503
    assert "image generation backend not configured" in (r.json().get("detail") or "")


def test_image_generate_rejects_empty_prompt(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    r = client.post("/image/generate", json={"user_id": 1, "prompt": "   "})
    assert r.status_code == 400


def test_image_generate_rejects_too_long_prompt(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    import image_gen

    r = client.post(
        "/image/generate",
        json={"user_id": 1, "prompt": "a" * (image_gen.PROMPT_MAX_LEN + 1)},
    )
    assert r.status_code == 400


def test_image_generate_style_redraw_requires_source(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    r = client.post(
        "/image/generate",
        json={"user_id": 1, "mode": "style_redraw", "prompt": ""},
    )
    assert r.status_code == 400


def test_image_generate_style_redraw_ok(
    monkeypatch: pytest.MonkeyPatch, client: TestClient,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    import base64

    import image_gen

    async def _fake_style(source_bytes: bytes, extra_prompt: str = "") -> bytes:  # noqa: ARG001
        assert source_bytes == b"jpeg-bytes"
        assert extra_prompt == "evening"
        return b"png-out"

    monkeypatch.setattr(image_gen, "generate_image_style_redraw", _fake_style)
    b64 = base64.b64encode(b"jpeg-bytes").decode("ascii")
    r = client.post(
        "/image/generate",
        json={
            "user_id": 1,
            "mode": "style_redraw",
            "prompt": "evening",
            "source_image_b64": b64,
        },
    )
    assert r.status_code == 200
    assert r.content == b"png-out"


def test_image_generate_style_redraw_empty_prompt_ok(
    monkeypatch: pytest.MonkeyPatch, client: TestClient,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    import base64

    import image_gen

    async def _fake_style(source_bytes: bytes, extra_prompt: str = "") -> bytes:  # noqa: ARG001
        assert extra_prompt == ""
        return b"png-out"

    monkeypatch.setattr(image_gen, "generate_image_style_redraw", _fake_style)
    b64 = base64.b64encode(b"jpeg-bytes").decode("ascii")
    r = client.post(
        "/image/generate",
        json={
            "user_id": 1,
            "mode": "style_redraw",
            "prompt": "",
            "source_image_b64": b64,
        },
    )
    assert r.status_code == 200
    assert r.content == b"png-out"


def test_photo_check_disabled(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setenv("PHOTO_CHECK_ENABLE", "0")
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    r = client.post(
        "/photo/check",
        files=[("images", ("a.jpg", io.BytesIO(jpeg), "image/jpeg"))],
        data={"mode": "single_album", "image_roles": "mixed"},
    )
    assert r.status_code == 503


def test_photo_check_mock(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setenv("AI_MOCK", "1")
    monkeypatch.setenv("PHOTO_CHECK_ENABLE", "1")
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    r = client.post(
        "/photo/check",
        files=[("images", ("a.jpg", io.BytesIO(jpeg), "image/jpeg"))],
        data={"mode": "single_album", "image_roles": "mixed"},
    )
    assert r.status_code == 200
    assert "result" in r.json()
    assert "/photo" in r.json()["result"] or "mock" in r.json()["result"].lower()


async def _fake_generate_image(prompt: str) -> bytes:
    import image_gen

    image_gen.record_image_gen_usage(None, prompt=prompt)
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def test_image_generate_records_llm_spend(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import image_gen

    monkeypatch.setattr(image_gen, "is_image_gen_configured", lambda: True)
    monkeypatch.setattr(image_gen, "generate_image", _fake_generate_image)
    recorded: list[dict] = []

    def _capture(**kw: object) -> None:
        recorded.append(dict(kw))

    monkeypatch.setattr("server._record_llm_spend", _capture)
    r = client.post(
        "/image/generate",
        json={"user_id": 42, "prompt": "тестовая картинка", "mode": "prompt"},
    )
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("image/png")
    assert recorded == [{"kind": "image_gen", "user_id": 42}]


def test_image_generate_returns_429_when_slot_busy(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import image_gen
    import server as server_mod
    from fastapi import HTTPException

    monkeypatch.setattr(image_gen, "is_image_gen_configured", lambda: True)

    async def _busy() -> None:
        raise HTTPException(429, "image generation already in progress")

    monkeypatch.setattr(server_mod, "_acquire_image_gen_slot", _busy)
    r = client.post(
        "/image/generate",
        json={"user_id": 2, "prompt": "b", "mode": "prompt"},
    )
    assert r.status_code == 429


def test_image_generate_records_llm_token_usage_via_spend(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    import image_gen

    monkeypatch.setattr(image_gen, "is_image_gen_configured", lambda: True)
    monkeypatch.setattr(image_gen, "generate_image", _fake_generate_image)
    monkeypatch.setenv("AI_MOCK", "0")
    monkeypatch.setenv("USER_DB_PATH", "/tmp/tgzh-test-llm-metrics.db")
    spend_calls: list[dict] = []

    def _spy(path: str, **kw: object) -> None:
        spend_calls.append({"path": path, **kw})

    monkeypatch.setattr("bot_stats.record_llm_spend", _spy)
    r = client.post(
        "/image/generate",
        json={"user_id": 7, "prompt": "circle", "mode": "prompt"},
    )
    assert r.status_code == 200
    assert spend_calls
    last = spend_calls[-1]
    assert last.get("kind") == "image_gen"
    assert int(last.get("prompt_tokens") or 0) >= 1
    assert int(last.get("completion_tokens") or 0) >= 1


