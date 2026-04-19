"""Общие фикстуры: изоляция от .env-переменных, которые могут утечь через `load_dotenv()`
при импорте `bot.py` / `server.py` в одном процессе с тестами."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_env_for_unit_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    1) `DATABASE_URL` — иначе `user_storage.connect(path)` уйдет в Postgres вместо SQLite.
    2) `CURSOR_CLI_CWD` — `.env` разработчика часто содержит абсолютный путь, которого нет
       на CI/в чужой машине; без сброса subprocess.exec в тестах падает по `FileNotFoundError`.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("CURSOR_CLI_CWD", raising=False)
