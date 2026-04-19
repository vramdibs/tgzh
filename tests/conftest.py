"""Общие фикстуры: изоляция от .env-переменных, которые могут утечь через `load_dotenv()`
при импорте `bot.py` / `server.py` в одном процессе с тестами."""

from __future__ import annotations

import pytest


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
        # CHAT_MEMORY_* / MEMORY_DIR_BASE — иначе тесты «сна памяти» подхватят
        # реальные настройки (например, кастомный путь к каталогу памяти),
        # и unit-тесты, изолированные через tmp_path, начнут видеть «лишние»
        # файлы или нестандартные пороги.
        "MEMORY_DIR_BASE",
        "CHAT_MEMORY_SLEEP_AFTER_MSGS",
        "CHAT_MEMORY_SLEEP_MIN_GAP_SEC",
        "CHAT_MEMORY_SLEEP_TRANSCRIPT_TURNS",
        "CHAT_MEMORY_SLEEP_TIMEOUT_S",
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
    ):
        monkeypatch.delenv(key, raising=False)
