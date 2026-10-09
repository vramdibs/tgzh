"""Тесты извлечения текста документов для /chat."""

from __future__ import annotations

import chat_documents


def test_classify_pdf_and_office() -> None:
    assert chat_documents.classify_chat_document("application/pdf", "a.pdf") == "pdf"
    assert (
        chat_documents.classify_chat_document(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "x.docx",
        )
        == "office_unsupported"
    )


def test_extract_plain_text() -> None:
    data = "тариф 500 руб\nинтернет 50 ГБ".encode("utf-8")
    out = chat_documents.extract_chat_document_text(data, "text/plain", "t.txt")
    assert "тариф" in out


def test_compose_two_files_with_markers() -> None:
    files = [
        chat_documents.ChatFileExtract("a.pdf", "тариф A"),
        chat_documents.ChatFileExtract("b.pdf", "тариф B"),
    ]
    msg = chat_documents.compose_chat_files_user_message("сравни", files)
    assert "<<<USER_FILE>>>" in msg
    assert "тариф A" in msg
    assert "тариф B" in msg
    assert "сравни" in msg


def test_history_placeholder() -> None:
    ph = chat_documents.history_placeholder_for_files(
        ["one.pdf", "two.pdf"],
        "лучший тариф",
    )
    assert "one.pdf" in ph
    assert "лучший тариф" in ph
    assert "base64" not in ph


def test_compose_truncation_note() -> None:
    long_body = "x" * 50_000
    files = [chat_documents.ChatFileExtract("big.pdf", long_body)]
    msg = chat_documents.compose_chat_files_user_message("", files)
    assert "обрезана" in msg or "…" in msg
