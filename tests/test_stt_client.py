"""Юниты STT-клиента: конфиг, выключенное состояние, успех, ошибки."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

import stt_client


# ====================== конфиг ======================


def test_is_configured_false_without_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STT_BASE_URL", raising=False)
    assert stt_client.is_configured() is False


def test_is_configured_true_with_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_BASE_URL", "https://api.example.com/v1")
    assert stt_client.is_configured() is True


def test_read_config_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_BASE_URL", "https://api.example.com/v1/")
    monkeypatch.delenv("STT_MODEL", raising=False)
    monkeypatch.delenv("STT_LANGUAGE", raising=False)
    cfg = stt_client._read_config()
    assert cfg is not None
    assert cfg.base_url == "https://api.example.com/v1"
    assert cfg.model == "whisper-1"
    assert cfg.language == "ru"
    assert cfg.timeout_s == 60.0
    assert cfg.max_audio_bytes == 25 * 1024 * 1024


def test_read_config_language_auto_means_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_BASE_URL", "https://x/v1")
    monkeypatch.setenv("STT_LANGUAGE", "auto")
    cfg = stt_client._read_config()
    assert cfg is not None
    assert cfg.language is None


def test_read_config_clamps_timeout_and_max(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_BASE_URL", "https://x/v1")
    monkeypatch.setenv("STT_TIMEOUT_S", "9999")
    monkeypatch.setenv("STT_MAX_AUDIO_BYTES", "10")
    cfg = stt_client._read_config()
    assert cfg is not None
    assert cfg.timeout_s == 600.0
    assert cfg.max_audio_bytes == 64 * 1024


def test_max_audio_bytes_default_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STT_BASE_URL", raising=False)
    assert stt_client.max_audio_bytes() == 25 * 1024 * 1024


# ====================== transcribe: предусловия ======================


def test_transcribe_disabled_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STT_BASE_URL", raising=False)
    with pytest.raises(stt_client.SttDisabledError):
        asyncio.run(stt_client.transcribe(b"abc"))


def test_transcribe_empty_audio_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_BASE_URL", "https://x/v1")
    with pytest.raises(stt_client.SttError):
        asyncio.run(stt_client.transcribe(b""))


def test_transcribe_too_large_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_BASE_URL", "https://x/v1")
    monkeypatch.setenv("STT_MAX_AUDIO_BYTES", str(64 * 1024))
    big = b"\x00" * (64 * 1024 + 1)
    with pytest.raises(stt_client.SttError, match="лимит"):
        asyncio.run(stt_client.transcribe(big))


# ====================== transcribe: HTTP ======================


def _patched_async_client(monkeypatch: pytest.MonkeyPatch, handler) -> list[dict[str, Any]]:
    """Подменяет `httpx.AsyncClient` фейком, ловящим параметры POST."""

    captured: list[dict[str, Any]] = []

    class _FakeClient:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            captured.append({"url": url, **kw})
            return await handler(url, **kw)

    monkeypatch.setattr(stt_client.httpx, "AsyncClient", _FakeClient)
    return captured


def test_transcribe_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("STT_API_KEY", "sk-test")
    monkeypatch.setenv("STT_MODEL", "whisper-test")
    monkeypatch.setenv("STT_LANGUAGE", "ru")

    async def _handler(url: str, **kw: Any) -> httpx.Response:
        assert url == "https://api.example.com/v1/audio/transcriptions"
        assert kw["headers"]["Authorization"] == "Bearer sk-test"
        assert kw["data"]["model"] == "whisper-test"
        assert kw["data"]["language"] == "ru"
        # Файл должен быть в multipart-полях.
        assert "file" in kw["files"]
        return httpx.Response(200, json={"text": "  привет, бот  "})

    _patched_async_client(monkeypatch, _handler)
    text = asyncio.run(
        stt_client.transcribe(b"OGGdata", mime="audio/ogg", filename="voice.ogg"),
    )
    assert text == "привет, бот"


def test_transcribe_no_language_when_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_BASE_URL", "https://x/v1")
    monkeypatch.setenv("STT_LANGUAGE", "auto")

    async def _handler(url: str, **kw: Any) -> httpx.Response:
        assert "language" not in kw["data"]
        return httpx.Response(200, json={"text": "ok"})

    _patched_async_client(monkeypatch, _handler)
    asyncio.run(stt_client.transcribe(b"audio"))


def test_transcribe_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_BASE_URL", "https://x/v1")

    async def _handler(url: str, **kw: Any) -> httpx.Response:
        return httpx.Response(503, text="Service unavailable")

    _patched_async_client(monkeypatch, _handler)
    with pytest.raises(stt_client.SttError, match="503"):
        asyncio.run(stt_client.transcribe(b"audio"))


def test_transcribe_empty_text_in_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_BASE_URL", "https://x/v1")

    async def _handler(url: str, **kw: Any) -> httpx.Response:
        return httpx.Response(200, json={"text": "   "})

    _patched_async_client(monkeypatch, _handler)
    with pytest.raises(stt_client.SttError, match="не вернул текст"):
        asyncio.run(stt_client.transcribe(b"audio"))


def test_transcribe_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_BASE_URL", "https://x/v1")

    async def _handler(url: str, **kw: Any) -> httpx.Response:
        return httpx.Response(200, text="not json")

    _patched_async_client(monkeypatch, _handler)
    with pytest.raises(stt_client.SttError, match="не JSON"):
        asyncio.run(stt_client.transcribe(b"audio"))


def test_transcribe_timeout_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_BASE_URL", "https://x/v1")

    async def _handler(url: str, **kw: Any) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    _patched_async_client(monkeypatch, _handler)
    with pytest.raises(stt_client.SttError, match="много времени"):
        asyncio.run(stt_client.transcribe(b"audio"))


def test_transcribe_no_auth_when_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STT_BASE_URL", "https://x/v1")
    monkeypatch.delenv("STT_API_KEY", raising=False)

    async def _handler(url: str, **kw: Any) -> httpx.Response:
        assert "Authorization" not in kw["headers"]
        return httpx.Response(200, json={"text": "ok"})

    _patched_async_client(monkeypatch, _handler)
    out = asyncio.run(stt_client.transcribe(b"audio"))
    assert out == "ok"


# ====================== вспомогательное ======================


def test_voice_filename_mapping_known_mimes() -> None:
    import bot

    assert bot._voice_filename_from_mime("audio/wav") == "voice.wav"
    assert bot._voice_filename_from_mime("audio/mpeg") == "voice.mp3"
    assert bot._voice_filename_from_mime("audio/m4a") == "voice.m4a"
    assert bot._voice_filename_from_mime("audio/flac") == "voice.flac"
    assert bot._voice_filename_from_mime("audio/webm") == "voice.webm"
    assert bot._voice_filename_from_mime("audio/ogg") == "voice.ogg"
    assert bot._voice_filename_from_mime("") == "voice.ogg"
    assert bot._voice_filename_from_mime(None) == "voice.ogg"
