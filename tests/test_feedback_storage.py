"""Таблица user_feedback в user_storage (одна строка на user_id, накопление в body)."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import user_storage


def test_append_feedback_same_user_one_row() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    try:
        user_storage.init_db(path)
        user_storage.append_user_feedback(path, 42, "testuser", "  first  ")
        user_storage.append_user_feedback(path, 42, "testuser", "second")
        assert user_storage.count_user_feedback(path) == 1
        rows = user_storage.list_user_feedback(path, limit=10, offset=0)
        assert len(rows) == 1
        assert rows[0].user_id == 42
        assert rows[0].username == "testuser"
        ent = user_storage.parse_feedback_entries(rows[0].body)
        assert len(ent) == 2
        assert ent[0].text == "first"
        assert ent[1].text == "second"
        one = user_storage.get_user_feedback_by_user_id(path, 42)
        assert one is not None
        assert len(user_storage.parse_feedback_entries(one.body)) == 2
        tickets = user_storage.list_feedback_tickets_for_user(path, 42)
        assert len(tickets) == 2
        assert tickets[0].body == "second"
        assert tickets[0].status == user_storage.FEEDBACK_TICKET_STATUS_PENDING
        assert tickets[1].body == "first"
    finally:
        os.unlink(path)


def test_migrate_legacy_multiple_rows_per_user() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    try:
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE user_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
        )
        conn.execute(
            "INSERT INTO user_feedback (user_id, username, body, created_at) VALUES (1, 'u', 'a', '2020-01-01T00:00:00+00:00')",
        )
        conn.execute(
            "INSERT INTO user_feedback (user_id, username, body, created_at) VALUES (1, 'u', 'b', '2020-01-02T00:00:00+00:00')",
        )
        conn.commit()
        conn.close()
        user_storage.init_db(path)
        rows = user_storage.list_user_feedback(path, limit=10, offset=0)
        assert len(rows) == 1
        ent = user_storage.parse_feedback_entries(rows[0].body)
        assert len(ent) == 2
        assert {e.text for e in ent} == {"a", "b"}
    finally:
        os.unlink(path)
