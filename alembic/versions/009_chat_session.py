"""Таблица chat_session: авторизация в скрытом /chat на 7 суток.

Revision ID: 009_chat_session
Revises: 008_user_id_bigint
Create Date: 2026-04-19

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009_chat_session"
down_revision: Union[str, None] = "008_user_id_bigint"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS chat_session (
                user_id BIGINT PRIMARY KEY NOT NULL,
                granted_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        ),
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS chat_session"))
