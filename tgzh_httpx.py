"""
Общий httpx AsyncHTTPTransport с предпочтением IPv4 при резолве имени хоста.
В Docker getaddrinfo(AF_UNSPEC) иногда дает сбой, при этом AF_INET работает.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend


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
