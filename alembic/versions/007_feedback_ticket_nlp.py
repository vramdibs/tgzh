"""Таблица feedback_ticket_nlp (TEI sentiment + go_emotions для /support).

Revision ID: 007_feedback_ticket_nlp
Revises: 006_bot_user_visit_day
Create Date: 2026-04-11

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007_feedback_ticket_nlp"
down_revision: Union[str, None] = "006_bot_user_visit_day"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS feedback_ticket_nlp (
                ticket_id BIGINT PRIMARY KEY NOT NULL,
                sentiment_label TEXT NOT NULL DEFAULT '',
                sentiment_score DOUBLE PRECISION NOT NULL DEFAULT 0,
                emotions_json TEXT NOT NULL DEFAULT '[]',
                badges TEXT NOT NULL DEFAULT '',
                error TEXT,
                analyzed_at TEXT NOT NULL
            )
            """
        ),
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS feedback_ticket_nlp"))
