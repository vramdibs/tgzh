"""user_storage.chat_memory_pref: дефолты, тоггл, счётчик, фиксация сна."""

from __future__ import annotations

import os
import tempfile

import user_storage


def _make_db():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    user_storage.init_db(path)
    return path


def test_default_pref_when_no_row() -> None:
    path = _make_db()
    try:
        pref = user_storage.chat_memory_get_pref(path, 100)
        assert pref.user_id == 100
        assert pref.enabled is True
        assert pref.msgs_since_sleep == 0
        assert pref.last_sleep_at is None
        assert pref.last_sleep_status is None
    finally:
        os.unlink(path)


def test_set_enabled_persists_value() -> None:
    path = _make_db()
    try:
        user_storage.chat_memory_set_enabled(path, 7, False)
        pref = user_storage.chat_memory_get_pref(path, 7)
        assert pref.enabled is False
        user_storage.chat_memory_set_enabled(path, 7, True)
        assert user_storage.chat_memory_get_pref(path, 7).enabled is True
    finally:
        os.unlink(path)


def test_increment_msgs_returns_new_value_and_persists() -> None:
    path = _make_db()
    try:
        assert user_storage.chat_memory_increment_msgs(path, 9) == 1
        assert user_storage.chat_memory_increment_msgs(path, 9) == 2
        pref = user_storage.chat_memory_get_pref(path, 9)
        assert pref.msgs_since_sleep == 2
        assert pref.enabled is True
    finally:
        os.unlink(path)


def test_increment_independent_per_user() -> None:
    path = _make_db()
    try:
        user_storage.chat_memory_increment_msgs(path, 1)
        user_storage.chat_memory_increment_msgs(path, 1)
        user_storage.chat_memory_increment_msgs(path, 2)
        assert user_storage.chat_memory_get_pref(path, 1).msgs_since_sleep == 2
        assert user_storage.chat_memory_get_pref(path, 2).msgs_since_sleep == 1
    finally:
        os.unlink(path)


def test_record_sleep_resets_counter_and_writes_metadata() -> None:
    path = _make_db()
    try:
        user_storage.chat_memory_increment_msgs(path, 5)
        user_storage.chat_memory_increment_msgs(path, 5)
        user_storage.chat_memory_record_sleep(path, 5, status="ok: written=2 deleted=0")
        pref = user_storage.chat_memory_get_pref(path, 5)
        assert pref.msgs_since_sleep == 0
        assert pref.last_sleep_at is not None
        assert pref.last_sleep_status is not None
        assert "ok" in pref.last_sleep_status
    finally:
        os.unlink(path)


def test_reset_msgs_zeroes_counter_only() -> None:
    path = _make_db()
    try:
        user_storage.chat_memory_set_enabled(path, 3, True)
        user_storage.chat_memory_increment_msgs(path, 3)
        user_storage.chat_memory_record_sleep(path, 3, status="ok")
        user_storage.chat_memory_increment_msgs(path, 3)
        user_storage.chat_memory_reset_msgs(path, 3)
        pref = user_storage.chat_memory_get_pref(path, 3)
        assert pref.msgs_since_sleep == 0
        assert pref.last_sleep_status == "ok"
    finally:
        os.unlink(path)


def test_set_enabled_does_not_clobber_counter_or_sleep() -> None:
    path = _make_db()
    try:
        user_storage.chat_memory_increment_msgs(path, 11)
        user_storage.chat_memory_record_sleep(path, 11, status="ok: x")
        user_storage.chat_memory_increment_msgs(path, 11)
        pref0 = user_storage.chat_memory_get_pref(path, 11)
        assert pref0.msgs_since_sleep == 1
        user_storage.chat_memory_set_enabled(path, 11, False)
        pref1 = user_storage.chat_memory_get_pref(path, 11)
        assert pref1.enabled is False
        assert pref1.msgs_since_sleep == 1
        assert pref1.last_sleep_status == "ok: x"
    finally:
        os.unlink(path)
