"""Бот: интеграция «сна памяти» — клавиатура, инъекция, планировщик авто-сна."""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import bot
import chat_memory
import user_storage


# ====================== fixtures ======================


@pytest.fixture(autouse=True)
def _isolated_memory_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("MEMORY_DIR_BASE", str(tmp_path))
    return tmp_path


@pytest.fixture
def db_path(monkeypatch: pytest.MonkeyPatch) -> str:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    user_storage.init_db(path)
    monkeypatch.setattr(bot, "USER_DB_PATH", path)
    yield path
    os.unlink(path)


# ====================== _chat_menu_keyboard ======================


def test_chat_menu_keyboard_includes_memory_buttons_when_active() -> None:
    kb = bot._chat_menu_keyboard(active=True, memory_enabled=True)
    flat = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "chat:mem_toggle" in flat
    assert "chat:mem_show" in flat
    assert "chat:mem_sleep" in flat
    assert "chat:mem_purge" in flat


def test_chat_menu_keyboard_toggle_label_reflects_state() -> None:
    on_kb = bot._chat_menu_keyboard(active=True, memory_enabled=True)
    off_kb = bot._chat_menu_keyboard(active=True, memory_enabled=False)
    on_label = next(
        b.text
        for row in on_kb.inline_keyboard
        for b in row
        if b.callback_data == "chat:mem_toggle"
    )
    off_label = next(
        b.text
        for row in off_kb.inline_keyboard
        for b in row
        if b.callback_data == "chat:mem_toggle"
    )
    assert "вкл" in on_label.lower()
    assert "выкл" in off_label.lower()


def test_chat_menu_keyboard_inactive_has_no_memory_buttons() -> None:
    kb = bot._chat_menu_keyboard(active=False)
    flat = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "chat:mem_toggle" not in flat
    assert "chat:mem_sleep" not in flat


def test_chat_menu_keyboard_for_user_uses_db_state(db_path: str) -> None:
    user_storage.chat_memory_set_enabled(db_path, 42, False)
    kb = bot._chat_menu_keyboard_for_user(True, 42)
    label = next(
        b.text
        for row in kb.inline_keyboard
        for b in row
        if b.callback_data == "chat:mem_toggle"
    )
    assert "выкл" in label.lower()


# ====================== _build_chat_system_prompt_with_memory ======================


def test_system_prompt_returns_none_when_disabled(db_path: str) -> None:
    chat_memory.write_memory_file(1, "MEMORY.md", "INDEX")
    user_storage.chat_memory_set_enabled(db_path, 1, False)
    assert bot._build_chat_system_prompt_with_memory(1) is None


def test_system_prompt_returns_none_when_no_memory(db_path: str) -> None:
    # enabled by default, но папка пуста
    assert bot._build_chat_system_prompt_with_memory(2) is None


def test_system_prompt_includes_memory_when_enabled(db_path: str) -> None:
    chat_memory.write_memory_file(3, "MEMORY.md", "MY-INDEX")
    chat_memory.write_memory_file(3, "projects.md", "PROJ-X")
    out = bot._build_chat_system_prompt_with_memory(3)
    assert out is not None
    assert "MY-INDEX" in out
    assert "PROJ-X" in out
    # base prompt тоже на месте
    assert "ИИ-ассистент" in out or "ассистент" in out.lower()
    # И блок безопасности — обязателен.
    assert bot._CHAT_SAFETY_POLICY in out


