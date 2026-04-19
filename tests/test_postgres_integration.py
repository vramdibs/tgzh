"""
Интеграция с PostgreSQL (схема BIGINT user_id, ON CONFLICT).

По умолчанию не запускаются. Прогон с хоста после поднятия compose:

  export TGZH_TEST_DATABASE_URL=postgresql://tgzh:tgzh@127.0.0.1:15432/tgzh
  pytest tests/test_postgres_integration.py -v

Порт 15432 проброшен из docker-compose для сервиса postgres.
"""

from __future__ import annotations

import os
import random

import pytest

import user_storage


def _postgres_test_url() -> str:
    return (os.getenv("TGZH_TEST_DATABASE_URL") or "").strip()


@pytest.fixture
def postgres_env(monkeypatch: pytest.MonkeyPatch) -> str:
    url = _postgres_test_url()
    if not url:
        pytest.skip(
            "Задайте TGZH_TEST_DATABASE_URL (например postgresql://tgzh:tgzh@127.0.0.1:15432/tgzh)"
        )
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def _random_large_telegram_user_id() -> int:
    """Выше INT32_MAX, как у части аккаунтов Telegram."""
    return random.randint(2_147_483_648, 5_500_000_000)


@pytest.mark.integration
def test_postgres_consent_upsert_large_user_id(postgres_env: str) -> None:
    user_storage.init_db("ignored_with_postgres")
    uid = _random_large_telegram_user_id()
    user_storage.upsert_disclaimer_quiz_passed("ignored", uid, version=1)
    row = user_storage.get_user_consent("ignored", uid)
    assert row is not None
    assert row.user_id == uid
    assert row.disclaimer_version == 1
    assert row.quiz_passed_at
    user_storage.upsert_disclaimer_quiz_passed("ignored", uid, version=1)
    row2 = user_storage.get_user_consent("ignored", uid)
    assert row2 is not None
    assert row2.user_id == uid


@pytest.mark.integration
def test_postgres_check_result_vote_large_user_id(postgres_env: str) -> None:
    user_storage.init_db("ignored_with_postgres")
    uid = _random_large_telegram_user_id()
    chat_id = -100_000_000_0000
    mid = random.randint(1, 999_999)
    assert user_storage.upsert_check_result_vote(
        "ignored",
        chat_id=chat_id,
        message_id=mid,
        user_id=uid,
        vote=1,
    )
    assert user_storage.upsert_check_result_vote(
        "ignored",
        chat_id=chat_id,
        message_id=mid,
        user_id=uid,
        vote=1,
    ) is False
    assert user_storage.upsert_check_result_vote(
        "ignored",
        chat_id=chat_id,
        message_id=mid,
        user_id=uid,
        vote=-1,
    )
