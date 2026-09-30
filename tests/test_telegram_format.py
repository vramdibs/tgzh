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


def test_prepare_check_display_plain_math_no_backslash() -> None:
    src = r"\((298 + 102) + 386 = 786\) и 2 \cdot 5 \mathbf{190} \frac{2}{3}"
    out = tf.prepare_check_display_text(src)
    assert "\\" not in out
    assert "frac" not in out
    assert "cdot" not in out
    assert "mathbf" not in out
    assert "(298 + 102) + 386 = 786" in out
    assert "2 · 5" in out
    assert "190" in out
    assert "2/3" in out


def test_prepare_check_display_line_marks_and_bold_verno() -> None:
    src = "\n".join(
        [
            "- Задача 6 а): группировка",
            r"- \((298 + 102) + 386 = 786\) - верно",
            r"- 2 \cdot 5 \cdot 19 - сначала 10, а не 900. Ошибка в ответе",
            "- Без текста условия с фото нельзя сказать",
            "- решение неверно",
        ]
    )
    out = tf.prepare_check_display_text(src)
    lines = out.splitlines()
    assert lines[0].startswith("- Задача 6")
    assert "✅" not in lines[0]
    assert lines[1].startswith("✅ ")
    assert not lines[1].startswith("-")
    assert "**верно**" in lines[1]
    assert "\\" not in lines[1]
    assert lines[2].startswith("❌ ")
    assert lines[3].startswith("❓ ")
    assert lines[4].startswith("❌ ")
    assert "**верно**" not in lines[4]


def test_prepare_check_display_splits_examples_one_mark() -> None:
    src = (
        "- Задача №6 (п. а): сложение. "
        "✓ (298+102)+386=786 - итог верно. "
        "✓ (489+11)+489=989 - итог верно. "
        "✗ 2·5·19=900 - ошибка в ответе."
    )
    lines = tf.prepare_check_display_text(src).splitlines()
    assert lines[0] == "**Задача №6 (п. а)**: сложение."
    assert "✅" not in lines[0]
    assert lines[1].startswith("✅ (298+102)+386=786")
    assert lines[1].count("✅") == 1
    assert "**верно**" in lines[1]
    assert lines[2].startswith("✅ (489+11)+489=989")
    assert lines[2].count("✅") == 1
    assert lines[3].startswith("❌ 2·5·19=900")
    assert lines[3].count("❌") == 1
    assert "✓" not in "\n".join(lines)
    assert "✗" not in "\n".join(lines)


def test_prepare_check_display_task_number_missing_text_and_answer_line() -> None:
    src = "\n".join(
        [
            "Задача №8: текста задачи в учебнике на фото нет, записи выражений есть.",
            "- формулировка из учебника на фото не видна.",
            "- (731-296)=435 - верно. Ответ 435 верно",
        ]
    )
    lines = tf.prepare_check_display_text(src).splitlines()
    assert lines[0].startswith("❓ ")
    assert "**текста задачи в учебнике на фото нет**" in lines[0].lower()
    assert "**Задача №8**" in lines[0]
    assert lines[1].startswith("❓ ")
    assert "**формулировка из учебника на фото не видна**" in lines[1].lower()
    assert "Ответ" not in lines[2]
    assert lines[3].startswith("✅ Ответ 435")


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
