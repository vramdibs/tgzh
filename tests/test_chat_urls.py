"""Тесты разбора и SSRF-проверки ссылок для /chat."""

from __future__ import annotations

from unittest.mock import patch

import chat_urls


def test_extract_http_urls_dedup_and_strip() -> None:
    text = (
        "Смотри https://example.com/a и https://example.com/b "
        "https://example.com/a снова."
    )
    urls = chat_urls.extract_http_urls(text)
    assert urls == ["https://example.com/a", "https://example.com/b"]
    assert chat_urls.strip_urls_from_text(text) == "Смотри и снова."


def test_is_safe_fetch_url_blocks_localhost() -> None:
    assert chat_urls.is_safe_fetch_url("http://localhost/page") is False


def test_is_safe_fetch_url_allows_public_hostname() -> None:
    with patch.object(chat_urls.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
        assert chat_urls.is_safe_fetch_url("https://example.com/tariff") is True


def test_html_to_text_strips_scripts() -> None:
    html = b"<html><script>x</script><body><p>Hello tariff</p></body></html>"
    out = chat_urls.html_to_text(html)
    assert "Hello tariff" in out
    assert "x" not in out


def test_compose_urls_and_files() -> None:
    import chat_documents

    msg = chat_urls.compose_chat_urls_and_files_message(
        "сравни",
        [chat_documents.ChatFileExtract("a.pdf", "A")],
        [chat_urls.ChatUrlExtract("https://ex.com", "B")],
    )
    assert "URL" in msg
    assert "Файл" in msg
