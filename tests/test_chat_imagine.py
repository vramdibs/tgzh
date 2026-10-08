"""Юниты `/imagine` и кнопки «Сгенерировать фото» в `/chat`.

Проверяется FSM-флаг `_CHAT_IMG_PROMPT_WAIT`, маршрутизация в
`_handle_chat_imagine_request`, корректное обращение к `/image/generate`
и плейсхолдеры в RAM/DB-истории.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import bot
import image_gen


class _FakeUserData(dict):
    pass


class _FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[int, str]] = []
        self.sent_photos: list[tuple[int, bytes, str]] = []
        self.deleted: list[tuple[int, int]] = []
        self.edited: list[tuple[int, int, str]] = []
        self._next_message_id = 1000

    async def send_chat_action(self, chat_id, action):
        return None

    async def send_message(self, chat_id, text, **_kw):
        self._next_message_id += 1
        self.sent_messages.append((chat_id, text))

        class _M:
            message_id = self._next_message_id

        # Сохраним id, иначе тесты не отличат «генерирую» от «меню»
        m = _M()
        m.message_id = self._next_message_id
        return m

    async def send_photo(self, chat_id, photo, caption=None, **_kw):
        self.sent_photos.append((chat_id, photo, caption or ""))
        return None

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def edit_message_text(self, chat_id, message_id, text, **_kw):
        self.edited.append((chat_id, message_id, text))


class _FakeContext:
    def __init__(self) -> None:
        self.user_data = _FakeUserData()
        self.bot = _FakeBot()


class _FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str, **_kw):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, text: str = "") -> None:
        self.message = _FakeMessage(text)


def test_is_image_gen_enabled_reflects_env(
    monkeypatch,
) -> None:
    assert bot._is_image_gen_enabled() is False
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    assert bot._is_image_gen_enabled() is True


def test_handle_chat_imagine_request_rejects_when_not_configured() -> None:
    update = _FakeUpdate()
    ctx = _FakeContext()
    ctx.user_data[bot._CHAT_IMG_PROMPT_WAIT] = True

    asyncio.run(
        bot._handle_chat_imagine_request(
            update, ctx, user_id=1, chat_id=1, prompt="cat"
        )
    )
    assert any("не настроена" in r for r in update.message.replies)
    # Флаг должен быть сброшен в любом случае — иначе пользователь застрянет.
    assert bot._CHAT_IMG_PROMPT_WAIT not in ctx.user_data


def test_handle_chat_imagine_request_rejects_empty_prompt(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    update = _FakeUpdate()
    ctx = _FakeContext()
    asyncio.run(
        bot._handle_chat_imagine_request(
            update, ctx, user_id=1, chat_id=1, prompt="   "
        )
    )
    assert any("Пустой промпт" in r for r in update.message.replies)


def test_handle_chat_imagine_request_rejects_too_long_prompt(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    update = _FakeUpdate()
    ctx = _FakeContext()
    asyncio.run(
        bot._handle_chat_imagine_request(
            update,
            ctx,
            user_id=1,
            chat_id=1,
            prompt="a" * (image_gen.PROMPT_MAX_LEN + 1),
        )
    )
    assert any("слишком длинн" in r.lower() for r in update.message.replies)


def test_handle_chat_imagine_request_sends_photo_and_records_history(
    monkeypatch,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    update = _FakeUpdate()
    ctx = _FakeContext()
    png = b"\x89PNG-bytes-from-server"

    async def _fake_request(*, user_id, prompt):
        assert user_id == 42
        assert prompt == "кот в шапке"
        return png

    with (
        patch.object(bot, "_request_image_from_server", side_effect=_fake_request),
        patch.object(bot.user_storage, "chat_dialog_upsert", return_value=99),
        # «Меню чата» внутри финального шага мы не проверяем — глушим.
        patch.object(bot, "_send_chat_menu", return_value=None),
    ):
        asyncio.run(
            bot._handle_chat_imagine_request(
                update, ctx, user_id=42, chat_id=7, prompt="кот в шапке"
            )
        )

    # Должна уйти ровно одна картинка с подписью = промпт.
    assert len(ctx.bot.sent_photos) == 1
    chat_id, sent_bytes, caption = ctx.bot.sent_photos[0]
    assert chat_id == 7
    assert sent_bytes == png
    assert caption == "кот в шапке"

    # История содержит парные плейсхолдеры (user / assistant).
    history = ctx.user_data[bot._CHAT_HISTORY]
    assert history[-2] == {"role": "user", "content": "[/imagine] кот в шапке"}
    assert history[-1] == {
        "role": "assistant",
        "content": "[сгенерировано фото: кот в шапке]",
    }
    # Активный диалог получил dialog_id из upsert.
    assert ctx.user_data[bot._CHAT_DIALOG_ID] == 99
    # Флаг ожидания сброшен.
    assert bot._CHAT_IMG_PROMPT_WAIT not in ctx.user_data
    # _CHAT_BUSY должен быть отпущен в finally.
    assert bot._CHAT_BUSY not in ctx.user_data


def test_handle_chat_imagine_request_truncates_long_caption(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    update = _FakeUpdate()
    ctx = _FakeContext()

    async def _fake_request(*, user_id, prompt):
        return b"x"

    long_prompt = "А" * 1500

    with (
        patch.object(bot, "_request_image_from_server", side_effect=_fake_request),
        patch.object(bot.user_storage, "chat_dialog_upsert", return_value=1),
        patch.object(bot, "_send_chat_menu", return_value=None),
    ):
        asyncio.run(
            bot._handle_chat_imagine_request(
                update, ctx, user_id=1, chat_id=1, prompt=long_prompt
            )
        )

    _, _, caption = ctx.bot.sent_photos[0]
    assert len(caption) <= bot._TG_PHOTO_CAPTION_MAX_LEN
    assert caption.endswith("…")


def test_handle_chat_imagine_request_reports_server_failure(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    update = _FakeUpdate()
    ctx = _FakeContext()

    async def _boom(*, user_id, prompt):
        raise RuntimeError("server 502: backend down")

    with patch.object(bot, "_request_image_from_server", side_effect=_boom):
        asyncio.run(
            bot._handle_chat_imagine_request(
                update, ctx, user_id=1, chat_id=1, prompt="cat"
            )
        )

    # Картинка не отправлялась.
    assert ctx.bot.sent_photos == []
    # Где-то всплыл текст ошибки (или в edit_message_text, или в reply_text).
    edited_texts = [t for *_, t in ctx.bot.edited]
    all_msgs = edited_texts + update.message.replies
    assert any("server 502" in m for m in all_msgs)
    # История не должна была пополниться — генерации не было.
    assert bot._CHAT_HISTORY not in ctx.user_data


def test_chat_menu_keyboard_includes_imagine_button() -> None:
    kb = bot._chat_menu_keyboard(active=True)
    flat = [b for row in kb.inline_keyboard for b in row]
    callbacks = [b.callback_data for b in flat]
    labels = [b.text for b in flat]
    assert "chat:imagine" in callbacks
    assert any("Сгенериров" in t for t in labels)


def test_chat_menu_keyboard_inactive_has_no_imagine() -> None:
    kb = bot._chat_menu_keyboard(active=False)
    flat = [b for row in kb.inline_keyboard for b in row]
    callbacks = [b.callback_data for b in flat]
    assert "chat:imagine" not in callbacks


def test_chat_imagine_wait_keyboard_includes_example_button() -> None:
    kb = bot._chat_imagine_wait_keyboard(1)
    flat = [b for row in kb.inline_keyboard for b in row]
    callbacks = [b.callback_data for b in flat]
    assert callbacks[0] == "chat:imagine:example"
    assert "chat:imagine" in callbacks


def test_chat_imagine_example_prompt_constant_non_empty() -> None:
    assert len(bot._CHAT_IMAGINE_EXAMPLE_PROMPT) > 50
    assert "поезд" in bot._CHAT_IMAGINE_EXAMPLE_PROMPT.lower()
