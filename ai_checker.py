"""
Обёртка для проверки домашних заданий: заглушка, VLLM (OpenAI-совместимый chat + VL).
Промпты по умолчанию в коде; переопределение через переменные окружения (см. .env.example, префикс VLLM_PROMPT_ и др.).
"""

from __future__ import annotations

import base64
import logging
import os
from collections import defaultdict
from typing import Any, Final

from dotenv import load_dotenv

from homework_check_status import (
    TGZH_MIXED_NUMBERS_MARKER,
    dedupe_homework_check_lines,
    detach_trailing_mixed_numbers_marker,
)

load_dotenv()

logger = logging.getLogger(__name__)


def _vllm_request_timeout_s() -> float:
    raw = (os.getenv("VLLM_REQUEST_TIMEOUT_SEC") or "").strip()
    try:
        v = float(raw)
        return max(5.0, min(900.0, v))
    except ValueError:
        return 180.0

# Параметры модели (документация / лимиты)
VLLM_MODEL_DEFAULT: Final = "qwen/qwen3-vl-8b"
VLLM_CONTEXT_WINDOW: Final = 32768
VLLM_VISION: Final = True

# Строка в конце ответа ученику, если pre-OCR дал непустой блок (см. summarize_check_parts)
PREOCR_RESULT_FOOTER_LINE: Final = (
    "Предварительное распознавание текста (pre-OCR) использовано."
)

# Qwen3: отключить режим «размышлений» (vLLM: chat_template_kwargs; плюс суффикс в промпте).
# VLLM_NO_THINK=0 — не добавлять суффикс и не слать chat_template_kwargs
_NO_THINK_SUFFIX: Final = " /no_think"

# Расширения, которые считаем поддерживаемыми на стороне пайплайна (см. README)
VLLM_DOCUMENT_EXTENSIONS: Final[tuple[str, ...]] = (
    "doc",
    "docx",
    "html",
    "markdown",
    "md",
    "pdf",
    "rtf",
    "txt",
)

_MIME_TO_EXT: dict[str, str] = {
    "text/plain": "txt",
    "text/html": "html",
    "text/markdown": "md",
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
}

# --- Значения промптов по умолчанию (если в .env пусто или ключ не задан) ---

_DEFAULT_CHECK_HOMEWORK: Final = (
    "Учебник (ориентир): {book} ({g}).\n"
    "Пользователь указал задание: {anchor} (номера как в оглавлении/разметке на gdz.ru или в учебнике).\n\n"
    "По изображению: распознай ответ ученика на листе. Сверь с тем, что ожидается для этого задания "
    "(параграф + упражнение или параграф + страница проверочной работы).\n\n"
    "Для проверочных заданий (страница проверочной в привязке): не считай ошибкой отсутствие развернутого письменного решения; "
    "допустимы короткие ответы: да, нет, одно слово или число, если это по условию уместно.\n\n"
    "Для любых заданий: опиши существенные ошибки и что сделано верно. Не используй отдельный блок или заголовок Вывод "
    "(в том числе **Вывод:** в markdown). Если на фото другое задание - явно напиши об этом. Пиши по-русски."
)

_DEFAULT_PROMPT_IGNORE_GDZ: Final = (
    "Если на изображении виден текст сайта готовых домашних заданий — латиницей **gdz.ru** (любой регистр, в том числе водяной знак) "
    "или по-русски **гдз.ру**, **гдз ру**, **гдзру** и похожие написания без смысловой нагрузки для задачи — "
    "игнорируй их полностью: не считай частью ответа ученика и не включай в оценку."
)

_DEFAULT_PROMPT_GDZ_TASK_CONDITION: Final = (
    "Ниже - текст условия задачи с gdz.ru (формулировка как в учебнике). "
    "Сверь его с тем, что видишь на работе ученика. "
    "Это только постановка задачи, не готовое решение и не эталон ответа.\n\n"
    "В ответе ученику **не копируй и не вставляй целиком** этот текст условия — бот покажет его отдельно в сообщении с результатом. "
    "Сразу пиши **только разбор** по фото: кратко, ровно одна строка-пункт на каждое задание (1-2 предложения). "
    "**Не зацикливайся:** вывод по заданию сформулируй ОДИН раз; "
    "если у задания несколько претензий — объедини их в этой одной строке через запятую, "
    "не выписывай по три-десять пунктов с переформулировками одного и того же.\n\n"
    "{condition}"
)

