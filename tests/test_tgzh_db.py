"""tgzh_db: нормализация URL для SQLAlchemy + psycopg3."""

from __future__ import annotations

import pytest

from tgzh_db import normalize_postgresql_url_for_sqlalchemy, use_postgres


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg2://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
        ("  postgresql://x/y  ", "postgresql+psycopg://x/y"),
    ],
)
def test_normalize_postgresql_url_for_sqlalchemy(raw: str, expected: str) -> None:
    assert normalize_postgresql_url_for_sqlalchemy(raw) == expected


def test_use_postgres_false_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert use_postgres() is False


def test_use_postgres_true_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://a:b@localhost/x")
    assert use_postgres() is True
