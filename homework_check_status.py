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

_TGZH_RESULT_TOKEN_RE = r"\[\s*tgzh_result\s*:\s*(?P<kind>correct|partial|incorrect)\s*\]"
_TGZH_RESULT_MARKER_RE = re.compile(_TGZH_RESULT_TOKEN_RE, flags=re.IGNORECASE)
_TGZH_OFFER_RE = re.compile(
    r"\[\s*tgzh_offer\s*:\s*(?P<ids>[^\]]+)\]",
    flags=re.IGNORECASE,
)


def _tgzh_result_kind_to_emoji(kind: str) -> str:
    k = (kind or "").strip().lower()
    if k == "correct":
        return "\u2705"
    if k == "incorrect":
        return "\u274c"
    return "\u2611\ufe0f"


def parse_tgzh_result_marker(raw: str) -> Literal["correct", "partial", "incorrect"] | None:
    m = _TGZH_RESULT_MARKER_RE.search(raw or "")
    if not m:
        return None
    return m.group("kind").lower()  # type: ignore[return-value]


def homework_check_has_mixed_numbers_marker(raw: str) -> bool:
    return bool(_MIXED_NUMBERS_MARKER_RE.search(raw or ""))


def strip_homework_check_machine_tags(text: str) -> str:
    """Убирает служебные метки перед показом текста ученику (в т.ч. с пробелами внутри скобок)."""
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"(?i)[ \t]*" + _MIXED_NUMBERS_TOKEN_RE + r"[ \t]*", "", t)
    t = _TGZH_RESULT_MARKER_RE.sub(
        lambda m: _tgzh_result_kind_to_emoji(m.group("kind")),
        t,
    )
    t = _TGZH_OFFER_RE.sub("", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t.rstrip()


_LIST_LINE_RE = re.compile(r"^(\s*)([-*•]|\d+[.)])\s+(.*)$")
_TASK_KEY_RE = re.compile(
    r"^(?P<key>"
    r"(?:№\s*\d+|"
    r"(?:задани[еяй]|задач[аи]|пункт[аы]?|упражнени[еяй]|вопрос[аы]?|часть|номер)"
    r"\s*(?:№\s*)?\d+(?:[a-zа-я])?(?:[.)])?"
    r")"
    r")",
    re.IGNORECASE,
)
_DUP_NORMALIZE_SPACES_RE = re.compile(r"\s+")
_DUP_STRIP_PUNCT_RE = re.compile(r"[«»\"'`*_]+")


def _normalize_for_dup(s: str) -> str:
    s = (s or "").strip().lower()
    s = _DUP_NORMALIZE_SPACES_RE.sub(" ", s)
    s = _DUP_STRIP_PUNCT_RE.sub("", s)
    return s


def dedupe_homework_check_lines(text: str) -> str:
    """
    Снимает повторы из ответа модели (страховка от «петель» VLM):
    - подряд идущие пустые строки схлопываются до одной;
    - точные дубликаты строк (после нормализации регистра/пробелов/кавычек/звёздочек) убираются;
    - несколько пунктов вида «- Задание N: ...» по одному и тому же N оставляем только один раз;
      сюда же попадают синонимы — Задача N, Упражнение N, Пункт N, Вопрос N, Номер N, № N
      (распознаются вместе с подпунктом-буквой: 1а, 1б — это разные ключи).

    Свободный текст (не список) не схлопывается, кроме точных повторов.
    """
    if not text:
        return text or ""
    lines = text.split("\n")
    out: list[str] = []
    seen_lines: set[str] = set()
    seen_task_keys: set[str] = set()
    last_blank = False
    for line in lines:
        norm = _normalize_for_dup(line)
        if not norm:
            if last_blank or not out:
                continue
            out.append("")
            last_blank = True
            continue
        if norm in seen_lines:
            continue
        m_list = _LIST_LINE_RE.match(line)
        if m_list:
            body = m_list.group(3).strip()
            m_task = _TASK_KEY_RE.match(body)
            if m_task:
                key = _normalize_for_dup(m_task.group("key"))
                if key in seen_task_keys:
                    continue
                seen_task_keys.add(key)
        seen_lines.add(norm)
        out.append(line)
        last_blank = False
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


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

    tgzh_result = parse_tgzh_result_marker(s)
    if tgzh_result == "correct":
        return "correct"
    if tgzh_result in ("partial", "incorrect"):
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

    hard_partial_markers = (
        "существенн",
        "неверно",
        "неправильно",
        "не верно",
        "с ошибками",
        "есть ошибки",
        "содержит ошибки",
        "ошибк",
        "недочёт",
        "недочет",
        "нужно исправить",
        "требует доработки",
        "не удалось проверить",
        "частичная проверка",
        "полностью проверить не удалось",
    )
    soft_partial_markers = (
        "частично",
        "не полностью",
        "не целиком",
        "не удалось полностью",
        "однако",
        " но ",
        ", но ",
        "неполн",
        "недостаточно",
        "неясно",
        "нельзя однозначно",
        "ограниченно",
        "только часть",
        "лишь часть",
        "проблем",
    )
    has_hard_partial = any(m in zl for m in hard_partial_markers)
    has_soft_partial = any(m in zl for m in soft_partial_markers)

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

    # Жёсткие partial-маркеры всегда блокируют correct: модель прямо сказала «ошибки/неверно».
    if has_hard_partial:
        return "partial"
    # Хоть один correct-маркер и нет жёстких partial → correct.
    # Мягкие partial («но», «частично», «однако» и т. п.) при наличии явного correct
    # больше не понижают вердикт — это были стилистические оговорки модели.
    if has_correct:
        return "correct"
    if has_soft_partial:
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
    """Заголовок блока результата для Telegram. Без эмодзи: пометки стоят у строк примеров."""
    _ = raw
    return _RESULT_TITLE_HTML


def format_merged_check_prefix(raw_parts: list[str]) -> str:
    """Префикс сводки из нескольких ответов: тот же заголовок без эмодзи."""
    _ = raw_parts
    return _RESULT_TITLE_HTML
