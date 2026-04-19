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


def test_markdown_to_telegram_html_bold_italic() -> None:
    out = tf.markdown_to_telegram_html("Это **жирный** и _курсив_, ~~зачёркнут~~.")
    assert "<b>жирный</b>" in out
    assert "<i>курсив</i>" in out
    assert "<s>зачёркнут</s>" in out


def test_markdown_to_telegram_html_inline_and_fenced_code() -> None:
    src = "Используй `print(1)`.\n\n```python\nfor i in range(2):\n    print(i)\n```\n"
    out = tf.markdown_to_telegram_html(src)
    assert "<code>print(1)</code>" in out
    assert "<pre><code class=\"language-python\">" in out
    assert "for i in range(2):" in out


def test_markdown_to_telegram_html_link_only_safe_schemes() -> None:
    out = tf.markdown_to_telegram_html("[Сайт](https://example.com) и [фейк](javascript:alert)")
    assert '<a href="https://example.com">Сайт</a>' in out
    assert "javascript:" not in out
    assert "фейк" in out


def test_markdown_to_telegram_html_escapes_html_in_text() -> None:
    out = tf.markdown_to_telegram_html("В тексте <b>не bold</b> и & нужно экранировать")
    assert "&lt;b&gt;" in out
    assert "&amp;" in out


def test_markdown_to_telegram_html_lists_become_bullets() -> None:
    out = tf.markdown_to_telegram_html("- первый\n- **второй**\n  - вложенный\n")
    assert "• первый" in out
    assert "• <b>второй</b>" in out
    assert "  • вложенный" in out


def test_markdown_to_telegram_html_headings() -> None:
    out = tf.markdown_to_telegram_html("# Заголовок\n\nТекст\n## Подзаголовок\n")
    assert "<b>Заголовок</b>" in out
    assert "<b>Подзаголовок</b>" in out


def test_markdown_to_telegram_html_partial_stream_safe() -> None:
    out = tf.markdown_to_telegram_html("Половина **жирного без закрытия")
    assert "&lt;" not in out
    assert "**жирного" in out
    out2 = tf.markdown_to_telegram_html("Хвост")
    assert out2 == "Хвост"
