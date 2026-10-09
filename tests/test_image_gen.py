"""Юниты для `image_gen.generate_image` и хелперов конфигурации."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import image_gen


def test_is_image_gen_configured_requires_url_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert image_gen.is_image_gen_configured() is False
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    assert image_gen.is_image_gen_configured() is False
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "sk-test")
    assert image_gen.is_image_gen_configured() is True
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "   ")
    assert image_gen.is_image_gen_configured() is False


def test_image_gen_defaults_when_unset() -> None:
    assert image_gen.image_gen_model() == "gpt-image-1"
    assert image_gen.image_gen_size() == "1024x1024"
    assert image_gen.image_gen_timeout_sec() == 120.0


def test_image_gen_timeout_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_TIMEOUT_SEC", "3")
    assert image_gen.image_gen_timeout_sec() == 10.0
    monkeypatch.setenv("IMAGE_GEN_TIMEOUT_SEC", "9999")
    assert image_gen.image_gen_timeout_sec() == 600.0
    monkeypatch.setenv("IMAGE_GEN_TIMEOUT_SEC", "abc")
    assert image_gen.image_gen_timeout_sec() == 120.0


def test_generate_image_raises_when_not_configured() -> None:
    with pytest.raises(image_gen.ImageGenNotConfigured):
        asyncio.run(image_gen.generate_image("cat"))


def test_generate_image_rejects_empty_and_long_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    with pytest.raises(image_gen.ImageGenBadPrompt):
        asyncio.run(image_gen.generate_image(""))
    with pytest.raises(image_gen.ImageGenBadPrompt):
        asyncio.run(image_gen.generate_image("a" * (image_gen.PROMPT_MAX_LEN + 1)))


def test_generate_image_returns_b64_decoded_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    raw = b"\x89PNG\r\n\x1a\nfake-bytes"
    payload_b64 = base64.b64encode(raw).decode("ascii")
    fake_resp = SimpleNamespace(
        data=[SimpleNamespace(b64_json=payload_b64, url=None)],
    )

    captured: dict[str, object] = {}

    class FakeImages:
        async def generate(self, **kwargs):
            captured.update(kwargs)
            return fake_resp

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.images = FakeImages()

    with patch("openai.AsyncOpenAI", FakeClient):
        out = asyncio.run(image_gen.generate_image("cat sitting on a fence"))

    assert out == raw
    assert captured["model"] == "gpt-image-1"
    assert captured["size"] == "1024x1024"
    assert captured["n"] == 1
    assert captured["prompt"] == "cat sitting on a fence"
    # Не должен передавать response_format (gpt-image-1 не принимает).
    assert "response_format" not in captured


def test_generate_image_falls_back_to_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если бэкенд вернул только `url` (стиль dall-e-3) — модуль скачивает картинку."""
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    fake_resp = SimpleNamespace(
        data=[SimpleNamespace(b64_json=None, url="https://cdn.example/x.png")],
    )

    class FakeImages:
        async def generate(self, **kwargs):
            return fake_resp

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            self.images = FakeImages()

    fake_get_resp = SimpleNamespace(
        content=b"png-bytes-from-url",
        raise_for_status=lambda: None,
    )

    class FakeHttpxClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args, **kwargs):
            return False

        async def get(self, url):
            assert url == "https://cdn.example/x.png"
            return fake_get_resp

    with (
        patch("openai.AsyncOpenAI", FakeClient),
        patch("httpx.AsyncClient", FakeHttpxClient),
    ):
        out = asyncio.run(image_gen.generate_image("dog"))

    assert out == b"png-bytes-from-url"


def test_generate_image_raises_on_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    fake_resp = SimpleNamespace(data=[])

    class FakeImages:
        async def generate(self, **kwargs):
            return fake_resp

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            self.images = FakeImages()

    with patch("openai.AsyncOpenAI", FakeClient):
        with pytest.raises(RuntimeError, match="empty data"):
            asyncio.run(image_gen.generate_image("x"))


def test_generate_image_style_redraw_posts_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    style_file = tmp_path / "style.png"
    style_file.write_bytes(b"style-ref")
    monkeypatch.setenv("IMAGE_GEN_STYLE_REFERENCE_PATH", str(style_file))
    captured: dict = {}

    class FakeHttpxClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args, **kwargs):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return SimpleNamespace(
                status_code=200,
                text="",
                json=lambda: {"data": [{"b64_json": base64.b64encode(b"out-png").decode()}]},
            )

    with patch("httpx.AsyncClient", FakeHttpxClient):
        out = asyncio.run(
            image_gen.generate_image_style_redraw(b"photo", extra_prompt="notes"),
        )

    assert out == b"out-png"
    assert captured["url"] == "https://api.example/v1/images/generations"
    assert captured["json"]["source_image_b64"]
    assert captured["json"]["style_reference_b64"]
    assert captured["json"]["prompt"] == "notes"


def test_generate_image_propagates_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    monkeypatch.setenv("IMAGE_GEN_TIMEOUT_SEC", "10")

    class FakeImages:
        async def generate(self, **kwargs):
            await asyncio.sleep(5)  # перекроется через wait_for(timeout=10), но мокнем wait_for
            return SimpleNamespace(data=[])

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            self.images = FakeImages()

    async def fake_wait_for(coro, timeout):
        coro.close()  # отменим внутренний awaitable, чтобы не ругался pytest
        raise asyncio.TimeoutError

    with (
        patch("openai.AsyncOpenAI", FakeClient),
        patch("asyncio.wait_for", new=fake_wait_for),
    ):
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(image_gen.generate_image("x"))
