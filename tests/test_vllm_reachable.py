"""
Доступность OpenAI-совместимого VLLM по HTTP (GET /v1/models).

По умолчанию тест **пропускается**, чтобы `pytest` не ходил в сеть из `.env`.

Явный запуск:
  RUN_LLM_LIVE=1 pytest -m llm

Нужны **VLLM_BASE_URL**, **AI_MOCK** не **1** (как в рабочем режиме бота/сервера).
"""

from __future__ import annotations

import os

import httpx
import pytest

from ai_checker import _normalize_vllm_base_url


@pytest.mark.llm
def test_vllm_openai_compatible_http_reachable() -> None:
    if os.getenv("RUN_LLM_LIVE", "").strip() != "1":
        pytest.skip("Проверка LLM: RUN_LLM_LIVE=1 pytest -m llm")

    raw = os.getenv("VLLM_BASE_URL", "").strip()
    if not raw:
        pytest.skip("Нет VLLM_BASE_URL — пропуск проверки доступности LLM")
    if os.getenv("AI_MOCK", "").strip() == "1":
        pytest.skip("AI_MOCK=1 — живой LLM не используется")

    base = _normalize_vllm_base_url(raw)
    url = f"{base}/models"
    api_key = os.getenv("VLLM_API_KEY", "") or "EMPTY"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        r = httpx.get(url, headers=headers, timeout=25.0, follow_redirects=True)
    except httpx.RequestError as e:
        pytest.fail(f"LLM недоступен по сети: {e}")

    assert r.status_code < 500, (
        f"LLM ответил {r.status_code} на GET {url!r}: {r.text[:400]!r}"
    )
