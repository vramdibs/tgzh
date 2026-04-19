"""Таблица user_poll."""

from __future__ import annotations

import os
import tempfile

import user_storage


def test_upsert_user_poll() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    try:
        user_storage.init_db(path)
        user_storage.upsert_user_poll(path, 100, 1, "  инженер  ")
        row = user_storage.get_user_poll(path, 100)
        assert row is not None
        assert row.likes_math == 1
        assert row.career_text == "инженер"
        user_storage.upsert_user_poll(path, 100, 0, "учитель")
        row2 = user_storage.get_user_poll(path, 100)
        assert row2.likes_math == 0
        assert row2.career_text == "учитель"
        assert user_storage.count_user_poll_rows(path) == 1
        y, n = user_storage.poll_math_yes_no_counts(path)
        assert y == 0 and n == 1
    finally:
        os.unlink(path)
