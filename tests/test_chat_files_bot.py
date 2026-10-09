"""Интеграционные тесты буфера документов в /chat (bot)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import bot


class _FakeUserData(dict):
    pass


class _FakeContext:
    def __init__(self) -> None:
        self.user_data = _FakeUserData()
        self.bot = AsyncMock()


class _FakeMessage:
    def __init__(self, chat_id: int = 1, text: str = "") -> None:
        self.chat_id = chat_id
        self.message_id = 10
        self.text = text
        self.reply_text = AsyncMock()


class _FakeUpdate:
    def __init__(self, message: _FakeMessage) -> None:
        self.message = message
        self.effective_chat = type("C", (), {"id": message.chat_id})()
        self.effective_user = type("U", (), {"id": 42})()


def test_clear_chat_file_buffer() -> None:
    ctx = _FakeContext()
    ctx.user_data[bot._CHAT_FILE_BUFFER] = [{"file_id": "x"}]
    bot._clear_chat_file_buffer(ctx)
    assert bot._CHAT_FILE_BUFFER not in ctx.user_data


@pytest.mark.asyncio
async def test_flush_builds_compose_and_calls_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _FakeContext()
    msg = _FakeMessage()
    upd = _FakeUpdate(msg)
    ctx.user_data[bot._CHAT_FILE_BUFFER] = [
        {
            "file_id": "fid1",
            "filename": "t.txt",
            "mime": "text/plain",
            "size": 10,
        },
    ]

    async def _dl(_ctx, _fid: str) -> bytes:
        return b"line one"

    monkeypatch.setattr(bot, "_chat_file_download_bytes", _dl)
    handle = AsyncMock()
    monkeypatch.setattr(bot, "_handle_chat_user_message", handle)

    ok = await bot._chat_file_flush_with_instruction(
        upd,
        ctx,
        user_id=42,
        chat_id=1,
        instruction="сравни",
    )
    assert ok is True
    handle.assert_awaited_once()
    kw = handle.await_args.kwargs
    assert "<<<USER_FILE>>>" in kw["text"]
    assert kw["history_text"].startswith("[файлы:")
