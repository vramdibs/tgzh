"""Таблица check_sticker_reward в user_storage."""

from __future__ import annotations

import os
import tempfile

import user_storage


def test_record_check_sticker_reward_requires_payload() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    try:
        user_storage.init_db(path)
        user_storage.record_check_sticker_reward(
            path,
            1,
            user_storage.CHECK_STICKER_KIND_REWARD,
            "fid",
            "Set",
            None,
        )
        conn = __import__("sqlite3").connect(path)
        n = conn.execute("SELECT COUNT(*) FROM check_sticker_reward").fetchone()[0]
        conn.close()
        assert n == 1
        user_storage.record_check_sticker_reward(
            path,
            1,
            user_storage.CHECK_STICKER_KIND_MOTIVATION,
            None,
            None,
            "Только текст",
        )
        conn = __import__("sqlite3").connect(path)
        n2 = conn.execute("SELECT COUNT(*) FROM check_sticker_reward").fetchone()[0]
        conn.close()
        assert n2 == 2
    finally:
        os.unlink(path)
