"""Чат-режим `/chat`: фото уходит в Cursor напрямую (multimodal, без pre-OCR).

Проверяется как «настоящий» путь (`_handle_chat_user_message` сохраняет в историю
текстовый плейсхолдер вместо base64), так и legacy-обёртка `_compose_chat_photo_message`,
оставленная для возможного отката на режим OCR.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import bot


# ---------- legacy: _compose_chat_photo_message (оставлена в коде как утилита) ----


def test_compose_chat_photo_message_with_caption_and_ocr() -> None:
    out = bot._compose_chat_photo_message(
        caption="Что это за цветок?",
        ocr_text="(на фото нет текста)",
    )
    assert "Что это за цветок?" in out
    assert "(на фото нет текста)" in out
    assert "OCR" in out
    assert "не вижу" in out.lower() or "визуальные" in out.lower()


def test_compose_chat_photo_message_default_question_when_no_caption() -> None:
    out = bot._compose_chat_photo_message(
        caption="",
        ocr_text="x + 5 = 12",
    )
    assert "x + 5 = 12" in out
    assert "Опиши, что распознано на фото" in out


def test_compose_chat_photo_message_empty_ocr_branch() -> None:
    out = bot._compose_chat_photo_message(
        caption="что тут?",
        ocr_text="   ",
    )
    assert "что тут?" in out
    assert "OCR не нашёл" in out or "не нашёл текста" in out
    assert "только с текстом" in out


def test_compose_chat_photo_message_truncates_long_ocr() -> None:
    huge = "А" * 10_000
    out = bot._compose_chat_photo_message(caption="?", ocr_text=huge)
    assert len(out) <= 7990 + 1
    assert "…" in out


def test_compose_chat_photo_message_strips_caption_whitespace() -> None:
    out = bot._compose_chat_photo_message(
        caption="   как решить?   ",
        ocr_text="2+2",
    )
    assert "как решить?" in out
    assert "   как решить?" not in out


# ---------- активный путь: _handle_chat_user_message с image_b64 ------------------


class _FakeUserData(dict):
    pass


class _FakeContext:
    def __init__(self) -> None:
        self.user_data = _FakeUserData()
        self.bot = object()


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **_kw):  # noqa: D401
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self) -> None:
        self.message = _FakeMessage()


def test_handle_chat_user_message_with_image_stores_placeholder_history() -> None:
    """С картинкой история не должна содержать base64 — только текст-плейсхолдер."""
    update = _FakeUpdate()
    ctx = _FakeContext()

    async def _fake_stream(*_a, **kw):
        # Подтверждаем, что image_b64 действительно дотекает до streamer'а.
        assert kw.get("image_b64") == "BASE64DATA"
        assert kw.get("image_mime") == "image/jpeg"
        return "ответ модели"

    with (
        patch.object(bot, "_stream_chat_response", side_effect=_fake_stream),
        patch.object(bot.user_storage, "chat_dialog_upsert", return_value=42),
    ):
        asyncio.run(
            bot._handle_chat_user_message(
                update,
                ctx,
                user_id=1,
                chat_id=1,
                text="Что это за цветок?",
                image_b64="BASE64DATA",
                image_mime="image/jpeg",
                history_text="[фото: Что это за цветок?]",
            ),
        )

    history = ctx.user_data[bot._CHAT_HISTORY]
    assert history[-2] == {"role": "user", "content": "[фото: Что это за цветок?]"}
    assert history[-1] == {"role": "assistant", "content": "ответ модели"}
    # Никаких base64 в истории — она целиком из текстовых строк.
    assert "BASE64DATA" not in history[-2]["content"]
    assert ctx.user_data[bot._CHAT_DIALOG_ID] == 42


def test_handle_chat_user_message_with_image_uses_default_placeholder_when_no_caption() -> None:
    update = _FakeUpdate()
    ctx = _FakeContext()

    async def _fake_stream(*_a, **_kw):
        return "ok"

    with (
        patch.object(bot, "_stream_chat_response", side_effect=_fake_stream),
        patch.object(bot.user_storage, "chat_dialog_upsert", return_value=1),
    ):
        asyncio.run(
            bot._handle_chat_user_message(
                update,
                ctx,
                user_id=1,
                chat_id=1,
                text=bot._CHAT_PHOTO_DEFAULT_CAPTION,
                image_b64="x",
                image_mime="image/jpeg",
                history_text="",  # пусто → ожидаем дефолтный плейсхолдер
            ),
        )

    history = ctx.user_data[bot._CHAT_HISTORY]
    assert history[-2]["content"] == "[фото без подписи]"


def test_handle_chat_user_message_with_image_skips_empty_text_check() -> None:
    """Без image_b64 пустой текст блокируется. С image_b64 — пропускается."""
    update = _FakeUpdate()
    ctx = _FakeContext()

    async def _fake_stream(*_a, **_kw):
        return "ok"

    with (
        patch.object(bot, "_stream_chat_response", side_effect=_fake_stream),
        patch.object(bot.user_storage, "chat_dialog_upsert", return_value=7),
    ):
        # Без картинки — пустой текст должен дать reply_text об ошибке и НЕ дойти до stream.
        asyncio.run(
            bot._handle_chat_user_message(
                update, ctx, user_id=1, chat_id=1, text="   ",
            ),
        )
    assert any("Пустое сообщение" in r for r in update.message.replies)
    assert bot._CHAT_HISTORY not in ctx.user_data

    # Теперь с картинкой — пустой текст разрешён, история заполнена плейсхолдером.
    ctx2 = _FakeContext()
    with (
        patch.object(bot, "_stream_chat_response", side_effect=_fake_stream),
        patch.object(bot.user_storage, "chat_dialog_upsert", return_value=8),
    ):
        asyncio.run(
            bot._handle_chat_user_message(
                _FakeUpdate(),
                ctx2,
                user_id=1,
                chat_id=1,
                text="",
                image_b64="DATA",
                image_mime="image/jpeg",
                history_text="[фото без подписи]",
            ),
        )
    assert ctx2.user_data[bot._CHAT_HISTORY][-2]["content"] == "[фото без подписи]"


# ---------- _stream_chat_response: проверим, что в payload уходит image_url ------


def test_stream_chat_response_builds_multimodal_user_message_when_image_present() -> None:
    """Главное: сервер получает `content` как list-of-parts с image_url(data:...)."""

    captured_payloads: list[dict] = []

    class _RespOK:
        status_code = 200

        async def aread(self) -> bytes:  # pragma: no cover - не должен зваться
            return b""

        def aiter_text(self):
            async def _gen():
                yield "финальный ответ"
            return _gen()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeClient:
        def __init__(self, *a, **kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, json=None, **kw):  # noqa: A002
            captured_payloads.append(json or {})
            return _RespOK()

    class _FakePlace:
        message_id = 100

    class _FakeBot:
        async def send_message(self, *a, **kw):
            return _FakePlace()

        async def edit_message_text(self, *a, **kw):
            return None

    ctx = _FakeContext()
    ctx.bot = _FakeBot()

    with (
        patch.object(bot.httpx, "AsyncClient", _FakeClient),
        patch.object(
            bot,
            "async_http_transport_ipv4_lookup",
            return_value=None,
        ),
    ):
        result = asyncio.run(
            bot._stream_chat_response(
                ctx,
                chat_id=1,
                user_id=1,
                history=[],
                user_text="Что это за цветок?",
                image_b64="QUJD",  # base64("ABC")
                image_mime="image/jpeg",
            ),
        )

    assert result == "финальный ответ"
    assert captured_payloads, "POST /chat/stream должен быть вызван"
    payload = captured_payloads[0]
    last_msg = payload["messages"][-1]
    assert last_msg["role"] == "user"
    parts = last_msg["content"]
    assert isinstance(parts, list)
    types = [p["type"] for p in parts]
    assert types == ["text", "image_url"]
    assert parts[0]["text"] == "Что это за цветок?"
    assert parts[1]["image_url"]["url"] == "data:image/jpeg;base64,QUJD"


def test_stream_chat_response_keeps_string_content_when_no_image() -> None:
    captured_payloads: list[dict] = []

    class _RespOK:
        status_code = 200

        async def aread(self) -> bytes:
            return b""

        def aiter_text(self):
            async def _gen():
                yield "ok"
            return _gen()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeClient:
        def __init__(self, *a, **kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, json=None, **kw):
            captured_payloads.append(json or {})
            return _RespOK()

    class _FakeBot:
        async def send_message(self, *a, **kw):
            return type("P", (), {"message_id": 1})()

        async def edit_message_text(self, *a, **kw):
            return None

    ctx = _FakeContext()
    ctx.bot = _FakeBot()

    with (
        patch.object(bot.httpx, "AsyncClient", _FakeClient),
        patch.object(bot, "async_http_transport_ipv4_lookup", return_value=None),
    ):
        asyncio.run(
            bot._stream_chat_response(
                ctx,
                chat_id=1,
                user_id=1,
                history=[],
                user_text="привет",
            ),
        )

    last_msg = captured_payloads[0]["messages"][-1]
    assert last_msg == {"role": "user", "content": "привет"}
