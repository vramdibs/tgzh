"""Юнит-тесты ai_checker: URL VLLM, MIME, промпт, режим mock."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ai_checker


@pytest.mark.parametrize(
    ("raw", "expected_suffix"),
    [
        ("http://localhost:8000/v1/chat/completions", "/v1"),
        ("http://localhost:8000/v1", "/v1"),
        ("http://host:1234", "/v1"),
        ("http://host:1234/", "/v1"),
    ],
)
def test_normalize_vllm_base_url(raw: str, expected_suffix: str) -> None:
    out = ai_checker._normalize_vllm_base_url(raw)
    assert out.endswith(expected_suffix)
    assert not out.endswith("/chat/completions")


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"GIF89a" + b"\x00" * 100, "image/gif"),
        (b"%PDF-1.4", "application/pdf"),
        (b"unknown", "text/plain"),
    ],
)
def test_mime_from_bytes(head: bytes, expected: str) -> None:
    data = head + b"x" * 200
    assert ai_checker._mime_from_bytes(data) == expected


def test_mime_from_bytes_utf8_cyrillic_text_plain() -> None:
    data = "Решение: x = 5".encode("utf-8")
    assert ai_checker._mime_from_bytes(data) == "text/plain"


def test_mime_from_bytes_invalid_utf8_returns_octet_stream() -> None:
    # Раньше неузнанные байты считались image/jpeg — это вело к лишней нагрузке на VL.
    # Теперь — application/octet-stream: дальше попадёт в ветку «неподдерживаемый MIME».
    data = b"\xff\xfe" * 120
    assert ai_checker._mime_from_bytes(data) == "application/octet-stream"


def test_mime_from_bytes_null_prefix_returns_octet_stream() -> None:
    data = b"hello\x00world"
    assert ai_checker._mime_from_bytes(data) == "application/octet-stream"


def test_mime_from_bytes_webp() -> None:
    head = b"RIFF" + b"\x00" * 4 + b"WEBP"
    assert ai_checker._mime_from_bytes(head + b"x" * 200) == "image/webp"


def test_normalize_vllm_base_url_strips_trailing_v1_slash() -> None:
    out = ai_checker._normalize_vllm_base_url("http://host:9/v1/")
    assert out == "http://host:9/v1"


@pytest.mark.parametrize(
    ("ct", "ok"),
    [
        ("image/jpeg", True),
        ("Image/PNG", True),
        ("application/pdf", True),
        ("text/plain; charset=utf-8", True),
        ("video/mp4", False),
        ("application/msword", True),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            True,
        ),
        ("application/rtf", True),
        ("text/rtf", True),
        (None, False),
    ],
)
def test_allowed_check_mime(ct: str | None, ok: bool) -> None:
    assert ai_checker.allowed_check_mime(ct) is ok


def test_homework_instruction_default_template() -> None:
    t = ai_checker._homework_instruction("1", "5", None, "Книга", 6)
    assert "Книга" in t
    assert "6 класс" in t
    assert "упражнение" in t.lower() or "5" in t


def test_homework_instruction_page_anchor() -> None:
    t = ai_checker._homework_instruction("2", None, 42, "X", 7)
    assert "42" in t
    assert "провероч" in t.lower() or "страниц" in t.lower()


def test_homework_instruction_empty_textbook_defaults() -> None:
    t = ai_checker._homework_instruction("3", None, None, "   ", None)
    assert "учебник" in t.lower()
    assert "6-7 класс" in t


def test_homework_instruction_paragraph_only_anchor() -> None:
    t = ai_checker._homework_instruction(" 4 ", None, None, "B", 5)
    assert "параграф 4" in t
    assert "пользователь указал задание: параграф 4" in t.lower()
    assert "упражнение (задача)" not in t.lower()
    assert "на странице" not in t.lower()


def test_homework_instruction_custom_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_CHECK_HOMEWORK", "Книга: {book}, {g}, задание: {anchor}")
    t = ai_checker._homework_instruction("1", "2", None, "Уч", 6)
    assert "Книга: Уч" in t
    assert "6 класс" in t
    assert "упражнение (задача) 2" in t


def test_homework_instruction_custom_template_unknown_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VLLM_CHECK_HOMEWORK", "{bad}")
    t = ai_checker._homework_instruction("1", "5", None, "К", 6)
    assert "Ошибка шаблона" in t
    assert "неизвестный плейсхолдер" in t.lower()


def test_prompt_ignore_gdz_overridable_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_PROMPT_IGNORE_GDZ", "CUSTOM_GDZ_RULE")
    full = ai_checker._full_check_prompt("1", "3", None, "Книга", 6)
    assert "CUSTOM_GDZ_RULE" in full


def test_full_check_prompt_includes_ignore_gdz_on_photo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLLM_PROMPT_IGNORE_GDZ", raising=False)
    full = ai_checker._full_check_prompt("1", "3", None, "Книга", 6)
    assert "gdz.ru" in full
    assert "гдз.ру" in full
    assert "игнорируй" in full.lower()
    assert "смешан" in full.lower()
    assert "[tgzh_mixed_numbers]" in full
    assert "строка-пункт" in full.lower()
    assert "не вини ученика" in full.lower()
    assert "markdown" in full.lower() and "жирн" in full.lower()
    assert "не зацикл" in full.lower()


def test_full_check_prompt_includes_rubric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLLM_PROMPT_IGNORE_GDZ", raising=False)
    monkeypatch.setenv("VLLM_CHECK_RUBRIC", "Рубрика: строго.")
    full = ai_checker._full_check_prompt("1", None, 10, "К", 6)
    assert "Рубрика: строго." in full
    idx_rubric = full.index("Рубрика: строго.")
    idx_ignore_gdz = full.index("Если на изображении виден текст сайта")
    assert idx_rubric < idx_ignore_gdz


def test_full_check_prompt_custom_homework_still_appends_global_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VLLM_CHECK_HOMEWORK", "Только {anchor}.")
    full = ai_checker._full_check_prompt("1", "9", None, "X", 7)
    assert "Только параграф" in full
    assert full.index("ОБЯЗАТЕЛЬНОЕ") < full.index("Только параграф")
    assert "markdown" in full.lower() and "жирн" in full.lower()
    assert "[tgzh_mixed_numbers]" in full


def test_full_check_prompt_gdz_exercise_list_sorted() -> None:
    full = ai_checker._full_check_prompt(
        "1",
        "3",
        None,
        "К",
        6,
        gdz_exercises="9,1,5",
        gdz_verif_pages="12,7",
        gdz_verif_works="Проверочная работа 1 — item 10",
    )
    assert "1, 5, 9" in full
    assert "7, 12" in full
    assert "Проверочная работа 1" in full
    assert "пересеч" in full.lower() or "нескольк" in full.lower()
    assert "Не хватает на этом фото" in full


def test_full_check_prompt_gdz_fallback_without_lists() -> None:
    full = ai_checker._full_check_prompt("1", "3", None, "К", 6)
    assert "оглавлен" in full.lower()


def test_full_check_prompt_includes_gdz_task_condition() -> None:
    full = ai_checker._full_check_prompt(
        "2",
        "2",
        None,
        "В",
        6,
        gdz_task_condition="Используя таблицу простых чисел, определите простые.",
    )
    assert "таблицу простых" in full
    assert "не копируй" in full.lower() or "не вставляй" in full.lower()
    assert "зацикливайся" in full.lower() or "один раз" in full.lower()
    assert (
        "не повторяй" in full.lower()
        or "одинаковых строк" in full.lower()
        or "разными словами" in full.lower()
    )


def test_mock_result_prefers_page_over_exercise() -> None:
    out = ai_checker._mock_result(b"ab", paragraph="1", exercise="5", page=3, textbook_label="", grade=None)
    assert "страница 3" in out
    assert "упражнение" not in out


def test_mock_result_with_exercise_no_page() -> None:
    out = ai_checker._mock_result(b"x", paragraph="2", exercise="8", page=None, textbook_label="T", grade=7)
    assert "упражнение 8" in out
    assert "учебник: T" in out
    assert "класс 7" in out


def test_multi_summary_user_text_default() -> None:
    text = ai_checker._multi_summary_user_text(["alpha", "beta"])
    assert "### Результат проверки по фото 1" in text
    assert "alpha" in text
    assert "### Результат проверки по фото 2" in text
    assert "beta" in text
    assert "один ответ" in text.lower() and "обязательно" in text.lower()
    assert "альбом" in text.lower() or "полнот" in text.lower()


def test_multi_summary_user_text_truncates_long_part(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLLM_CHECK_MULTI_SUMMARY", raising=False)
    long = "z" * 3000
    text = ai_checker._multi_summary_user_text([long])
    assert "zzz..." in text
    assert "z" * 2500 not in text


def test_multi_summary_user_text_custom_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_CHECK_MULTI_SUMMARY", "N={n}\n{parts}")
    text = ai_checker._multi_summary_user_text(["a", "b"])
    assert "N=2" in text
    assert "Полнота" in text
    assert text.index("Полнота") < text.index("N=2")
    assert "Результат проверки по фото" in text


def test_multi_summary_user_text_custom_template_bad_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VLLM_CHECK_MULTI_SUMMARY", "{only_bad}")
    text = ai_checker._multi_summary_user_text(["x"])
    assert "один ответ" in text.lower() and "обязательно" in text.lower()
    assert "x" in text


@pytest.mark.asyncio
async def test_summarize_check_parts_empty() -> None:
    assert await ai_checker.summarize_check_parts([]) == ""
    assert await ai_checker.summarize_check_parts(["  ", ""]) == ""


@pytest.mark.asyncio
async def test_summarize_check_parts_single_no_api() -> None:
    assert await ai_checker.summarize_check_parts(["  one  "]) == "one"


@pytest.mark.asyncio
async def test_summarize_check_parts_two_in_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MOCK", "1")
    out = await ai_checker.summarize_check_parts(["first", "second"])
    assert "[Тестовый режим]" in out
    assert "2 фото" in out
    assert "first" in out
    assert "Модель:" in out
    assert "тестовый режим" in out.lower()
    assert ai_checker.PREOCR_RESULT_FOOTER_LINE not in out


@pytest.mark.asyncio
async def test_summarize_check_parts_mock_preocr_footer_when_part_had_ocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_MOCK", "1")
    p1 = f"aa\n{ai_checker.PREOCR_RESULT_FOOTER_LINE}"
    out = await ai_checker.summarize_check_parts([p1, "bb"])
    assert ai_checker.PREOCR_RESULT_FOOTER_LINE in out


@pytest.mark.asyncio
async def test_summarize_check_parts_no_vllm_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MOCK", "0")
    monkeypatch.setenv("VLLM_BASE_URL", "http://unused")
    monkeypatch.setattr(ai_checker, "_normalize_vllm_base_url", lambda _raw: "")
    out = await ai_checker.summarize_check_parts(["a", "b"])
    assert "недоступна" in out.lower()
    assert "VLLM_BASE_URL" in out
    assert "Модель:" in out


@pytest.mark.asyncio
async def test_generate_check_quip_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MOCK", "1")
    q = await ai_checker.generate_check_quip(excerpt="x")
    assert len(q) > 10


@pytest.mark.asyncio
async def test_generate_check_quip_empty_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MOCK", "0")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    q = await ai_checker.generate_check_quip(excerpt="y")
    assert q == ""


@pytest.mark.asyncio
async def test_check_vllm_unsupported_mime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:9/v1")
    out = await ai_checker._check_vllm(
        b"data",
        "application/zip",
        paragraph="1",
        exercise=None,
        page=None,
        textbook_label="",
        grade=None,
    )
    assert "Неподдерживаемый" in out
    assert "zip" in out.lower()


@pytest.mark.asyncio
async def test_check_vllm_doc_not_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:9/v1")
    out = await ai_checker._check_vllm(
        b"%PDF-not" + b"\x00" * 20,
        "application/msword",
        paragraph="1",
        exercise=None,
        page=None,
        textbook_label="",
        grade=None,
    )
    assert ".doc" in out or "docx" in out or "rtf" in out


@pytest.mark.asyncio
async def test_check_vllm_image_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:9/v1")
    monkeypatch.setenv("VLLM_API_KEY", "")

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "  Ответ модели  "

    with patch("openai.AsyncOpenAI") as mock_cls:
        inst = mock_cls.return_value
        inst.chat.completions.create = AsyncMock(return_value=mock_resp)
        out = await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 40,
            "image/jpeg",
            paragraph="1",
            exercise="2",
            page=None,
            textbook_label="К",
            grade=6,
        )

    mock_cls.assert_called_once()
    inst.chat.completions.create.assert_awaited_once()
    assert out.startswith("Ответ модели")
    assert "Модель:" in out
    assert "Предварительное распознавание" not in out
    call_kw = inst.chat.completions.create.await_args.kwargs
    assert call_kw["model"]
    msgs = call_kw["messages"]
    assert msgs[0]["role"] == "user"
    content = msgs[0]["content"]
    assert any(c.get("type") == "image_url" for c in content)
    text_parts = [c["text"] for c in content if c.get("type") == "text"]
    assert text_parts
    assert "Учебник" in text_parts[0] or "учебник" in text_parts[0].lower()
    assert "/no_think" in text_parts[0]
    assert call_kw["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.asyncio
async def test_check_vllm_image_prepends_preocr_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:9/v1")
    monkeypatch.setenv("VLLM_API_KEY", "")
    monkeypatch.setenv("PREOCR_URL", "http://preocr:8088")

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "ok"

    with (
        patch("openai.AsyncOpenAI") as mock_cls,
        patch(
            "preocr_client.fetch_preocr_block",
            AsyncMock(return_value="PREOCR_HINT_LINE\n\n"),
        ),
    ):
        inst = mock_cls.return_value
        inst.chat.completions.create = AsyncMock(return_value=mock_resp)
        out_pre = await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 40,
            "image/jpeg",
            paragraph="1",
            exercise="2",
            page=None,
            textbook_label="К",
            grade=6,
        )

    text_parts = [
        c["text"]
        for c in inst.chat.completions.create.await_args.kwargs["messages"][0]["content"]
        if c.get("type") == "text"
    ]
    assert text_parts
    assert "PREOCR_HINT_LINE" in text_parts[0]
    assert "Предварительное распознавание" in out_pre
    assert "Модель:" in out_pre
    assert out_pre.startswith("ok")


@pytest.mark.asyncio
async def test_check_vllm_no_think_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:9/v1")
    monkeypatch.setenv("VLLM_NO_THINK", "0")

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "ok"

    with patch("openai.AsyncOpenAI") as mock_cls:
        inst = mock_cls.return_value
        inst.chat.completions.create = AsyncMock(return_value=mock_resp)
        await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 40,
            "image/jpeg",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
        )

    call_kw = inst.chat.completions.create.await_args.kwargs
    assert "extra_body" not in call_kw
    text_parts = [
        c["text"]
        for c in call_kw["messages"][0]["content"]
        if c.get("type") == "text"
    ]
    assert text_parts
    assert "/no_think" not in text_parts[0]


@pytest.mark.asyncio
async def test_check_vllm_pdf_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:9/v1")

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "pdf ok"

    with patch("openai.AsyncOpenAI") as mock_cls:
        inst = mock_cls.return_value
        inst.chat.completions.create = AsyncMock(return_value=mock_resp)
        out = await ai_checker._check_vllm(
            b"%PDF-1.4\n",
            "application/pdf",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
        )

    assert out.startswith("pdf ok")
    assert "Модель:" in out
    content = inst.chat.completions.create.await_args.kwargs["messages"][0]["content"]
    assert any(
        c.get("type") == "image_url"
        and "application/pdf" in c.get("image_url", {}).get("url", "")
        for c in content
    )


@pytest.mark.asyncio
async def test_check_vllm_empty_model_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:9/v1")

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = None

    with patch("openai.AsyncOpenAI") as mock_cls:
        inst = mock_cls.return_value
        inst.chat.completions.create = AsyncMock(return_value=mock_resp)
        out = await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 20,
            "image/jpeg",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
        )

    assert "пустой" in out.lower()
    assert "Модель:" in out


@pytest.mark.asyncio
async def test_check_vllm_api_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:9/v1")

    from openai import APIConnectionError

    with patch("openai.AsyncOpenAI") as mock_cls:
        inst = mock_cls.return_value
        inst.chat.completions.create = AsyncMock(
            side_effect=APIConnectionError(request=MagicMock())
        )
        out = await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 20,
            "image/jpeg",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
        )

    assert "подключиться" in out.lower()
    assert "Модель:" in out


@pytest.mark.asyncio
async def test_summarize_check_parts_vllm_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MOCK", "0")
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:9/v1")

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "  Итог  "

    p1 = f"x\n\n{ai_checker.PREOCR_RESULT_FOOTER_LINE}\nМодель: m"
    with patch("openai.AsyncOpenAI") as mock_cls:
        inst = mock_cls.return_value
        inst.chat.completions.create = AsyncMock(return_value=mock_resp)
        out = await ai_checker.summarize_check_parts([p1, "b"])

    assert out.startswith("Итог")
    assert "Модель:" in out
    assert ai_checker.PREOCR_RESULT_FOOTER_LINE in out
    inst.chat.completions.create.assert_awaited_once()
    sk = inst.chat.completions.create.await_args.kwargs
    assert sk["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "/no_think" in sk["messages"][0]["content"]


@pytest.mark.asyncio
async def test_check_vllm_plain_text_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:9/v1")

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "ok"

    with patch("openai.AsyncOpenAI") as mock_cls:
        inst = mock_cls.return_value
        inst.chat.completions.create = AsyncMock(return_value=mock_resp)
        out = await ai_checker._check_vllm(
            "привет".encode("utf-8"),
            "text/plain",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
        )

    assert out.startswith("ok")
    assert "Модель:" in out
    call_kw = inst.chat.completions.create.await_args.kwargs
    text = call_kw["messages"][0]["content"][0]["text"]
    assert "привет" in text
    assert "--- Текст файла ---" in text
    assert "/no_think" in text


@pytest.mark.asyncio
async def test_check_homework_ai_mock_returns_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MOCK", "1")
    monkeypatch.delenv("VLLM_BASE_URL", raising=False)
    out = await ai_checker.check_homework(
        b"\xff\xd8\xff" + b"\x00" * 50,
        content_type="image/jpeg",
        paragraph="1",
        exercise="7",
        textbook_label="Test",
        grade=6,
    )
    assert "[Тестовый режим]" in out
    assert "Модель:" in out
    assert "7" in out or "упражнение" in out
    assert "тестовый режим" in out.lower()


@pytest.mark.asyncio
async def test_check_homework_empty_vllm_url_falls_back_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_MOCK", "0")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    out = await ai_checker.check_homework(
        b"hello",
        content_type="text/plain",
        paragraph="1",
        exercise=None,
        page=None,
        textbook_label="",
        grade=None,
    )
    assert "[Тестовый режим]" in out
    assert "Модель:" in out


def test_append_analysis_footer_preserves_mixed_numbers_marker() -> None:
    raw = "Текст разбора.\n[tgzh_mixed_numbers]"
    model = "m1"
    out = ai_checker._append_analysis_footer_to_llm_text(
        raw,
        model=model,
        preocr_used=True,
        empty_fallback="empty",
    )
    assert out.endswith("[tgzh_mixed_numbers]")
    assert "Предварительное распознавание" in out
    assert "Модель: m1" in out
    assert out.index("Модель:") < out.rindex("[tgzh_mixed_numbers]")


# --- Опциональный fallback на Cursor-bridge ---
#
# Все тесты ниже подменяют openai.AsyncOpenAI так, что primary и fallback
# получают разные mock-инстансы (через side_effect-список). Это позволяет
# проверить ровно ту последовательность вызовов, которую делает
# `_chat_with_fallback`.


def _make_mock_response(text: str | None) -> MagicMock:
    resp = MagicMock()
    if text is None:
        resp.choices = []
    else:
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = text
    return resp


def _enable_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:9/v1")
    monkeypatch.setenv("VLLM_FALLBACK_ENABLE", "1")
    monkeypatch.setenv("VLLM_FALLBACK_BASE_URL", "http://bridge:8787/v1")
    monkeypatch.setenv("VLLM_FALLBACK_API_KEY", "tok")
    monkeypatch.setenv("VLLM_FALLBACK_MODEL", "cursor-agent")


@pytest.mark.asyncio
async def test_fallback_used_on_api_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_fallback(monkeypatch)
    from openai import APIConnectionError

    primary = MagicMock()
    primary.chat.completions.create = AsyncMock(side_effect=APIConnectionError(request=MagicMock()))
    fallback = MagicMock()
    fallback.chat.completions.create = AsyncMock(return_value=_make_mock_response("из бриджа"))

    with patch("openai.AsyncOpenAI", side_effect=[primary, fallback]) as mock_cls:
        out = await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 20,
            "image/jpeg",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
        )

    assert mock_cls.call_count == 2
    primary.chat.completions.create.assert_awaited_once()
    fallback.chat.completions.create.assert_awaited_once()
    fb_kwargs = fallback.chat.completions.create.await_args.kwargs
    assert fb_kwargs["model"] == "cursor-agent"
    assert "из бриджа" in out
    # В футере должна быть отмечена fallback-модель, чтобы пользователь видел.
    assert "Модель: cursor-agent" in out


@pytest.mark.asyncio
async def test_fallback_used_on_empty_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_fallback(monkeypatch)

    primary = MagicMock()
    primary.chat.completions.create = AsyncMock(return_value=_make_mock_response(None))
    fallback = MagicMock()
    fallback.chat.completions.create = AsyncMock(return_value=_make_mock_response("спасение"))

    with patch("openai.AsyncOpenAI", side_effect=[primary, fallback]):
        out = await ai_checker.summarize_check_parts(["один", "два"])

    primary.chat.completions.create.assert_awaited_once()
    fallback.chat.completions.create.assert_awaited_once()
    assert "спасение" in out


@pytest.mark.asyncio
async def test_fallback_disabled_returns_primary_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:9/v1")
    monkeypatch.delenv("VLLM_FALLBACK_ENABLE", raising=False)
    monkeypatch.delenv("VLLM_FALLBACK_BASE_URL", raising=False)
    from openai import APIConnectionError

    primary = MagicMock()
    primary.chat.completions.create = AsyncMock(side_effect=APIConnectionError(request=MagicMock()))

    with patch("openai.AsyncOpenAI", side_effect=[primary]):
        out = await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 20,
            "image/jpeg",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
        )

    primary.chat.completions.create.assert_awaited_once()
    assert "подключиться" in out.lower()


@pytest.mark.asyncio
async def test_fallback_both_fail_returns_safe_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_fallback(monkeypatch)
    from openai import APIConnectionError

    primary = MagicMock()
    primary.chat.completions.create = AsyncMock(side_effect=APIConnectionError(request=MagicMock()))
    fallback = MagicMock()
    fallback.chat.completions.create = AsyncMock(side_effect=RuntimeError("bridge dead"))

    with patch("openai.AsyncOpenAI", side_effect=[primary, fallback]):
        out = await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 20,
            "image/jpeg",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
        )

    primary.chat.completions.create.assert_awaited_once()
    fallback.chat.completions.create.assert_awaited_once()
    # При падении обоих эндпоинтов пользователь должен увидеть «человеческое»
    # сообщение от primary (APIConnectionError), а не голое исключение.
    assert "подключиться" in out.lower()
    assert "Модель:" in out


@pytest.mark.asyncio
async def test_fallback_not_triggered_on_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_fallback(monkeypatch)
    from openai import APIStatusError

    err = APIStatusError(
        message="bad input",
        response=MagicMock(status_code=400),
        body=None,
    )

    primary = MagicMock()
    primary.chat.completions.create = AsyncMock(side_effect=err)
    fallback = MagicMock()
    fallback.chat.completions.create = AsyncMock(return_value=_make_mock_response("never"))

    with patch("openai.AsyncOpenAI", side_effect=[primary, fallback]):
        out = await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 20,
            "image/jpeg",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
        )

    primary.chat.completions.create.assert_awaited_once()
    # 4xx — клиентская ошибка, fallback не пытаемся (он бы её повторил).
    fallback.chat.completions.create.assert_not_called()
    assert "Ошибка VLLM" in out


@pytest.mark.asyncio
async def test_force_fallback_skips_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Кнопка «Проверить ещё раз (Cursor)» — primary VLLM не должен дёргаться."""
    _enable_fallback(monkeypatch)

    primary = MagicMock()
    primary.chat.completions.create = AsyncMock(return_value=_make_mock_response("должно игнорироваться"))
    fallback = MagicMock()
    fallback.chat.completions.create = AsyncMock(return_value=_make_mock_response("ответ от cursor"))

    with (
        patch("openai.AsyncOpenAI", side_effect=[fallback]) as mock_cls,
        patch(
            "preocr_client.fetch_preocr_block",
            AsyncMock(return_value="PRE_OCR_TEXT"),
        ),
    ):
        out = await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 20,
            "image/jpeg",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
            force_fallback=True,
        )

    assert mock_cls.call_count == 1, "должен быть создан только fallback-клиент"
    fb_kwargs = fallback.chat.completions.create.await_args.kwargs
    assert fb_kwargs["model"] == "cursor-agent"
    # При recheck через Cursor payload должен быть text-only (без image_url):
    # cursor-agent текстовый, base64-картинку всё равно не «видит».
    msg_content = fb_kwargs["messages"][0]["content"]
    assert isinstance(msg_content, list)
    assert all(c.get("type") == "text" for c in msg_content), msg_content
    assert any("PRE_OCR_TEXT" in c["text"] for c in msg_content)
    assert "ответ от cursor" in out
    assert "Модель: cursor-agent" in out
    primary.chat.completions.create.assert_not_called()


