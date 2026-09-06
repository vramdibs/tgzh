#!/usr/bin/env python3
"""
Скачивает списки учебников с gdz.ru в data/gdz_catalog.json.
Браузерный User-Agent, пауза между запросами.

Примеры:
  python scripts/fetch_gdz_textbooks.py --subject matematika --grades 6,7
  python scripts/fetch_gdz_textbooks.py --all
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "gdz_catalog.json"

sys.path.insert(0, str(REPO_ROOT))
from catalog import load_catalog  # noqa: E402
from subjects import ALL_GRADES, ALL_SUBJECT_SLUGS, SUBJECT_LABELS  # noqa: E402

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

TEXTBOOK_MARKER = "Тип книги: Учебник"


def _href_re(subject: str) -> re.Pattern[str]:
    return re.compile(
        rf"^https?://gdz\.ru/class-(\d+)/{re.escape(subject)}/([^/?#]+)/?$"
    )


def _label_from_anchor_text(text: str, subject: str, grade: int) -> str:
    t = " ".join(text.split())
    low = t.lower()
    if low.startswith("премиум "):
        t = t[low.index("премиум") + len("премиум") :].strip()
    idx = t.find("Тип книги:")
    if idx > 0:
        t = t[:idx].strip()
    label = SUBJECT_LABELS.get(subject, subject)
    t = re.sub(
        rf"^{re.escape(label)}\s+{grade}\s+класс(\s+Базовый уровень)?\s*",
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


def parse_grade_page(html: str, grade: int, subject: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    out: list[dict] = []
    href_re = _href_re(subject)

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href:
            continue
        if href.startswith("/"):
            href = "https://gdz.ru" + href
        m = href_re.match(href)
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
                "label": _label_from_anchor_text(text, subject, grade),
                "is_premium": _is_premium(text),
            }
        )

    return out


def grade_url(subject: str, grade: int) -> str:
    return f"https://gdz.ru/class-{grade}/{subject}/"


def fetch_subject_grades(
    subject: str,
    grades: list[int],
    *,
    client: httpx.Client,
) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {str(g): [] for g in ALL_GRADES}
    href_re = _href_re(subject)
    for i, grade in enumerate(grades):
        if i:
            time.sleep(random.uniform(0.8, 2.5))
        url = grade_url(subject, grade)
        try:
            r = client.get(url)
            r.raise_for_status()
            result[str(grade)] = parse_grade_page(r.text, grade, subject)
        except httpx.HTTPError as exc:
            print(f"WARN {subject} grade {grade}: {exc}", file=sys.stderr)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch gdz.ru textbook lists")
    parser.add_argument(
        "--subject",
        action="append",
        choices=ALL_SUBJECT_SLUGS,
        help="Subject slug (repeatable)",
    )
    parser.add_argument(
        "--grades",
        default=",".join(str(g) for g in ALL_GRADES),
        help="Comma-separated grades, e.g. 6,7,8",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Fetch all subjects and grades 6-11",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT_PATH,
        help="Output JSON path",
    )
    args = parser.parse_args()

    if args.all:
        subjects = list(ALL_SUBJECT_SLUGS)
        grades = list(ALL_GRADES)
    else:
        subjects = args.subject or [ALL_SUBJECT_SLUGS[0]]
        grades = [int(x.strip()) for x in args.grades.split(",") if x.strip()]

    if OUT_PATH.is_file() or args.out.is_file():
        try:
            catalog = load_catalog(args.out if args.out.exists() else OUT_PATH)
        except FileNotFoundError:
            catalog = {s: {str(g): [] for g in ALL_GRADES} for s in ALL_SUBJECT_SLUGS}
    else:
        catalog = {s: {str(g): [] for g in ALL_GRADES} for s in ALL_SUBJECT_SLUGS}

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    }
    with httpx.Client(headers=headers, follow_redirects=True, timeout=30.0) as client:
        for si, subject in enumerate(subjects):
            if si:
                time.sleep(random.uniform(1.0, 2.0))
            fetched = fetch_subject_grades(subject, grades, client=client)
            for g, items in fetched.items():
                if items:
                    catalog[subject][g] = items
            total = sum(len(v) for v in catalog[subject].values())
            print(f"{subject}: {total} textbooks")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
