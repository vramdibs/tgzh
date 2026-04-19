"""`handle_photo`: приоритет маршрута проверки ДЗ над `/chat`.

Если пользователь нажал «Отправить фото» в режиме проверки ДЗ
(`_AWAIT_PHOTO_ANSWER=True`), фото должно уйти в проверку, а НЕ в чат-бот —
даже если параллельно «жива» `/chat`-сессия (флаг `_CHAT_ACTIVE` мог быть
лениво поднят из БД после рестарта бота).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import bot
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
def hw_ready_user(db_path: str) -> int:
    user_id = 4242
    user_storage.set_textbook(
        db_path,
        user_id=user_id,
        grade=6,
        slug="example-6",
        url="https://gdz.ru/x",
        label="Учебник 6",
        is_premium=False,
    )
    user_storage.set_homework_meta(
        db_path,
        user_id=user_id,
        paragraph="1",
        exercise="1",
        page=None,
    )
    return user_id


# ====================== fakes ======================


class _FakeUserData(dict):
    pass


class _FakeBot:
    async def send_chat_action(self, *_a: Any, **_kw: Any) -> None:
        return None


class _FakeContext:
    def __init__(self) -> None:
        self.user_data = _FakeUserData()
        self.bot = _FakeBot()


class _FakeMessage:
    def __init__(self, *, chat_id: int, photo_file_id: str = "fid:photo") -> None:
        self.chat_id = chat_id
        self.message_id = 777
        self.media_group_id: str | None = None
        self.replies: list[str] = []
        self.photo = [
            type(
                "PhotoSize",
                (),
                {"file_id": photo_file_id, "file_size": 12345},
            )()
        ]

    async def reply_text(self, text: str, **_kw: Any) -> Any:
        self.replies.append(text)
        return None


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


# ====================== тесты ======================


def test_handle_photo_homework_priority_wins_over_chat(
    db_path: str,
    hw_ready_user: int,
) -> None:
    """`_AWAIT_PHOTO_ANSWER` + активная /chat → фото уходит в ДЗ, не в чат."""
    msg = _FakeMessage(chat_id=42)
    update = _FakeUpdate(user_id=hw_ready_user, chat_id=42, message=msg)
    ctx = _FakeContext()
    ctx.user_data[bot._AWAIT_PHOTO_ANSWER] = True
    # Симулируем активную /chat-сессию — она НЕ должна перебить ДЗ-маршрут.
    ctx.user_data[bot._CHAT_ACTIVE] = True

    chat_photo_mock = AsyncMock()
    store_mock = AsyncMock()

    with (
        patch.object(bot, "_handle_chat_photo", chat_photo_mock),
        patch.object(bot, "_store_photo_batch", store_mock),
        patch.object(
            bot,
            "_safe_blocked_state",
            new=AsyncMock(return_value=(None, True)),
        ),
        patch.object(
            bot.bot_stats,
            "record_photo_uploaded",
            return_value=None,
        ),
    ):
        asyncio.run(bot.handle_photo(update, ctx))

    chat_photo_mock.assert_not_called()
    store_mock.assert_called_once()
    args, _ = store_mock.call_args
    assert args[0] == hw_ready_user
    assert args[1] == ["fid:photo"]
    # Флаг сохраняется — нужен для корректного маршрута следующих фото в альбоме.
    assert ctx.user_data.get(bot._AWAIT_PHOTO_ANSWER) is True
    # Пользователь видит подтверждение «Фото получено».
    assert any("Фото получено" in r for r in msg.replies)


def test_handle_photo_homework_priority_album_keeps_flag(
    db_path: str,
    hw_ready_user: int,
) -> None:
    """Альбом из нескольких фото: флаг сохраняется, все фото идут в ДЗ-батч, не в чат."""
    msgs = [_FakeMessage(chat_id=42, photo_file_id=f"fid:{i}") for i in range(3)]
    for m in msgs:
        m.media_group_id = "album-1"
    ctx = _FakeContext()
    ctx.user_data[bot._AWAIT_PHOTO_ANSWER] = True
    ctx.user_data[bot._CHAT_ACTIVE] = True

    chat_photo_mock = AsyncMock()

    async def _noop_flush(*_a: Any, **_kw: Any) -> None:
        return None

    with (
        patch.object(bot, "_handle_chat_photo", chat_photo_mock),
        patch.object(
            bot,
            "_safe_blocked_state",
            new=AsyncMock(return_value=(None, True)),
        ),
        patch.object(bot, "_photo_batch_flush_delayed", side_effect=_noop_flush),
    ):
        for m in msgs:
            update = _FakeUpdate(user_id=hw_ready_user, chat_id=42, message=m)
            asyncio.run(bot.handle_photo(update, ctx))

    # Ни одно фото из альбома не должно уйти в /chat.
    chat_photo_mock.assert_not_called()
    # Флаг по-прежнему висит — пользователь не выходил из режима upload.
    assert ctx.user_data.get(bot._AWAIT_PHOTO_ANSWER) is True
    # Все фото попали в один и тот же батч.
    entries = ctx.user_data.get(bot._PHOTO_BATCH_ENTRIES)
    assert entries is not None
    assert [e[1] for e in entries] == ["fid:0", "fid:1", "fid:2"]


def test_handle_photo_priority_clears_flag_when_no_profile(
    db_path: str,
) -> None:
    """`_AWAIT_PHOTO_ANSWER` без валидного профиля → флаг снимается, фото идёт в /chat-фолбэк."""
    user_id = 9090
    msg = _FakeMessage(chat_id=42)
    update = _FakeUpdate(user_id=user_id, chat_id=42, message=msg)
    ctx = _FakeContext()
    ctx.user_data[bot._AWAIT_PHOTO_ANSWER] = True
    ctx.user_data[bot._CHAT_ACTIVE] = True
    # Имитируем активную сессию /chat в БД (нужно для прохода проверки `chat_session_active_until`).
    user_storage.chat_session_login(db_path, user_id)

    chat_photo_mock = AsyncMock()
    store_mock = AsyncMock()

    with (
        patch.object(bot, "_handle_chat_photo", chat_photo_mock),
        patch.object(bot, "_store_photo_batch", store_mock),
        patch.object(
            bot,
            "_safe_blocked_state",
            new=AsyncMock(return_value=(None, True)),
        ),
    ):
        asyncio.run(bot.handle_photo(update, ctx))

    # Профиля нет → флаг снимается, путь продолжается как /chat-фото.
    assert bot._AWAIT_PHOTO_ANSWER not in ctx.user_data
    chat_photo_mock.assert_called_once()
    store_mock.assert_not_called()


def test_handle_photo_chat_path_unaffected_when_no_homework_flag(
    db_path: str,
    hw_ready_user: int,
) -> None:
    """Без `_AWAIT_PHOTO_ANSWER` поведение прежнее: активный `/chat` забирает фото."""
    msg = _FakeMessage(chat_id=42)
    update = _FakeUpdate(user_id=hw_ready_user, chat_id=42, message=msg)
    ctx = _FakeContext()
    ctx.user_data[bot._CHAT_ACTIVE] = True
    user_storage.chat_session_login(db_path, hw_ready_user)

    chat_photo_mock = AsyncMock()
    store_mock = AsyncMock()

    with (
        patch.object(bot, "_handle_chat_photo", chat_photo_mock),
        patch.object(bot, "_store_photo_batch", store_mock),
        patch.object(
            bot,
            "_safe_blocked_state",
            new=AsyncMock(return_value=(None, True)),
        ),
    ):
        asyncio.run(bot.handle_photo(update, ctx))

    chat_photo_mock.assert_called_once()
    store_mock.assert_not_called()
