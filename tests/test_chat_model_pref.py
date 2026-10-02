"""chat_model_slug в user_settings / chat_memory_pref."""

from __future__ import annotations

import tempfile
from pathlib import Path

import ai_checker
import user_storage


def test_chat_model_get_set_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "users.sqlite")
        user_storage.init_db(path)
        uid = 4242
        assert user_storage.chat_model_get(path, uid) == ai_checker.chat_cursor_model_default()
        saved = user_storage.chat_model_set(path, uid, "cursor-grok-4.6-low")
        assert saved == "cursor-grok-4.6-low"
        assert user_storage.chat_model_get(path, uid) == "cursor-grok-4.6-low"
        # Недопустимый slug → дефолт из каталога.
        saved2 = user_storage.chat_model_set(path, uid, "composer-2.5-fast")
        assert saved2 == ai_checker.chat_cursor_model_default()
        assert user_storage.chat_model_get(path, uid) == ai_checker.chat_cursor_model_default()
        saved3 = user_storage.chat_model_set(path, uid, "cursor-grok-4.8-low")
        assert saved3 == "grok-4.8-low"
        saved4 = user_storage.chat_model_set(path, uid, "cursor-grok-4.7-low")
        assert saved4 == "grok-4.7-low"
        assert user_storage.chat_model_get(path, uid) == "grok-4.7-low"
        saved5 = user_storage.chat_model_set(path, uid, "grok-5-low")
        assert saved5 == "grok-5-low"