_DEFAULT_PROMPT_RESPONSE_FORMAT: Final = (
    "ОБЯЗАТЕЛЬНОЕ оформление ответа для чата (если нарушишь - ответ неудобен ученику, так делать нельзя):\n"
    "1) По каждому заданию — РОВНО ОДНА строка-пункт списка (- Задание N: ...), максимум 1-2 коротких предложения; "
    "не разворачивай разбор одного задания в несколько пунктов и не пиши простыню.\n"
    "2) Неверный ответ ученика, неверное число или ключевая ошибка в формулировке - только в markdown-жирном: "
    "**краткая фраза или число** (ровно двойные звездочки вокруг); верные значения можно оставлять обычным текстом.\n"
    "3) Не обращайся к ученику обвиняющим тоном из-за смешанных чисел - см. отдельное правило про смешанные числа ниже.\n"
    "4) Пример допустимого фрагмента:\n"
    "- Задание 1: верно.\n"
    "- Задание 2: ошибка в вычитании, в работе **3/8**, ожидалось **5/8**.\n"
    "Пиши весь ответ в таком стиле.\n"
    "5) ЗАПРЕЩЕНО повторять один и тот же вывод разными словами: если по заданию уже сказано «не указано / не объяснено / не отмечено», "
    "не выписывай это ещё десятком пунктов с переформулировками — все замечания по заданию объедини в одну фразу.\n"
    "6) Если зацикливаешься (повторяешь подряд похожие пункты) — прервись и переходи к следующему заданию или закончи ответ."
)

_DEFAULT_PROMPT_RESPONSE_FORMAT_REMINDER: Final = (
    "Напоминание перед выводом: по каждому заданию ровно ОДНА строка-пункт (1-2 предложения), "
    "ошибки оборачивай в **...**, не повторяй одно и то же разными словами и не зацикливайся."
)

_DEFAULT_PROMPT_MIXED_NUMBERS: Final = (
    "Смешанные числа. Старое правило про перевод в неправильную дробь и пересчет умножения на целое - отменено; "
    "не используй его. Где бы ни фигурировали смешанные числа (целое и дробь вместе, N целых m/k и т.п.), "
    "никогда не вини ученика в том, что он не умеет смешанные числа или неправильно их переводит: пиши от первого лица "
    "проверки (например, что ты как модель не умеешь надежно считать и проверять смешанные числа, или что автоматическая "
    "проверка в этом слаба). Если в задании или на листе смешанные числа существенны для проверки - не оценивай пошаговые "
    "вычисления по ним как верные или неверные; честно скажи, что не можешь надежно проверить такой фрагмент, попроси "
    "решить самостоятельно и свериться с учебником или учителем. В самом конце ответа, отдельной строкой после всего текста, "
    "добавь ровно эту метку (без изменений): "
    "{marker}. Если смешанных чисел в задании и на листе нет - метку не добавляй и проверяй как обычно."
)

_DEFAULT_PROMPT_MULTI_SUMMARY_INTRO: Final = (
    "Ты помогаешь проверять домашнее задание по математике. Ниже - текстовые результаты "
    "проверки каждого снимка по отдельности (уже без картинок). Сформируй один ответ ученику на русском "
    "с кратким общим итогом и при необходимости по каждому фото. ОБЯЗАТЕЛЬНО соблюдай оформление для чата: "
    "по каждому заданию РОВНО ОДНА строка-пункт списка (- Задание N: ...), 1-2 коротких предложения, "
    "не один сплошной абзац и не несколько пунктов на одно задание; "
    "неверные ответы и явные ошибки в числах - в markdown **жирным** вокруг короткой фразы или числа. "
    "Про смешанные числа не виняй ученика в неумении - сохрани или усиль формулировки про ограничение проверки/модели. "
    "Не используй отдельный заголовок \"Вывод:\" и не дублируй markdown **Вывод:**. "
    "Ясно укажи, верно ли решение, есть ли существенные ошибки, видно ли решение на фото. "
    "Условие задачи ученик видит отдельно в том же сообщении — **не вставляй** в сводку дословный текст условия. "
    "Если во фрагментах один и тот же разбор повторяется — объедини в ОДИН пункт; "
    "ЗАПРЕЩЕНО переписывать одно и то же разными словами десятком пунктов."
)

_DEFAULT_PROMPT_MULTI_SUMMARY_COVERAGE: Final = (
    "Полнота заданий по альбому: каждый фрагмент выше относится к **одному** снимку. "
    "Если в них говорится, что на конкретном кадре не видно номеров из списка заданий, в общей сводке кратко объедини вывод: "
    "какие номера, по совокупности текстов, могли не попасть ни на один снимок (только если это следует из текста). "
    "Не растягивай повтор одного и того же по каждому фото."
)

