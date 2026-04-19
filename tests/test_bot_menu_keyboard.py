"""Меню команд и снятие reply-клавиатуры (Telegram API через моки)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import MenuButtonCommands, MenuButtonDefault
from telegram.error import BadRequest

import bot as bot_module


@pytest.mark.asyncio
async def test_flow_ensure_chat_commands_menu_calls_set_chat_menu_button() -> None:
    bot = MagicMock()
    bot.set_chat_menu_button = AsyncMock(return_value=True)
    await bot_module.flow_ensure_chat_commands_menu(bot, 12345)
    assert bot.set_chat_menu_button.await_count == 2
    bot.set_chat_menu_button.assert_any_await(
        chat_id=12345,
        menu_button=MenuButtonDefault(),
    )
    bot.set_chat_menu_button.assert_any_await(menu_button=MenuButtonCommands())


@pytest.mark.asyncio
async def test_flow_ensure_chat_commands_menu_swallows_bad_request() -> None:
    bot = MagicMock()
    bot.set_chat_menu_button = AsyncMock(
        side_effect=[BadRequest("not a private chat"), True],
    )
    await bot_module.flow_ensure_chat_commands_menu(bot, -100123)
    assert bot.set_chat_menu_button.await_count == 2


@pytest.mark.asyncio
async def test_flow_remove_reply_keyboard_then_ensures_menu() -> None:
    bot = MagicMock()
    rm = MagicMock()
    rm.message_id = 777
    bot.send_message = AsyncMock(return_value=rm)
    bot.delete_message = AsyncMock()
    bot.set_chat_menu_button = AsyncMock(return_value=True)
    await bot_module.flow_remove_reply_keyboard(bot, 42)
    bot.send_message.assert_awaited_once()
    bot.delete_message.assert_awaited_once_with(42, 777)
    assert bot.set_chat_menu_button.await_count == 2
    bot.set_chat_menu_button.assert_any_await(
        chat_id=42,
        menu_button=MenuButtonDefault(),
    )
    bot.set_chat_menu_button.assert_any_await(menu_button=MenuButtonCommands())
