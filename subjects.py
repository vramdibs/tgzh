"""Канонические предметы ДЗ (slug как на gdz.ru)."""

from __future__ import annotations

SUBJECT_MATEMAIKA = "matematika"
SUBJECT_ALGEBRA = "algebra"
SUBJECT_GEOMETRIYA = "geometriya"
SUBJECT_RUSSKIY = "russkiy-yazyk"
SUBJECT_FIZIKA = "fizika"

ALL_SUBJECT_SLUGS: tuple[str, ...] = (
    SUBJECT_MATEMAIKA,
    SUBJECT_ALGEBRA,
    SUBJECT_GEOMETRIYA,
    SUBJECT_RUSSKIY,
    SUBJECT_FIZIKA,
)

SUBJECT_LABELS: dict[str, str] = {
    SUBJECT_MATEMAIKA: "Математика",
    SUBJECT_ALGEBRA: "Алгебра",
    SUBJECT_GEOMETRIYA: "Геометрия",
    SUBJECT_RUSSKIY: "Русский язык",
    SUBJECT_FIZIKA: "Физика",
}

DEFAULT_SUBJECT = SUBJECT_MATEMAIKA

# Классы, для которых показываем выбор в боте (пустой каталог — отдельное сообщение).
ALL_GRADES: tuple[int, ...] = (6, 7, 8, 9, 10, 11)

# Предметы без отдельного каталога в 6 классе на gdz.ru.
SUBJECTS_FROM_GRADE_7: frozenset[str] = frozenset(
    {SUBJECT_ALGEBRA, SUBJECT_GEOMETRIYA, SUBJECT_FIZIKA}
)


def subject_label(slug: str) -> str:
    return SUBJECT_LABELS.get(slug, slug)


def normalize_subject_slug(slug: str | None) -> str:
    s = (slug or "").strip()
    if s in SUBJECT_LABELS:
        return s
    return DEFAULT_SUBJECT


def grades_for_subject(subject_slug: str) -> tuple[int, ...]:
    if subject_slug in SUBJECTS_FROM_GRADE_7:
        return tuple(g for g in ALL_GRADES if g >= 7)
    return ALL_GRADES
