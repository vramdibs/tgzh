from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import hub_client


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    hub_client.clear_cache()
    monkeypatch.setenv("MOTOK_HUB_URL", "http://hub.test")
    monkeypatch.setenv("MOTOK_INTERNAL_TOKEN", "int-1")


@pytest.mark.asyncio
async def test_ensure_token_caches() -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"person_id": "p1", "token": "tok-1"}

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

        post = AsyncMock(return_value=resp)

    with patch("hub_client.httpx.AsyncClient", _Client):
        a = await hub_client.ensure_token(42)
        b = await hub_client.ensure_token(42)
    assert a == "tok-1"
    assert b == "tok-1"
    assert _Client.post.await_count == 1


def test_hub_configured_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOTOK_HUB_URL", raising=False)
    assert hub_client.hub_configured() is False
