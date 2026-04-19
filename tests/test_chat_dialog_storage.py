"""Хранилище сохранённых диалогов /chat: upsert/list/load/purge + лимит 10."""

from __future__ import annotations

import os
import tempfile
import time

import pytest

import user_storage


def _make_db() -> str:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    user_storage.init_db(path)
    return path


def _hist(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    """Удобный конструктор истории: пары (user_text, assistant_text)."""
    out: list[dict[str, str]] = []
    for u, a in pairs:
        if u:
            out.append({"role": "user", "content": u})
        if a:
            out.append({"role": "assistant", "content": a})
    return out


def test_upsert_creates_new_dialog_and_returns_id() -> None:
    path = _make_db()
    try:
        new_id = user_storage.chat_dialog_upsert(
            path,
            user_id=10,
            dialog_id=None,
            history=_hist(("Привет", "Здравствуй")),
        )
        assert isinstance(new_id, int)
        assert new_id > 0
        loaded = user_storage.chat_dialog_load(path, 10, new_id)
        assert loaded == [
            {"role": "user", "content": "Привет"},
            {"role": "assistant", "content": "Здравствуй"},
        ]
    finally:
        os.unlink(path)


def test_upsert_updates_existing_dialog_in_place() -> None:
    path = _make_db()
    try:
        first = user_storage.chat_dialog_upsert(
            path, 10, None, _hist(("Q1", "A1")),
        )
        assert first is not None
        time.sleep(0.01)
        second = user_storage.chat_dialog_upsert(
            path, 10, first, _hist(("Q1", "A1"), ("Q2", "A2")),
        )
        assert second == first
        loaded = user_storage.chat_dialog_load(path, 10, first)
        assert loaded is not None
        assert len(loaded) == 4
        assert loaded[-1]["content"] == "A2"
    finally:
        os.unlink(path)


def test_upsert_ignores_dialog_id_belonging_to_other_user() -> None:
    path = _make_db()
    try:
        own = user_storage.chat_dialog_upsert(path, 1, None, _hist(("hi", "hey")))
        # Чужой пользователь подсовывает свой dialog_id — должно создаться новое.
        new_id = user_storage.chat_dialog_upsert(
            path, 2, own, _hist(("foo", "bar")),
        )
        assert new_id is not None
        assert new_id != own
        # У владельца запись осталась нетронутой.
        loaded = user_storage.chat_dialog_load(path, 1, own)
        assert loaded == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey"},
        ]
    finally:
        os.unlink(path)


def test_upsert_skips_when_no_user_messages() -> None:
    path = _make_db()
    try:
        new_id = user_storage.chat_dialog_upsert(path, 7, None, [])
        assert new_id is None
        new_id2 = user_storage.chat_dialog_upsert(
            path, 7, None, [{"role": "system", "content": "init"}],
        )
        # system без user-реплик не должен создавать запись (пустой "диалог" с точки зрения чата).
        assert new_id2 is None
        assert user_storage.chat_dialog_list(path, 7) == []
    finally:
        os.unlink(path)


def test_list_orders_newest_first_and_carries_history_len() -> None:
    path = _make_db()
    try:
        ids: list[int] = []
        for i in range(3):
            new_id = user_storage.chat_dialog_upsert(
                path, 5, None, _hist((f"q{i}", f"a{i}")),
            )
            assert new_id is not None
            ids.append(new_id)
            time.sleep(0.01)
        rows = user_storage.chat_dialog_list(path, 5)
        assert [r.dialog_id for r in rows] == list(reversed(ids))
        for r in rows:
            assert r.history_len == 2
    finally:
        os.unlink(path)


def test_upsert_prunes_to_limit_keeping_newest() -> None:
    path = _make_db()
    try:
        limit = user_storage.CHAT_DIALOG_HISTORY_LIMIT
        ids: list[int] = []
        for i in range(limit + 3):
            new_id = user_storage.chat_dialog_upsert(
                path, 99, None, _hist((f"q{i}", f"a{i}")),
            )
            assert new_id is not None
            ids.append(new_id)
            time.sleep(0.01)
        rows = user_storage.chat_dialog_list(path, 99, limit=limit + 5)
        assert len(rows) == limit
        # Должны остаться `limit` новейших — это последние `limit` ids в порядке создания.
        assert sorted([r.dialog_id for r in rows]) == sorted(ids[-limit:])
    finally:
        os.unlink(path)


def test_purge_removes_only_target_user_records() -> None:
    path = _make_db()
    try:
        user_storage.chat_dialog_upsert(path, 1, None, _hist(("a", "b")))
        user_storage.chat_dialog_upsert(path, 1, None, _hist(("c", "d")))
        user_storage.chat_dialog_upsert(path, 2, None, _hist(("e", "f")))
        deleted = user_storage.chat_dialog_purge(path, 1)
        assert deleted == 2
        assert user_storage.chat_dialog_list(path, 1) == []
        # У соседнего пользователя ничего не пострадало.
        assert len(user_storage.chat_dialog_list(path, 2)) == 1
    finally:
        os.unlink(path)


def test_load_returns_none_for_missing_dialog() -> None:
    path = _make_db()
    try:
        assert user_storage.chat_dialog_load(path, 1, 999_999) is None
    finally:
        os.unlink(path)


def test_title_from_first_user_message_is_truncated() -> None:
    long_question = "Q" * 200
    title = user_storage.chat_dialog_title_from_history(
        [{"role": "user", "content": long_question}],
    )
    assert title.endswith("…")
    assert len(title) <= 60


def test_title_falls_back_when_no_user_message() -> None:
    title = user_storage.chat_dialog_title_from_history([])
    assert title == "Без названия"
    title = user_storage.chat_dialog_title_from_history(
        [{"role": "system", "content": "..."}],
    )
    assert title == "Без названия"


def test_title_collapses_whitespace_inside_first_user_message() -> None:
    title = user_storage.chat_dialog_title_from_history(
        [{"role": "user", "content": "  привет\n\n как  дела?  "}],
    )
    assert title == "привет как дела?"


@pytest.mark.parametrize("path_value", ["/tmp/__definitely_does_not_exist__.sqlite"])
def test_upsert_returns_none_when_path_unusable(path_value: str) -> None:
    # Sentinel: для SQLite-режима put-несуществующий файл — db_path_usable вернёт False.
    assert user_storage.chat_dialog_upsert(
        path_value, 1, None, [{"role": "user", "content": "hi"}],
    ) is None
    assert user_storage.chat_dialog_list(path_value, 1) == []
    assert user_storage.chat_dialog_purge(path_value, 1) == 0
