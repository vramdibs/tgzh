"""Стриминговый чат через Cursor: настройки и нормализация сообщений."""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

import httpx
import pytest

import ai_checker
import server as _server_mod
from server import (
    ChatMessage,
    ChatMessageContentPart,
    ChatStreamRequest,
    _normalize_chat_messages,
)


def test_chat_default_system_prompt_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHAT_SYSTEM_PROMPT", raising=False)
    default = ai_checker.chat_default_system_prompt()
    assert "ассистент" in default.lower()
    monkeypatch.setenv("CHAT_SYSTEM_PROMPT", "  своя инструкция  ")
    assert ai_checker.chat_default_system_prompt() == "своя инструкция"


def test_chat_history_turns_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_MAX_HISTORY_TURNS", "200")
    assert ai_checker.chat_max_history_turns() == 64
    monkeypatch.setenv("CHAT_MAX_HISTORY_TURNS", "1")
    assert ai_checker.chat_max_history_turns() == 2


def test_chat_temperature_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_TEMPERATURE", "5")
    assert ai_checker.chat_stream_temperature() == 2.0
    monkeypatch.setenv("CHAT_TEMPERATURE", "garbage")
    assert ai_checker.chat_stream_temperature() == 0.7


def test_normalize_chat_messages_keeps_last_turns_with_system() -> None:
    req = ChatStreamRequest(
        user_id=1,
        messages=[
            ChatMessage(role="user", content="первый вопрос"),
            ChatMessage(role="assistant", content="первый ответ"),
            ChatMessage(role="user", content="второй вопрос"),
        ],
    )
    out = _normalize_chat_messages(req)
    assert out[0]["role"] == "system"
    assert out[-1]["role"] == "user"
    assert out[-1]["content"] == "второй вопрос"


def test_normalize_chat_messages_rejects_non_user_last() -> None:
    req = ChatStreamRequest(
        user_id=1,
        messages=[
            ChatMessage(role="user", content="вопрос"),
            ChatMessage(role="assistant", content="ответ"),
        ],
    )
    with pytest.raises(Exception):
        _normalize_chat_messages(req)


def test_normalize_chat_messages_rejects_empty() -> None:
    req = ChatStreamRequest(user_id=1, messages=[])
    with pytest.raises(Exception):
        _normalize_chat_messages(req)


def test_normalize_chat_messages_drops_extra_system_role() -> None:
    req = ChatStreamRequest(
        user_id=1,
        system_prompt="моя система",
        messages=[
            ChatMessage(role="system", content="игнорируется"),
            ChatMessage(role="user", content="привет"),
        ],
    )
    out = _normalize_chat_messages(req)
    assert out[0] == {"role": "system", "content": "моя система"}
    assert out[-1] == {"role": "user", "content": "привет"}


def test_normalize_chat_messages_accepts_multimodal_image_url() -> None:
    """Последняя user-реплика — list-of-parts с image_url(data:...). Должно проходить."""
    img_url = "data:image/jpeg;base64,QUJD"  # base64("ABC")
    req = ChatStreamRequest(
        user_id=42,
        messages=[
            ChatMessage(
                role="user",
                content=[
                    ChatMessageContentPart(type="text", text="что на фото?"),
                    ChatMessageContentPart(
                        type="image_url",
                        image_url={"url": img_url},
                    ),
                ],
            ),
        ],
    )
    out = _normalize_chat_messages(req)
    last = out[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    types = [p["type"] for p in last["content"]]
    assert types == ["text", "image_url"]
    assert last["content"][1]["image_url"]["url"] == img_url


def test_normalize_chat_messages_rejects_oversized_image_url() -> None:
    huge = "data:image/jpeg;base64," + ("A" * (_server_mod._CHAT_IMAGE_DATA_URL_MAX_LEN + 1))
    req = ChatStreamRequest(
        user_id=1,
        messages=[
            ChatMessage(
                role="user",
                content=[
                    ChatMessageContentPart(type="text", text="?"),
                    ChatMessageContentPart(
                        type="image_url",
                        image_url={"url": huge},
                    ),
                ],
            ),
        ],
    )
    with pytest.raises(Exception):
        _normalize_chat_messages(req)


def test_normalize_chat_messages_rejects_non_data_or_http_image_url() -> None:
    req = ChatStreamRequest(
        user_id=1,
        messages=[
            ChatMessage(
                role="user",
                content=[
                    ChatMessageContentPart(
                        type="image_url",
                        image_url={"url": "ftp://example.com/x.jpg"},
                    ),
                ],
            ),
        ],
    )
    with pytest.raises(Exception):
        _normalize_chat_messages(req)


def test_normalize_chat_messages_rejects_too_many_images() -> None:
    too_many = _server_mod._CHAT_MAX_IMAGES_PER_MESSAGE + 1
    req = ChatStreamRequest(
        user_id=1,
        messages=[
            ChatMessage(
                role="user",
                content=(
                    [ChatMessageContentPart(type="text", text="?")]
                    + [
                        ChatMessageContentPart(
                            type="image_url",
                            image_url={"url": f"data:image/jpeg;base64,A{i}"},
                        )
                        for i in range(too_many)
                    ]
                ),
            ),
        ],
    )
    with pytest.raises(Exception):
        _normalize_chat_messages(req)


def test_normalize_chat_messages_rejects_unknown_content_part_type() -> None:
    req = ChatStreamRequest(
        user_id=1,
        messages=[
            ChatMessage(
                role="user",
                content=[
                    ChatMessageContentPart(type="audio_url", text=None),
                ],
            ),
        ],
    )
    with pytest.raises(Exception):
        _normalize_chat_messages(req)


def test_normalize_chat_messages_text_length_limit_only_counts_text_parts() -> None:
    """8000-символьный лимит должен считаться по тексту; image_url не учитывается."""
    big_image_url = "data:image/jpeg;base64," + ("A" * 100_000)  # большая, но в пределах image-лимита
    req = ChatStreamRequest(
        user_id=1,
        messages=[
            ChatMessage(
                role="user",
                content=[
                    ChatMessageContentPart(type="text", text="ок"),
                    ChatMessageContentPart(
                        type="image_url",
                        image_url={"url": big_image_url},
                    ),
                ],
            ),
        ],
    )
    # Не должно быть exception: image_url не попадает в подсчёт текстовых символов.
    out = _normalize_chat_messages(req)
    assert out[-1]["content"][1]["image_url"]["url"] == big_image_url


def test_stream_chat_via_cursor_requires_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLLM_FALLBACK_ENABLE", raising=False)
    monkeypatch.delenv("VLLM_FALLBACK_BASE_URL", raising=False)

    async def _runner() -> None:
        await ai_checker.stream_chat_via_cursor(
            [{"role": "user", "content": "hi"}],
            on_delta=lambda piece: None,  # type: ignore[arg-type]
        )

    with pytest.raises(RuntimeError):
        asyncio.run(_runner())
