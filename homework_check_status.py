"""
Эвристика статуса проверки ДЗ по тексту ответа модели (эмодзи в заголовке сообщения бота).
"""

from __future__ import annotations

import re
from typing import Literal

VerdictKind = Literal["absent", "partial", "correct"]

# Метка в конце ответа модели при сценарии «смешанные числа» (см. промпт в ai_checker).
# В ответе VLM иногда вставляет пробелы: [ tgzh_mixed_numbers ] — учитываем при снятии и детекте.
TGZH_MIXED_NUMBERS_MARKER = "[tgzh_mixed_numbers]"
_MIXED_NUMBERS_TOKEN_RE = r"\[\s*tgzh_mixed_numbers\s*\]"
_MIXED_NUMBERS_MARKER_RE = re.compile(_MIXED_NUMBERS_TOKEN_RE, flags=re.IGNORECASE)
_TRAILING_MIXED_NUMBERS_MARKER_RE = re.compile(
    r"(\s*\n*" + _MIXED_NUMBERS_TOKEN_RE + r"\s*)$",
    flags=re.IGNORECASE,
)


def homework_check_has_mixed_numbers_marker(raw: str) -> bool:
    return bool(_MIXED_NUMBERS_MARKER_RE.search(raw or ""))


def strip_homework_check_machine_tags(text: str) -> str:
    """Убирает служебные метки перед показом текста ученику (в т.ч. с пробелами внутри скобок)."""
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"(?i)[ \t]*" + _MIXED_NUMBERS_TOKEN_RE + r"[ \t]*", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t.rstrip()


def detach_trailing_mixed_numbers_marker(text: str) -> tuple[str, str]:
    """
    Отрезает хвостовую метку смешанных чисел, если она в самом конце строки.
    Возвращает (текст_до_метки, хвост_с_меткой_или_пусто) — чтобы вставить футер между ними.
    """
    if not text:
        return "", ""
    t = text.rstrip()
    m = _TRAILING_MIXED_NUMBERS_MARKER_RE.search(t)
    if not m:
        return t, ""
    head = t[: m.start()].rstrip()
    return head, m.group(1)


def homework_check_mixed_number_escalation_active(outs: list[str], body_raw: str) -> bool:
    """True, если хотя бы в одном фрагменте ответа есть сценарий смешанных чисел."""
    if homework_check_has_mixed_numbers_marker(body_raw):
        return True
    return any(homework_check_has_mixed_numbers_marker(o) for o in outs)

_CHECK_RESULT_FOOTER_HTML = (
    "\n\n"
    "<i>Нейросеть может давать неправильные ответы - это стимул прокачать скиллы, выполнив ручную проверку.</i>\n\n"
    "<b>Оцени ответ:</b>\n"
)

_RESULT_TITLE_HTML = "<b>Результат проверки:</b>\n\n"


def _verdict_zone(text: str) -> str:
    low = text.lower()
    i = low.rfind("вывод:")
    if i >= 0:
        return text[i:]
    tail = 1500
    return text[-tail:] if len(text) > tail else text


def _compute_verdict(raw: str) -> VerdictKind:
    """Три категории для счетчиков в bot_stats."""
    s = (raw or "").strip()
    if not s:
        return "partial"

    if homework_check_has_mixed_numbers_marker(s):
        return "partial"

    low = s.lower()

    absent_markers = (
        "решение отсутствует",
        "решения нет",
        "нет решения",
        "нет письменного решения",
        "отсутствует решение",
        "отсутствует письменное решение",
        "не представлено решение",
        "решение не представлено",
        "не видно решения",
        "не видно письменного решения",
        "на изображении нет решения",
        "на фото нет решения",
        "работа не выполнена",
        "задание не выполнено",
        "лист пуст",
        "пустой лист",
        "ничего не написано",
        "не написано решение",
        "не удалось распознать решение",
        "решение не обнаружено",
    )
    if any(m in low for m in absent_markers):
        return "absent"

    if "[тестовый режим]" in low:
        return "partial"

    zone = _verdict_zone(s)
    zl = zone.lower()

    partial_markers = (
        "частично",
        "не полностью",
        "не целиком",
        "не удалось полностью",
        "однако",
        " но ",
        ", но ",
        "с ошибками",
        "есть ошибки",
        "содержит ошибки",
        "существенн",
        "неверно",
        "неправильно",
        "не верно",
        "нужно исправить",
        "требует доработки",
        "неполн",
        "недостаточно",
        "неясно",
        "нельзя однозначно",
        "ограниченно",
        "только часть",
        "лишь часть",
        "проблем",
        "недочёт",
        "недочет",
        "не удалось проверить",
        "частичная проверка",
        "полностью проверить не удалось",
    )
    has_partial = any(m in zl for m in partial_markers)

    correct_markers = (
        "решение верно",
        "решение полностью верно",
        "ответ верный",
        "ответ правильный",
        "ответ верен",
        "все верно",
        "все шаги верны",
        "ошибок нет",
        "без ошибок",
        "ошибок в вычислениях нет",
        "полностью соответствует",
        "полностью корректн",
    )
    has_correct = any(m in zl for m in correct_markers)

    if has_correct and not has_partial:
        return "correct"
    if has_correct and has_partial:
        return "partial"
    if has_partial:
        return "partial"

    if any(m in zl for m in ("ошибок нет", "без ошибок", "все верно")):
        return "correct"

    return "partial"


def homework_check_stats_result(raw: str) -> VerdictKind:
    """
    Три категории для счетчиков в bot_stats и заголовка в чате (эмодзи до текста результата).
    Сценарий смешанных чисел: partial (метка в конце ответа модели).
    """
    return _compute_verdict(raw or "")


def homework_check_stats_verdict(raw: str) -> VerdictKind:
    """
    Три категории для счетчиков в bot_stats (как прежняя логика ✅ / ☑️ / ❌).

    absent - решения на работе нет / не видно
    partial - оговорки, ошибки, неполная проверка, тестовый режим
    correct - по тексту вывода похоже на полностью верное решение без противоречий
    """
    return homework_check_stats_result(raw)


def homework_check_status_emoji(raw: str) -> str:
    """
    В интерфейсе только ❌ (нет решения на работе) или ✅ (остальное).
    Оговорка про ограничения нейросети - в format_check_result_suffix_html (после текста модели).
    """
    if homework_check_stats_verdict(raw) == "absent":
        return "❌"
    return "✅"


def format_check_result_suffix_html() -> str:
    """HTML после тела ответа LLM: дисклеймер и подпись к кнопкам 👍/👎."""
    return _CHECK_RESULT_FOOTER_HTML


def format_check_result_prefix(raw: str) -> str:
    """Заголовок блока результата для Telegram (до HTML-разметки тела)."""
    if (
        not raw
        or raw.startswith("Ошибка связи с сервером")
        or raw.startswith("Ошибка:")
    ):
        return _RESULT_TITLE_HTML
    verdict = homework_check_stats_result(raw)
    if verdict == "absent":
        return f"❌ {_RESULT_TITLE_HTML}"
    return f"✅ {_RESULT_TITLE_HTML}"


def format_merged_check_prefix(raw_parts: list[str]) -> str:
    """
    Префикс, если итог собран из нескольких ответов модели без текстовой сводки.
    ❌ если хотя бы одна часть absent, иначе ✅.
    """
    if not raw_parts:
        return _RESULT_TITLE_HTML
    results = [homework_check_stats_result((p or "").strip()) for p in raw_parts]
    if any(v == "absent" for v in results):
        return f"❌ {_RESULT_TITLE_HTML}"
    return f"✅ {_RESULT_TITLE_HTML}"
