"""Telegram user_id в BIGINT (IDs > 2^31-1).

Revision ID: 008_user_id_bigint
Revises: 007_feedback_ticket_nlp
Create Date: 2026-04-12

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_user_id_bigint"
down_revision: Union[str, None] = "007_feedback_ticket_nlp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # bot_user_visit_day.user_id уже BIGINT с ревизии 006
    stmts = [
        "ALTER TABLE user_profile ALTER COLUMN user_id TYPE BIGINT",
        "ALTER TABLE user_feedback ALTER COLUMN user_id TYPE BIGINT",
        "ALTER TABLE user_consent ALTER COLUMN user_id TYPE BIGINT",
        "ALTER TABLE user_poll ALTER COLUMN user_id TYPE BIGINT",
        "ALTER TABLE feedback_ticket ALTER COLUMN user_id TYPE BIGINT",
        "ALTER TABLE check_result_vote ALTER COLUMN user_id TYPE BIGINT",
        "ALTER TABLE user_block ALTER COLUMN user_id TYPE BIGINT",
        "ALTER TABLE check_sticker_reward ALTER COLUMN user_id TYPE BIGINT",
    ]
    for sql in stmts:
        op.execute(sa.text(sql))


def downgrade() -> None:
    raise NotImplementedError("downgrade user_id to INTEGER would break Telegram IDs above 2^31-1")
