"""
Общий слой подключения к БД: SQLite по пути USER_DB_PATH или PostgreSQL по DATABASE_URL.
Плейсхолдеры в SQL - только «?»; при PostgreSQL перед выполнением заменяются на «%s».
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def use_postgres() -> bool:
    return bool((os.getenv("DATABASE_URL") or "").strip())


def normalize_postgresql_url_for_sqlalchemy(url: str) -> str:
    """
    SQLAlchemy для postgresql:// по умолчанию тянет psycopg2; в проекте - psycopg3 (пакет psycopg).
    """
    u = url.strip()
    if not u:
        return u
    scheme, sep, rest = u.partition("://")
    if sep and "+" not in scheme and scheme in ("postgresql", "postgres"):
        return f"postgresql+psycopg://{rest}"
    return u


def connect(db_path: str) -> Any:
    if use_postgres():
        import psycopg

        return psycopg.connect(os.environ["DATABASE_URL"].strip())
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    import sqlite3

    return sqlite3.connect(db_path)


def execute(conn: Any, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
    s = sql.replace("?", "%s") if use_postgres() else sql
    return conn.execute(s, tuple(params))


def db_path_usable(db_path: str) -> bool:
    """Для SQLite - файл существует; для Postgres URL задан."""
    if use_postgres():
        return True
    return Path(db_path).is_file()
