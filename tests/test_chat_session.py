"""Скрытая сессия `/chat`: 7-дневный TTL, login/logout/active_until."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import user_storage
from tgzh_db import connect, execute as _e


def _make_db():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    user_storage.init_db(path)
    return path


def test_chat_session_login_active_default_ttl() -> None:
    path = _make_db()
    try:
        assert user_storage.chat_session_active_until(path, 100) is None
        before = datetime.now(timezone.utc)
        exp_iso = user_storage.chat_session_login(path, 100)
        after = datetime.now(timezone.utc)
        exp_dt = datetime.fromisoformat(exp_iso)
        assert exp_dt - before >= timedelta(days=user_storage.CHAT_SESSION_TTL_DAYS) - timedelta(seconds=2)
        assert exp_dt - after <= timedelta(days=user_storage.CHAT_SESSION_TTL_DAYS) + timedelta(seconds=2)
        until = user_storage.chat_session_active_until(path, 100)
        assert until is not None
        assert until == exp_dt
    finally:
        os.unlink(path)


def test_chat_session_relogin_extends_window() -> None:
    path = _make_db()
    try:
        first = user_storage.chat_session_login(path, 7, ttl_days=1)
        second = user_storage.chat_session_login(path, 7, ttl_days=2)
        first_dt = datetime.fromisoformat(first)
        second_dt = datetime.fromisoformat(second)
        assert second_dt > first_dt
    finally:
        os.unlink(path)


def test_chat_session_logout_removes_record() -> None:
    path = _make_db()
    try:
        user_storage.chat_session_login(path, 42)
        assert user_storage.chat_session_active_until(path, 42) is not None
        assert user_storage.chat_session_logout(path, 42) is True
        assert user_storage.chat_session_active_until(path, 42) is None
        assert user_storage.chat_session_logout(path, 42) is False
    finally:
        os.unlink(path)


def test_chat_session_expired_returns_none() -> None:
    path = _make_db()
    try:
        # вручную записываем сессию, истёкшую вчера
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        granted = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        conn = connect(path)
        try:
            _e(
                conn,
                "INSERT INTO chat_session (user_id, granted_at, expires_at) VALUES (?, ?, ?)",
                (777, granted, past),
            )
            conn.commit()
        finally:
            conn.close()
        assert user_storage.chat_session_active_until(path, 777) is None
    finally:
        os.unlink(path)


def test_chat_session_large_user_id() -> None:
    path = _make_db()
    try:
        uid = 5_241_098_336
        user_storage.chat_session_login(path, uid)
        until = user_storage.chat_session_active_until(path, uid)
        assert until is not None
    finally:
        os.unlink(path)
