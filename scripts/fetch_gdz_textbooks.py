#!/usr/bin/env python3
"""
Скачивает списки учебников с gdz.ru (математика 6 и 7 класс).
Браузерный User-Agent, пауза между запросами. Результат: data/gdz_matematika_textbooks.json
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "gdz_matematika_textbooks.json"

URLS = {
    6: "https://gdz.ru/class-6/matematika/",
    7: "https://gdz.ru/class-7/matematika/",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

TEXTBOOK_MARKER = "Тип книги: Учебник"
HREF_RE = re.compile(r"^https?://gdz\.ru/class-([67])/matematika/([^/?#]+)/?$")


def _label_from_anchor_text(text: str) -> str:
    t = " ".join(text.split())
    low = t.lower()
    if low.startswith("премиум "):
        t = t[low.index("премиум") + len("премиум") :].strip()
    idx = t.find("Тип книги:")
    if idx > 0:
        t = t[:idx].strip()
    t = re.sub(
        r"^Математика\s+[567]\s+класс(\s+Базовый уровень)?\s*",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = t.strip()
    if len(t) > 58:
        t = t[:55] + "..."
    return t or "Учебник"


def _is_premium(text: str) -> bool:
    return "премиум" in text.lower()[:20]


def parse_grade_page(html: str, grade: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    out: list[dict] = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue
        if href.startswith("/"):
            href = "https://gdz.ru" + href
        m = HREF_RE.match(href)
        if not m:
            continue
        g = int(m.group(1))
        if g != grade:
            continue
        slug = m.group(2)
        text = a.get_text(" ", strip=True)
        if TEXTBOOK_MARKER not in text:
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append(
            {
                "grade": grade,
                "slug": slug,
                "url": href,
                "label": _label_from_anchor_text(text),
                "is_premium": _is_premium(text),
            }
        )

    return out


def fetch_all() -> dict[str, list[dict]]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    }
    result: dict[str, list[dict]] = {"6": [], "7": []}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=30.0) as client:
        for i, grade in enumerate((6, 7)):
            if i:
                delay = random.uniform(0.8, 2.5)
                time.sleep(delay)
            r = client.get(URLS[grade])
            r.raise_for_status()
            result[str(grade)] = parse_grade_page(r.text, grade)
    return result


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = fetch_all()
    OUT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUT_PATH} ({len(data['6'])} + {len(data['7'])} textbooks)")


if __name__ == "__main__":
    main()
