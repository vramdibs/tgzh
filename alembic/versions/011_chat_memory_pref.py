"""Таблица chat_memory_pref: тоггл «сна памяти» + счётчик с прошлого сна.

Revision ID: 011_chat_memory_pref
Revises: 010_chat_dialog
Create Date: 2026-04-19

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011_chat_memory_pref"
down_revision: Union[str, None] = "010_chat_dialog"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS chat_memory_pref (
                user_id BIGINT PRIMARY KEY NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                msgs_since_sleep INTEGER NOT NULL DEFAULT 0,
                last_sleep_at TEXT,
                last_sleep_status TEXT,
                updated_at TEXT NOT NULL
            )
            """
        ),
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS chat_memory_pref"))
