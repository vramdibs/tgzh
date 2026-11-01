"""Интеграционные тесты буфера документов в /chat (bot)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import bot
import chat_documents


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
        self.from_user = type("U", (), {"id": 42})()
        self.chat = type("C", (), {"id": chat_id})()
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


@pytest.mark.asyncio
async def test_append_with_caption_schedules_debounce_not_instant_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _FakeContext()
    msg = _FakeMessage()
    upd = _FakeUpdate(msg)
    flush = AsyncMock()
    monkeypatch.setattr(bot, "_chat_sources_flush_with_instruction", flush)
    monkeypatch.setattr(bot, "_chat_file_batch_debounce_sec", lambda: 0.01)

    await bot._chat_file_buffer_append(
        upd,
        ctx,
        user_id=42,
        chat_id=1,
        file_id="f1",
        filename="a.xlsx",
        mime=None,
        size=10,
        caption="задание для презентации",
        media_group_id=None,
    )

    flush.assert_not_awaited()
    assert bot._CHAT_FILE_BATCH_TASK in ctx.user_data
    task = ctx.user_data[bot._CHAT_FILE_BATCH_TASK]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_multi_file_debounce_triggers_presentation_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _FakeContext()
    presentation = AsyncMock()
    chat_msg = AsyncMock()
    monkeypatch.setattr(bot, "_chat_presentation_fill", presentation)
    monkeypatch.setattr(bot, "_handle_chat_user_message", chat_msg)
    monkeypatch.setattr(bot, "_chat_file_batch_debounce_sec", lambda: 0.0)

    async def _dl(_ctx, _fid: str) -> bytes:
        return b"data"

    monkeypatch.setattr(bot, "_chat_file_download_bytes", _dl)
    monkeypatch.setattr(
        chat_documents,
        "extract_chat_document_text_for_chat",
        AsyncMock(return_value="extracted"),
    )

    files = [
        ("f1", "report.xlsx", "задание презентации"),
        ("f2", "template.pptx", ""),
        ("f3", "reg.pdf", ""),
    ]
    mid = 10
    for fid, name, cap in files:
        msg = _FakeMessage()
        msg.message_id = mid
        mid += 1
        upd = _FakeUpdate(msg)
        await bot._chat_file_buffer_append(
            upd,
            ctx,
            user_id=42,
            chat_id=1,
            file_id=fid,
            filename=name,
            mime=None,
            size=100,
            caption=cap,
            media_group_id=None,
        )

    task = ctx.user_data.get(bot._CHAT_FILE_BATCH_TASK)
    assert task is not None
    await task

    presentation.assert_awaited_once()
    chat_msg.assert_not_awaited()
    assert presentation.await_args.kwargs["instruction"] == "задание презентации"
