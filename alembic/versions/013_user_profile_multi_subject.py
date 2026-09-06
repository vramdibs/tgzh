"""user_profile: несколько предметов на пользователя; user_settings: активный предмет."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "013_user_profile_multi_subject"
down_revision: Union[str, None] = "012_admin_session"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(
        sa.text(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'user_settings'
            """
        )
    ).fetchone()
    if not row:
        op.execute(
            sa.text(
                """
                CREATE TABLE user_settings (
                    user_id BIGINT PRIMARY KEY NOT NULL,
                    active_subject_slug TEXT NOT NULL DEFAULT 'matematika',
                    updated_at TEXT NOT NULL
                )
                """
            )
        )

    pk = conn.execute(
        sa.text(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'user_profile'::regclass AND i.indisprimary
            """
        )
    ).fetchall()
    pk_cols = {r[0] for r in pk}
    if pk_cols != {"user_id", "subject_slug"}:
        op.execute(
            sa.text(
                """
                CREATE TABLE user_profile__new (
                    user_id BIGINT NOT NULL,
                    subject_slug TEXT NOT NULL DEFAULT 'matematika',
                    grade INTEGER NOT NULL CHECK (grade >= 6 AND grade <= 11),
                    textbook_slug TEXT NOT NULL,
                    textbook_url TEXT NOT NULL,
                    textbook_label TEXT NOT NULL,
                    is_premium INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    hw_paragraph TEXT,
                    hw_exercise TEXT,
                    hw_page INTEGER,
                    PRIMARY KEY (user_id, subject_slug)
                )
                """
            )
        )
        op.execute(
            sa.text(
                """
                INSERT INTO user_profile__new (
                    user_id, subject_slug, grade, textbook_slug, textbook_url, textbook_label,
                    is_premium, updated_at, hw_paragraph, hw_exercise, hw_page
                )
                SELECT user_id,
                       COALESCE(NULLIF(TRIM(subject_slug), ''), 'matematika'),
                       grade, textbook_slug, textbook_url, textbook_label,
                       is_premium, updated_at, hw_paragraph, hw_exercise, hw_page
                FROM user_profile
                """
            )
        )
        op.execute(sa.text("DROP TABLE user_profile"))
        op.execute(sa.text("ALTER TABLE user_profile__new RENAME TO user_profile"))

    op.execute(
        sa.text(
            """
            INSERT INTO user_settings (user_id, active_subject_slug, updated_at)
            SELECT user_id,
                   COALESCE(NULLIF(TRIM(subject_slug), ''), 'matematika'),
                   updated_at
            FROM user_profile
            ON CONFLICT (user_id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade 013_user_profile_multi_subject not supported")