@pytest.mark.asyncio
async def test_force_fallback_image_without_preocr_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor recheck по фото без pre-OCR — внятное сообщение, fallback не вызываем.

    cursor-agent текстовый, base64-картинка ему бесполезна; без preocr нечего
    отправлять, поэтому возвращаем готовый текст с подсказкой про PREOCR_URL.
    """
    _enable_fallback(monkeypatch)

    fallback = MagicMock()
    fallback.chat.completions.create = AsyncMock(return_value=_make_mock_response("не должно вызваться"))

    with (
        patch("openai.AsyncOpenAI", side_effect=[fallback]) as mock_cls,
        patch(
            "preocr_client.fetch_preocr_block",
            AsyncMock(return_value=""),
        ),
    ):
        out = await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 20,
            "image/jpeg",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
            force_fallback=True,
        )

    assert mock_cls.call_count == 0
    fallback.chat.completions.create.assert_not_called()
    assert "PREOCR_URL" in out
    assert "только с текстом" in out.lower()


@pytest.mark.asyncio
async def test_force_fallback_image_uses_preocr_text_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cursor recheck по фото с pre-OCR: payload без image_url, GDZ-условие в нём же."""
    _enable_fallback(monkeypatch)

    fallback = MagicMock()
    fallback.chat.completions.create = AsyncMock(return_value=_make_mock_response("разбор"))

    with (
        patch("openai.AsyncOpenAI", side_effect=[fallback]),
        patch(
            "preocr_client.fetch_preocr_block",
            AsyncMock(return_value="OCR: ученик написал 12"),
        ),
    ):
        await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 20,
            "image/jpeg",
            paragraph="3",
            exercise="42",
            page=None,
            textbook_label="К",
            grade=6,
            gdz_task_condition="Найдите сумму 5 и 7.",
            force_fallback=True,
        )

    fb_kwargs = fallback.chat.completions.create.await_args.kwargs
    msg_content = fb_kwargs["messages"][0]["content"]
    assert isinstance(msg_content, list)
    # Ни одного image_url — Cursor получает только текст.
    assert all(c.get("type") == "text" for c in msg_content), msg_content
    joined = "\n".join(c["text"] for c in msg_content)
    assert "OCR: ученик написал 12" in joined
    # gdz_task_condition должен попасть в тот же текст — это и есть «сверка с ГДЗ».
    assert "Найдите сумму 5 и 7." in joined


