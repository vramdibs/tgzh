"""Колонка feedback_ticket.archived (архив для админа, скрыто от пользователя).

Revision ID: 005_feedback_ticket_archived
Revises: 004_check_sticker_reward
Create Date: 2026-04-11

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005_feedback_ticket_archived"
down_revision: Union[str, None] = "004_check_sticker_reward"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Идемпотентно: свежая установка по scripts/postgres_schema.sql уже содержит archived
    op.execute(
        sa.text(
            "ALTER TABLE feedback_ticket ADD COLUMN IF NOT EXISTS archived INTEGER NOT NULL DEFAULT 0"
        ),
    )
    op.execute(
        sa.text(
            "UPDATE feedback_ticket SET archived = 1 WHERE status IN ('reviewed', 'rejected')"
        ),
    )
    op.alter_column("feedback_ticket", "archived", server_default=None)


def downgrade() -> None:
    op.drop_column("feedback_ticket", "archived")
