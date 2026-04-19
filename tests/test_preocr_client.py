"""Клиент предварительного OCR."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from preocr_client import fetch_preocr_block


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
