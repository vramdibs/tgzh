"""Inline-меню /chat: новые кнопки «Мои чаты» и список сохранённых диалогов."""

from __future__ import annotations

import bot
import user_storage


def _flatten(markup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def test_chat_menu_active_contains_all_five_buttons() -> None:
    flat = _flatten(bot._chat_menu_keyboard(active=True))
    callbacks = [c for _, c in flat]
    assert "chat:new" in callbacks
    assert "chat:list" in callbacks
    assert "chat:purge" in callbacks
    assert "chat:logout" in callbacks
    assert "chat:back" in callbacks
    # Лимит проброшен в подпись (10 на пользователя по умолчанию).
    list_label = next(t for t, c in flat if c == "chat:list")
    assert str(user_storage.CHAT_DIALOG_HISTORY_LIMIT) in list_label
    assert "Мои чаты" in list_label


def test_chat_menu_inactive_only_back_button() -> None:
    flat = _flatten(bot._chat_menu_keyboard(active=False))
    assert flat == [("Вернуться к проверке ДЗ", "chat:back")]


def test_chat_dialog_list_keyboard_includes_open_buttons_and_back() -> None:
    dialogs = [
        user_storage.ChatDialogSummary(
            dialog_id=1, title="Первый", updated_at="", history_len=2,
        ),
        user_storage.ChatDialogSummary(
            dialog_id=2, title="Второй", updated_at="", history_len=4,
        ),
    ]
    flat = _flatten(bot._chat_dialog_list_keyboard(dialogs))
    callbacks = [c for _, c in flat]
    assert "chat:open:1" in callbacks
    assert "chat:open:2" in callbacks
    assert callbacks[-1] == "chat:menu"


def test_chat_dialog_list_keyboard_truncates_long_titles() -> None:
    long_title = "Заголовок " * 30
    dialogs = [
        user_storage.ChatDialogSummary(
            dialog_id=42, title=long_title, updated_at="", history_len=10,
        ),
    ]
    flat = _flatten(bot._chat_dialog_list_keyboard(dialogs))
    label, cb = flat[0]
    assert cb == "chat:open:42"
    # Подпись inline-кнопки не должна превышать 64 байта (мы режем до 64 символов).
    assert len(label) <= 64
    assert label.endswith("…")


def test_format_chat_dialog_when_handles_invalid_input() -> None:
    assert bot._format_chat_dialog_when("") == ""
    assert bot._format_chat_dialog_when("not-a-date") == ""


def test_format_chat_dialog_when_returns_short_local_format() -> None:
    s = bot._format_chat_dialog_when("2026-04-19T12:34:56+00:00")
    assert len(s) == len("dd.mm HH:MM")
    assert s[2] == "."
    assert s[5] == " "
    assert s[8] == ":"
