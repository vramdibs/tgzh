"""Клиент предварительного OCR."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

import preocr_client
from preocr_client import fetch_preocr_block, fetch_preocr_text, strip_gdz_watermark


@pytest.fixture(autouse=True)
def _reset_preocr_singleton() -> None:
    # Singleton AsyncClient переиспользуется между запросами,
    # тесты должны видеть свежий mock на каждом сценарии.
    preocr_client._client = None
    preocr_client._client_timeout = None


@pytest.mark.asyncio
async def test_fetch_preocr_empty_when_no_url() -> None:
    with patch.dict(os.environ, {"PREOCR_URL": ""}, clear=False):
        out = await fetch_preocr_block(image_bytes=b"x", content_type="image/jpeg")
    assert out == ""


@pytest.mark.asyncio
async def test_fetch_preocr_merges_body() -> None:
    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {"merged_markdown": "line1\nline2"}

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post = AsyncMock(return_value=FakeResp())

    with (
        patch.dict(os.environ, {"PREOCR_URL": "http://preocr:8088"}, clear=False),
        patch("preocr_client.httpx.AsyncClient", return_value=fake_client),
    ):
        out = await fetch_preocr_block(image_bytes=b"jpgbytes", content_type="image/jpeg")

    assert "line1" in out
    assert "line2" in out
    fake_client.post.assert_awaited_once()
    call_kw = fake_client.post.await_args
    assert call_kw[0][0] == "http://preocr:8088/v1/preocr"


@pytest.mark.asyncio
async def test_fetch_preocr_non_200() -> None:
    class FakeResp:
        status_code = 502
        text = "bad"

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post = AsyncMock(return_value=FakeResp())

    with (
        patch.dict(os.environ, {"PREOCR_URL": "http://p:1"}, clear=False),
        patch("preocr_client.httpx.AsyncClient", return_value=fake_client),
    ):
        out = await fetch_preocr_block(image_bytes=b"x", content_type="image/jpeg")
    assert out == ""


@pytest.mark.asyncio
async def test_fetch_preocr_invalid_json() -> None:
    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            raise ValueError("not json")

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post = AsyncMock(return_value=FakeResp())

    with (
        patch.dict(os.environ, {"PREOCR_URL": "http://p:1"}, clear=False),
        patch("preocr_client.httpx.AsyncClient", return_value=fake_client),
    ):
        out = await fetch_preocr_block(image_bytes=b"x", content_type="image/jpeg")
    assert out == ""


@pytest.mark.asyncio
async def test_fetch_preocr_empty_merged_markdown() -> None:
    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {"merged_markdown": "   "}

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post = AsyncMock(return_value=FakeResp())

    with (
        patch.dict(os.environ, {"PREOCR_URL": "http://p:1"}, clear=False),
        patch("preocr_client.httpx.AsyncClient", return_value=fake_client),
    ):
        out = await fetch_preocr_block(image_bytes=b"x", content_type="image/jpeg")
    assert out == ""


@pytest.mark.asyncio
async def test_fetch_preocr_truncates_body() -> None:
    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {"merged_markdown": "x" * 50}

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post = AsyncMock(return_value=FakeResp())

    with (
        patch.dict(os.environ, {"PREOCR_URL": "http://p:1"}, clear=False),
        patch("preocr_client.httpx.AsyncClient", return_value=fake_client),
        patch("preocr_client._max_chars", return_value=12),
    ):
        out = await fetch_preocr_block(image_bytes=b"x", content_type="image/jpeg")
    assert "..." in out
    assert "x" * 9 in out
    assert "x" * 12 not in out.replace("...", "")


@pytest.mark.asyncio
async def test_fetch_preocr_custom_template_no_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_PROMPT_PREOCR_BLOCK", "Prefix only")

    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {"merged_markdown": "bodytext"}

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post = AsyncMock(return_value=FakeResp())

    with (
        patch.dict(os.environ, {"PREOCR_URL": "http://p:1"}, clear=False),
        patch("preocr_client.httpx.AsyncClient", return_value=fake_client),
    ):
        out = await fetch_preocr_block(image_bytes=b"x", content_type="image/jpeg")
    assert "Prefix only" in out
    assert "bodytext" in out


def test_strip_gdz_watermark_latin_variants() -> None:
    src = "Ответ: 12 gdz.ru\nрешение GDZ.RU тут\nверсия gdz ru и gdzru."
    out = strip_gdz_watermark(src)
    assert "gdz" not in out.lower()
    assert "Ответ: 12" in out
    assert "решение" in out
    assert "тут" in out
    assert "версия" in out
    assert "и" in out
    assert "." in out


def test_strip_gdz_watermark_cyrillic_variants() -> None:
    src = "гдз.ру по матике\nГДЗ.РУ\nгдз ру\nгдзру"
    out = strip_gdz_watermark(src)
    low = out.lower()
    assert "гдз" not in low
    assert "по матике" in out


def test_strip_gdz_watermark_does_not_touch_math() -> None:
    src = "1+1=2\nx_2 + y^3 = z\nответ: 4/5"
    assert strip_gdz_watermark(src) == "1+1=2\nx_2 + y^3 = z\nответ: 4/5"


def test_strip_gdz_watermark_collapses_blank_lines() -> None:
    src = "abc\n\ngdz.ru\n\ndef"
    out = strip_gdz_watermark(src)
    assert out == "abc\n\ndef"


@pytest.mark.asyncio
async def test_fetch_preocr_strips_watermark_from_body() -> None:
    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {"merged_markdown": "ответ ученика 7  gdz.ru\nстрока 2 ГДЗ.РУ"}

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post = AsyncMock(return_value=FakeResp())

    with (
        patch.dict(os.environ, {"PREOCR_URL": "http://p:1"}, clear=False),
        patch("preocr_client.httpx.AsyncClient", return_value=fake_client),
    ):
        out = await fetch_preocr_block(image_bytes=b"x", content_type="image/jpeg")
    assert "gdz" not in out.lower()
    assert "ответ ученика 7" in out
    assert "строка 2" in out


@pytest.mark.asyncio
async def test_fetch_preocr_text_returns_raw_body_without_template() -> None:
    """`fetch_preocr_text` — «сырой» OCR без обёртки-инструкции `VLLM_PROMPT_PREOCR_BLOCK`."""

    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {"merged_markdown": "только распознанный текст"}

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post = AsyncMock(return_value=FakeResp())

    with (
        patch.dict(
            os.environ,
            {
                "PREOCR_URL": "http://p:1",
                # Шаблон задан, но в text-варианте он не должен примешиваться.
                "VLLM_PROMPT_PREOCR_BLOCK": "Не должно появиться: {body}",
            },
            clear=False,
        ),
        patch("preocr_client.httpx.AsyncClient", return_value=fake_client),
    ):
        out = await fetch_preocr_text(image_bytes=b"x", content_type="image/jpeg")
    assert out == "только распознанный текст"
    assert "Не должно появиться" not in out


@pytest.mark.asyncio
async def test_fetch_preocr_text_strips_watermark() -> None:
    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {"merged_markdown": "ответ 7 gdz.ru\nстрока ГДЗ.РУ"}

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post = AsyncMock(return_value=FakeResp())

    with (
        patch.dict(os.environ, {"PREOCR_URL": "http://p:1"}, clear=False),
        patch("preocr_client.httpx.AsyncClient", return_value=fake_client),
    ):
        out = await fetch_preocr_text(image_bytes=b"x", content_type="image/jpeg")
    assert "gdz" not in out.lower()
    assert "ответ 7" in out
    assert "строка" in out


@pytest.mark.asyncio
async def test_fetch_preocr_text_empty_when_no_url() -> None:
    with patch.dict(os.environ, {"PREOCR_URL": ""}, clear=False):
        out = await fetch_preocr_text(image_bytes=b"x", content_type="image/jpeg")
    assert out == ""


@pytest.mark.asyncio
async def test_fetch_preocr_uses_png_filename_for_png_content_type() -> None:
    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {"merged_markdown": "x"}

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post = AsyncMock(return_value=FakeResp())

    with (
        patch.dict(os.environ, {"PREOCR_URL": "http://p:1"}, clear=False),
        patch("preocr_client.httpx.AsyncClient", return_value=fake_client),
    ):
        await fetch_preocr_block(image_bytes=b"x", content_type="image/png")

    files = fake_client.post.await_args.kwargs["files"]
    assert files["image"][0] == "photo.png"
