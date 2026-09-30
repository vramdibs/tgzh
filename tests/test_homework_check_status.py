"""Эвристика статуса проверки по тексту ответа модели."""

from __future__ import annotations

import homework_check_status as hcs


def test_absent_solution_cross() -> None:
    assert hcs.homework_check_stats_verdict("На фото нет решения, лист пуст.") == "absent"
    assert hcs.homework_check_stats_result("На фото нет решения, лист пуст.") == "absent"
    assert hcs.homework_check_status_emoji("На фото нет решения, лист пуст.") == "\u274c"


def test_mock_partial_stats_green_emoji() -> None:
    t = "[Тестовый режим] Получено."
    assert hcs.homework_check_stats_verdict(t) == "partial"
    assert hcs.homework_check_status_emoji(t) == "\u2705"


def test_verdict_fully_correct() -> None:
    text = (
        "Разбор...\n\n"
        "Вывод: Решение верно, ответ правильный, ошибок нет."
    )
    assert hcs.homework_check_stats_verdict(text) == "correct"
    assert hcs.homework_check_status_emoji(text) == "\u2705"


def test_verdict_correct_with_soft_caveat_stays_correct() -> None:
    text = (
        "Вывод: Решение верно. Однако оформление можно улучшить."
    )
    assert hcs.homework_check_stats_verdict(text) == "correct"
    assert hcs.homework_check_status_emoji(text) == "\u2705"


def test_verdict_correct_with_hard_partial_marker_drops_to_partial() -> None:
    text = "Вывод: Ответ верный, но в вычислениях есть ошибки."
    assert hcs.homework_check_stats_verdict(text) == "partial"
    assert hcs.homework_check_status_emoji(text) == "\u2705"


def test_errors_partial_stats() -> None:
    text = "Вывод: В решении есть ошибки в вычислениях."
    assert hcs.homework_check_stats_verdict(text) == "partial"
    assert hcs.homework_check_status_emoji(text) == "\u2705"


def test_format_prefix_errors_plain() -> None:
    assert hcs.format_check_result_prefix("Ошибка связи с сервером: x") == (
        "<b>Результат проверки:</b>\n\n"
    )


def test_format_prefix_absent_no_gdz_line() -> None:
    p = hcs.format_check_result_prefix("На фото нет решения, лист пуст.")
    assert p == "<b>Результат проверки:</b>\n\n"
    assert "\u274c" not in p
    assert "ГДЗ" not in p
    assert "Оценка выполнения" not in p


def test_format_prefix_ok_no_footer_in_prefix() -> None:
    p = hcs.format_check_result_prefix("Вывод: Решение верно.")
    assert p == "<b>Результат проверки:</b>\n\n"
    assert "\u2705" not in p
    assert "Оценка выполнения" not in p
    assert "ГДЗ" not in p


def test_format_suffix_disclaimer_and_vote_label() -> None:
    s = hcs.format_check_result_suffix_html()
    assert s.startswith("\n\n")
    assert "нейросеть" in s.lower()
    assert "скилл" in s.lower()
    assert "ручную проверку" in s.lower()
    assert "<b>Оцени ответ:</b>" in s
    assert "<i>" in s


def test_mixed_numbers_marker_and_strip() -> None:
    raw = "Не могу проверить смешанные числа. Реши сам.\n[tgzh_mixed_numbers]"
    assert hcs.homework_check_stats_result(raw) == "partial"
    assert hcs.homework_check_status_emoji(raw) == "\u2705"
    assert hcs.strip_homework_check_machine_tags(raw) == "Не могу проверить смешанные числа. Реши сам."


def test_mixed_numbers_marker_strip_spaced_brackets() -> None:
    raw = "Задание 6: смешанные числа.\n[ tgzh_mixed_numbers ]\n\nМодель: x"
    out = hcs.strip_homework_check_machine_tags(raw)
    assert "tgzh_mixed" not in out.lower()
    assert "Задание 6" in out
    assert "Модель" in out


def test_tgzh_result_marker_strip_to_emoji() -> None:
    raw = "Задача 6: ошибка.\n[tgzh_result:partial]"
    out = hcs.strip_homework_check_machine_tags(raw)
    assert "tgzh_result" not in out.lower()
    assert "\u2611\ufe0f" in out
    assert hcs.homework_check_stats_result(raw) == "partial"


def test_tgzh_result_correct_verdict_and_emoji() -> None:
    raw = "Все верно.\n[tgzh_result:correct]"
    assert hcs.homework_check_stats_result(raw) == "correct"
    out = hcs.strip_homework_check_machine_tags(raw)
    assert "\u2705" in out
    assert "tgzh_result" not in out.lower()


