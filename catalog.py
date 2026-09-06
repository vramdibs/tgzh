"""Загрузка каталога учебников gdz.ru."""

from __future__ import annotations

import json
import os
from pathlib import Path

from subjects import ALL_GRADES, ALL_SUBJECT_SLUGS, DEFAULT_SUBJECT

_REPO_ROOT = Path(__file__).resolve().parent
_DEFAULT_CATALOG = _REPO_ROOT / "data" / "gdz_catalog.json"
_LEGACY_CATALOG = _REPO_ROOT / "data" / "gdz_matematika_textbooks.json"


def _empty_grade_map() -> dict[str, list[dict]]:
    return {str(g): [] for g in ALL_GRADES}


def _normalize_grade_map(data: object) -> dict[str, list[dict]]:
    out = _empty_grade_map()
    if not isinstance(data, dict):
        return out
    for key in out:
        items = data.get(key, [])
        out[key] = list(items) if isinstance(items, list) else []
    return out


def load_catalog(path: str | Path | None = None) -> dict[str, dict[str, list[dict]]]:
    """
    Вложенный каталог: subject_slug -> grade str -> list of textbook dicts.
    Старый плоский JSON (только matematika по классам) — оборачивается в ветку matematika.
    """
    p = Path(path or os.getenv("GDZ_CATALOG_PATH", str(_DEFAULT_CATALOG)))
    if not p.is_file():
        legacy = _LEGACY_CATALOG
        if legacy.is_file():
            p = legacy
        else:
            raise FileNotFoundError(f"Нет файла каталога учебников: {p.resolve()}")

    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Каталог: ожидается объект JSON")

    # Legacy: {"6": [...], "7": [...]}
    if any(k in raw for k in ("6", "7", "8", "9", "10", "11")) and not any(
        s in raw for s in ALL_SUBJECT_SLUGS
    ):
        result: dict[str, dict[str, list[dict]]] = {
            s: _empty_grade_map() for s in ALL_SUBJECT_SLUGS
        }
        result[DEFAULT_SUBJECT] = _normalize_grade_map(raw)
        return result

    result = {s: _empty_grade_map() for s in ALL_SUBJECT_SLUGS}
    for slug in ALL_SUBJECT_SLUGS:
        branch = raw.get(slug)
        if branch is not None:
            result[slug] = _normalize_grade_map(branch)
    return result


def books_for(catalog: dict[str, dict[str, list[dict]]], subject_slug: str, grade: int) -> list[dict]:
    return catalog.get(subject_slug, {}).get(str(grade), [])
