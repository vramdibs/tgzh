"""Общие фикстуры: изоляция от DATABASE_URL в окружении для тестов на SQLite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_database_url_for_sqlite_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    При загруженном .env с DATABASE_URL вызовы user_storage.connect(path) ушли бы в Postgres.
    Юнит-тесты на временных файлах SQLite ожидают пустой DATABASE_URL.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
