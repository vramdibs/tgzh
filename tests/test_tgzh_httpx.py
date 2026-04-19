"""IPv4-предпочитающий httpx-транспорт (общий для бота и TEI)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import tgzh_httpx


def test_async_http_transport_ipv4_lookup_returns_async_http_transport() -> None:
    t = tgzh_httpx.async_http_transport_ipv4_lookup()
    assert isinstance(t, httpx.AsyncHTTPTransport)
    assert getattr(t, "_pool", None) is not None


@pytest.mark.asyncio
async def test_ipv4_backend_resolves_hostname_via_getaddrinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = tgzh_httpx._IPv4PreferredAutoBackend()
    inner = MagicMock()
    inner.connect_tcp = AsyncMock(return_value="sock")

    async def fake_init() -> None:
        backend._backend = inner

    monkeypatch.setattr(backend, "_init_backend", fake_init)
    monkeypatch.setattr(
        tgzh_httpx.socket,
        "getaddrinfo",
        lambda *a, **k: [(0, 0, 0, 0, ("198.51.100.2", 8080))],
    )
    out = await backend.connect_tcp("tei.example.test", 8080)
    assert out == "sock"
    inner.connect_tcp.assert_awaited_once()
    assert inner.connect_tcp.call_args[0][0] == "198.51.100.2"
    assert inner.connect_tcp.call_args[0][1] == 8080


@pytest.mark.asyncio
async def test_ipv4_backend_skips_getaddrinfo_for_literal_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = tgzh_httpx._IPv4PreferredAutoBackend()
    inner = MagicMock()
    inner.connect_tcp = AsyncMock(return_value="sock")

    async def fake_init() -> None:
        backend._backend = inner

    monkeypatch.setattr(backend, "_init_backend", fake_init)
    spy = MagicMock()
    monkeypatch.setattr(tgzh_httpx.socket, "getaddrinfo", spy)
    out = await backend.connect_tcp("192.0.2.1", 443)
    assert out == "sock"
    spy.assert_not_called()
    inner.connect_tcp.assert_awaited_once()
    assert inner.connect_tcp.call_args[0][0] == "192.0.2.1"
    assert inner.connect_tcp.call_args[0][1] == 443
