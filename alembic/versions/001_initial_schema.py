"""Начальная схема PostgreSQL (таблицы user_*, bot_stats_by_year).

Revision ID: 001_initial
Revises:
Create Date: 2026-04-11

"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _run_postgres_schema_sql() -> None:
    root = Path(__file__).resolve().parents[2]
    raw = (root / "scripts" / "postgres_schema.sql").read_text(encoding="utf-8")
    for chunk in raw.split(";"):
        lines = [
            ln
            for ln in chunk.splitlines()
            if ln.strip() and not ln.strip().startswith("--")
        ]
        stmt = "\n".join(lines).strip()
        if stmt:
            op.execute(sa.text(stmt))


def upgrade() -> None:
    _run_postgres_schema_sql()


def downgrade() -> None:
    """
    Downgrade начальной схемы намеренно не реализован: upgrade() прогоняет полный
    scripts/postgres_schema.sql (около десятка таблиц с FK), и неполный DROP может
    оставить осиротевшие объекты. Если действительно нужно «снести всё», делать это
    отдельным DDL-скриптом или drop_all через SQLAlchemy metadata.
    """
    raise NotImplementedError(
        "Downgrade начальной схемы 001_initial не поддерживается. "
        "Удаляйте схему вручную (DROP SCHEMA public CASCADE; CREATE SCHEMA public;) "
        "или пересоздайте БД."
    )
