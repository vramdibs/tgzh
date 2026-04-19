"""Таблица check_result_vote (лайк/дизлайк под результатом проверки).

Revision ID: 002_check_result_vote
Revises: 001_initial
Create Date: 2026-04-11

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002_check_result_vote"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS check_result_vote (
                chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                vote INTEGER NOT NULL CHECK (vote IN (1, -1)),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, message_id, user_id)
            )
            """
        ),
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS check_result_vote"))
