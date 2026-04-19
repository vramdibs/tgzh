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


# --- Мини-DNS-клиент --------------------------------------------------------

import struct  # noqa: E402


def _make_dns_response(txid: int, qname: str, ip: str, ttl: int = 60) -> bytes:
    """Собрать корректный DNS A-ответ для unit-тестов."""
    # Header: ID, flags=0x8180 (response, RA, no error), QD=1, AN=1, NS=0, AR=0
    header = struct.pack(">HHHHHH", txid & 0xFFFF, 0x8180, 1, 1, 0, 0)
    qname_bytes = tgzh_httpx._encode_qname(qname)
    question = qname_bytes + struct.pack(">HH", 1, 1)  # A, IN
    # Answer: name compressed pointer 0xC00C (offset 12 == start of question)
    rdata = bytes(int(p) for p in ip.split("."))
    answer = (
        b"\xc0\x0c"
        + struct.pack(">HHIH", 1, 1, ttl, 4)
        + rdata
    )
    return header + question + answer


def test_encode_qname_basic() -> None:
    out = tgzh_httpx._encode_qname("br.example.com")
    assert out == b"\x02br\x07example\x03com\x00"


def test_encode_qname_rejects_long_label() -> None:
    with pytest.raises(ValueError):
        tgzh_httpx._encode_qname("a" * 64 + ".example.com")


def test_parse_a_answers_extracts_ip_and_ttl() -> None:
    payload = _make_dns_response(0x1234, "br.example.com", "203.0.113.7", ttl=42)
    answers = tgzh_httpx._parse_a_answers(payload, 0x1234)
    assert answers == [("203.0.113.7", 42)]


def test_parse_a_answers_rejects_bad_txid() -> None:
    payload = _make_dns_response(0x1234, "br.example.com", "203.0.113.7")
    with pytest.raises(ValueError):
        tgzh_httpx._parse_a_answers(payload, 0x9999)


def test_parse_a_answers_rejects_nxdomain() -> None:
    # flags=0x8183 == response, RA, RCODE=3 (NXDOMAIN); AN=0
    header = struct.pack(">HHHHHH", 0x4242, 0x8183, 1, 0, 0, 0)
    qname = tgzh_httpx._encode_qname("missing.example") + struct.pack(">HH", 1, 1)
    with pytest.raises(ValueError):
        tgzh_httpx._parse_a_answers(header + qname, 0x4242)


def test_parse_dns_servers_env_filters_invalid() -> None:
    out = tgzh_httpx.parse_dns_servers_env(" 1.1.1.1 , bogus, 8.8.8.8, , 999.1.1.1 ")
    assert out == ("1.1.1.1", "8.8.8.8")


def test_parse_dns_servers_env_empty() -> None:
    assert tgzh_httpx.parse_dns_servers_env(None) == ()
    assert tgzh_httpx.parse_dns_servers_env("") == ()
    assert tgzh_httpx.parse_dns_servers_env("   ") == ()


@pytest.mark.asyncio
async def test_resolve_via_upstream_uses_first_responding_server(monkeypatch: pytest.MonkeyPatch) -> None:
    tgzh_httpx._clear_dns_cache_for_tests()
    calls: list[tuple[str, int]] = []

    def fake_udp(server: str, payload: bytes, timeout: float) -> bytes:
        # txid лежит в первых 2 байтах payload
        txid = struct.unpack(">H", payload[:2])[0]
        calls.append((server, txid))
        if server == "9.9.9.9":
            raise TimeoutError("simulated")
        return _make_dns_response(txid, "br.example.com", "203.0.113.42", ttl=30)

    monkeypatch.setattr(tgzh_httpx, "_udp_query_sync", fake_udp)
    ip = await tgzh_httpx._resolve_via_upstream_cached(
        "br.example.com",
        ("9.9.9.9", "1.1.1.1"),
    )
    assert ip == "203.0.113.42"
    assert calls[0][0] == "9.9.9.9"
    assert calls[1][0] == "1.1.1.1"