@pytest.mark.asyncio
async def test_force_fallback_without_config_returns_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если fallback не сконфигурирован, force_fallback не должен «молча» уходить в primary."""
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:9/v1")
    monkeypatch.delenv("VLLM_FALLBACK_ENABLE", raising=False)
    monkeypatch.delenv("VLLM_FALLBACK_BASE_URL", raising=False)

    primary = MagicMock()
    primary.chat.completions.create = AsyncMock(return_value=_make_mock_response("primary"))

    with (
        patch("openai.AsyncOpenAI", side_effect=[primary]) as mock_cls,
        patch(
            "preocr_client.fetch_preocr_block",
            AsyncMock(return_value="PRE_OCR_TEXT"),
        ),
    ):
        out = await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 20,
            "image/jpeg",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
            force_fallback=True,
        )

    assert mock_cls.call_count == 0, "primary не должен создаваться при force_fallback"
    primary.chat.completions.create.assert_not_called()
    assert "недоступна" in out.lower()


@pytest.mark.asyncio
async def test_force_fallback_summarize(monkeypatch: pytest.MonkeyPatch) -> None:
    """summarize_check_parts с force_fallback=True уходит сразу в bridge."""
    _enable_fallback(monkeypatch)

    primary = MagicMock()
    primary.chat.completions.create = AsyncMock(return_value=_make_mock_response("primary"))
    fallback = MagicMock()
    fallback.chat.completions.create = AsyncMock(return_value=_make_mock_response("сводка из cursor"))

    with patch("openai.AsyncOpenAI", side_effect=[fallback]):
        out = await ai_checker.summarize_check_parts(
            ["часть один", "часть два"],
            force_fallback=True,
        )

    primary.chat.completions.create.assert_not_called()
    fallback.chat.completions.create.assert_awaited_once()
    assert "сводка из cursor" in out


@pytest.mark.asyncio
async def test_fallback_http_client_uses_dns_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если задан VLLM_FALLBACK_DNS_SERVERS, fallback-клиент создается с http_client,
    в котором стоит наш upstream-DNS transport, и этот клиент пробрасывается в AsyncOpenAI.
    """
    import tgzh_httpx

    _enable_fallback(monkeypatch)
    monkeypatch.setenv("VLLM_FALLBACK_DNS_SERVERS", "1.1.1.1, 8.8.8.8")
    # Сбросить кэшированный клиент между тестами.
    ai_checker._fallback_http_client_state["servers"] = None
    ai_checker._fallback_http_client_state["client"] = None
    tgzh_httpx._clear_dns_cache_for_tests()

    fallback = MagicMock()
    fallback.chat.completions.create = AsyncMock(return_value=_make_mock_response("ответ от cursor"))

    with (
        patch("openai.AsyncOpenAI", side_effect=[fallback]) as mock_cls,
        patch(
            "preocr_client.fetch_preocr_block",
            AsyncMock(return_value="PRE_OCR_TEXT"),
        ),
    ):
        await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 20,
            "image/jpeg",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
            force_fallback=True,
        )

    assert mock_cls.call_count == 1
    fb_kwargs = mock_cls.call_args.kwargs
    assert "http_client" in fb_kwargs, "должен прокинуться http_client с upstream-DNS"
    http_client = fb_kwargs["http_client"]
    transport = http_client._transport
    pool = getattr(transport, "_pool", None)
    assert pool is not None
    assert isinstance(pool._network_backend, tgzh_httpx._UpstreamDnsBackend)
    assert pool._network_backend._dns_servers == ("1.1.1.1", "8.8.8.8")


