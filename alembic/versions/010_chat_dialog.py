"""Таблица chat_dialog: до 10 сохранённых диалогов /chat на пользователя.

Revision ID: 010_chat_dialog
Revises: 009_chat_session
Create Date: 2026-04-19

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010_chat_dialog"
down_revision: Union[str, None] = "009_chat_session"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS chat_dialog (
                dialog_id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                title TEXT NOT NULL,
                history_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        ),
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_chat_dialog_user_updated "
            "ON chat_dialog (user_id, updated_at DESC)"
        ),
    )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS idx_chat_dialog_user_updated"))
    op.execute(sa.text("DROP TABLE IF EXISTS chat_dialog"))