@pytest.mark.asyncio
async def test_resolve_via_upstream_caches_result(monkeypatch: pytest.MonkeyPatch) -> None:
    tgzh_httpx._clear_dns_cache_for_tests()
    counter = {"n": 0}

    def fake_udp(server: str, payload: bytes, timeout: float) -> bytes:
        counter["n"] += 1
        txid = struct.unpack(">H", payload[:2])[0]
        return _make_dns_response(txid, "br.example.com", "198.51.100.9", ttl=300)

    monkeypatch.setattr(tgzh_httpx, "_udp_query_sync", fake_udp)
    ip1 = await tgzh_httpx._resolve_via_upstream_cached("br.example.com", ("1.1.1.1",))
    ip2 = await tgzh_httpx._resolve_via_upstream_cached("BR.example.com", ("1.1.1.1",))
    assert ip1 == "198.51.100.9"
    assert ip2 == "198.51.100.9"
    assert counter["n"] == 1


@pytest.mark.asyncio
async def test_upstream_dns_backend_connects_to_resolved_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    tgzh_httpx._clear_dns_cache_for_tests()
    backend = tgzh_httpx._UpstreamDnsBackend(("1.1.1.1",))
    inner = MagicMock()
    inner.connect_tcp = AsyncMock(return_value="sock")

    async def fake_init() -> None:
        backend._backend = inner

    monkeypatch.setattr(backend, "_init_backend", fake_init)

    async def fake_resolve(host: str, servers: tuple[str, ...]) -> str:
        assert host == "br.example.com"
        assert servers == ("1.1.1.1",)
        return "203.0.113.55"

    monkeypatch.setattr(tgzh_httpx, "_resolve_via_upstream_cached", fake_resolve)
    out = await backend.connect_tcp("br.example.com", 8787)
    assert out == "sock"
    inner.connect_tcp.assert_awaited_once()
    assert inner.connect_tcp.call_args[0][0] == "203.0.113.55"
    assert inner.connect_tcp.call_args[0][1] == 8787


@pytest.mark.asyncio
async def test_upstream_dns_backend_falls_back_to_system_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tgzh_httpx._clear_dns_cache_for_tests()
    backend = tgzh_httpx._UpstreamDnsBackend(("1.1.1.1",))
    inner = MagicMock()
    inner.connect_tcp = AsyncMock(return_value="sock")

    async def fake_init() -> None:
        backend._backend = inner

    monkeypatch.setattr(backend, "_init_backend", fake_init)

    async def fake_resolve(host: str, servers: tuple[str, ...]) -> str | None:
        return None

    monkeypatch.setattr(tgzh_httpx, "_resolve_via_upstream_cached", fake_resolve)
    monkeypatch.setattr(
        tgzh_httpx.socket,
        "getaddrinfo",
        lambda *a, **k: [(0, 0, 0, 0, ("198.51.100.10", 8787))],
    )
    out = await backend.connect_tcp("br.example.com", 8787)
    assert out == "sock"
    assert inner.connect_tcp.call_args[0][0] == "198.51.100.10"


def test_async_http_transport_with_dns_servers_returns_transport() -> None:
    t = tgzh_httpx.async_http_transport_with_dns_servers(["1.1.1.1", "8.8.8.8"])
    assert isinstance(t, httpx.AsyncHTTPTransport)
    pool = getattr(t, "_pool", None)
    assert pool is not None
    assert isinstance(pool._network_backend, tgzh_httpx._UpstreamDnsBackend)


def test_async_http_transport_with_dns_servers_empty_returns_default() -> None:
    t = tgzh_httpx.async_http_transport_with_dns_servers([])
    assert isinstance(t, httpx.AsyncHTTPTransport)
    pool = getattr(t, "_pool", None)
    assert pool is not None
    assert isinstance(pool._network_backend, tgzh_httpx._IPv4PreferredAutoBackend)


# --- HTTP-echo резолвер -----------------------------------------------------


