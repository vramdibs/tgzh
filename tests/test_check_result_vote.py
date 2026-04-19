"""Голосование 👍/👎 под сообщением с результатом проверки."""

from __future__ import annotations

import tempfile
from pathlib import Path

import user_storage


def test_upsert_and_totals() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "u.sqlite")
        user_storage.init_db(path)
        assert user_storage.check_result_vote_totals(path) == (0, 0)
        assert (
            user_storage.upsert_check_result_vote(
                path,
                chat_id=-100,
                message_id=42,
                user_id=7,
                vote=1,
            )
            is True
        )
        assert user_storage.check_result_vote_totals(path) == (1, 0)
        assert (
            user_storage.upsert_check_result_vote(
                path,
                chat_id=-100,
                message_id=42,
                user_id=7,
                vote=1,
            )
            is False
        )
        assert (
            user_storage.upsert_check_result_vote(
                path,
                chat_id=-100,
                message_id=42,
                user_id=7,
                vote=-1,
            )
            is True
        )
        assert user_storage.check_result_vote_totals(path) == (0, 1)
