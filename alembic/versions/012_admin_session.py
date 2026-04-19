"""Таблица admin_session: авторизация в скрытом /begemot на 365 суток.

Revision ID: 012_admin_session
Revises: 011_chat_memory_pref
Create Date: 2026-04-19

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012_admin_session"
down_revision: Union[str, None] = "011_chat_memory_pref"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS admin_session (
                user_id BIGINT PRIMARY KEY NOT NULL,
                granted_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        ),
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS admin_session"))
