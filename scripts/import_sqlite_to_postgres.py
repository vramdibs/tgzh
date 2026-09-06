#!/usr/bin/env python3
"""
Разовый импорт данных из SQLite (USER_DB_PATH) в PostgreSQL (DATABASE_URL).
Порядок таблиц без FK; при конфликте по PK строки перезаписываются (ON CONFLICT DO UPDATE).

Пример:
  DATABASE_URL=postgresql://tgzh:secret@localhost:5432/tgzh \\
  python3 scripts/import_sqlite_to_postgres.py data/users.sqlite
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: import_sqlite_to_postgres.py PATH_TO_SQLITE", file=sys.stderr)
        return 2
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    sqlite_path = Path(sys.argv[1])
    if not sqlite_path.is_file():
        print(f"SQLite file not found: {sqlite_path}", file=sys.stderr)
        return 2

    import psycopg

    lite = sqlite3.connect(str(sqlite_path))
    lite.row_factory = sqlite3.Row
    pg = psycopg.connect(url)
    try:
        _copy_table(
            lite,
            pg,
            "user_profile",
            [
                "user_id",
                "grade",
                "textbook_slug",
                "textbook_url",
                "textbook_label",
                "is_premium",
                "updated_at",
                "hw_paragraph",
                "hw_exercise",
                "hw_page",
                "subject_slug",
            ],
        )
        _copy_table(
            lite,
            pg,
            "user_settings",
            ["user_id", "active_subject_slug", "updated_at"],
        )
        _copy_table(
            lite,
            pg,
            "user_feedback",
            ["user_id", "username", "body", "created_at", "updated_at"],
        )
        _copy_table(
            lite,
            pg,
            "feedback_ticket",
            [
                "id",
                "user_id",
                "username",
                "body",
                "status",
                "staff_response",
                "created_at",
                "status_updated_at",
                "archived",
            ],
        )
        _copy_table(
            lite,
            pg,
            "feedback_ticket_nlp",
            [
                "ticket_id",
                "sentiment_label",
                "sentiment_score",
                "emotions_json",
                "badges",
                "error",
                "analyzed_at",
            ],
        )
        _copy_table(
            lite,
            pg,
            "user_consent",
            ["user_id", "disclaimer_version", "quiz_passed_at", "accepted_at"],
        )
        _copy_table(
            lite,
            pg,
            "user_poll",
            ["user_id", "likes_math", "career_text", "updated_at"],
        )
        _copy_table(
            lite,
            pg,
            "bot_stats_by_year",
            [
                "academic_year",
                "photos_uploaded",
                "checks_completed",
                "checks_technical_failed",
                "verdict_correct",
                "verdict_partial",
                "verdict_absent",
                "verdict_percent_sum",
            ],
        )
        _copy_table(
            lite,
            pg,
            "bot_user_visit_day",
            ["user_id", "visit_day"],
        )
        _copy_check_result_vote(lite, pg)
        _copy_table(
            lite,
            pg,
            "user_block",
            ["user_id", "block_type", "reason", "until_utc", "created_at"],
        )
        _copy_table(
            lite,
            pg,
            "check_sticker_reward",
            ["id", "user_id", "kind", "sticker_file_id", "sticker_set_name", "quip", "created_at"],
        )
        # После UPSERT сиквенсы SERIAL/IDENTITY в Postgres не двигаются: следующая
        # auto-вставка может уйти в дубликат PK. Ставим setval на MAX(id) каждой таблицы с сиквенсом.
        _resync_sequences(pg, ["feedback_ticket", "check_sticker_reward"])
        pg.commit()
    finally:
        lite.close()
        pg.close()
    print("import ok")
    return 0


def _resync_sequences(pg: Any, tables: list[str]) -> None:
    with pg.cursor() as c:
        for table in tables:
            c.execute(
                "SELECT pg_get_serial_sequence(%s, %s)",
                (table, "id"),
            )
            row = c.fetchone()
            seq = row[0] if row else None
            if not seq:
                print(f"{table}: no serial sequence, skip setval")
                continue
            c.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}")
            mx_row = c.fetchone()
            mx = int(mx_row[0]) if mx_row else 0
            if mx <= 0:
                print(f"{table}: empty, skip setval")
                continue
            c.execute("SELECT setval(%s, %s, true)", (seq, mx))
            print(f"{table}: setval({seq}, {mx})")


def _copy_table(
    lite: sqlite3.Connection,
    pg: sqlite3.Connection,
    table: str,
    cols: list[str],
) -> None:
    ncols = len(cols)
    ph_pg = ", ".join(["%s"] * ncols)
    col_list = ", ".join(cols)
    rest = cols[1:]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in rest) if rest else f"{cols[0]} = EXCLUDED.{cols[0]}"
    upsert = (
        f"INSERT INTO {table} ({col_list}) VALUES ({ph_pg}) "
        f"ON CONFLICT ({cols[0]}) DO UPDATE SET {set_clause}"
    )
    try:
        cur_l = lite.execute(f"SELECT {col_list} FROM {table}")
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            print(f"{table}: skip (no table in SQLite)")
            return
        raise
    rows = cur_l.fetchall()
    if not rows:
        print(f"{table}: 0 rows")
        return
    with pg.cursor() as c:
        for row in rows:
            c.execute(upsert, tuple(row[i] for i in range(ncols)))
    print(f"{table}: {len(rows)} rows")


def _copy_check_result_vote(lite: sqlite3.Connection, pg: Any) -> None:
    """Составной PK (chat_id, message_id, user_id) - отдельный upsert от универсального _copy_table."""
    cols = ["chat_id", "message_id", "user_id", "vote", "updated_at"]
    col_list = ", ".join(cols)
    ph_pg = ", ".join(["%s"] * len(cols))
    upsert = (
        f"INSERT INTO check_result_vote ({col_list}) VALUES ({ph_pg}) "
        "ON CONFLICT (chat_id, message_id, user_id) DO UPDATE SET "
        "vote = EXCLUDED.vote, updated_at = EXCLUDED.updated_at"
    )
    try:
        cur_l = lite.execute(f"SELECT {col_list} FROM check_result_vote")
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            print("check_result_vote: skip (no table in SQLite)")
            return
        raise
    rows = cur_l.fetchall()
    if not rows:
        print("check_result_vote: 0 rows")
        return
    with pg.cursor() as c:
        for row in rows:
            c.execute(upsert, tuple(row[i] for i in range(len(cols))))
    print(f"check_result_vote: {len(rows)} rows")


if __name__ == "__main__":
    raise SystemExit(main())
