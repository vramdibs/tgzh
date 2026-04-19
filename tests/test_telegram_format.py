"""Форматирование текста для Telegram."""

from __future__ import annotations

import telegram_format as tf


def test_format_llm_check_reply_plain_dashes_and_paragraphs() -> None:
    src = (
        "Ты выполнил большинство заданий правильно, особенно в третьем фото — там все верно. "
        "В первом фото ты допустил одну ошибку — в задании 2 плохо. В остальных заданиях все ок. "
        "Во втором фото ты дал ответы. В целом, хорошо — это итог."
    )
    out = tf.format_llm_check_reply_plain(src)
    assert "\u2014" not in out
    assert " - " in out or out.count("-") >= 3
    assert ".\n\nВ первом фото" in out
    assert ".\n\nВ остальных заданиях" in out
    assert ".\n\nВо втором фото" in out
    assert ".\n\nВ целом," in out


def test_format_llm_check_reply_plain_empty() -> None:
    assert tf.format_llm_check_reply_plain("") == ""
    assert tf.format_llm_check_reply_plain("   ") == ""
