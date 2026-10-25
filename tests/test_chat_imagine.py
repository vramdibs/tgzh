"""Юниты `/imagine` и кнопки «Сгенерировать фото» в `/chat`.

Проверяется FSM-флаг `_CHAT_IMG_PROMPT_WAIT`, маршрутизация в
`_handle_chat_imagine_request`, корректное обращение к `/image/generate`
и плейсхолдеры в RAM/DB-истории.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

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
        self.message_id = 77
        self.replies: list[str] = []

    async def reply_text(self, text: str, **_kw):
        self.replies.append(text)


class _FakeUser:
    def __init__(self, user_id: int = 42) -> None:
        self.id = user_id


class _FakeChat:
    def __init__(self, chat_id: int = 100) -> None:
        self.id = chat_id


class _FakeUpdate:
    def __init__(self, text: str = "") -> None:
        self.message = _FakeMessage(text)
        self.effective_user = _FakeUser()
        self.effective_chat = _FakeChat()


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

    async def _fake_request(*, user_id, prompt, size=None):
        assert user_id == 42
        assert prompt == "кот в шапке"
        assert size == "1024x1024"
        return png

    with (
        patch.object(bot, "_request_image_from_server", side_effect=_fake_request),
        patch.object(bot.user_storage, "chat_dialog_upsert", return_value=99),
        # «Меню чата» внутри финального шага мы не проверяем — глушим.
        patch.object(bot, "_send_post_imagine_menu", new=AsyncMock()),
    ):
        asyncio.run(
            bot._handle_chat_imagine_request(
                update, ctx, user_id=42, chat_id=7, prompt="кот в шапке"
            )
        )

    assert len(ctx.bot.sent_photos) == 1
    chat_id, sent_bytes, caption = ctx.bot.sent_photos[0]
    assert chat_id == 7
    assert sent_bytes == png
    assert caption == ""

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


def test_handle_chat_imagine_request_sends_photo_without_caption(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    update = _FakeUpdate()
    ctx = _FakeContext()

    async def _fake_request(*, user_id, prompt, size=None):
        return b"x"

    long_prompt = "А" * 1500

    with (
        patch.object(bot, "_request_image_from_server", side_effect=_fake_request),
        patch.object(bot.user_storage, "chat_dialog_upsert", return_value=1),
        patch.object(bot, "_send_post_imagine_menu", new=AsyncMock()),
    ):
        asyncio.run(
            bot._handle_chat_imagine_request(
                update, ctx, user_id=1, chat_id=1, prompt=long_prompt
            )
        )

    _, _, caption = ctx.bot.sent_photos[0]
    assert caption == ""


def test_handle_chat_imagine_request_reports_server_failure(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    update = _FakeUpdate()
    ctx = _FakeContext()

    async def _boom(*, user_id, prompt, size=None):
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
    assert bot._chat_menu_keyboard(active=False) is None


def test_chat_imagine_choice_keyboard_has_example_and_custom() -> None:
    kb = bot._chat_imagine_choice_keyboard()
    flat = [b for row in kb.inline_keyboard for b in row]
    callbacks = [b.callback_data for b in flat]
    labels = [b.text for b in flat]
    assert "chat:imagine:example" in callbacks
    assert "chat:imagine:custom" in callbacks
    assert "chat:imagine:style_photo" in callbacks
    assert "chat:menu" in callbacks
    assert any("Перерисовать в стиле примера" in t for t in labels)


def test_chat_imagine_wait_keyboard_includes_example_and_back() -> None:
    kb = bot._chat_imagine_wait_keyboard(1, _FakeContext())
    flat = [b for row in kb.inline_keyboard for b in row]
    callbacks = [b.callback_data for b in flat]
    assert callbacks[0] == "chat:imagine:example"
    assert "chat:imagine" in callbacks
    assert "chat:imagine:size:256x256" in callbacks
    assert "chat:imagine:size:1024x1024" in callbacks
    assert f"chat:imagine:size:{image_gen.CHAT_IMAGINE_SIZE_ORIGINAL}" in callbacks
    assert "chat:list" not in callbacks
    assert "chat:model" not in callbacks


def test_chat_imagine_style_photo_wait_keyboard_no_chat_menu() -> None:
    kb = bot._chat_imagine_style_photo_wait_keyboard(1, _FakeContext())
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "chat:imagine:size:1024x1024" in callbacks
    assert f"chat:imagine:size:{image_gen.CHAT_IMAGINE_SIZE_ORIGINAL}" in callbacks
    assert "chat:list" not in callbacks
    assert "chat:model" not in callbacks


def test_chat_imagine_pick_size_default_and_set() -> None:
    ctx = _FakeContext()
    assert bot._chat_imagine_pick_size(ctx) == "1024x1024"
    bot._set_chat_imagine_pick_size(ctx, "256x256")
    assert bot._chat_imagine_pick_size(ctx) == "256x256"


def test_style_photo_wait_keyboard_marks_original_when_set() -> None:
    ctx = _FakeContext()
    assert bot._chat_imagine_pick_size(ctx) == "1024x1024"
    bot._set_chat_imagine_pick_size(ctx, image_gen.CHAT_IMAGINE_SIZE_ORIGINAL)
    kb = bot._chat_imagine_style_photo_wait_keyboard(1, ctx)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "Оригинал до 1280 ✓" in labels
    assert "1024×1024 ✓" not in labels


def test_chat_imagine_example_prompt_constant_non_empty() -> None:
    assert len(bot._CHAT_IMAGINE_EXAMPLE_PROMPT) > 50
    assert "поезд" in bot._CHAT_IMAGINE_EXAMPLE_PROMPT.lower()


def test_chat_imagine_status_placeholders() -> None:
    for text in (
        bot._CHAT_IMAGINE_GENERATE_PLACEHOLDER,
        bot._CHAT_IMAGINE_STYLE_REDRAW_PLACEHOLDER,
        bot._CHAT_IMAGINE_EDIT_PLACEHOLDER,
    ):
        assert "5 минут" in text
        assert "очередь" not in text.lower()
        assert "нагрузк" not in text.lower()
    assert bot._CHAT_IMAGINE_STYLE_REDRAW_PLACEHOLDER.startswith("Перерисовываю фото")


def test_image_gen_server_user_error_429() -> None:
    msg = bot._image_gen_server_user_error(
        429,
        '{"detail":"image generation already in progress; retry in a minute"}',
    )
    assert "слоты генерации" in msg
    assert "429" not in msg


def test_chat_imagine_choice_help_shows_copyable_example_prompt() -> None:
    html = bot._chat_imagine_choice_help_html()
    assert "<pre>" in html
    assert "купе поезда" in html
    assert bot._CHAT_IMAGINE_EXAMPLE_PROMPT.split()[0] in html


def test_handle_chat_imagine_style_redraw_calls_server(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    update = _FakeUpdate()
    ctx = _FakeContext()
    captured: dict = {}

    async def _fake_style(**kwargs):
        captured.update(kwargs)
        return b"png-out"

    with (
        patch.object(bot, "_request_image_style_redraw_from_server", side_effect=_fake_style),
        patch.object(bot, "_send_post_imagine_menu", new=AsyncMock()),
        patch.object(bot.user_storage, "chat_dialog_upsert", lambda *a, **k: None),
    ):
        asyncio.run(
            bot._handle_chat_imagine_style_redraw(
                update,
                ctx,
                user_id=42,
                chat_id=100,
                source_jpeg=b"jpeg",
                notes="evening",
            ),
        )

    assert captured["source_jpeg"] == b"jpeg"
    assert captured["notes"] == bot._style_redraw_prompt_for_api("evening")
    assert ctx.bot.sent_photos
    assert ctx.bot.sent_photos[0][2] == ""
    assert (100, 77) in ctx.bot.deleted
    assert bot._CHAT_IMAGINE_STYLE_PHOTO_WAIT not in ctx.user_data


def test_should_route_chat_text_to_imagine_edit_after_last_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    ctx = _FakeContext()
    ctx.user_data[bot._CHAT_IMAGINE_SESSION] = True
    ctx.user_data[bot._CHAT_IMAGINE_LAST_PNG] = b"png"
    ctx.user_data[bot._CHAT_HISTORY] = [
        {"role": "assistant", "content": "[перерисовано фото в стиле примера: x]"},
    ]
    assert bot._should_route_chat_text_to_imagine_edit(
        ctx,
        "А можешь с фото убрать провода?",
    )
    assert not bot._should_route_chat_text_to_imagine_edit(
        ctx,
        "Что такое интеграл?",
    )
    ctx.user_data.pop(bot._CHAT_IMAGINE_SESSION, None)
    assert not bot._should_route_chat_text_to_imagine_edit(
        ctx,
        "А можешь с фото убрать провода?",
    )


def test_imagine_edit_not_routed_without_imagine_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    ctx = _FakeContext()
    ctx.user_data[bot._CHAT_IMAGINE_SESSION] = True
    ctx.user_data[bot._CHAT_IMAGINE_LAST_PNG] = b"png"
    ctx.user_data[bot._CHAT_HISTORY] = [
        {"role": "user", "content": "[фото: усилитель]"},
        {"role": "assistant", "content": "Да, подходит."},
    ]
    assert not bot._should_route_chat_text_to_imagine_edit(
        ctx,
        "Это Amp a275. Под динамики 140 Вт номинал это ок?",
    )


def test_nudge_imagine_when_edit_request_in_plain_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    ctx = _FakeContext()
    ctx.user_data[bot._CHAT_HISTORY] = [
        {"role": "user", "content": "[фото: x]"},
    ]
    assert bot._should_nudge_imagine_for_edit_in_chat(ctx, "убери провода с фото")


def test_handle_chat_imagine_edit_calls_server(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    ctx = _FakeContext()
    ctx.user_data[bot._CHAT_IMAGINE_LAST_PNG] = b"last"
    update = _FakeUpdate("убери провода")
    captured: dict = {}

    async def _fake_edit(**kwargs):
        captured.update(kwargs)
        return b"edited"

    with (
        patch.object(bot, "_request_image_edit_from_server", side_effect=_fake_edit),
        patch.object(bot, "_send_post_imagine_menu", new=AsyncMock()),
    ):
        asyncio.run(
            bot._handle_chat_imagine_edit(
                update,
                ctx,
                user_id=1,
                chat_id=1,
                instruction="убери провода",
            ),
        )

    assert captured["source_png"] == b"last"
    assert captured["instruction"] == "убери провода"
    assert ctx.bot.sent_photos
    assert ctx.bot.sent_photos[0][2] == ""
    assert ctx.user_data[bot._CHAT_IMAGINE_LAST_PNG] == b"edited"


def test_imagine_cmd_without_active_chat_opens_choice_menu(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    ctx = _FakeContext()
    assert bot._CHAT_ACTIVE not in ctx.user_data
    update = _FakeUpdate("/imagine")
    with (
        patch("bot._reply_if_blocked_cmd", new=AsyncMock(return_value=False)),
        patch("bot.user_storage.chat_session_login", lambda *a, **k: None),
    ):
        asyncio.run(bot.imagine_cmd(update, ctx))
    assert ctx.user_data.get(bot._CHAT_ACTIVE) is True
    assert update.message.replies
    assert "Сгенерировать фото" in update.message.replies[0]
    assert not any("только из активного" in r for r in update.message.replies)


def test_style_batch_gate_and_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_gen, "image_gen_max_concurrent", lambda: 4)
    assert bot._style_batch_gate("сделай ярче", 3) is None
    assert bot._style_batch_gate("", 4) is None
    assert bot._style_batch_gate("А" * 121, 2)
    detail = "убери карандаш из манжеты женщины и поправь тень на столе"
    assert bot._style_batch_gate(detail, 1) is None
    assert "не больше" in (bot._style_batch_gate("", 5) or "")
    brighter = bot._style_redraw_prompt_for_api("сделай ярче")
    assert brighter.startswith(image_gen.STYLE_REDRAW_DEFAULT_PROMPT)
    assert brighter.endswith("сделай ярче")
    assert bot._style_redraw_prompt_for_api("") == ""
    assert detail in bot._style_redraw_prompt_for_api(detail)


def test_style_batch_shares_short_note_and_deletes_sources(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    monkeypatch.setattr(image_gen, "image_gen_max_concurrent", lambda: 4)
    ctx = _FakeContext()
    ctx.user_data[bot._CHAT_IMAGINE_STYLE_PROMPT_MSG] = 55
    ctx.user_data[bot._CHAT_IMAGINE_STYLE_PHOTO_WAIT] = True
    update = _FakeUpdate()
    calls: list[dict] = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return b"png"

    with (
        patch.object(bot, "_request_image_style_redraw_from_server", side_effect=_fake),
        patch.object(bot, "_send_post_imagine_menu", new=AsyncMock()),
        patch.object(bot.user_storage, "chat_dialog_upsert", lambda *a, **k: None),
    ):
        asyncio.run(
            bot._handle_chat_imagine_style_batch(
                update,
                ctx,
                user_id=1,
                chat_id=9,
                items=[(11, b"a"), (12, b"b")],
                notes="сделай ярче",
            ),
        )

    assert len(calls) == 2
    assert calls[0]["notes"] == calls[1]["notes"]
    assert calls[0]["notes"].endswith("сделай ярче")
    assert (9, 11) in ctx.bot.deleted
    assert (9, 12) in ctx.bot.deleted
    assert (9, 55) in ctx.bot.deleted
    assert bot._CHAT_IMAGINE_STYLE_PHOTO_WAIT not in ctx.user_data
    assert all(photo[2] == "" for photo in ctx.bot.sent_photos)


def test_style_batch_rejects_five_photos_and_long_album_note(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "k")
    monkeypatch.setattr(image_gen, "image_gen_max_concurrent", lambda: 4)
    ctx = _FakeContext()
    update = _FakeUpdate()
    called = False

    async def _fake(**kwargs):
        nonlocal called
        called = True
        return b"png"

    with patch.object(bot, "_request_image_style_redraw_from_server", side_effect=_fake):
        asyncio.run(
            bot._handle_chat_imagine_style_batch(
                update,
                ctx,
                user_id=1,
                chat_id=9,
                items=[(i, b"x") for i in range(1, 6)],
                notes="",
            ),
        )
        asyncio.run(
            bot._handle_chat_imagine_style_batch(
                update,
                ctx,
                user_id=1,
                chat_id=9,
                items=[(1, b"a"), (2, b"b")],
                notes="А" * 121,
            ),
        )

    assert called is False
    assert any("не больше" in r for r in update.message.replies)
    assert any("общее уточнение" in r for r in update.message.replies)
