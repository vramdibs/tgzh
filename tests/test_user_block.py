"""Блокировки пользователей user_block в user_storage."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import user_storage


def test_temp_block_and_clear() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    try:
        user_storage.init_db(path)
        until = user_storage.set_temp_user_block(path, 7, "спам", hours=1.0)
        assert until
        st = user_storage.blocked_state(path, 7)
        assert st is not None
        assert st["kind"] == user_storage.USER_BLOCK_TEMP
        assert st["reason"] == "спам"
        user_storage.clear_user_block(path, 7)
        assert user_storage.blocked_state(path, 7) is None
    finally:
        os.unlink(path)


def test_permanent_block() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    try:
        user_storage.init_db(path)
        user_storage.set_permanent_user_block(path, 9)
        st = user_storage.blocked_state(path, 9)
        assert st is not None
        assert st["kind"] == user_storage.USER_BLOCK_PERMANENT
        assert st["reason"] is None
        user_storage.clear_user_block(path, 9)
        assert user_storage.blocked_state(path, 9) is None
    finally:
        os.unlink(path)


def test_expired_temp_block_cleared_lazily() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    try:
        user_storage.init_db(path)
        user_storage.set_temp_user_block(path, 3, "x", hours=1.0)
        conn = sqlite3.connect(path)
        conn.execute(
            "UPDATE user_block SET until_utc = ? WHERE user_id = ?",
            ("2000-01-01T00:00:00+00:00", 3),
        )
        conn.commit()
        conn.close()
        assert user_storage.blocked_state(path, 3) is None
    finally:
        os.unlink(path)
