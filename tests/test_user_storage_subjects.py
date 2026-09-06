"""Мультипредметные профили user_storage."""

from __future__ import annotations

import tempfile
from pathlib import Path

import user_storage


def test_two_subjects_same_user() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "u.sqlite")
        user_storage.init_db(path)
        user_storage.set_textbook(
            path, 1, 6, "math-a", "http://m", "Math A", False, "matematika"
        )
        user_storage.set_textbook(
            path, 1, 8, "alg-b", "http://a", "Alg B", False, "algebra"
        )
        math_prof = user_storage.get_profile(path, 1, "matematika")
        alg_prof = user_storage.get_profile(path, 1, "algebra")
        assert math_prof is not None
        assert alg_prof is not None
        assert math_prof.textbook_slug == "math-a"
        assert alg_prof.textbook_slug == "alg-b"
        assert math_prof.grade == 6
        assert alg_prof.grade == 8


def test_active_subject_switch() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "u.sqlite")
        user_storage.init_db(path)
        user_storage.set_textbook(
            path, 2, 7, "g1", "http://g", "Geo", False, "geometriya"
        )
        user_storage.set_textbook(
            path, 2, 6, "m1", "http://m", "Math", False, "matematika"
        )
        assert user_storage.get_active_subject(path, 2) == "matematika"
        user_storage.set_active_subject(path, 2, "geometriya")
        prof = user_storage.get_profile(path, 2)
        assert prof is not None
        assert prof.subject_slug == "geometriya"
        assert prof.textbook_slug == "g1"


def test_homework_meta_per_subject() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "u.sqlite")
        user_storage.init_db(path)
        user_storage.set_textbook(
            path, 3, 6, "m", "http://m", "M", False, "matematika"
        )
        user_storage.set_textbook(
            path, 3, 8, "a", "http://a", "A", False, "algebra"
        )
        user_storage.set_homework_meta(path, 3, "§1", "12", None, "matematika")
        user_storage.set_active_subject(path, 3, "algebra")
        user_storage.set_homework_meta(path, 3, "§3", None, 5, "algebra")
        m = user_storage.get_profile(path, 3, "matematika")
        a = user_storage.get_profile(path, 3, "algebra")
        assert m is not None and m.hw_exercise == "12"
        assert a is not None and a.hw_page == 5
