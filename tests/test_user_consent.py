"""Таблица user_consent и вспомогательные проверки."""

from __future__ import annotations

import os
import tempfile

import user_storage


def test_consent_quiz_then_accept() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    try:
        user_storage.init_db(path)
        assert user_storage.get_user_consent(path, 7) is None
        user_storage.upsert_disclaimer_quiz_passed(path, 7, version=2)
        row = user_storage.get_user_consent(path, 7)
        assert row is not None
        assert row.disclaimer_version == 2
        assert row.quiz_passed_at
        assert not row.accepted_at
        assert user_storage.consent_fully_accepted(row, 2) is False
        assert user_storage.consent_awaiting_final_accept(row, 2) is True
        assert user_storage.consent_fully_accepted(row, 3) is False

        user_storage.set_disclaimer_accepted(path, 7, 2)
        row2 = user_storage.get_user_consent(path, 7)
        assert row2 is not None
        assert row2.accepted_at
        assert user_storage.consent_fully_accepted(row2, 2) is True
        assert user_storage.consent_awaiting_final_accept(row2, 2) is False
    finally:
        os.unlink(path)


def test_consent_large_user_id_sqlite() -> None:
    """Регрессия: Telegram user_id может быть больше INT32_MAX; SQLite хранит BIGINT."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    try:
        user_storage.init_db(path)
        uid = 5_241_098_336
        user_storage.upsert_disclaimer_quiz_passed(path, uid, version=1)
        row = user_storage.get_user_consent(path, uid)
        assert row is not None
        assert row.user_id == uid
        assert row.disclaimer_version == 1
    finally:
        os.unlink(path)


def test_version_bump_requires_reaccept() -> None:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    try:
        user_storage.init_db(path)
        user_storage.upsert_disclaimer_quiz_passed(path, 1, 1)
        user_storage.set_disclaimer_accepted(path, 1, 1)
        row = user_storage.get_user_consent(path, 1)
        assert user_storage.consent_fully_accepted(row, 1) is True
        assert user_storage.consent_fully_accepted(row, 2) is False
    finally:
        os.unlink(path)
