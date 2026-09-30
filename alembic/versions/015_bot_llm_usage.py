"""Таблицы учета токенов/запросов LLM для /stats и Grafana.

Revision ID: 015_bot_llm_usage
Revises: 014_chat_model_slug
Create Date: 2026-09-30

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "015_bot_llm_usage"
down_revision: Union[str, None] = "014_chat_model_slug"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS bot_llm_usage_by_year (
                academic_year INTEGER NOT NULL PRIMARY KEY,
                requests INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0
            )
            """
        ),
    )
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS bot_llm_user_year (
                user_id BIGINT NOT NULL,
                academic_year INTEGER NOT NULL,
                PRIMARY KEY (user_id, academic_year)
            )
            """
        ),
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS bot_llm_user_year"))
    op.execute(sa.text("DROP TABLE IF EXISTS bot_llm_usage_by_year"))
