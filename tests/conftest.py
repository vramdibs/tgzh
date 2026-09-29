"""Общие фикстуры: изоляция от .env-переменных, которые могут утечь через `load_dotenv()`
при импорте `bot.py` / `server.py` в одном процессе с тестами."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


def _openai_exc(name: str) -> type[Exception]:
    class _Exc(Exception):
        def __init__(self, *args, **kwargs):
            super().__init__(args[0] if args else kwargs.get("message", name))

    _Exc.__name__ = name
    return _Exc


class _APIStatusError(Exception):
    def __init__(self, *args, message: str = "", response=None, body=None, **kwargs):
        self.message = message or (args[0] if args else "")
        super().__init__(self.message)
        self.response = response
        self.status_code = getattr(response, "status_code", None) if response else None
        self.body = body


@pytest.fixture(autouse=True)
def _stub_openai_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Локальные import openai в ai_checker.py; без пакета openai patch() падает."""
    module = types.ModuleType("openai")
    module.APIConnectionError = _openai_exc("APIConnectionError")
    module.APIStatusError = _APIStatusError
    module.AsyncOpenAI = MagicMock()
    monkeypatch.setitem(sys.modules, "openai", module)


@pytest.fixture(autouse=True)
def _clear_env_for_unit_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """`DATABASE_URL` — иначе `user_storage.connect(path)` уйдет в Postgres вместо SQLite.

    `VLLM_FALLBACK_*` — иначе при включенном в dev-`.env` cursor-bridge unit-тесты могут
    «случайно» успешно сходить в реальный bridge и сломать assert-ы на `""`/mock-ответ
    (см. `test_generate_check_quip_empty_base_url`). Тесты, которым fallback нужен,
    включают переменные сами через `_enable_fallback`.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for key in (
        "VLLM_FALLBACK_ENABLE",
        "VLLM_FALLBACK_BASE_URL",
        "VLLM_FALLBACK_API_KEY",
        "VLLM_FALLBACK_MODEL",
        "VLLM_FALLBACK_TIMEOUT_SEC",
        "VLLM_FALLBACK_DNS_SERVERS",
        "VLLM_FALLBACK_RESOLVE_HTTP_ECHO_URL",
        # PREOCR_URL включенный в dev-`.env` приведет к реальному походу в tgzh-preocr
        # из юнитов; тесты, которым preocr нужен, включают переменную сами или мокают
        # `preocr_client.fetch_preocr_block`.
        "PREOCR_URL",
        "CHAT_SYSTEM_PROMPT",
        # STT_* — у нас опциональный STT для голосовых; в юнит-тестах не должны
        # случайно стучаться в реальный Whisper-эндпоинт через dev-`.env`.
        "STT_BASE_URL",
        "STT_API_KEY",
        "STT_MODEL",
        "STT_LANGUAGE",
        "STT_TIMEOUT_S",
        "STT_MAX_AUDIO_BYTES",
        # DISCLAIMER_* — в проде висит DISCLAIMER_VERSION=N и пока пользователь
        # не «согласен», `_disclaimer_consent_ok → False`. Юниты, которые этого
        # явно не моделируют, иначе уходят в reply «Сначала открой /start…».
        # Тесты, которым нужен дисклеймер, выставляют переменные сами.
        "DISCLAIMER_VERSION",
        "DISCLAIMER_SKIP",
        "DISCLAIMER_TEXT",
        "DISCLAIMER_QUIZ_QUESTION",
        "DISCLAIMER_QUIZ_CORRECT",
        "DISCLAIMER_QUIZ_OPT_0",
        "DISCLAIMER_QUIZ_OPT_1",
        "DISCLAIMER_QUIZ_OPT_2",
        "DISCLAIMER_QUIZ_OPT_3",
        "DISCLAIMER_ACCEPT_BUTTON",
        "MOTOK_HUB_URL",
        "MOTOK_HUB_TOKEN_SECRET",
        "MOTOK_INTERNAL_TOKEN",
        "VLLM_VISION",
    ):
        monkeypatch.delenv(key, raising=False)
