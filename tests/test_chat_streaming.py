"""Стриминговый чат через Cursor: настройки и нормализация сообщений."""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any
from unittest.mock import AsyncMock, patch

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
    overridden = ai_checker.chat_default_system_prompt()
    assert overridden.endswith("своя инструкция")
    assert ai_checker.CHAT_SAFETY_POLICY in overridden


def test_chat_safety_policy_always_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Префикс безопасности должен идти всегда, даже при пустом / переопределённом промпте."""
    monkeypatch.delenv("CHAT_SYSTEM_PROMPT", raising=False)
    default_prompt = ai_checker.chat_default_system_prompt()
    assert default_prompt.startswith(ai_checker.CHAT_SAFETY_POLICY)
    for keyword in (
        "ЗАПРЕЩЕНО",
        "shell",
        "листинг",
        "соседние файлы",
        "multimodal-контент",
    ):
        assert keyword in default_prompt
    monkeypatch.setenv("CHAT_SYSTEM_PROMPT", "  кастомный промпт  ")
    custom_prompt = ai_checker.chat_default_system_prompt()
    assert custom_prompt.startswith(ai_checker.CHAT_SAFETY_POLICY)
    assert custom_prompt.endswith("кастомный промпт")


def test_chat_safety_policy_in_normalized_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/chat/stream` должен передавать политику безопасности в system, даже если бот её не прислал."""
    monkeypatch.delenv("CHAT_SYSTEM_PROMPT", raising=False)
    req = ChatStreamRequest(
        user_id=42,
        messages=[ChatMessage(role="user", content="привет")],
        system_prompt=None,
    )
    msgs = _normalize_chat_messages(req)
    assert msgs[0]["role"] == "system"
    assert ai_checker.CHAT_SAFETY_POLICY in msgs[0]["content"]


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


def test_chat_cursor_model_catalog_filters_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "CHAT_CURSOR_MODELS",
        "composer-2.5,composer-2.5-fast,cursor-grok-4.6-low",
    )
    slugs = [slug for slug, _ in ai_checker.chat_cursor_model_catalog()]
    assert slugs == ["composer-2.5", "cursor-grok-4.6-low"]


def test_chat_cursor_model_try_chain_default_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHAT_CURSOR_MODEL_DEFAULT", raising=False)
    monkeypatch.delenv("CHAT_CURSOR_MODEL_FALLBACK", raising=False)
    assert ai_checker.chat_cursor_model_try_chain("composer-2.5") == [
        "composer-2.5",
        "cursor-grok-4.6-low",
    ]
    assert ai_checker.chat_cursor_model_try_chain("cursor-grok-4.6-low") == [
        "cursor-grok-4.6-low",
    ]


@pytest.mark.asyncio
async def test_stream_chat_model_fallback_on_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VLLM_FALLBACK_ENABLE", "1")
    monkeypatch.setenv("VLLM_FALLBACK_BASE_URL", "http://bridge:8787/v1")

    calls: list[str] = []
    from openai import APIStatusError
    from unittest.mock import MagicMock

    async def _fake_once(client, model, messages, *, on_delta):
        calls.append(model)
        if model == "composer-2.5":
            resp = MagicMock(status_code=400)
            resp.text = "model 'composer-2.5' is not in allowed"
            raise APIStatusError(message="bad model", response=resp, body=None)
        await on_delta("ok")
        return "ok"

    with (
        patch(
            "ai_checker._cursor_openai_client",
            AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "ai_checker._stream_chat_model_once",
            side_effect=_fake_once,
        ),
    ):
        out = await ai_checker.stream_chat_via_cursor(
            [{"role": "user", "content": "hi"}],
            on_delta=AsyncMock(),
            model="composer-2.5",
        )
    assert out == "ok"
    assert calls == ["composer-2.5", "cursor-grok-4.6-low"]

