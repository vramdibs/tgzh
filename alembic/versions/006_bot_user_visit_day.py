"""Таблица bot_user_visit_day (посещаемость по дням для /stats).

Revision ID: 006_bot_user_visit_day
Revises: 005_feedback_ticket_archived
Create Date: 2026-04-11

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006_bot_user_visit_day"
down_revision: Union[str, None] = "005_feedback_ticket_archived"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS bot_user_visit_day (
                user_id BIGINT NOT NULL,
                visit_day TEXT NOT NULL,
                PRIMARY KEY (user_id, visit_day)
            )
            """
        ),
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS bot_user_visit_day"))
