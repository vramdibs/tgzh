"""chat_memory_pref: выбранная модель Cursor для /chat."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014_chat_model_slug"
down_revision: Union[str, None] = "013_user_profile_multi_subject"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            ALTER TABLE chat_memory_pref
            ADD COLUMN IF NOT EXISTS chat_model_slug TEXT
            """
        ),
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            ALTER TABLE chat_memory_pref
            DROP COLUMN IF EXISTS chat_model_slug
            """
        ),
    )
