"""
Файловый кеш готовых решений gdz.ru: условие + скачанные картинки по ключу (учебник + параграф + упражнение/страница).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import httpx


def cache_key(
    textbook_url: str,
    paragraph: str,
    exercise: str | None,
    page: int | None,
) -> str:
    ex = (exercise or "").strip()
    pg = "" if page is None else str(int(page))
    norm = f"{textbook_url.strip()}|{paragraph.strip()}|{ex}|{pg}"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _entry_dir(cache_root: Path, key: str) -> Path:
    return cache_root / key[:2] / key


def load_task_meta(cache_root: Path, key: str) -> dict | None:
    """
    Читает meta.json (v1): URL страницы и текст условия без проверки файлов картинок.
    Удобно для подстановки условия в промпт проверки, если кеш уже есть.
    """
    d = _entry_dir(cache_root, key)
    meta_path = d / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if meta.get("v") != 1:
        return None
    return {
        "page_url": str(meta.get("page_url") or ""),
        "condition_text": str(meta.get("condition_text") or ""),
    }


def load_bundle(cache_root: Path, key: str) -> dict | None:
    """
    Возвращает {"page_url", "condition_text", "paths": [абсолютные пути]} или None.
    """
    d = _entry_dir(cache_root, key)
    meta_path = d / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if meta.get("v") != 1:
        return None
    files = meta.get("files")
    if not isinstance(files, list) or not files:
        return None
    paths: list[str] = []
    for name in files:
        if not isinstance(name, str) or "/" in name or ".." in name:
            return None
        p = d / name
        if not p.is_file():
            return None
        paths.append(str(p.resolve()))
    return {
        "page_url": str(meta.get("page_url") or ""),
        "condition_text": str(meta.get("condition_text") or ""),
        "paths": paths,
    }


def save_bundle(
    cache_root: Path,
    key: str,
    page_url: str,
    condition_text: str,
    image_urls: list[str],
    client: httpx.Client,
) -> list[str] | None:
    """
    Скачивает картинки и пишет meta.json. Возвращает абсолютные пути или None при ошибке.
    """
    if not image_urls:
        return None
    d = _entry_dir(cache_root, key)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    try:
        for i, url in enumerate(image_urls):
            r = client.get(url)
            r.raise_for_status()
            ct = (r.headers.get("content-type") or "").lower()
            ext = ".png" if "png" in ct else ".jpg"
            name = f"{i}{ext}"
            (d / name).write_bytes(r.content)
            names.append(name)
        meta = {
            "v": 1,
            "page_url": page_url,
            "condition_text": condition_text,
            "files": names,
        }
        (d / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=0),
            encoding="utf-8",
        )
        return [str((d / n).resolve()) for n in names]
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        return None
