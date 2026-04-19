"""Разбор оглавления gdz.ru для параграфа."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import gdz_cache
import gdz_solution


def test_collect_paragraph_task_meta_exercises_and_verif() -> None:
    html = """
    <html><body>
    <a href="/book/1-1-item-1/">1.1</a>
    <a href="/book/1-1-item-198/">1.198</a>
    <a href="/book/1-1-item-5/" title="стр. 42">проверочная</a>
    </body></html>
    """
    m = gdz_solution.collect_paragraph_task_meta(html, "https://gdz.ru/book/", "1")
    assert 198 in m.exercise_items
    assert 1 in m.exercise_items
    assert 5 not in m.exercise_items
    assert m.verification_pages == (42,)
    assert len(m.verification_works) == 1
    assert m.verification_works[0].item_index == 5
    assert "42" in m.verification_works[0].label or "провероч" in m.verification_works[0].label.lower()


def test_collect_paragraph_task_meta_verif_other_subsection() -> None:
    """На gdz.ru проверочные часто в 1-2-item-*, а упражнения в 1-1-item-*."""
    html = """
    <html><body>
    <a href="/book/1-1-item-1/">1</a>
    <a href="/book/1-1-item-10/">10</a>
    <a href="/book/1-2-item-99/">стр. 25</a>
    <a href="/book/1-2-item-100/">стр. 41</a>
    </body></html>
    """
    m = gdz_solution.collect_paragraph_task_meta(html, "https://gdz.ru/book/", "1")
    assert 1 in m.exercise_items
    assert 10 in m.exercise_items
    assert m.verification_pages == (25, 41)
    assert len(m.verification_works) == 2
    assert {w.item_index for w in m.verification_works} == {99, 100}


def test_collect_paragraph_task_meta_two_verif_same_page() -> None:
    html = """
    <html><body>
    <a href="/book/1-1-item-1/">1</a>
    <a href="/book/1-2-item-10/" title="стр. 10">Проверочная работа 1</a>
    <a href="/book/1-2-item-11/" title="стр. 10">Проверочная работа 2</a>
    </body></html>
    """
    m = gdz_solution.collect_paragraph_task_meta(html, "https://gdz.ru/book/", "1")
    assert m.verification_pages == (10,)
    assert len(m.verification_works) == 2
    labels = [w.label for w in m.verification_works]
    assert any("1" in lab for lab in labels)
    assert any("2" in lab for lab in labels)


def test_collect_paragraph_task_meta_verif_from_img_alt() -> None:
    html = """
    <html><body>
    <a href="/book/1-1-item-2/">2</a>
    <a href="/book/1-2-item-7/"><img alt="Проверочная работа 2, стр. 55" src="/i.png" /></a>
    </body></html>
    """
    m = gdz_solution.collect_paragraph_task_meta(html, "https://gdz.ru/book/", "1")
    assert 2 in m.exercise_items
    assert m.verification_pages == (55,)
    assert len(m.verification_works) == 1
    assert "Проверочная работа 2" in m.verification_works[0].label


def test_target_from_paragraph_exercise_simple() -> None:
    assert gdz_solution.target_from_paragraph_exercise("1", "126") == (1, 1, 126)
    assert gdz_solution.target_from_paragraph_exercise("1.2", "7") == (1, 2, 7)


def test_fetch_homework_check_gdz_data_condition_from_task_page(monkeypatch: pytest.MonkeyPatch) -> None:
    index_html = """
    <html><body>
    <a href="/book/2-1-item-2/">2</a>
    </body></html>"""
    task_html = (
        '<html><body><p class="task__condition">'
        "Используя таблицу простых чисел, определите простые."
        "</p></body></html>"
    )

    class R:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def get(self, url: str) -> R:
            if "2-1-item-2" in url:
                return R(task_html)
            return R(index_html)

    monkeypatch.setattr("gdz_solution.httpx.Client", FakeClient)
    meta, cond = gdz_solution.fetch_homework_check_gdz_data(
        "https://gdz.ru/book/",
        "2",
        "2",
        None,
    )
    assert 2 in meta.exercise_items
    assert "таблицу простых" in cond


def test_load_task_meta_without_image_files(tmp_path: Path) -> None:
    key = gdz_cache.cache_key("https://gdz.ru/book/x/", "1", "1", None)
    d = tmp_path / key[:2] / key
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps({"v": 1, "page_url": "https://u/", "condition_text": "Условие из кеша", "files": []}),
        encoding="utf-8",
    )
    m = gdz_cache.load_task_meta(tmp_path, key)
    assert m is not None
    assert m["condition_text"] == "Условие из кеша"


def test_max_chapter_index_from_index_html() -> None:
    html = """
    <a href="/t/3-1-item-1/">x</a>
    <a href="/t/10-2-item-3/">y</a>
    """
    n = gdz_solution.max_chapter_index_from_index_html(html, "https://gdz.ru/t/")
    assert n == 10
