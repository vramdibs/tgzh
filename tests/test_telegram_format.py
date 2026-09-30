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
            "- Задача 6 а) группировка",
            r"- \((298 + 102) + 386 = 786\) - верно",
            r"- 2 \cdot 5 \cdot 19 - сначала 10, а не 900. Ошибка в ответе",
            "- Без текста условия с фото нельзя сказать",
            "- решение неверно",
            "? формулировка из учебника на фото не видна",
        ]
    )
    out = tf.prepare_check_display_text(src)
    lines = out.splitlines()
    assert lines[0].startswith("- Задача 6")
    assert "✅" not in lines[0]
    assert lines[1].startswith("- ")
    assert "✅" not in lines[1]
    assert "**верно**" in lines[1]
    assert "\\" not in lines[1]
    assert lines[2].startswith("- ")
    assert "❌" not in lines[2]
    assert lines[3].startswith("- ")
    assert "❓" not in lines[3]
    assert lines[4].startswith("- ")
    assert "❌" not in lines[4]
    assert "**верно**" not in lines[4]
    assert lines[5].startswith("❓ ")
    assert not lines[5].startswith("?")
    assert "**формулировка из учебника на фото не видна**" in lines[5].lower()


def test_prepare_check_display_splits_examples_one_mark() -> None:
    src = (
        "- Задача №6 (п. а): сложение. "
        "✓ (298+102)+386=786 - итог верно. "
        "✓ (489+11)+489=989 - итог верно. "
        "✗ 2·5·19=900 - ошибка в ответе."
    )
    lines = tf.prepare_check_display_text(src).splitlines()
    assert lines[0] == "- **Задача №6 (п. а)**:"
    assert "✅" not in lines[0]
    joined = "\n".join(lines)
    assert any(line.startswith("✅ (298+102)+386=786") for line in lines)
    assert any("**верно**" in line and "(298+102)+386=786" in line for line in lines)
    assert any(line.startswith("✅ (489+11)+489=989") for line in lines)
    assert any(line.startswith("❌ 2·5·19=900") for line in lines)
    assert joined.count("✅") == 2
    assert joined.count("❌") == 1
    assert "✓" not in joined
    assert "✗" not in joined


def test_prepare_check_display_task_number_missing_text_and_answer_line() -> None:
    src = "\n".join(
        [
            "Задача №8: текста задачи в учебнике на фото нет, записи выражений есть.",
            "- формулировка из учебника на фото не видна.",
            "- (731-296)=435 - верно. Ответ 435 верно",
        ]
    )
    lines = tf.prepare_check_display_text(src).splitlines()
    assert lines[0] == "**Задача №8**:"
    assert any("**текста задачи в учебнике на фото нет**" in line.lower() for line in lines)
    assert not any(line.startswith("❓ ") and "текста задачи" in line.lower() for line in lines)
    formul = next(line for line in lines if "формулировка" in line.lower())
    assert formul.startswith("- ")
    assert "❓" not in formul
    assert "**формулировка из учебника на фото не видна**" in formul.lower()
    assert any(line.startswith("Ответ 435") for line in lines)
    assert not any("(731-296)=435" in line and "Ответ" in line for line in lines)


def test_prepare_check_display_collapses_extra_stars_around_missing_text() -> None:
    src = "****Текста задачи из учебника на фото нет****"
    out = tf.prepare_check_display_text(src)
    assert "****" not in out
    assert "**Текста задачи из учебника на фото нет**" in out


def test_prepare_check_display_replaces_em_dash() -> None:
    src = "пункт а) \u2014 три примера, ответ 989 \u2013 верно"
    out = tf.prepare_check_display_text(src)
    assert "\u2014" not in out
    assert "\u2013" not in out
    assert "пункт а) - три примера" in out
    assert "989 - **верно**" in out


def test_prepare_check_display_breaks_after_colon_not_division() -> None:
    src = "Задача №6: пункт а) три примера\n12 : 4 = 3"
    lines = tf.prepare_check_display_text(src).splitlines()
    assert lines[0] == "**Задача №6**:"
    assert lines[1].startswith("пункт а)")
    assert any("12 : 4 = 3" in line for line in lines)
    assert not any(line.strip() == "4 = 3" for line in lines)


def test_prepare_check_display_breaks_comma_expressions() -> None:
    src = "298 + 102 = 400, 400 + 386 = 786\nтри примера на сложение со скобками и группировкой"
    lines = tf.prepare_check_display_text(src).splitlines()
    assert "298 + 102 = 400" in lines
    assert "400 + 386 = 786" in lines
    assert any("со скобками и группировкой" in line for line in lines)
    prose = next(line for line in lines if "группировкой" in line)
    assert "," not in prose or "скобками и группировкой" in prose
    src_verno = "ответ 786, верно"
    out_verno = tf.prepare_check_display_text(src_verno)
    assert "\n" not in out_verno
    assert "786, **верно**" in out_verno or "786, верно" in out_verno.lower()


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
