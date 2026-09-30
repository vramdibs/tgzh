"""Бот: `/chat` — голосовое сообщение → STT → текстовый промпт.

Проверяем основные ветки `handle_voice`:
- вне активного `/chat` — тихо игнорируем (никаких reply);
- сессия истекла — мягкое сообщение и сброс RAM;
- STT не сконфигурирован — вежливое сообщение, не валится;
- успешное распознавание — текст уходит в `_handle_chat_user_message`;
- ошибка STT — пользователь видит короткую фразу, чат-стрим не зовётся.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import bot
import stt_client
import user_storage


# ====================== fixtures ======================


@pytest.fixture
def db_path(monkeypatch: pytest.MonkeyPatch) -> str:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    user_storage.init_db(path)
    monkeypatch.setattr(bot, "USER_DB_PATH", path)
    yield path
    os.unlink(path)


@pytest.fixture
def active_chat_session(db_path: str) -> int:
    user_id = 7
    user_storage.chat_session_login(db_path, user_id)
    return user_id


@pytest.fixture
def stt_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_BASE_URL", "https://stt.example/v1")
    monkeypatch.setenv("STT_MODEL", "whisper-1")
    monkeypatch.setenv("STT_LANGUAGE", "ru")


# ====================== fakes ======================


class _FakeUserData(dict):
    pass


class _FakeFile:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self._payload)


class _FakeBot:
    def __init__(self, audio: bytes = b"OGGdata") -> None:
        self._audio = audio
        self.actions: list[tuple[int, Any]] = []

    async def get_file(self, file_id: str) -> _FakeFile:
        return _FakeFile(self._audio)

    async def send_chat_action(self, chat_id: int, action: Any) -> None:
        self.actions.append((chat_id, action))


class _FakeContext:
    def __init__(self, *, audio: bytes = b"OGGdata") -> None:
        self.user_data = _FakeUserData()
        self.bot = _FakeBot(audio=audio)


class _FakeMessage:
    def __init__(self, *, voice: Any, audio: Any = None) -> None:
        self.voice = voice
        self.audio = audio
        self.replies: list[str] = []
        self.statuses: list[str] = []

    async def reply_text(self, text: str, **_kw: Any) -> "_FakeStatusMessage":
        self.replies.append(text)
        return _FakeStatusMessage(self, text)


class _FakeStatusMessage:
    def __init__(self, parent: _FakeMessage, text: str) -> None:
        self._parent = parent
        self.text = text
        parent.statuses.append(text)

    async def edit_text(self, text: str, **_kw: Any) -> None:
        self.text = text
        self._parent.statuses.append(text)


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeUpdate:
    def __init__(self, *, user_id: int, chat_id: int, message: _FakeMessage) -> None:
        self.effective_user = _FakeUser(user_id)
        self.effective_chat = _FakeChat(chat_id)
        self.message = message


class _FakeVoice:
    def __init__(
        self,
        *,
        file_id: str = "fid:voice",
        file_size: int = 1024,
        duration: int = 3,
        mime_type: str | None = "audio/ogg",
    ) -> None:
        self.file_id = file_id
        self.file_size = file_size
        self.duration = duration
        self.mime_type = mime_type


# ====================== тесты ======================


def test_handle_voice_silently_skips_when_no_chat_session(
    db_path: str,
    stt_enabled: None,
) -> None:
    msg = _FakeMessage(voice=_FakeVoice())
    update = _FakeUpdate(user_id=999, chat_id=42, message=msg)
    ctx = _FakeContext()

    asyncio.run(bot.handle_voice(update, ctx))

    assert msg.replies == []
    assert msg.statuses == []
    assert ctx.bot.actions == []


def test_handle_voice_replies_when_session_expired(
    db_path: str,
    stt_enabled: None,
) -> None:
    msg = _FakeMessage(voice=_FakeVoice())
    update = _FakeUpdate(user_id=11, chat_id=42, message=msg)
    ctx = _FakeContext()
    # RAM-флаг есть, но в БД сессии нет — `chat_session_active_until → None`.
    ctx.user_data[bot._CHAT_ACTIVE] = True
    ctx.user_data[bot._CHAT_HISTORY] = [{"role": "user", "content": "x"}]

    asyncio.run(bot.handle_voice(update, ctx))

    assert any("истекла" in r for r in msg.replies)
    assert bot._CHAT_ACTIVE not in ctx.user_data
    assert bot._CHAT_HISTORY not in ctx.user_data


def test_handle_voice_busy_check(
    db_path: str,
    active_chat_session: int,
    stt_enabled: None,
) -> None:
    msg = _FakeMessage(voice=_FakeVoice())
    update = _FakeUpdate(user_id=active_chat_session, chat_id=42, message=msg)
    ctx = _FakeContext()
    ctx.user_data[bot._CHAT_ACTIVE] = True
    ctx.user_data[bot._CHAT_BUSY] = True

    asyncio.run(bot.handle_voice(update, ctx))

    assert any("ещё печатает" in r for r in msg.replies)


def test_handle_voice_when_stt_disabled(
    db_path: str,
    active_chat_session: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STT_BASE_URL", raising=False)
    msg = _FakeMessage(voice=_FakeVoice())
    update = _FakeUpdate(user_id=active_chat_session, chat_id=42, message=msg)
    ctx = _FakeContext()
    ctx.user_data[bot._CHAT_ACTIVE] = True

    asyncio.run(bot.handle_voice(update, ctx))

    assert any("не настроено" in r for r in msg.replies)


def test_handle_voice_too_large_declared_size(
    db_path: str,
    active_chat_session: int,
    stt_enabled: None,
) -> None:
    huge = bot._CHAT_VOICE_GETFILE_MAX_BYTES + 1
    msg = _FakeMessage(voice=_FakeVoice(file_size=huge))
    update = _FakeUpdate(user_id=active_chat_session, chat_id=42, message=msg)
    ctx = _FakeContext()
    ctx.user_data[bot._CHAT_ACTIVE] = True

    asyncio.run(bot.handle_voice(update, ctx))

    assert any("слишком большое" in r for r in msg.replies)


def test_handle_voice_success_routes_to_chat(
    db_path: str,
    active_chat_session: int,
    stt_enabled: None,
) -> None:
    msg = _FakeMessage(voice=_FakeVoice())
    update = _FakeUpdate(user_id=active_chat_session, chat_id=42, message=msg)
    ctx = _FakeContext(audio=b"OGGfake")
    ctx.user_data[bot._CHAT_ACTIVE] = True

    captured: dict[str, Any] = {}

    async def _fake_transcribe(audio: bytes, **kw: Any) -> str:
        captured["audio_len"] = len(audio)
        captured["mime"] = kw.get("mime")
        return "Привет, бот"

    async def _fake_chat_user(*args: Any, **kw: Any) -> None:
        captured["chat_kwargs"] = kw

    with (
        patch.object(stt_client, "transcribe", side_effect=_fake_transcribe),
        patch.object(bot, "_handle_chat_user_message", side_effect=_fake_chat_user),
    ):
        asyncio.run(bot.handle_voice(update, ctx))

    assert captured["audio_len"] == len(b"OGGfake")
    assert captured["mime"] == "audio/ogg"
    assert captured["chat_kwargs"]["text"] == bot._voice_text_for_chat_cursor("Привет, бот")
    assert captured["chat_kwargs"]["history_text"] == bot._voice_history_placeholder("Привет, бот")
    assert captured["chat_kwargs"]["user_id"] == active_chat_session
    # Пользователь видит распознанный текст в превью.
    assert any("Распознано" in s for s in msg.statuses)


def test_handle_voice_stt_error_does_not_call_chat(
    db_path: str,
    active_chat_session: int,
    stt_enabled: None,
) -> None:
    msg = _FakeMessage(voice=_FakeVoice())
    update = _FakeUpdate(user_id=active_chat_session, chat_id=42, message=msg)
    ctx = _FakeContext()
    ctx.user_data[bot._CHAT_ACTIVE] = True

    async def _fake_transcribe(audio: bytes, **kw: Any) -> str:
        raise stt_client.SttError("Сервис распознавания ответил 502: bad gateway")

    chat_mock = AsyncMock()
    with (
        patch.object(stt_client, "transcribe", side_effect=_fake_transcribe),
        patch.object(bot, "_handle_chat_user_message", chat_mock),
    ):
        asyncio.run(bot.handle_voice(update, ctx))

    assert any("Не удалось распознать" in s for s in msg.statuses)
    chat_mock.assert_not_called()


def test_handle_voice_homework_route_sends_to_check(
    db_path: str,
    stt_enabled: None,
) -> None:
    """`_AWAIT_TEXT_ANSWER` → распознанный голос идёт в `_run_homework_text_answer_check`,
    `_handle_chat_user_message` НЕ вызывается, флаг ожидания снимается."""
    user_id = 555
    user_storage.set_textbook(
        db_path,
        user_id=user_id,
        grade=6,
        slug="merzlyak-6",
        url="https://gdz.ru/x",
        label="Мерзляк 6",
        is_premium=False,
        subject_slug="matematika",
    )
    user_storage.set_homework_meta(
        db_path,
        user_id=user_id,
        paragraph="1",
        exercise="1",
        page=None,
    )

    msg = _FakeMessage(voice=_FakeVoice())
    update = _FakeUpdate(user_id=user_id, chat_id=42, message=msg)
    ctx = _FakeContext(audio=b"OGGhomework")
    ctx.user_data[bot._AWAIT_TEXT_ANSWER] = True
    # Дополнительно симулируем активный /chat — он не должен перебивать ДЗ-маршрут.
    ctx.user_data[bot._CHAT_ACTIVE] = True

    captured: dict[str, Any] = {}

    transcribed_text = "fake transcription"

    async def _fake_transcribe(audio: bytes, **kw: Any) -> str:
        return transcribed_text

    async def _fake_hw_check(*args: Any, **kw: Any) -> None:
        captured["hw_kwargs"] = kw

    chat_mock = AsyncMock()

    async def _fake_typing(_bot: Any, _cid: int, coro: Any) -> None:
        await coro

    with (
        patch.object(stt_client, "transcribe", side_effect=_fake_transcribe),
        patch.object(bot, "_run_homework_text_answer_check", side_effect=_fake_hw_check),
        patch.object(bot, "_handle_chat_user_message", chat_mock),
        patch.object(bot, "run_with_typing", side_effect=_fake_typing),
    ):
        asyncio.run(bot.handle_voice(update, ctx))

    chat_mock.assert_not_called()
    assert "hw_kwargs" in captured, "homework check must be called for AWAIT_TEXT_ANSWER voice"
    assert captured["hw_kwargs"]["answer_plain"] == transcribed_text
    assert captured["hw_kwargs"]["user_id"] == user_id
    assert captured["hw_kwargs"]["chat_id"] == 42
    # Флаг ожидания должен быть снят после маршрутизации.
    assert bot._AWAIT_TEXT_ANSWER not in ctx.user_data
    # Превью «🎙 Распознано: …» показано пользователю.
    assert any("Распознано" in s for s in msg.statuses)


def test_handle_voice_homework_route_when_no_profile(
    db_path: str,
    stt_enabled: None,
) -> None:
    """`_AWAIT_TEXT_ANSWER`, но профиль/привязка не заполнены → без STT-вызова, мягкое сообщение."""
    msg = _FakeMessage(voice=_FakeVoice())
    update = _FakeUpdate(user_id=777, chat_id=42, message=msg)
    ctx = _FakeContext()
    ctx.user_data[bot._AWAIT_TEXT_ANSWER] = True

    transcribe_mock = AsyncMock()
    hw_mock = AsyncMock()
    with (
        patch.object(stt_client, "transcribe", transcribe_mock),
        patch.object(bot, "_run_homework_text_answer_check", hw_mock),
    ):
        asyncio.run(bot.handle_voice(update, ctx))

    transcribe_mock.assert_not_called()
    hw_mock.assert_not_called()
    assert any("Сначала укажи задание" in r for r in msg.replies)
    assert bot._AWAIT_TEXT_ANSWER not in ctx.user_data


def test_handle_voice_lazy_loads_chat_active_from_db(
    db_path: str,
    active_chat_session: int,
    stt_enabled: None,
) -> None:
    """RAM-флаг `_CHAT_ACTIVE` пуст (после рестарта), но сессия в БД жива → STT всё равно срабатывает."""
    msg = _FakeMessage(voice=_FakeVoice())
    update = _FakeUpdate(user_id=active_chat_session, chat_id=42, message=msg)
    ctx = _FakeContext()
    # Намеренно не выставляем _CHAT_ACTIVE.

    captured: dict[str, Any] = {}

    async def _fake_transcribe(audio: bytes, **kw: Any) -> str:
        return "ok"

    async def _fake_chat_user(*args: Any, **kw: Any) -> None:
        captured["called"] = True

    with (
        patch.object(stt_client, "transcribe", side_effect=_fake_transcribe),
        patch.object(bot, "_handle_chat_user_message", side_effect=_fake_chat_user),
    ):
        asyncio.run(bot.handle_voice(update, ctx))

    assert captured.get("called") is True
    assert ctx.user_data.get(bot._CHAT_ACTIVE) is True


def test_voice_leftover_pw_wait_is_ignored(
    db_path: str,
    stt_enabled: None,
) -> None:
    """Пароль /chat больше не спрашивается: старый `_CHAT_PW_WAIT` не крадёт голос."""
    msg = _FakeMessage(voice=_FakeVoice())
    update = _FakeUpdate(user_id=88, chat_id=42, message=msg)
    ctx = _FakeContext()
    ctx.user_data[bot._CHAT_PW_WAIT] = True

    asyncio.run(bot.handle_voice(update, ctx))

    assert bot._CHAT_PW_WAIT not in ctx.user_data
    assert msg.replies == []
    assert ctx.user_data.get(bot._CHAT_ACTIVE) is not True


def test_voice_after_chat_activate_not_stolen_by_await_text_answer(
    db_path: str,
    stt_enabled: None,
) -> None:
    """После входа в /chat сбрасывается `_AWAIT_TEXT_ANSWER` — голос идёт в чат, не в ДЗ."""
    user_id = 89
    user_storage.set_textbook(
        db_path,
        user_id=user_id,
        grade=6,
        slug="merzlyak-6",
        url="https://gdz.ru/x",
        label="Мерзляк 6",
        is_premium=False,
        subject_slug="matematika",
    )
    user_storage.set_homework_meta(
        db_path,
        user_id=user_id,
        paragraph="1",
        exercise="1",
        page=None,
    )
    user_storage.chat_session_login(db_path, user_id)

    ctx = _FakeContext()
    ctx.user_data[bot._AWAIT_TEXT_ANSWER] = True
    bot._activate_chat_session_ram(ctx, fresh=True)
    assert bot._AWAIT_TEXT_ANSWER not in ctx.user_data
    assert ctx.user_data.get(bot._CHAT_ACTIVE) is True

    msg2 = _FakeMessage(voice=_FakeVoice())
    update2 = _FakeUpdate(user_id=user_id, chat_id=42, message=msg2)
    captured: dict[str, Any] = {}
    hw_mock = AsyncMock()
    chat_mock = AsyncMock(side_effect=lambda *a, **kw: captured.update(kw))

    async def _fake_transcribe2(audio: bytes, **kw: Any) -> str:
        return "Как решить уравнение?"

    with (
        patch.object(stt_client, "transcribe", side_effect=_fake_transcribe2),
        patch.object(bot, "_run_homework_text_answer_check", hw_mock),
        patch.object(bot, "_handle_chat_user_message", chat_mock),
    ):
        asyncio.run(bot.handle_voice(update2, ctx))

    hw_mock.assert_not_called()
    chat_mock.assert_called_once()
    assert "Как решить уравнение?" in captured["text"]
    assert captured.get("history_text", "").startswith("[голос:")
