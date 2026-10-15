"""Параллельные апдейты разных пользователей при per-user lock."""

from __future__ import annotations

import asyncio

import pytest

import bot


class _FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _FakeUpdate:
    def __init__(self, user_id: int) -> None:
        self.effective_user = _FakeUser(user_id)


@pytest.mark.asyncio
async def test_per_user_lock_different_users_run_in_parallel() -> None:
    order: list[str] = []

    async def slow_handler(_update: bot.Update, _context: bot.ContextTypes.DEFAULT_TYPE) -> None:
        order.append("a_start")
        await asyncio.sleep(0.15)
        order.append("a_end")

    async def fast_handler(_update: bot.Update, _context: bot.ContextTypes.DEFAULT_TYPE) -> None:
        order.append("b_done")

    wrapped_slow = bot._per_user_update_lock(slow_handler)
    wrapped_fast = bot._per_user_update_lock(fast_handler)

    await asyncio.gather(
        wrapped_slow(_FakeUpdate(1), None),
        wrapped_fast(_FakeUpdate(2), None),
    )

    assert order.index("b_done") < order.index("a_end")


@pytest.mark.asyncio
async def test_per_user_lock_same_user_is_serial() -> None:
    order: list[str] = []

    async def handler_a(_update: bot.Update, _context: bot.ContextTypes.DEFAULT_TYPE) -> None:
        order.append("first_start")
        await asyncio.sleep(0.05)
        order.append("first_end")

    async def handler_b(_update: bot.Update, _context: bot.ContextTypes.DEFAULT_TYPE) -> None:
        order.append("second")

    wrapped_a = bot._per_user_update_lock(handler_a)
    wrapped_b = bot._per_user_update_lock(handler_b)

    await asyncio.gather(
        wrapped_a(_FakeUpdate(99), None),
        wrapped_b(_FakeUpdate(99), None),
    )

    assert order == ["first_start", "first_end", "second"]
