"""Разбор условия задачи для экрана «Ответить текстом»."""

from __future__ import annotations

import bot as bot_module


def test_split_cyrillic_subitems_user_example() -> None:
    raw = (
        "Раскройте скобки: а) 46 + (51 + 34); б) 72 - (11 - 28); "
        "в) 3,71 + (4,5 - 3,71); г) 4,8 - (11,3 + 2,5)."
    )
    intro, items = bot_module.split_task_condition_items(raw)
    assert "Раскройте скобки" in intro
    assert len(items) == 4
    assert items[0] == "а) 46 + (51 + 34)"
    assert items[1] == "б) 72 - (11 - 28)"
    assert "3,71" in items[2]
    assert items[3].startswith("г)")


def test_no_false_split_on_inner_paren_close() -> None:
    raw = "Вычислите: а) (51 + 34) + 1; б) 2."
    intro, items = bot_module.split_task_condition_items(raw)
    assert len(items) == 2
    assert "51 + 34" in items[0]
    assert items[0].startswith("а)")


def test_numbered_after_semicolon() -> None:
    raw = "Решите:; 1) x + 1; 2) y - 2"
    intro, items = bot_module.split_task_condition_items(raw)
    assert len(items) == 2
    assert items[0].startswith("1)")
    assert "x + 1" in items[0]


def test_strip_trailing_item_punct() -> None:
    assert bot_module._strip_trailing_item_punct("a, ") == "a"
    assert bot_module._strip_trailing_item_punct("x.;") == "x"