def test_parse_echo_ip_valid() -> None:
    assert tgzh_httpx._parse_echo_ip("80.234.2.114") == "80.234.2.114"
    assert tgzh_httpx._parse_echo_ip("  203.0.113.7\n") == "203.0.113.7"
    assert tgzh_httpx._parse_echo_ip("198.51.100.1 some extra\nignored") == "198.51.100.1"


def test_parse_echo_ip_invalid() -> None:
    assert tgzh_httpx._parse_echo_ip("") is None
    assert tgzh_httpx._parse_echo_ip("   ") is None
    assert tgzh_httpx._parse_echo_ip("not an ip") is None
    assert tgzh_httpx._parse_echo_ip("999.1.1.1") is None
    # IPv6 — сервис их не отдает; считаем за невалидное для нашего IPv4-маршрута
    assert tgzh_httpx._parse_echo_ip("2001:db8::1") is None


@pytest.mark.asyncio
async def test_resolve_via_echo_caches_result(monkeypatch: pytest.MonkeyPatch) -> None:
    tgzh_httpx._clear_echo_cache_for_tests()
    counter = {"n": 0}

    async def fake_fetch(url: str, timeout: float = 8.0) -> str | None:
        counter["n"] += 1
        return "203.0.113.42"

    monkeypatch.setattr(tgzh_httpx, "_fetch_echo_ip", fake_fetch)
    ip1 = await tgzh_httpx._resolve_via_echo_cached("http://echo.example/")
    ip2 = await tgzh_httpx._resolve_via_echo_cached("http://echo.example/")
    assert ip1 == "203.0.113.42"
    assert ip2 == "203.0.113.42"
    assert counter["n"] == 1


@pytest.mark.asyncio
async def test_resolve_via_echo_caches_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    tgzh_httpx._clear_echo_cache_for_tests()
    counter = {"n": 0}

    async def fake_fetch(url: str, timeout: float = 8.0) -> str | None:
        counter["n"] += 1
        return None

    monkeypatch.setattr(tgzh_httpx, "_fetch_echo_ip", fake_fetch)
    assert await tgzh_httpx._resolve_via_echo_cached("http://echo.example/") is None
    # негативный кэш тоже работает — повторно сразу не дергаем
    assert await tgzh_httpx._resolve_via_echo_cached("http://echo.example/") is None
    assert counter["n"] == 1


@pytest.mark.asyncio
async def test_http_echo_backend_resolves_target_via_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    tgzh_httpx._clear_echo_cache_for_tests()
    backend = tgzh_httpx._HttpEchoBackend(
        target_host="br.example.com",
        echo_url="http://echo.example/",
    )
    inner = MagicMock()
    inner.connect_tcp = AsyncMock(return_value="sock")

    async def fake_init() -> None:
        backend._backend = inner

    monkeypatch.setattr(backend, "_init_backend", fake_init)

    async def fake_echo(url: str) -> str:
        assert url == "http://echo.example/"
        return "203.0.113.55"

    monkeypatch.setattr(tgzh_httpx, "_resolve_via_echo_cached", fake_echo)
    out = await backend.connect_tcp("br.example.com", 8787)
    assert out == "sock"
    assert inner.connect_tcp.call_args[0][0] == "203.0.113.55"
    assert inner.connect_tcp.call_args[0][1] == 8787


@pytest.mark.asyncio
async def test_http_echo_backend_skips_echo_for_other_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    tgzh_httpx._clear_echo_cache_for_tests()
    backend = tgzh_httpx._HttpEchoBackend(
        target_host="br.example.com",
        echo_url="http://echo.example/",
    )
    inner = MagicMock()
    inner.connect_tcp = AsyncMock(return_value="sock")

    async def fake_init() -> None:
        backend._backend = inner

    monkeypatch.setattr(backend, "_init_backend", fake_init)
    echo_spy = MagicMock()
    monkeypatch.setattr(tgzh_httpx, "_resolve_via_echo_cached", echo_spy)
    monkeypatch.setattr(
        tgzh_httpx.socket,
        "getaddrinfo",
        lambda *a, **k: [(0, 0, 0, 0, ("198.51.100.99", 443))],
    )
    out = await backend.connect_tcp("other.example.com", 443)
    assert out == "sock"
    echo_spy.assert_not_called()  # echo бьем только по target
    assert inner.connect_tcp.call_args[0][0] == "198.51.100.99"


