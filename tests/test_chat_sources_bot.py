"""Интеграция буфера файлов и ссылок в /chat."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import bot
import chat_documents
import chat_urls


class _FakeUserData(dict):
    pass


class _FakeContext:
    def __init__(self) -> None:
        self.user_data = _FakeUserData()
        self.bot = AsyncMock()


class _FakeMessage:
    def __init__(self) -> None:
        self.chat_id = 1
        self.message_id = 7
        self.reply_text = AsyncMock()


class _FakeUpdate:
    def __init__(self) -> None:
        self.message = _FakeMessage()
        self.effective_chat = type("C", (), {"id": 1})()
        self.effective_user = type("U", (), {"id": 99})()


@pytest.mark.asyncio
async def test_sources_try_handle_urls_with_instruction(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _FakeContext()
    upd = _FakeUpdate()
    flush = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, "_chat_sources_flush_with_instruction", flush)
    text = "https://a.example/t https://b.example/t сравни тарифы"
    handled = await bot._chat_sources_try_handle(
        upd,
        ctx,
        user_id=99,
        chat_id=1,
        text=text,
    )
    assert handled is True
    flush.assert_awaited_once()
    assert flush.await_args.kwargs["instruction"] == "сравни тарифы"


@pytest.mark.asyncio
async def test_sources_flush_merges_file_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _FakeContext()
    upd = _FakeUpdate()
    ctx.user_data[bot._CHAT_FILE_BUFFER] = [
        {
            "file_id": "fid",
            "filename": "t.txt",
            "mime": "text/plain",
            "size": 5,
        },
    ]
    ctx.user_data[bot._CHAT_URL_BUFFER] = ["https://example.com/page"]

    async def _dl(_ctx, _fid: str) -> bytes:
        return b"file body"

    monkeypatch.setattr(bot, "_chat_file_download_bytes", _dl)
    monkeypatch.setattr(
        chat_urls,
        "fetch_url_text",
        AsyncMock(return_value=chat_urls.ChatUrlExtract("https://example.com/page", "url body")),
    )
    handle = AsyncMock()
    monkeypatch.setattr(bot, "_handle_chat_user_message", handle)

    ok = await bot._chat_sources_flush_with_instruction(
        upd,
        ctx,
        user_id=99,
        chat_id=1,
        instruction="сравни",
    )
    assert ok is True
    handle.assert_awaited_once()
    body = handle.await_args.kwargs["text"]
    assert "file body" in body
    assert "url body" in body
    assert handle.await_args.kwargs["history_text"].startswith("[")