_DEFAULT_PROMPT_GDZ_CHECKLIST: Final = (
    "Сверка с оглавлением gdz.ru для выбранного параграфа.\n"
    "Номера упражнений из оглавления: {exercises}.\n"
    "Страницы проверочных, упомянутые в оглавлении: {verif_pages}.\n"
    "Проверочные работы (подписи из оглавления; в параграфе их может быть несколько, номера заданий в разных работах могут совпадать — "
    "ориентируйся на подпись работы, а не только на номер):\n{verif_works}\n\n"
    "Ты видишь **одно** изображение за этот запрос (ученик мог прислать несколько фото по очереди). "
    "По **этому** снимку сопоставь видимые работы с номерами из списка упражнений "
    "(номер в условии, в шапке или однозначно узнаваемый фрагмент).\n\n"
    "Если список упражнений пуст — не выдумывай номера; опирайся на привязку задания пользователя и на фото.\n\n"
    "Обязательно: если на **этом** изображении часть номеров из списка не видна, нечитаема или нельзя надежно сопоставить — "
    "в конце ответа отдельный блок: первая строка ровно **Не хватает на этом фото:** "
    "далее маркированный список таких номеров. "
    "Не утверждай, что работа не сдана целиком, если номер просто отсутствует на этом кадре — "
    "формулируй, что на этом снимке не видно. "
    "Если на этом снимке по твоей оценке представлены все номера из списка — одной фразой сообщи об этом; "
    "если уверенность неполная — обозначь кратко."
)

_DEFAULT_PROMPT_GDZ_CHECKLIST_FALLBACK: Final = (
    "Список упражнений из оглавления gdz.ru для параграфа не передан (парсинг мог не сработать). "
    "Ориентируйся на привязку задания пользователя и на фото. "
    "Если по содержимому снимка похоже, что часть типовых заданий параграфа отсутствует — предупреди ученика в конце ответа кратко."
)

_DEFAULT_PROMPT_TEXT_FILE: Final = "{instruction}\n\n--- Текст файла ---\n{file_body}\n---"

_DEFAULT_PROMPT_QUIP: Final = (
    "Фрагмент текста автоматической проверки для контекста:\n{excerpt}\n\n"
    "Напиши ровно ОДНО предложение по-русски: ироничное, остроумное, \"беспощадное\" к слабым местам в рассуждениях, "
    "но без мата, без унижения личности и без политики. Тон - саркастичный наставник. "
    "Только текст предложения, без кавычек вокруг всего ответа."
)

_DEFAULT_QUIP_MOCK: Final = (
    "Математика смотрит на лист с выражением профессионального любопытства."
)

_DEFAULT_QUIP_FALLBACK: Final = "Математика смотрит на лист с выражением профессионального любопытства."

_DEFAULT_MSG_DOC_UNSUPPORTED: Final = (
    "Формат .doc / .docx / .rtf пока не разбирается на сервере. "
    "Отправь фото страницы, PDF или текст (.txt / .html / .md)"
)

_DEFAULT_MSG_EMPTY_MODEL: Final = "Модель вернула пустой ответ"

_DEFAULT_MSG_EMPTY_SUMMARY: Final = "Модель вернула пустую сводку"

_DEFAULT_MSG_UNSUPPORTED_MIME: Final = "Неподдерживаемый тип файла для проверки: {mime}"


def _env_prompt(key: str, default: str) -> str:
    """Текст промпта из окружения; пустая строка или отсутствие ключа - default."""
    v = (os.getenv(key) or "").strip()
    return v if v else default