@pytest.mark.asyncio
async def test_http_echo_backend_falls_back_to_dns_then_system(monkeypatch: pytest.MonkeyPatch) -> None:
    tgzh_httpx._clear_echo_cache_for_tests()
    tgzh_httpx._clear_dns_cache_for_tests()
    backend = tgzh_httpx._HttpEchoBackend(
        target_host="br.example.com",
        echo_url="http://echo.example/",
        dns_fallback_servers=("1.1.1.1",),
    )
    inner = MagicMock()
    inner.connect_tcp = AsyncMock(return_value="sock")

    async def fake_init() -> None:
        backend._backend = inner

    monkeypatch.setattr(backend, "_init_backend", fake_init)

    async def echo_fail(url: str) -> str | None:
        return None

    async def dns_ok(host: str, servers: tuple[str, ...]) -> str:
        assert host == "br.example.com"
        assert servers == ("1.1.1.1",)
        return "203.0.113.77"

    monkeypatch.setattr(tgzh_httpx, "_resolve_via_echo_cached", echo_fail)
    monkeypatch.setattr(tgzh_httpx, "_resolve_via_upstream_cached", dns_ok)
    sysspy = MagicMock(return_value=[(0, 0, 0, 0, ("198.51.100.1", 8787))])
    monkeypatch.setattr(tgzh_httpx.socket, "getaddrinfo", sysspy)

    out = await backend.connect_tcp("br.example.com", 8787)
    assert out == "sock"
    assert inner.connect_tcp.call_args[0][0] == "203.0.113.77"
    sysspy.assert_not_called()


@pytest.mark.asyncio
async def test_http_echo_backend_full_failure_uses_system_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tgzh_httpx._clear_echo_cache_for_tests()
    backend = tgzh_httpx._HttpEchoBackend(
        target_host="br.example.com",
        echo_url="http://echo.example/",
        dns_fallback_servers=("1.1.1.1",),
    )
    inner = MagicMock()
    inner.connect_tcp = AsyncMock(return_value="sock")

    async def fake_init() -> None:
        backend._backend = inner

    monkeypatch.setattr(backend, "_init_backend", fake_init)

    async def echo_fail(url: str) -> str | None:
        return None

    async def dns_fail(host: str, servers: tuple[str, ...]) -> str | None:
        return None

    monkeypatch.setattr(tgzh_httpx, "_resolve_via_echo_cached", echo_fail)
    monkeypatch.setattr(tgzh_httpx, "_resolve_via_upstream_cached", dns_fail)
    monkeypatch.setattr(
        tgzh_httpx.socket,
        "getaddrinfo",
        lambda *a, **k: [(0, 0, 0, 0, ("198.51.100.123", 8787))],
    )
    out = await backend.connect_tcp("br.example.com", 8787)
    assert out == "sock"
    assert inner.connect_tcp.call_args[0][0] == "198.51.100.123"


def test_async_http_transport_with_http_echo_returns_transport() -> None:
    t = tgzh_httpx.async_http_transport_with_http_echo(
        target_host="br.example.com",
        echo_url="http://echo.example/",
        dns_fallback_servers=("1.1.1.1",),
    )
    assert isinstance(t, httpx.AsyncHTTPTransport)
    pool = getattr(t, "_pool", None)
    assert pool is not None
    backend = pool._network_backend
    assert isinstance(backend, tgzh_httpx._HttpEchoBackend)
    assert backend._target_host == "br.example.com"
    assert backend._echo_url == "http://echo.example/"
    assert backend._dns_fallback == ("1.1.1.1",)