def test_bot_safety_policy_always_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_bot_chat_default_system_prompt` всегда префиксует политику безопасности."""
    monkeypatch.delenv("CHAT_SYSTEM_PROMPT", raising=False)
    default_prompt = bot._bot_chat_default_system_prompt()
    assert default_prompt.startswith(bot._CHAT_SAFETY_POLICY)
    for keyword in (
        "ЗАПРЕЩЕНО",
        "shell",
        "листинг",
        "соседние файлы",
        "multimodal-контент",
    ):
        assert keyword in default_prompt
    monkeypatch.setenv("CHAT_SYSTEM_PROMPT", "  кастомный  ")
    custom = bot._bot_chat_default_system_prompt()
    assert custom.startswith(bot._CHAT_SAFETY_POLICY)
    assert custom.endswith("кастомный")


def test_bot_and_server_safety_policy_are_in_sync() -> None:
    """Дубликат политики безопасности на стороне бота и сервера должен совпадать."""
    import ai_checker

    assert bot._CHAT_SAFETY_POLICY == ai_checker.CHAT_SAFETY_POLICY


# ====================== auto-sleep scheduler ======================


def test_maybe_schedule_auto_sleep_skips_when_disabled(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_storage.chat_memory_set_enabled(db_path, 1, False)
    # счётчик выше порога
    for _ in range(50):
        user_storage.chat_memory_increment_msgs(db_path, 1)

    called = {"n": 0}

    def _fake_create_task(coro, *a, **kw):
        called["n"] += 1
        coro.close()

        class _T:
            def done(self) -> bool:
                return True

        return _T()

    monkeypatch.setattr(asyncio, "create_task", _fake_create_task)
    bot._maybe_schedule_auto_sleep(1, [{"role": "user", "content": "x"}])
    assert called["n"] == 0


def test_maybe_schedule_auto_sleep_skips_below_threshold(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHAT_MEMORY_SLEEP_AFTER_MSGS", "5")
    user_storage.chat_memory_increment_msgs(db_path, 1)
    user_storage.chat_memory_increment_msgs(db_path, 1)
    called = {"n": 0}

    def _fake_create_task(coro, *a, **kw):
        called["n"] += 1
        coro.close()

        class _T:
            def done(self) -> bool:
                return True

        return _T()

    monkeypatch.setattr(asyncio, "create_task", _fake_create_task)
    bot._maybe_schedule_auto_sleep(1, [{"role": "user", "content": "x"}])
    assert called["n"] == 0


def test_maybe_schedule_auto_sleep_starts_task_when_threshold_reached(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHAT_MEMORY_SLEEP_AFTER_MSGS", "3")
    monkeypatch.setenv("CHAT_MEMORY_SLEEP_MIN_GAP_SEC", "0")
    for _ in range(3):
        user_storage.chat_memory_increment_msgs(db_path, 1)

    started = {"n": 0}

    def _fake_create_task(coro, *a, **kw):
        started["n"] += 1
        coro.close()  # не запускаем — нам важен лишь сам факт планирования

        class _T:
            def done(self) -> bool:
                return True

        return _T()

    bot._CHAT_SLEEP_RUNNING.pop(1, None)
    monkeypatch.setattr(asyncio, "create_task", _fake_create_task)
    bot._maybe_schedule_auto_sleep(
        1, [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
    )
    assert started["n"] == 1
    bot._CHAT_SLEEP_RUNNING.pop(1, None)


def test_maybe_schedule_auto_sleep_respects_min_gap(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHAT_MEMORY_SLEEP_AFTER_MSGS", "3")
    monkeypatch.setenv("CHAT_MEMORY_SLEEP_MIN_GAP_SEC", "3600")
    # последний сон только что
    user_storage.chat_memory_record_sleep(db_path, 1, status="ok")
    for _ in range(5):
        user_storage.chat_memory_increment_msgs(db_path, 1)

    started = {"n": 0}

    def _fake_create_task(coro, *a, **kw):
        started["n"] += 1
        coro.close()

        class _T:
            def done(self) -> bool:
                return True

        return _T()

    monkeypatch.setattr(asyncio, "create_task", _fake_create_task)
    bot._maybe_schedule_auto_sleep(1, [{"role": "user", "content": "x"}])
    assert started["n"] == 0


# ====================== _run_chat_sleep ======================


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_run_chat_sleep_applies_ops_and_records_status(
    db_path: str,
) -> None:
    fake_response = (
        "<<<FILE:MEMORY.md>>>\n"
        "* projects.md — что строим\n"
        "<<<END>>>\n"
        "<<<FILE:projects.md>>>\n"
        "Проект A: ...\n"
        "<<<END>>>\n"
    )

    async def _fake_request(*, user_id, messages, timeout_s):
        assert user_id == 5
        assert any(m["role"] == "user" for m in messages)
        return fake_response

    with patch.object(bot, "_request_chat_once_from_server", _fake_request):
        ok, status = asyncio.run(
            bot._run_chat_sleep(
                user_id=5,
                transcript=[{"role": "user", "content": "hi"}],
                trigger="test",
            ),
        )
    assert ok is True
    assert "written=2" in status
    files = chat_memory.list_memory_files(5)
    assert "MEMORY.md" in files
    assert "projects.md" in files
    pref = user_storage.chat_memory_get_pref(db_path, 5)
    assert pref.last_sleep_status is not None
    assert "written=2" in pref.last_sleep_status
    assert pref.msgs_since_sleep == 0


def test_run_chat_sleep_records_error_when_request_fails(db_path: str) -> None:
    async def _fail(*, user_id, messages, timeout_s):
        raise RuntimeError("server 503: not configured")

    with patch.object(bot, "_request_chat_once_from_server", _fail):
        ok, status = asyncio.run(
            bot._run_chat_sleep(
                user_id=6,
                transcript=[{"role": "user", "content": "hi"}],
                trigger="test",
            ),
        )
    assert ok is False
    assert "error" in status
    pref = user_storage.chat_memory_get_pref(db_path, 6)
    assert pref.last_sleep_status is not None
    assert "error" in pref.last_sleep_status


def test_run_chat_sleep_noop_when_model_returns_empty(db_path: str) -> None:
    async def _empty(*, user_id, messages, timeout_s):
        return ""

    with patch.object(bot, "_request_chat_once_from_server", _empty):
        ok, status = asyncio.run(
            bot._run_chat_sleep(
                user_id=8,
                transcript=[{"role": "user", "content": "hi"}],
                trigger="test",
            ),
        )
    assert ok is True
    assert status == "noop"
    pref = user_storage.chat_memory_get_pref(db_path, 8)
    assert pref.last_sleep_status == "noop"


# ====================== _chat_history_for_sleep ======================


def test_chat_history_for_sleep_keeps_only_user_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_MEMORY_SLEEP_TRANSCRIPT_TURNS", "10")
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": ""},  # пустые отбрасываем
        {"role": "user", "content": "u2"},
    ]
    out = bot._chat_history_for_sleep(history)
    roles = [m["role"] for m in out]
    assert "system" not in roles
    assert roles == ["user", "assistant", "user"]


def test_chat_history_for_sleep_keeps_only_last_n(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_MEMORY_SLEEP_TRANSCRIPT_TURNS", "3")
    history = [
        {"role": "user", "content": f"m{i}"} for i in range(10)
    ]
    out = bot._chat_history_for_sleep(history)
    assert len(out) == 3
    assert out[-1]["content"] == "m9"
