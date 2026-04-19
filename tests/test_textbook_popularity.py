"""Метки популярности учебников (🔥) и счетчики из SQLite."""

from __future__ import annotations

import tempfile
from pathlib import Path

import bot as bot_module
import user_storage


def test_textbook_popularity_leader_slug() -> None:
    items = [{"slug": "a"}, {"slug": "b"}, {"slug": "c"}]
    assert bot_module._textbook_popularity_leader_slug(items, {"a": 2, "b": 5, "c": 1}) == "b"
    assert bot_module._textbook_popularity_leader_slug(items, {"a": 3, "b": 3, "c": 0}) == "a"
    assert bot_module._textbook_popularity_leader_slug(items, {}) is None
    assert bot_module._textbook_popularity_leader_slug(items, {"a": 0, "b": 0}) is None


def test_textbook_popularity_by_grade() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "u.sqlite")
        user_storage.init_db(path)
        user_storage.set_textbook(path, 1, 6, "book-a", "http://a", "A", False)
        user_storage.set_textbook(path, 2, 6, "book-a", "http://a", "A", False)
        user_storage.set_textbook(path, 3, 6, "book-b", "http://b", "B", False)
        pop = user_storage.textbook_popularity_by_grade(path, 6)
        assert pop.get("book-a") == 2
        assert pop.get("book-b") == 1


def test_truncate_inline_button_text() -> None:
    s = "x" * 100
    out = bot_module._truncate_inline_button_text(s, max_len=64)
    assert len(out) == 64
    assert out.endswith("…")
