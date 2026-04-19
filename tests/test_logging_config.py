"""Тесты обрезки тел для логов /check."""

import os

import pytest

from logging_config import check_log_body_max_chars, clip_check_log_body


def test_clip_check_log_body_short() -> None:
    assert clip_check_log_body("abc") == "abc"
    assert clip_check_log_body(None) == ""


def test_clip_check_log_body_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECK_LOG_BODY_MAX_CHARS", "12")
    s = "x" * 40
    out = clip_check_log_body(s)
    assert out.startswith("x" * 12)
    assert "truncated" in out
    assert "len=40" in out


def test_check_log_body_max_chars_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHECK_LOG_BODY_MAX_CHARS", raising=False)
    assert check_log_body_max_chars() == 16_384
    monkeypatch.setenv("CHECK_LOG_BODY_MAX_CHARS", "999999")
    assert check_log_body_max_chars() == 256_000
    monkeypatch.setenv("CHECK_LOG_BODY_MAX_CHARS", "50")
    assert check_log_body_max_chars() == 50
    monkeypatch.setenv("CHECK_LOG_BODY_MAX_CHARS", "10")
    assert check_log_body_max_chars() == 10
