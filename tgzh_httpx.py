"""
Общий httpx AsyncHTTPTransport с предпочтением IPv4 при резолве имени хоста.
В Docker getaddrinfo(AF_UNSPEC) иногда дает сбой, при этом AF_INET работает.

Дополнительно — `async_http_transport_with_dns_servers(servers)` для случаев,
когда системный DNS отдает «протухший» ответ (например, кеш на роутере), а нужно
взять актуальный IP с публичных DNS (1.1.1.1 / 8.8.8.8). Реализован минимальный
UDP DNS-клиент (RFC 1035), без сторонних зависимостей. Подключение идет на
полученный IP, заголовок `Host:` остается оригинальным (httpx собирает его из URL),
поэтому виртуальные хосты на стороне сервера работают как обычно.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import struct
import time
from typing import Iterable

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend

logger = logging.getLogger("tgzh.httpx")


class _IPv4PreferredAutoBackend(AutoBackend):
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        await self._init_backend()
        try:
            ipaddress.ip_address(host)
            connect_host = host
        except ValueError:
            infos = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                port,
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                0,
            )
            connect_host = infos[0][4][0]
        return await self._backend.connect_tcp(
            connect_host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


def async_http_transport_ipv4_lookup() -> httpx.AsyncHTTPTransport:
    from httpx._config import DEFAULT_LIMITS, create_ssl_context

    limits = DEFAULT_LIMITS
    ssl_context = create_ssl_context(verify=True, cert=None, trust_env=True)
    pool = httpcore.AsyncConnectionPool(
        ssl_context=ssl_context,
        network_backend=_IPv4PreferredAutoBackend(),
        max_connections=limits.max_connections,
        max_keepalive_connections=limits.max_keepalive_connections,
        keepalive_expiry=limits.keepalive_expiry,
        http1=True,
        http2=False,
        retries=0,
        socket_options=[],
    )
    transport = httpx.AsyncHTTPTransport.__new__(httpx.AsyncHTTPTransport)
    transport._pool = pool
    return transport


# --- Мини-DNS-клиент (RFC 1035), только A-записи (IPv4) ---------------------
#
# Используется, когда системный резолвер по какой-то причине отдает
# неподходящий IP (типичный кейс — кэш на домашнем роутере, который еще не
# обновил A-запись после переезда сервера). Вместо смены /etc/hosts или
# добавления `extra_hosts` в docker-compose делаем точечный обход для
# ОДНОГО httpx-клиента — остальное окружение остается как есть.

_DNS_CACHE_LOCK = asyncio.Lock()
# host_lower -> (ip, expires_monotonic)
_DNS_CACHE: dict[str, tuple[str, float]] = {}
_DNS_MIN_TTL_SEC = 5.0
_DNS_MAX_TTL_SEC = 600.0
_DNS_NEGATIVE_TTL_SEC = 30.0
_DNS_TIMEOUT_SEC = 3.0
_DNS_PORT = 53


def _encode_qname(name: str) -> bytes:
    """Кодирование имени в формате DNS-labels: \\x03www\\x07example\\x03com\\x00."""
    parts = name.rstrip(".").split(".")
    out = bytearray()
    for p in parts:
        if not p or len(p) > 63:
            raise ValueError(f"invalid DNS label: {p!r} in {name!r}")
        out.append(len(p))
        out.extend(p.encode("ascii"))
    out.append(0)
    return bytes(out)


def _build_a_query(name: str, txid: int) -> bytes:
    # ID, flags=0x0100 (RD=1), QDCOUNT=1, остальные =0
    header = struct.pack(">HHHHHH", txid & 0xFFFF, 0x0100, 1, 0, 0, 0)
    qname = _encode_qname(name)
    qtype_qclass = struct.pack(">HH", 1, 1)  # A, IN
    return header + qname + qtype_qclass


def _read_name(buf: bytes, offset: int) -> tuple[str, int]:
    """Читает DNS-имя с поддержкой message compression (0xC0...). Возвращает
    (имя, новая_позиция_после_первоначального_имени_без_прыжка)."""
    parts: list[str] = []
    jumped = False
    end_offset = offset
    safety = 0
    while True:
        safety += 1
        if safety > 256:
            raise ValueError("DNS name parse loop")
        ln = buf[offset]
        if (ln & 0xC0) == 0xC0:
            ptr = ((ln & 0x3F) << 8) | buf[offset + 1]
            if not jumped:
                end_offset = offset + 2
                jumped = True
            offset = ptr
            continue
        if ln == 0:
            offset += 1
            if not jumped:
                end_offset = offset
            return ".".join(parts), end_offset
        parts.append(buf[offset + 1 : offset + 1 + ln].decode("ascii", errors="replace"))
        offset += 1 + ln


def _parse_a_answers(buf: bytes, expected_txid: int) -> list[tuple[str, int]]:
    """Возвращает список (ip, ttl) по A-записям из ответа. Бросает ValueError
    при несовпадении ID или бракованном ответе."""
    if len(buf) < 12:
        raise ValueError("DNS response too short")
    txid, flags, qdcount, ancount, _nscount, _arcount = struct.unpack(">HHHHHH", buf[:12])
    if txid != (expected_txid & 0xFFFF):
        raise ValueError(f"DNS txid mismatch: got {txid:x}, want {expected_txid:x}")
    rcode = flags & 0x000F
    if rcode != 0:
        # 3 = NXDOMAIN, 2 = SERVFAIL, ...
        raise ValueError(f"DNS rcode={rcode}")
    pos = 12
    for _ in range(qdcount):
        _, pos = _read_name(buf, pos)
        pos += 4  # qtype + qclass
    out: list[tuple[str, int]] = []
    for _ in range(ancount):
        _, pos = _read_name(buf, pos)
        atype, _aclass, ttl, rdlen = struct.unpack(">HHIH", buf[pos : pos + 10])
        pos += 10
        rdata = buf[pos : pos + rdlen]
        pos += rdlen
        if atype == 1 and rdlen == 4:
            ip = ".".join(str(b) for b in rdata)
            out.append((ip, max(0, int(ttl))))
    return out


def _udp_query_sync(server: str, payload: bytes, timeout: float) -> bytes:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(payload, (server, _DNS_PORT))
        data, _addr = sock.recvfrom(4096)
        return data
    finally:
        sock.close()


async def _query_a_via_upstream(
    name: str,
    servers: Iterable[str],
    timeout: float = _DNS_TIMEOUT_SEC,
) -> tuple[str | None, int]:
    """Опрос upstream-серверов до первого успеха. Возвращает (ip, ttl).
    При полном провале — (None, _DNS_NEGATIVE_TTL_SEC)."""
    last_exc: BaseException | None = None
    for server in servers:
        s = (server or "").strip()
        if not s:
            continue
        # Свой txid для каждого сервера, чтобы протухший ответ не «прилип» из
        # общего сокета (мы и так используем разовый сокет, но на всякий случай).
        txid = int(time.monotonic_ns()) & 0xFFFF
        try:
            payload = _build_a_query(name, txid)
            raw = await asyncio.to_thread(_udp_query_sync, s, payload, timeout)
            answers = _parse_a_answers(raw, txid)
        except Exception as e:
            last_exc = e
            logger.debug("upstream DNS %s for %s failed: %s", s, name, e)
            continue
        if answers:
            ip, ttl = answers[0]
            ttl_norm = max(_DNS_MIN_TTL_SEC, min(_DNS_MAX_TTL_SEC, float(ttl)))
            return ip, int(ttl_norm)
    if last_exc is not None:
        logger.warning("upstream DNS resolve %s failed via %s: %s", name, list(servers), last_exc)
    return None, int(_DNS_NEGATIVE_TTL_SEC)


async def _resolve_via_upstream_cached(name: str, servers: tuple[str, ...]) -> str | None:
    """Кеш на TTL: повторный резолв в пределах TTL не делает сетевых вызовов.
    `servers` — упорядоченный кортеж, чтобы попадание в кеш было детерминированным."""
    key = name.lower().rstrip(".")
    now = time.monotonic()
    cached = _DNS_CACHE.get(key)
    if cached is not None and cached[1] > now:
        return cached[0]
    async with _DNS_CACHE_LOCK:
        cached = _DNS_CACHE.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]
        ip, ttl = await _query_a_via_upstream(name, servers)
        # Негативный кеш с коротким TTL — чтобы не долбить upstream при отказе.
        ttl_real = ttl if ip else int(_DNS_NEGATIVE_TTL_SEC)
        _DNS_CACHE[key] = (ip or "", now + ttl_real)
        return ip


def _clear_dns_cache_for_tests() -> None:
    """Точка для unit-тестов; в проде не вызывается."""
    _DNS_CACHE.clear()


class _UpstreamDnsBackend(AutoBackend):
    """httpcore network backend, который резолвит host через указанные DNS
    upstream-сервера, минуя системный резолвер. Если резолв провалился — fallback
    на системный (`socket.getaddrinfo` AF_INET), чтобы не уронить запрос совсем.
    """

    def __init__(self, dns_servers: tuple[str, ...]):
        super().__init__()
        if not dns_servers:
            raise ValueError("dns_servers must be non-empty")
        self._dns_servers = dns_servers

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        await self._init_backend()
        try:
            ipaddress.ip_address(host)
            connect_host = host
        except ValueError:
            ip = await _resolve_via_upstream_cached(host, self._dns_servers)
            if ip:
                connect_host = ip
            else:
                infos = await asyncio.to_thread(
                    socket.getaddrinfo,
                    host,
                    port,
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    0,
                    0,
                )
                connect_host = infos[0][4][0]
                logger.warning(
                    "upstream DNS for %s exhausted, falling back to system resolver -> %s",
                    host,
                    connect_host,
                )
        return await self._backend.connect_tcp(
            connect_host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


def _build_transport(network_backend) -> httpx.AsyncHTTPTransport:
    from httpx._config import DEFAULT_LIMITS, create_ssl_context

    limits = DEFAULT_LIMITS
    ssl_context = create_ssl_context(verify=True, cert=None, trust_env=True)
    pool = httpcore.AsyncConnectionPool(
        ssl_context=ssl_context,
        network_backend=network_backend,
        max_connections=limits.max_connections,
        max_keepalive_connections=limits.max_keepalive_connections,
        keepalive_expiry=limits.keepalive_expiry,
        http1=True,
        http2=False,
        retries=0,
        socket_options=[],
    )
    transport = httpx.AsyncHTTPTransport.__new__(httpx.AsyncHTTPTransport)
    transport._pool = pool
    return transport


def parse_dns_servers_env(value: str | None) -> tuple[str, ...]:
    """Разбор CSV-переменной окружения: `1.1.1.1, 8.8.8.8` -> ('1.1.1.1','8.8.8.8').
    Невалидные элементы (не IPv4-литералы) — отбрасываются с предупреждением.
    """
    raw = (value or "").strip()
    if not raw:
        return ()
    out: list[str] = []
    for part in raw.split(","):
        s = part.strip()
        if not s:
            continue
        try:
            ipaddress.IPv4Address(s)
        except (ipaddress.AddressValueError, ValueError):
            logger.warning("ignoring non-IPv4 DNS upstream %r in env", s)
            continue
        out.append(s)
    return tuple(out)


def async_http_transport_with_dns_servers(
    dns_servers: Iterable[str],
) -> httpx.AsyncHTTPTransport:
    """Транспорт для httpx.AsyncClient, который резолвит имена через указанные
    upstream DNS-сервера (UDP). Полезно, когда системный резолвер возвращает
    устаревший IP (например, кеш на домашнем роутере)."""
    servers = tuple(s for s in dns_servers if s)
    if not servers:
        return async_http_transport_ipv4_lookup()
    return _build_transport(_UpstreamDnsBackend(servers))


# --- HTTP-echo резолвер (типа `http-echo.example.com`) -----------------------
#
# Сервис возвращает в plain text **публичный IP клиента**. Полезен, когда bridge
# стоит за тем же NAT, что и наш контейнер: WAN-IP — это адрес, по которому
# bridge на самом деле доступен извне (порт-форвард / hairpin NAT). Это
# авторитетнее любого DNS — A-запись может быть протухшей в кеше провайдера, а
# echo всегда отдает фактический выходной IP.
#
# Источник применим **только** к одному "якорному" хосту (тому, который реально
# на нашем NAT). Поэтому в backend хранится `target_host` — для других имен
# делаем обычный системный резолв.

_ECHO_CACHE_LOCK = asyncio.Lock()
# echo_url -> (ip, expires_monotonic)
_ECHO_CACHE: dict[str, tuple[str, float]] = {}
_ECHO_TTL_SEC = 300.0
_ECHO_NEGATIVE_TTL_SEC = 30.0
_ECHO_TIMEOUT_SEC = 8.0


def _parse_echo_ip(payload: str) -> str | None:
    text = (payload or "").strip()
    if not text:
        return None
    # Берем первый токен — на случай если сервис когда-нибудь добавит перевод строки
    # с дополнительной диагностикой; основной формат no-ip — голый IP.
    candidate = text.split()[0].strip()
    try:
        ip = ipaddress.IPv4Address(candidate)
    except (ipaddress.AddressValueError, ValueError):
        logger.warning("http-echo returned non-IPv4 payload: %r", text[:120])
        return None
    return str(ip)


async def _fetch_echo_ip(echo_url: str, timeout: float = _ECHO_TIMEOUT_SEC) -> str | None:
    """Запрашивает echo-сервис через httpx с **системным** резолвом (без рекурсии
    на наш же backend). Возвращает IP или None при отказе."""

    def _do() -> str | None:
        # urllib — sync, поэтому в to_thread. httpx тут не нужен (и опасен —
        # утянул бы за собой наш кастомный транспорт по дефолту).
        import urllib.request

        req = urllib.request.Request(echo_url, headers={"User-Agent": "tgzh-bridge-resolver/1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — http по дизайну
            charset = resp.headers.get_content_charset() or "ascii"
            return _parse_echo_ip(resp.read(256).decode(charset, errors="replace"))

    try:
        return await asyncio.to_thread(_do)
    except Exception as e:
        logger.warning("http-echo resolve via %s failed: %s", echo_url, e)
        return None


async def _resolve_via_echo_cached(echo_url: str) -> str | None:
    now = time.monotonic()
    cached = _ECHO_CACHE.get(echo_url)
    if cached is not None and cached[1] > now:
        return cached[0] or None
    async with _ECHO_CACHE_LOCK:
        cached = _ECHO_CACHE.get(echo_url)
        if cached is not None and cached[1] > now:
            return cached[0] or None
        ip = await _fetch_echo_ip(echo_url)
        ttl = _ECHO_TTL_SEC if ip else _ECHO_NEGATIVE_TTL_SEC
        _ECHO_CACHE[echo_url] = (ip or "", now + ttl)
        return ip


def _clear_echo_cache_for_tests() -> None:
    _ECHO_CACHE.clear()


class _HttpEchoBackend(AutoBackend):
    """Резолвит **`target_host`** через HTTP-echo-сервис (например, `http-echo.example.com`),
    кэширует результат на TTL. Для всех остальных хостов — системный резолвер
    (или fallback-DNS, если задан).

    Параметр `dns_fallback_servers` — те же upstream-DNS, что используются как
    запасной путь, когда echo упал. Пусто — fallback на `socket.getaddrinfo`.
    """

    def __init__(
        self,
        target_host: str,
        echo_url: str,
        dns_fallback_servers: tuple[str, ...] = (),
    ):
        super().__init__()
        if not target_host:
            raise ValueError("target_host is required")
        if not echo_url:
            raise ValueError("echo_url is required")
        self._target_host = target_host.lower().rstrip(".")
        self._echo_url = echo_url
        self._dns_fallback = dns_fallback_servers

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        await self._init_backend()
        try:
            ipaddress.ip_address(host)
            connect_host = host
        except ValueError:
            connect_host = await self._resolve(host, port)
        return await self._backend.connect_tcp(
            connect_host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def _resolve(self, host: str, port: int) -> str:
        if host.lower().rstrip(".") == self._target_host:
            ip = await _resolve_via_echo_cached(self._echo_url)
            if ip:
                return ip
            logger.warning(
                "http-echo failed for target %s, trying DNS fallback",
                host,
            )
            if self._dns_fallback:
                ip2 = await _resolve_via_upstream_cached(host, self._dns_fallback)
                if ip2:
                    return ip2
        # Любой нецелевой хост или полный отказ echo+upstream — системный резолв.
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            socket.AF_INET,
            socket.SOCK_STREAM,
            0,
            0,
        )
        return infos[0][4][0]


def async_http_transport_with_http_echo(
    target_host: str,
    echo_url: str,
    dns_fallback_servers: Iterable[str] = (),
) -> httpx.AsyncHTTPTransport:
    """Транспорт для httpx.AsyncClient, резолвящий `target_host` через
    HTTP-echo-сервис (возвращающий публичный IP клиента в plain text).
    Применимо, когда сервер за тем же NAT, что и клиент (типичный случай
    self-hosted bridge через port-forward / hairpin)."""
    return _build_transport(
        _HttpEchoBackend(
            target_host=target_host,
            echo_url=echo_url,
            dns_fallback_servers=tuple(s for s in dns_fallback_servers if s),
        ),
    )
