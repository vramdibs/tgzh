"""Allowlist и флаг включения /cursorask в bot (без Telegram)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import bot as bot_module


def test_cursor_ask_allowlist_ids_empty(monkeypatch):
    monkeypatch.delenv("CURSOR_ASK_ALLOW_TELEGRAM_IDS", raising=False)
    assert bot_module._cursor_ask_allowlist_ids() == set()


def test_cursor_ask_allowlist_ids_parses_and_skips_garbage(monkeypatch):
    monkeypatch.setenv("CURSOR_ASK_ALLOW_TELEGRAM_IDS", " 1 , 2 , bad , , 3 ")
    assert bot_module._cursor_ask_allowlist_ids() == {1, 2, 3}


def test_cursor_ask_feature_enabled_requires_both(monkeypatch, tmp_path):
    monkeypatch.delenv("CURSOR_ASK_ALLOW_TELEGRAM_IDS", raising=False)
    fake = tmp_path / "agent"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CURSOR_CLI_BIN", str(fake))
    assert bot_module._cursor_ask_feature_enabled() is False

    monkeypatch.setenv("CURSOR_ASK_ALLOW_TELEGRAM_IDS", "42")
    monkeypatch.setenv("CURSOR_CLI_BIN", "nonexistent-binary-xyz-12345")
    monkeypatch.delenv("PATH", raising=False)
    assert bot_module._cursor_ask_feature_enabled() is False

    monkeypatch.setenv("CURSOR_CLI_BIN", str(fake))
    monkeypatch.setenv("CURSOR_ASK_ALLOW_TELEGRAM_IDS", "42")
    assert bot_module._cursor_ask_feature_enabled() is True


def test_cursor_ask_feature_enabled_uses_cli_configured(monkeypatch):
    monkeypatch.setenv("CURSOR_ASK_ALLOW_TELEGRAM_IDS", "1")
    with patch.object(bot_module.cursor_cli_client, "cli_configured", return_value=False):
        assert bot_module._cursor_ask_feature_enabled() is False
    with patch.object(bot_module.cursor_cli_client, "cli_configured", return_value=True):
        assert bot_module._cursor_ask_feature_enabled() is True
