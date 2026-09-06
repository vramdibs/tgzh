"""Клиент хаба motok: person_id и JWT для POST /check."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from tgzh_httpx import async_http_transport_ipv4_lookup

logger = logging.getLogger(__name__)

_token_by_user: dict[int, str] = {}


class HubUnavailable(Exception):
    """MOTOK_HUB_URL задан, но хаб не ответил или отверг запрос."""


def hub_configured() -> bool:
    return bool((os.getenv("MOTOK_HUB_URL") or "").strip())


def _base() -> str:
    return (os.getenv("MOTOK_HUB_URL") or "").strip().rstrip("/")


def _internal_headers() -> dict[str, str]:
    token = (os.getenv("MOTOK_INTERNAL_TOKEN") or "").strip()
    if not token:
        raise HubUnavailable("MOTOK_INTERNAL_TOKEN empty")
    return {"x-motok-internal": token, "content-type": "application/json"}


async def ensure_token(telegram_user_id: int) -> str:
    if not hub_configured():
        return ""
    cached = _token_by_user.get(telegram_user_id)
    if cached:
        return cached
    url = f"{_base()}/internal/telegram/upsert"
    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            transport=async_http_transport_ipv4_lookup(),
        ) as client:
            r = await client.post(
                url,
                json={"telegram_user_id": str(telegram_user_id)},
                headers=_internal_headers(),
            )
    except httpx.RequestError as exc:
        raise HubUnavailable(str(exc)) from exc
    if r.status_code >= 400:
        raise HubUnavailable(f"hub upsert {r.status_code}")
    body: dict[str, Any] = r.json()
    token = str(body.get("token") or "")
    if not token:
        raise HubUnavailable("hub upsert empty token")
    _token_by_user[telegram_user_id] = token
    return token


async def consume_link(telegram_user_id: int, code: str) -> str:
    if not hub_configured():
        raise HubUnavailable("MOTOK_HUB_URL empty")
    url = f"{_base()}/internal/telegram/link"
    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            transport=async_http_transport_ipv4_lookup(),
        ) as client:
            r = await client.post(
                url,
                json={
                    "telegram_user_id": str(telegram_user_id),
                    "code": code,
                },
                headers=_internal_headers(),
            )
    except httpx.RequestError as exc:
        raise HubUnavailable(str(exc)) from exc
    if r.status_code == 409:
        raise HubUnavailable("conflict")
    if r.status_code >= 400:
        raise HubUnavailable(f"hub link {r.status_code}")
    token = str(r.json().get("token") or "")
    if not token:
        raise HubUnavailable("hub link empty token")
    _token_by_user[telegram_user_id] = token
    return token


def clear_cache() -> None:
    _token_by_user.clear()