def test_tgzh_result_incorrect_maps_to_partial() -> None:
    raw = "Неверно.\n[ tgzh_result : incorrect ]"
    assert hcs.homework_check_stats_result(raw) == "partial"
    out = hcs.strip_homework_check_machine_tags(raw)
    assert "\u274c" in out


def test_detach_trailing_mixed_numbers_marker() -> None:
    raw = "Текст.\n[tgzh_mixed_numbers]"
    head, trail = hcs.detach_trailing_mixed_numbers_marker(raw)
    assert head == "Текст."
    assert "tgzh_mixed_numbers" in trail.lower()

    h3, t3 = hcs.detach_trailing_mixed_numbers_marker("Текст.\n[ tgzh_mixed_numbers ]")
    assert h3 == "Текст."
    assert "tgzh_mixed_numbers" in t3.lower()

    h2, t2 = hcs.detach_trailing_mixed_numbers_marker("без метки")
    assert h2 == "без метки"
    assert t2 == ""


def test_mixed_numbers_escalation_any_part() -> None:
    outs = ["Ошибки.", "Лимит\n[TGZH_MIXED_NUMBERS]"]
    assert hcs.homework_check_mixed_number_escalation_active(outs, "**Фото 1**\nx") is True


def test_format_merged_prefix_any_absent() -> None:
    p = hcs.format_merged_check_prefix(
        [
            "Вывод: Решение верно.",
            "На фото нет решения, лист пуст.",
        ],
    )
    assert p == "<b>Результат проверки:</b>\n\n"
    assert "\u274c" not in p
    assert "Оценка выполнения" not in p


def test_format_merged_prefix_all_ok() -> None:
    p = hcs.format_merged_check_prefix(
        [
            "Вывод: Решение верно, ошибок нет.",
            "Вывод: В решении есть ошибки в вычислениях.",
        ],
    )
    assert p == "<b>Результат проверки:</b>\n\n"
    assert "\u2705" not in p
    assert "Оценка выполнения" not in p
    assert "%" not in p


def test_dedupe_collapses_repeated_task_bullets() -> None:
    raw = (
        "- Задание 1: ответ **8**, не отмечено на координатной прямой.\n"
        "- Задание 1: не указано, что число 8 - это среднее арифметическое.\n"
        "- Задание 1: не объяснено, как это связано с делением отрезка пополам.\n"
        "- Задание 1: не указано, что на координатной прямой отмечены числа 4 и 12.\n"
        "- Задание 2: верно."
    )
    out = hcs.dedupe_homework_check_lines(raw)
    lines = [l for l in out.split("\n") if l.strip()]
    assert len(lines) == 2
    assert lines[0].startswith("- Задание 1:")
    assert lines[1].startswith("- Задание 2:")


def test_dedupe_keeps_distinct_subparts() -> None:
    raw = (
        "- Задание 1а: верно.\n"
        "- Задание 1б: ошибка в **3/8**.\n"
        "- Задание 2: верно."
    )
    out = hcs.dedupe_homework_check_lines(raw)
    assert out.count("Задание 1") == 2
    assert "Задание 2" in out


def test_dedupe_drops_exact_duplicate_lines() -> None:
    raw = (
        "Решение видно на фото.\n"
        "Решение видно на фото.\n"
        "решение видно на фото.\n"
        "Дополнительный текст."
    )
    out = hcs.dedupe_homework_check_lines(raw)
    lines = [l for l in out.split("\n") if l.strip()]
    assert lines == ["Решение видно на фото.", "Дополнительный текст."]


def test_dedupe_collapses_blank_lines_and_preserves_order() -> None:
    raw = "А\n\n\nБ\n\nВ\n"
    out = hcs.dedupe_homework_check_lines(raw)
    assert out == "А\n\nБ\n\nВ"


def test_dedupe_handles_synonyms_for_task_label() -> None:
    raw = (
        "- Задача 3: ошибка.\n"
        "- Задача 3: пояснение к ошибке.\n"
        "- Упражнение 3: что-то ещё.\n"
        "- № 3: и снова про то же."
    )
    out = hcs.dedupe_homework_check_lines(raw)
    lines = [l for l in out.split("\n") if l.strip()]
    assert len(lines) == 3
    assert lines[0].startswith("- Задача 3:")
    assert lines[1].startswith("- Упражнение 3:")
    assert lines[2].startswith("- № 3:")


def test_dedupe_empty_and_none_safe() -> None:
    assert hcs.dedupe_homework_check_lines("") == ""
    assert hcs.dedupe_homework_check_lines("   \n\n  ") == ""
