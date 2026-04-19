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
    ):
        monkeypatch.delenv(key, raising=False)