def _vllm_no_think_enabled() -> bool:
    v = (os.getenv("VLLM_NO_THINK") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _append_no_think_to_prompt(text: str) -> str:
    """Добавляет суффикс /no_think для Qwen3 (см. документацию Qwen3)."""
    if not _vllm_no_think_enabled():
        return text
    t = (text or "").rstrip()
    if not t:
        return text
    low = t.lower().rstrip()
    for suf in ("/no_think", "/nothink"):
        if low.endswith(suf):
            return text
    return t + _NO_THINK_SUFFIX


def _vllm_chat_completions_extra_body() -> dict[str, Any] | None:
    """Параметры шаблона чата для vLLM (OpenAI-совместимый клиент: extra_body)."""
    if not _vllm_no_think_enabled():
        return None
    return {"chat_template_kwargs": {"enable_thinking": False}}


def _vllm_chat_completion_kwargs(
    *,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    extra = _vllm_chat_completions_extra_body()
    if extra is not None:
        kw["extra_body"] = extra
    return kw


def _normalize_vllm_base_url(raw: str) -> str:
    u = raw.strip().rstrip("/")
    if u.endswith("/chat/completions"):
        u = u[: -len("/chat/completions")]
    if not u.endswith("/v1"):
        if u.endswith("/v1/"):
            u = u.rstrip("/")
        elif "/v1" not in u:
            u = f"{u}/v1"
    return u


def _mime_from_bytes(data: bytes) -> str:
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 4 and data[:4] == b"%PDF":
        return "application/pdf"
    # Текстовые ответы в multipart иногда приходят без MIME или как octet-stream;
    # не считать их JPEG - иначе ветка image/ + preOCR и лишняя нагрузка на VL.
    # Если данные не похожи на UTF-8 текст, возвращаем application/octet-stream:
    # дальше в _check_vllm это попадёт в ветку «неподдерживаемый MIME» и не будет угадывания JPEG.
    head = data[:4096]
    if b"\x00" in head:
        return "application/octet-stream"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "text/plain"


def _prompt_ignore_gdz() -> str:
    return _env_prompt("VLLM_PROMPT_IGNORE_GDZ", _DEFAULT_PROMPT_IGNORE_GDZ)


def _prompt_response_format() -> str:
    return _env_prompt("VLLM_PROMPT_RESPONSE_FORMAT", _DEFAULT_PROMPT_RESPONSE_FORMAT)


def _prompt_response_format_reminder() -> str:
    return _env_prompt(
        "VLLM_PROMPT_RESPONSE_FORMAT_REMINDER",
        _DEFAULT_PROMPT_RESPONSE_FORMAT_REMINDER,
    )


def _prompt_mixed_numbers() -> str:
    t = _env_prompt("VLLM_PROMPT_MIXED_NUMBERS", _DEFAULT_PROMPT_MIXED_NUMBERS)
    return t.replace("{marker}", TGZH_MIXED_NUMBERS_MARKER).replace(
        "{MARKER}", TGZH_MIXED_NUMBERS_MARKER
    )


def _homework_instruction(
    paragraph: str,
    exercise: str | None,
    page: int | None,
    textbook_label: str,
    grade: int | None,
) -> str:
    book = textbook_label.strip() or "учебник"
    g = f"{grade} класс" if grade is not None else "6-7 класс"
    if page is not None:
        anchor = f"параграф {paragraph.strip()}, проверочная работа / материал на странице {page}"
    elif exercise:
        anchor = f"параграф {paragraph.strip()}, упражнение (задача) {exercise.strip()}"
    else:
        anchor = f"параграф {paragraph.strip()}"

    tpl = os.getenv("VLLM_CHECK_HOMEWORK", "").strip()
    if not tpl:
        tpl = _env_prompt("VLLM_CHECK_HOMEWORK_DEFAULT", _DEFAULT_CHECK_HOMEWORK)
    try:
        return tpl.format(book=book, g=g, anchor=anchor)
    except KeyError as e:
        return (
            f"[Ошибка шаблона задания проверки: неизвестный плейсхолдер {e}]\n"
            f"Учебник (ориентир): {book} ({g}).\n"
            f"Пользователь указал задание: {anchor}."
        )


def _check_rubric() -> str:
    return os.getenv("VLLM_CHECK_RUBRIC", "").strip()


def _gdz_task_condition_max_chars() -> int:
    try:
        return max(500, int((os.getenv("VLLM_GDZ_TASK_CONDITION_MAX_CHARS") or "6000").strip()))
    except ValueError:
        return 6000


def _gdz_task_condition_block(gdz_task_condition: str) -> str:
    raw = (gdz_task_condition or "").strip()
    if not raw:
        return ""
    cap = _gdz_task_condition_max_chars()
    if len(raw) > cap:
        raw = raw[: cap - 3] + "..."
    tpl = _env_prompt("VLLM_PROMPT_GDZ_TASK_CONDITION", _DEFAULT_PROMPT_GDZ_TASK_CONDITION)
    try:
        return tpl.format(condition=raw)
    except KeyError:
        return ""


def _parse_gdz_csv_ids(raw: str) -> tuple[str, ...]:
    seen: set[str] = set()
    for part in (raw or "").split(","):
        p = part.strip()
        if p.isdigit():
            seen.add(p)
    return tuple(sorted(seen, key=int))


_GDZ_CHECKLIST_IDS_CAP: Final = 120


def _format_gdz_id_list_display(ids: tuple[str, ...]) -> str:
    if not ids:
        return ""
    if len(ids) <= _GDZ_CHECKLIST_IDS_CAP:
        return ", ".join(ids)
    head = ", ".join(ids[:_GDZ_CHECKLIST_IDS_CAP])
    return f"{head} … всего {len(ids)} номеров"


def _gdz_paragraph_checklist_block(
    gdz_exercises: str,
    gdz_verif_pages: str,
    gdz_verif_works: str = "",
) -> str:
    ex_ids = _parse_gdz_csv_ids(gdz_exercises)
    pg_ids = _parse_gdz_csv_ids(gdz_verif_pages)
    ex_h = _format_gdz_id_list_display(ex_ids)
    pg_h = _format_gdz_id_list_display(pg_ids)
    vw_raw = (gdz_verif_works or "").strip()
    if not ex_h and not pg_h and not vw_raw:
        return _env_prompt(
            "VLLM_PROMPT_GDZ_CHECKLIST_FALLBACK",
            _DEFAULT_PROMPT_GDZ_CHECKLIST_FALLBACK,
        )
    ex_show = ex_h if ex_h else "—"
    pg_show = pg_h if pg_h else "—"
    vw_show = vw_raw if vw_raw else "—"
    tpl = _env_prompt("VLLM_PROMPT_GDZ_CHECKLIST", _DEFAULT_PROMPT_GDZ_CHECKLIST)
    try:
        return tpl.format(exercises=ex_show, verif_pages=pg_show, verif_works=vw_show)
    except KeyError:
        try:
            return tpl.format(exercises=ex_show, verif_pages=pg_show)
        except KeyError:
            return ""


def _full_check_prompt(
    paragraph: str,
    exercise: str | None,
    page: int | None,
    textbook_label: str,
    grade: int | None,
    *,
    gdz_exercises: str = "",
    gdz_verif_pages: str = "",
    gdz_verif_works: str = "",
    gdz_task_condition: str = "",
) -> str:
    base = _homework_instruction(paragraph, exercise, page, textbook_label, grade)
    gdz_block = _gdz_paragraph_checklist_block(
        gdz_exercises,
        gdz_verif_pages,
        gdz_verif_works,
    )
    rubric = _check_rubric()
    chunks = [_prompt_response_format(), _prompt_mixed_numbers(), base]
    tc_block = _gdz_task_condition_block(gdz_task_condition)
    if tc_block:
        chunks.append(tc_block)
    if gdz_block:
        chunks.append(gdz_block)
    if rubric:
        chunks.append(rubric)
    chunks.append(_prompt_ignore_gdz())
    chunks.append(_prompt_response_format_reminder())
    return "\n\n".join(chunks)


async def check_homework(
    data: bytes,
    content_type: str | None = None,
    *,
    paragraph: str = "",
    exercise: str | None = None,
    page: int | None = None,
    textbook_label: str = "",
    grade: int | None = None,
    gdz_exercises: str = "",
    gdz_verif_pages: str = "",
    gdz_verif_works: str = "",
    gdz_task_condition: str = "",
) -> str:
    """
    Анализирует вложение (фото ДЗ или поддерживаемый документ).
    paragraph / exercise / page — привязка к заданию для сверки с ответом LLM.
    gdz_exercises / gdz_verif_pages — списки из оглавления gdz.ru (строки с номерами через запятую).
    gdz_verif_works — многострочное описание проверочных работ из оглавления (подписи, item).
    gdz_task_condition — текст условия со страницы задания gdz.ru (как в учебнике).
    """
    if os.getenv("AI_MOCK") == "1":
        return _mock_result(
            data,
            paragraph=paragraph,
            exercise=exercise,
            page=page,
            textbook_label=textbook_label,
            grade=grade,
            gdz_exercises=gdz_exercises,
            gdz_verif_pages=gdz_verif_pages,
            gdz_verif_works=gdz_verif_works,
            gdz_task_condition=gdz_task_condition,
        )

    base_url = os.getenv("VLLM_BASE_URL", "").strip()
    if not base_url:
        return _mock_result(
            data,
            paragraph=paragraph,
            exercise=exercise,
            page=page,
            textbook_label=textbook_label,
            grade=grade,
            gdz_exercises=gdz_exercises,
            gdz_verif_pages=gdz_verif_pages,
            gdz_verif_works=gdz_verif_works,
            gdz_task_condition=gdz_task_condition,
        )

    return await _check_vllm(
        data,
        content_type,
        paragraph=paragraph,
        exercise=exercise,
        page=page,
        textbook_label=textbook_label,
        grade=grade,
        gdz_exercises=gdz_exercises,
        gdz_verif_pages=gdz_verif_pages,
        gdz_verif_works=gdz_verif_works,
        gdz_task_condition=gdz_task_condition,
    )


def _mock_result(
    data: bytes,
    *,
    paragraph: str = "",
    exercise: str | None = None,
    page: int | None = None,
    textbook_label: str = "",
    grade: int | None = None,
    gdz_exercises: str = "",
    gdz_verif_pages: str = "",
    gdz_verif_works: str = "",
    gdz_task_condition: str = "",
) -> str:
    size = len(data)
    parts = [
        f"[Тестовый режим] Получено ({size} байт).",
        f"Привязка: параграф {paragraph or '—'}",
    ]
    if page is not None:
        parts.append(f"страница {page}")
    elif exercise:
        parts.append(f"упражнение {exercise}")
    if textbook_label:
        parts.append(f"учебник: {textbook_label}")
    if grade is not None:
        parts.append(f"класс {grade}")
    if (gdz_exercises or "").strip():
        disp = _format_gdz_id_list_display(_parse_gdz_csv_ids(gdz_exercises))
        if disp:
            parts.append(f"ГДЗ упражнения (оглавление): {disp[:500]}")
    if (gdz_verif_pages or "").strip():
        disp_p = _format_gdz_id_list_display(_parse_gdz_csv_ids(gdz_verif_pages))
        if disp_p:
            parts.append(f"ГДЗ проверочные стр. (оглавление): {disp_p[:300]}")
    if (gdz_verif_works or "").strip():
        parts.append(f"ГДЗ проверочные работы (оглавление):\n{gdz_verif_works.strip()[:800]}")
    if (gdz_task_condition or "").strip():
        parts.append(f"Условие (gdz.ru): {gdz_task_condition.strip()[:600]}")
    parts.append(
        "\nЧтобы включить VLLM: AI_MOCK=0, VLLM_BASE_URL, VLLM_API_KEY, VLLM_MODEL"
    )
    model = os.getenv("VLLM_MODEL", VLLM_MODEL_DEFAULT)
    parts.append(f"Модель: {model} (тестовый режим)")
    return "\n".join(parts)


def _parts_had_preocr_footer(parts: list[str]) -> bool:
    """True, если хотя бы в одном фрагменте результата проверки был футер pre-OCR."""
    return any(PREOCR_RESULT_FOOTER_LINE in (p or "") for p in parts)


def _analysis_result_footer(*, model: str, preocr_used: bool) -> str:
    """Хвост ответа ученику: опционально pre-OCR, всегда имя модели."""
    lines: list[str] = []
    if preocr_used:
        lines.append(PREOCR_RESULT_FOOTER_LINE)
    lines.append(f"Модель: {model}")
    return "\n\n" + "\n".join(lines)


def _append_analysis_footer_to_llm_text(
    raw: str,
    *,
    model: str,
    preocr_used: bool,
    empty_fallback: str,
) -> str:
    """Вставляет футер перед хвостовой меткой смешанных чисел (если есть)."""
    text = (raw or "").strip()
    if not text:
        return empty_fallback + _analysis_result_footer(model=model, preocr_used=preocr_used)
    head, trail = detach_trailing_mixed_numbers_marker(text)
    core = head if trail else text
    core = dedupe_homework_check_lines(core)
    if not core.strip() and not trail:
        return empty_fallback + _analysis_result_footer(model=model, preocr_used=preocr_used)
    return core.rstrip() + _analysis_result_footer(model=model, preocr_used=preocr_used) + trail


async def _check_vllm(
    data: bytes,
    content_type: str | None,
    *,
    paragraph: str,
    exercise: str | None,
    page: int | None,
    textbook_label: str,
    grade: int | None,
    gdz_exercises: str = "",
    gdz_verif_pages: str = "",
    gdz_verif_works: str = "",
    gdz_task_condition: str = "",
) -> str:
    from openai import APIConnectionError, APIStatusError, AsyncOpenAI

    base_url = _normalize_vllm_base_url(os.getenv("VLLM_BASE_URL", ""))
    api_key = os.getenv("VLLM_API_KEY", "")
    model = os.getenv("VLLM_MODEL", VLLM_MODEL_DEFAULT)
    max_tokens = int(os.getenv("VLLM_MAX_TOKENS", "4096"))
    max_tokens = min(max_tokens, VLLM_CONTEXT_WINDOW // 4)

    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key or "EMPTY",
        timeout=_vllm_request_timeout_s(),
    )

    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct or ct == "application/octet-stream":
        ct = _mime_from_bytes(data)

    preocr_used = False

    instr = _append_no_think_to_prompt(
        _full_check_prompt(
            paragraph,
            exercise,
            page,
            textbook_label,
            grade,
            gdz_exercises=gdz_exercises,
            gdz_verif_pages=gdz_verif_pages,
            gdz_verif_works=gdz_verif_works,
            gdz_task_condition=gdz_task_condition,
        )
    )

    if ct.startswith("image/"):
        from preocr_client import fetch_preocr_block

        pre_block = await fetch_preocr_block(image_bytes=data, content_type=ct)
        preocr_used = bool(pre_block.strip())
        if preocr_used:
            instr = f"{pre_block.strip()}\n\n{instr}"

        b64 = base64.standard_b64encode(data).decode()
        content: list[dict] = [
            {"type": "text", "text": instr},
            {"type": "image_url", "image_url": {"url": f"data:{ct};base64,{b64}"}},
        ]
    elif ct == "application/pdf":
        b64 = base64.standard_b64encode(data).decode()
        content = [
            {"type": "text", "text": instr},
            {
                "type": "image_url",
                "image_url": {"url": f"data:application/pdf;base64,{b64}"},
            },
        ]
    elif ct in ("text/plain", "text/html", "text/markdown"):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        wrap = _env_prompt("VLLM_PROMPT_TEXT_FILE", _DEFAULT_PROMPT_TEXT_FILE)
        file_block = wrap.format(instruction=instr, file_body=text)
        content = [
            {
                "type": "text",
                "text": file_block,
            }
        ]
    else:
        ext = _MIME_TO_EXT.get(ct, "")
        if ext in ("doc", "docx", "rtf"):
            return _env_prompt("VLLM_MSG_DOC_UNSUPPORTED", _DEFAULT_MSG_DOC_UNSUPPORTED)
        unsup = _env_prompt("VLLM_MSG_UNSUPPORTED_MIME", _DEFAULT_MSG_UNSUPPORTED_MIME)
        return unsup.format(mime=ct or "неизвестно")

    foot = _analysis_result_footer(model=model, preocr_used=preocr_used)
    try:
        response = await client.chat.completions.create(
            **_vllm_chat_completion_kwargs(
                model=model,
                messages=[{"role": "user", "content": content}],
                max_tokens=max_tokens,
            ),
        )
    except APIConnectionError as e:
        return f"Не удалось подключиться к VLLM: {e}{foot}"
    except APIStatusError as e:
        return f"Ошибка VLLM ({e.status_code}): {e.message}{foot}"
    except Exception as e:
        return f"Ошибка при обращении к VLLM: {e}{foot}"

    msg = response.choices[0].message
    out = (msg.content or "").strip()
    empty_msg = _env_prompt("VLLM_MSG_EMPTY_MODEL_REPLY", _DEFAULT_MSG_EMPTY_MODEL)
    return _append_analysis_footer_to_llm_text(
        out,
        model=model,
        preocr_used=preocr_used,
        empty_fallback=empty_msg,
    )


def _multi_summary_user_text(parts: list[str]) -> str:
    chunks: list[str] = []
    for i, p in enumerate(parts, start=1):
        t = (p or "").strip()
        if len(t) > 2500:
            t = t[:2497] + "..."
        chunks.append(f"### Результат проверки по фото {i}\n{t}")
    block = "\n\n".join(chunks)
    tpl = os.getenv("VLLM_CHECK_MULTI_SUMMARY", "").strip()
    cov = _env_prompt("VLLM_PROMPT_MULTI_SUMMARY_COVERAGE", _DEFAULT_PROMPT_MULTI_SUMMARY_COVERAGE)
    if tpl:
        try:
            inner = tpl.format(parts=block, n=len(parts))
        except KeyError:
            pass
        else:
            if cov.strip():
                return f"{cov}\n\n{inner}"
            return inner
    intro = _env_prompt("VLLM_PROMPT_MULTI_SUMMARY_INTRO", _DEFAULT_PROMPT_MULTI_SUMMARY_INTRO)
    rem = _prompt_response_format_reminder()
    return f"{intro}\n\n{cov}\n\n{rem}\n\n{block}"


async def summarize_check_parts(parts: list[str]) -> str:
    """
    Второй вызов LLM: объединить несколько текстовых результатов проверки в одну сводку.
    При одном элементе возвращает его без вызова API.
    """
    clean = [(p or "").strip() for p in parts if (p or "").strip()]
    if len(clean) < 2:
        return clean[0] if clean else ""
    model = os.getenv("VLLM_MODEL", VLLM_MODEL_DEFAULT)
    preocr_in_parts = _parts_had_preocr_footer(clean)
    if os.getenv("AI_MOCK") == "1":
        body = (
            "[Тестовый режим] Сводка по "
            f"{len(clean)} фото:\n\n"
            + "\n\n".join(f"--- Фото {i} ---\n{t[:800]}" for i, t in enumerate(clean, start=1))
        )
        foot_lines: list[str] = []
        if preocr_in_parts:
            foot_lines.append(PREOCR_RESULT_FOOTER_LINE)
        foot_lines.append(f"Модель: {model} (тестовый режим)")
        return body + "\n\n" + "\n".join(foot_lines)

    from openai import APIConnectionError, APIStatusError, AsyncOpenAI

    base_url = _normalize_vllm_base_url(os.getenv("VLLM_BASE_URL", ""))
    if not base_url:
        return (
            "Сводка недоступна: не задан VLLM_BASE_URL"
            + _analysis_result_footer(model=model, preocr_used=preocr_in_parts)
        )

    api_key = os.getenv("VLLM_API_KEY", "")
    max_tokens = int(os.getenv("VLLM_MAX_TOKENS", "4096"))
    max_tokens = min(max_tokens, 2048)

    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key or "EMPTY",
        timeout=_vllm_request_timeout_s(),
    )
    instr = _append_no_think_to_prompt(_multi_summary_user_text(clean))

    foot = _analysis_result_footer(model=model, preocr_used=preocr_in_parts)
    try:
        response = await client.chat.completions.create(
            **_vllm_chat_completion_kwargs(
                model=model,
                messages=[{"role": "user", "content": instr}],
                max_tokens=max_tokens,
            ),
        )
    except APIConnectionError as e:
        return f"Не удалось подключиться к VLLM (сводка): {e}{foot}"
    except APIStatusError as e:
        return f"Ошибка VLLM при сводке ({e.status_code}): {e.message}{foot}"
    except Exception as e:
        return f"Ошибка при сводке: {e}{foot}"

    msg = response.choices[0].message
    out = (msg.content or "").strip()
    empty_s = _env_prompt("VLLM_MSG_EMPTY_SUMMARY", _DEFAULT_MSG_EMPTY_SUMMARY)
    return _append_analysis_footer_to_llm_text(
        out,
        model=model,
        preocr_used=preocr_in_parts,
        empty_fallback=empty_s,
    )


async def generate_check_quip(*, excerpt: str) -> str:
    """
    Одно ироничное предложение по фрагменту проверки (только текст, без оскорблений личности).
    """
    if os.getenv("AI_MOCK") == "1":
        return _env_prompt("VLLM_QUIP_MOCK_TEXT", _DEFAULT_QUIP_MOCK)
    base_url = _normalize_vllm_base_url(os.getenv("VLLM_BASE_URL", ""))
    if not base_url:
        return ""

    from openai import APIConnectionError, APIStatusError, AsyncOpenAI

    api_key = os.getenv("VLLM_API_KEY", "")
    model = os.getenv("VLLM_MODEL", VLLM_MODEL_DEFAULT)
    ex = (excerpt or "").strip()
    if len(ex) > 2000:
        ex = ex[:1997] + "..."
    quip_t = _env_prompt("VLLM_PROMPT_QUIP", _DEFAULT_PROMPT_QUIP)
    quip_fields: dict[str, str] = defaultdict(str)
    quip_fields["excerpt"] = ex
    instr = _append_no_think_to_prompt(quip_t.format_map(quip_fields))
    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key or "EMPTY",
        timeout=min(60.0, _vllm_request_timeout_s()),
    )
    try:
        response = await client.chat.completions.create(
            **_vllm_chat_completion_kwargs(
                model=model,
                messages=[{"role": "user", "content": instr}],
                max_tokens=150,
            ),
        )
    except Exception as e:
        logger.warning("generate_check_quip failed: %s", e)
        return ""
    msg = response.choices[0].message
    out = (msg.content or "").strip().split("\n")[0].strip()
    fb = _env_prompt("VLLM_QUIP_FALLBACK", _DEFAULT_QUIP_FALLBACK)
    return out if out else fb


def allowed_check_mime(content_type: str | None) -> bool:
    """Проверка MIME для POST /check."""
    if not content_type:
        return False
    ct = content_type.split(";")[0].strip().lower()
    if ct.startswith("image/"):
        return True
    return ct in (
        "application/pdf",
        "text/plain",
        "text/html",
        "text/markdown",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/rtf",
        "text/rtf",
    )
