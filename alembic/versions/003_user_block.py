"""Таблица user_block (временный и постоянный бан).

Revision ID: 003_user_block
Revises: 002_check_result_vote
Create Date: 2026-04-11

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_user_block"
down_revision: Union[str, None] = "002_check_result_vote"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS user_block (
                user_id INTEGER PRIMARY KEY NOT NULL,
                block_type TEXT NOT NULL CHECK (block_type IN ('temp', 'permanent')),
                reason TEXT,
                until_utc TEXT,
                created_at TEXT NOT NULL
            )
            """
        ),
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS user_block"))