@pytest.mark.asyncio
async def test_fallback_no_http_client_when_dns_servers_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без VLLM_FALLBACK_DNS_SERVERS / _RESOLVE_HTTP_ECHO_URL — клиент создается
    без http_client (системный резолвер)."""
    _enable_fallback(monkeypatch)
    monkeypatch.delenv("VLLM_FALLBACK_DNS_SERVERS", raising=False)
    monkeypatch.delenv("VLLM_FALLBACK_RESOLVE_HTTP_ECHO_URL", raising=False)
    ai_checker._fallback_http_client_state["servers"] = None
    ai_checker._fallback_http_client_state["client"] = None

    fallback = MagicMock()
    fallback.chat.completions.create = AsyncMock(return_value=_make_mock_response("ok"))

    with (
        patch("openai.AsyncOpenAI", side_effect=[fallback]) as mock_cls,
        patch(
            "preocr_client.fetch_preocr_block",
            AsyncMock(return_value="PRE_OCR_TEXT"),
        ),
    ):
        await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 20,
            "image/jpeg",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
            force_fallback=True,
        )

    assert "http_client" not in mock_cls.call_args.kwargs


@pytest.mark.asyncio
async def test_fallback_http_client_uses_http_echo_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """С VLLM_FALLBACK_RESOLVE_HTTP_ECHO_URL — фабрика собирает _HttpEchoBackend
    с правильным target_host (хост из VLLM_FALLBACK_BASE_URL) и DNS как страховка."""
    import tgzh_httpx

    _enable_fallback(monkeypatch)
    monkeypatch.setenv("VLLM_FALLBACK_BASE_URL", "http://br.example.com:8787/v1")
    monkeypatch.setenv("VLLM_FALLBACK_RESOLVE_HTTP_ECHO_URL", "http://echo.example/")
    monkeypatch.setenv("VLLM_FALLBACK_DNS_SERVERS", "1.1.1.1, 8.8.8.8")
    ai_checker._fallback_http_client_state["servers"] = None
    ai_checker._fallback_http_client_state["client"] = None
    tgzh_httpx._clear_echo_cache_for_tests()
    tgzh_httpx._clear_dns_cache_for_tests()

    fallback = MagicMock()
    fallback.chat.completions.create = AsyncMock(return_value=_make_mock_response("ok"))

    with (
        patch("openai.AsyncOpenAI", side_effect=[fallback]) as mock_cls,
        patch(
            "preocr_client.fetch_preocr_block",
            AsyncMock(return_value="PRE_OCR_TEXT"),
        ),
    ):
        await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 20,
            "image/jpeg",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
            force_fallback=True,
        )

    fb_kwargs = mock_cls.call_args.kwargs
    http_client = fb_kwargs["http_client"]
    backend = http_client._transport._pool._network_backend
    assert isinstance(backend, tgzh_httpx._HttpEchoBackend)
    assert backend._target_host == "br.example.com"
    assert backend._echo_url == "http://echo.example/"
    assert backend._dns_fallback == ("1.1.1.1", "8.8.8.8")


@pytest.mark.asyncio
async def test_fallback_http_client_echo_without_target_host_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если echo задан, но в VLLM_FALLBACK_BASE_URL нет валидного хоста — echo
    игнорируется; при наличии DNS — используем DNS-backend."""
    import tgzh_httpx

    _enable_fallback(monkeypatch)
    monkeypatch.setenv("VLLM_FALLBACK_BASE_URL", "http:///v1")  # без хоста
    monkeypatch.setenv("VLLM_FALLBACK_RESOLVE_HTTP_ECHO_URL", "http://echo.example/")
    monkeypatch.setenv("VLLM_FALLBACK_DNS_SERVERS", "1.1.1.1")
    ai_checker._fallback_http_client_state["servers"] = None
    ai_checker._fallback_http_client_state["client"] = None

    fallback = MagicMock()
    fallback.chat.completions.create = AsyncMock(return_value=_make_mock_response("ok"))

    with (
        patch("openai.AsyncOpenAI", side_effect=[fallback]) as mock_cls,
        patch(
            "preocr_client.fetch_preocr_block",
            AsyncMock(return_value="PRE_OCR_TEXT"),
        ),
    ):
        await ai_checker._check_vllm(
            b"\xff\xd8\xff" + b"\x00" * 20,
            "image/jpeg",
            paragraph="1",
            exercise=None,
            page=None,
            textbook_label="",
            grade=None,
            force_fallback=True,
        )

    fb_kwargs = mock_cls.call_args.kwargs
    backend = fb_kwargs["http_client"]._transport._pool._network_backend
    assert isinstance(backend, tgzh_httpx._UpstreamDnsBackend)
