"""
Поиск страницы задания на gdz.ru и извлечение условия + изображений решения (эвристики по верстке сайта).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

import gdz_cache

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Allowlist для всех HTTP-обращений к GDZ: блокирует SSRF через подменённые URL
# (учебники, картинки решений) — например на 169.254.169.254/cloud-metadata.
_GDZ_ALLOWED_HOST_SUFFIXES: tuple[str, ...] = (".gdz.ru",)
_GDZ_ALLOWED_HOSTS: tuple[str, ...] = ("gdz.ru",)


class GdzUrlNotAllowed(ValueError):
    """URL не относится к gdz.ru: блокируем для защиты от SSRF."""


def _is_allowed_gdz_url(url: str) -> bool:
    try:
        p = urlparse((url or "").strip())
    except (ValueError, TypeError):
        return False
    if p.scheme not in ("https", "http"):
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    if host in _GDZ_ALLOWED_HOSTS:
        return True
    return any(host.endswith(suf) for suf in _GDZ_ALLOWED_HOST_SUFFIXES)


def _assert_gdz_url(url: str) -> str:
    """Возвращает нормализованный URL или поднимает GdzUrlNotAllowed."""
    u = (url or "").strip()
    if not _is_allowed_gdz_url(u):
        raise GdzUrlNotAllowed(f"URL вне gdz.ru заблокирован: {u!r}")
    return u


@dataclass(frozen=True)
class VerificationWorkInfo:
    """Одна проверочная работа в оглавлении: подпись с сайта, страница (если есть), item в URL."""

    label: str
    page: int | None
    item_index: int


@dataclass(frozen=True)
class ParagraphTaskMeta:
    """Упражнения и страницы проверочных по оглавлению учебника для выбранного параграфа."""

    exercise_items: frozenset[int]
    verification_pages: tuple[int, ...]
    verification_works: tuple[VerificationWorkInfo, ...] = ()


@dataclass(frozen=True)
class GdzSolutionResult:
    page_url: str
    condition_text: str
    image_urls: tuple[str, ...] = ()
    """URL картинок, если ответ не из кеша (или кеш не удалось записать)."""
    image_paths: tuple[str, ...] = ()
    """Локальные файлы кеша (предпочтительно для отправки в Telegram)."""


def _book_base_path(textbook_url: str) -> str:
    p = urlparse(textbook_url.strip())
    return p.path.rstrip("/") + "/"


def _normalize_img_url(src: str) -> str | None:
    """Возвращает абсолютный URL только в пределах gdz.ru (защита от SSRF через парсинг)."""
    s = (src or "").strip()
    if not s:
        return None
    if s.startswith("//"):
        candidate = "https:" + s
    elif s.startswith("/"):
        candidate = "https://gdz.ru" + s
    elif s.startswith("http://") or s.startswith("https://"):
        candidate = s
    else:
        return None
    return candidate if _is_allowed_gdz_url(candidate) else None


def paragraph_parts(paragraph: str) -> tuple[int | None, int | None]:
    p = paragraph.strip().replace("§", "").replace(",", ".").replace(" ", "")
    m = re.match(r"^(\d+)[.\-](\d+)$", p)
    if m:
        return int(m.group(1)), int(m.group(2))
    m2 = re.match(r"^(\d+)$", p)
    if m2:
        return int(m2.group(1)), None
    return None, None


def target_from_paragraph_exercise(
    paragraph: str,
    exercise: str,
) -> tuple[int, int, int] | None:
    """(glava, parag, item) как в URL .../G-P-item-N/."""
    pc, ps = paragraph_parts(paragraph)
    ex = exercise.strip()
    m = re.match(r"^(\d+)\.(\d+)$", ex)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        ch = pc if pc is not None else a
        return ch, a, b
    m2 = re.fullmatch(r"\d+", ex)
    if m2:
        n = int(ex)
        if pc is None:
            return None
        if ps is not None:
            return pc, ps, n
        return pc, 1, n
    m3 = re.search(r"(\d+)", ex)
    if m3 and pc is not None:
        n = int(m3.group(1))
        if ps is not None:
            return pc, ps, n
        return pc, 1, n
    return None


@dataclass
class _IndexLink:
    path: str
    chapter: int
    section: int
    item: int
    title_raw: str
    text_raw: str
    anchor: Any

    @property
    def title_low(self) -> str:
        return self.title_raw.lower()

    @property
    def text_low(self) -> str:
        return self.text_raw.lower()

    @property
    def match_blob_low(self) -> str:
        parts: list[str] = [self.title_low, self.text_low]
        if self.anchor is not None:
            img = self.anchor.find("img")
            if img is not None:
                alt = (img.get("alt") or "").strip().lower()
                if alt:
                    parts.append(alt)
        return " ".join(p for p in parts if p).strip()


def _collect_candidates(soup: BeautifulSoup, base: str) -> list[_IndexLink]:
    out: list[_IndexLink] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if not href.startswith(base) or "item" not in href:
            continue
        m = re.search(r"(\d+)-(\d+)-item-(\d+)/?$", href)
        if not m:
            continue
        path = href.split("?")[0]
        if not path.endswith("/"):
            path += "/"
        if path in seen:
            continue
        seen.add(path)
        title_raw = (a.get("title") or "").strip()
        text_raw = a.get_text(" ", strip=True)
        out.append(
            _IndexLink(
                path=path,
                chapter=int(m.group(1)),
                section=int(m.group(2)),
                item=int(m.group(3)),
                title_raw=title_raw,
                text_raw=text_raw,
                anchor=a,
            )
        )
    return out


def _verification_work_label(title_raw: str, text_raw: str, anchor: Any) -> str:
    if anchor is not None:
        img = anchor.find("img")
        if img is not None:
            alt = (img.get("alt") or "").strip()
            if len(alt) >= 2:
                return re.sub(r"\s+", " ", alt)[:220]
    for chunk in (text_raw.strip(), title_raw.strip()):
        if len(chunk) >= 2:
            return re.sub(r"\s+", " ", chunk)[:220]
    return ""


def _looks_like_verification_blob(blob_low: str) -> bool:
    if re.search(r"стр\.?\s*\d+", blob_low):
        return True
    if "провероч" in blob_low:
        return True
    return False


def _verification_page_from_anchor(title_raw: str, text_raw: str, anchor: Any = None) -> int | None:
    chunks = [title_raw, text_raw]
    if anchor is not None:
        img = anchor.find("img")
        if img is not None:
            chunks.append(img.get("alt") or "")
    blob = " ".join(chunks)
    m = re.search(r"стр\.?\s*(\d+)", blob, re.I)
    if m:
        return int(m.group(1))
    return None


def collect_paragraph_task_meta(index_html: str, textbook_url: str, paragraph: str) -> ParagraphTaskMeta:
    """
    По HTML оглавления учебника: номера упражнений (третий сегмент item-N в .../C-S-item-N/)
    для пары (класс параграфа, подпараграф); проверочные — отдельные записи с подписью (текст ссылки, title, alt картинки).
    """
    pc, ps = paragraph_parts(paragraph)
    if pc is None:
        return ParagraphTaskMeta(frozenset(), ())
    sec = ps if ps is not None else 1
    base = _book_base_path(textbook_url)
    soup = BeautifulSoup(index_html, "html.parser")
    cands = _collect_candidates(soup, base)
    exercises: set[int] = set()
    pages: set[int] = set()
    ver_works: list[VerificationWorkInfo] = []
    for link in cands:
        if link.chapter != pc:
            continue
        if _looks_like_verification_blob(link.match_blob_low):
            pg = _verification_page_from_anchor(link.title_raw, link.text_raw, link.anchor)
            if pg is not None:
                pages.add(pg)
            label = _verification_work_label(link.title_raw, link.text_raw, link.anchor)
            if not label:
                label = (
                    f"Проверочная, стр. {pg}"
                    if pg is not None
                    else f"Проверочная (item {link.item})"
                )
            ver_works.append(VerificationWorkInfo(label=label, page=pg, item_index=link.item))
            continue
        if link.section != sec:
            continue
        exercises.add(link.item)
    ver_works.sort(key=lambda w: (w.page if w.page is not None else 10**9, w.item_index))
    return ParagraphTaskMeta(
        frozenset(exercises),
        tuple(sorted(pages)),
        tuple(ver_works),
    )


def fetch_paragraph_task_meta(
    textbook_url: str,
    paragraph: str,
    timeout: float = 25.0,
) -> ParagraphTaskMeta:
    """GET оглавления учебника и разбор списков для параграфа; при ошибке — пустые множества."""
    url = (textbook_url or "").strip()
    if not url:
        return ParagraphTaskMeta(frozenset(), ())
    try:
        url = _assert_gdz_url(url)
    except GdzUrlNotAllowed:
        return ParagraphTaskMeta(frozenset(), ())
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"}
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
            r = client.get(url)
            r.raise_for_status()
            return collect_paragraph_task_meta(r.text, url, paragraph)
    except Exception as e:
        logger.warning(
            "fetch_paragraph_task_meta failed url=%s paragraph=%r: %s",
            url,
            paragraph,
            e,
        )
        return ParagraphTaskMeta(frozenset(), ())


def fetch_homework_check_gdz_data(
    textbook_url: str,
    paragraph: str,
    exercise: str | None,
    page: int | None,
    timeout: float = 25.0,
) -> tuple[ParagraphTaskMeta, str]:
    """
    Один GET оглавления: метаданные параграфа (упражнения, проверочные) и при необходимости
    текст условия задания со страницы gdz.ru (как в учебнике), без скачивания картинок решения.
    """
    url = (textbook_url or "").strip()
    if not url:
        return ParagraphTaskMeta(frozenset(), ()), ""
    try:
        url = _assert_gdz_url(url)
    except GdzUrlNotAllowed:
        return ParagraphTaskMeta(frozenset(), ()), ""
    want_condition = bool((exercise or "").strip()) or page is not None
    precache_cond = ""
    root = _cache_root()
    if want_condition and root is not None:
        ck = gdz_cache.cache_key(url, paragraph, exercise, page)
        hit = gdz_cache.load_task_meta(root, ck)
        if hit and (hit.get("condition_text") or "").strip():
            precache_cond = str(hit["condition_text"]).strip()

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"}
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
            r = client.get(url)
            r.raise_for_status()
            html = r.text
            meta = collect_paragraph_task_meta(html, url, paragraph)
            if precache_cond:
                return meta, precache_cond
            if not want_condition:
                return meta, ""
            rel = find_task_path_on_index(html, url, paragraph, exercise, page)
            if not rel:
                return meta, ""
            full_url = _assert_gdz_url(urljoin("https://gdz.ru/", rel))
            r2 = client.get(full_url)
            r2.raise_for_status()
            condition, _imgs = extract_solution_from_task_page(r2.text, full_url)
            ct = (condition or "").strip()
            return meta, ct
    except Exception as e:
        logger.warning(
            "fetch_homework_check_gdz_data failed url=%s paragraph=%r exercise=%r page=%s: %s",
            url,
            paragraph,
            exercise,
            page,
            e,
        )
        return ParagraphTaskMeta(frozenset(), ()), ""


def max_chapter_index_from_index_html(index_html: str, textbook_url: str) -> int | None:
    """
    Максимальный первый индекс в ссылках .../G-P-item-N/ на странице учебника gdz.ru.
    Эвристика для числа «параграфов» верхнего уровня (кнопки §1…§N); при отсутствии ссылок — None.
    """
    base = _book_base_path(textbook_url)
    soup = BeautifulSoup(index_html, "html.parser")
    cands = _collect_candidates(soup, base)
    if not cands:
        return None
    return max(link.chapter for link in cands)


def find_task_path_on_index(
    index_html: str,
    textbook_url: str,
    paragraph: str,
    exercise: str | None,
    page: int | None,
) -> str | None:
    base = _book_base_path(textbook_url)
    soup = BeautifulSoup(index_html, "html.parser")
    cands = _collect_candidates(soup, base)

    if page is not None:
        needle = f"стр. {page}"
        needle2 = f"стр.{page}"
        for link in cands:
            b = link.match_blob_low
            if needle in b or needle2 in b:
                return link.path
        return None

    if not exercise:
        return None
    t = target_from_paragraph_exercise(paragraph, exercise)
    if t is None:
        return None
    ch, sec, item = t
    for link in cands:
        if link.chapter == ch and link.section == sec and link.item == item:
            return link.path
    return None


def extract_solution_from_task_page(html: str, page_url: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    cond_el = soup.select_one("p.task__condition")
    condition = cond_el.get_text(" ", strip=True) if cond_el else ""

    urls: list[str] = []
    for fig in soup.select("figure"):
        for img in fig.find_all("img", src=True):
            u = _normalize_img_url(img["src"])
            if u and "attachments/images/tasks" in u and u not in urls:
                urls.append(u)
    if not urls:
        for img in soup.select(".task-img-container img[src]"):
            u = _normalize_img_url(img["src"])
            if u and u not in urls:
                urls.append(u)

    return condition, urls


def _cache_root() -> Path | None:
    raw = os.getenv("GDZ_CACHE_DIR", "data/gdz_cache").strip()
    if not raw or raw.lower() in ("0", "false", "no", "off"):
        return None
    return Path(raw)


def fetch_solution(
    textbook_url: str,
    paragraph: str,
    exercise: str | None,
    page: int | None,
    timeout: float = 25.0,
) -> tuple[GdzSolutionResult | None, str | None]:
    """
    Возвращает (результат, сообщение_об_ошибке).
    При включенном кеше (GDZ_CACHE_DIR) читает с диска без запросов к gdz.ru.
    """
    root = _cache_root()
    key = gdz_cache.cache_key(textbook_url, paragraph, exercise, page)
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
        hit = gdz_cache.load_bundle(root, key)
        if hit is not None:
            return (
                GdzSolutionResult(
                    page_url=hit["page_url"],
                    condition_text=hit["condition_text"],
                    image_urls=(),
                    image_paths=tuple(hit["paths"]),
                ),
                None,
            )

    try:
        textbook_url = _assert_gdz_url(textbook_url)
    except GdzUrlNotAllowed as e:
        return None, f"URL учебника не относится к gdz.ru: {e}"
    base = _book_base_path(textbook_url)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"}
    try:
        with httpx.Client(
            headers=headers,
            follow_redirects=True,
            timeout=timeout,
        ) as client:
            r = client.get(textbook_url)
            r.raise_for_status()
            rel = find_task_path_on_index(r.text, textbook_url, paragraph, exercise, page)
            if not rel:
                hint = (
                    "❗ Не нашли страницу задания в оглавлении учебника. "
                    "Проверь параграф (например 1.1 или 4-5) и номер упражнения или страницу; "
                    f"оглавление: {textbook_url}"
                )
                return None, hint

            full_url = _assert_gdz_url(urljoin("https://gdz.ru/", rel))
            r2 = client.get(full_url)
            r2.raise_for_status()
            condition, imgs = extract_solution_from_task_page(r2.text, full_url)
            if not imgs and not condition:
                return (
                    None,
                    f"Страница открылась, но не удалось извлечь текст или картинки: {full_url}",
                )
            paths: tuple[str, ...] = ()
            urls_t = tuple(imgs)
            if root is not None and imgs:
                saved = gdz_cache.save_bundle(
                    root,
                    key,
                    full_url,
                    condition,
                    list(imgs),
                    client,
                )
                if saved:
                    paths = tuple(saved)
            return (
                GdzSolutionResult(
                    page_url=full_url,
                    condition_text=condition,
                    image_urls=() if paths else urls_t,
                    image_paths=paths,
                ),
                None,
            )
    except httpx.HTTPError as e:
        return None, f"Ошибка загрузки gdz.ru: {e}"
    except Exception as e:
        return None, f"Ошибка разбора: {e}"
