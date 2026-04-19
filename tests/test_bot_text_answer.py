"""Кнопка «Ответить текстом» в главном меню."""

from __future__ import annotations

import bot as bot_module
import user_storage


def test_get_main_keyboard_includes_answer_text_when_hw_complete() -> None:
    p = user_storage.UserProfile(
        user_id=1,
        grade=7,
        textbook_slug="x",
        textbook_url="http://x",
        textbook_label="L",
        is_premium=False,
        hw_paragraph="5",
        hw_exercise="1",
        hw_page=None,
        subject_slug="matematika",
    )
    kb = bot_module.get_main_keyboard(uploaded=False, profile=p, user_id=1)
    flat_cb = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "answer_text" in flat_cb
    kb2 = bot_module.get_main_keyboard(uploaded=True, profile=p, user_id=1)
    flat2 = [b.callback_data for row in kb2.inline_keyboard for b in row]
    assert "answer_text" in flat2


def test_format_text_answer_prompt_single_pre_block() -> None:
    out = bot_module._format_text_answer_prompt_html("а) 1 + 1\nб) 2 + 2")
    assert out.count("<pre>") == 1
    assert out.count("</pre>") == 1
    assert "Условия задачи (можно скопировать и дописать решение)" in out
    assert "Черновик" not in out
    assert "а) 1 + 1" in out


def test_format_text_answer_prompt_empty_condition() -> None:
    out = bot_module._format_text_answer_prompt_html("")
    assert "<pre>" not in out
    assert "gdz.ru" in out.lower()
