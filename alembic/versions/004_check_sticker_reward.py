"""Таблица check_sticker_reward (стикеры и quip после проверки).

Revision ID: 004_check_sticker_reward
Revises: 003_user_block
Create Date: 2026-04-11

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_check_sticker_reward"
down_revision: Union[str, None] = "003_user_block"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS check_sticker_reward (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('reward', 'motivation')),
                sticker_file_id TEXT,
                sticker_set_name TEXT,
                quip TEXT,
                created_at TEXT NOT NULL
            )
            """
        ),
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS check_sticker_reward"))
