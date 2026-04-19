"""Глобальная статистика бота в SQLite."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import bot_stats
import user_storage


def test_stats_counters_and_verdicts() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "u.sqlite")
        bot_stats.init_stats(path)
        bot_stats.record_photo_uploaded(path)
        bot_stats.record_photo_uploaded(path)
        bot_stats.record_check_completed(path, "Вывод: Решение верно, ошибок нет.")
        bot_stats.record_check_completed(path, "Вывод: Однако есть ошибка в вычислениях.")
        bot_stats.record_check_completed(path, "На фото нет решения, лист пуст.")
        bot_stats.record_check_technical_failed(path)
        t = bot_stats.get_totals(path)
        assert t["photos_uploaded"] == 2
        assert t["checks_completed"] == 3
        assert t["checks_technical_failed"] == 1
        assert t["verdict_correct"] == 1
        assert t["verdict_partial"] == 1
        assert t["verdict_absent"] == 1
        assert t["verdict_percent_sum"] == 0
        html = bot_stats.format_all_stats_html(path)
        assert "По учебным годам" in html
        assert "Всего за все годы" in html
        assert "Средняя оценка выполнения задания" not in html
        assert "Посещаемость" in html
        assert "Неделя" in html
        assert "Месяц" in html
        assert "✅ Верных" not in html
        assert "☑️" not in html


def test_stats_poll_bars_and_no_correct_line() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "u.sqlite")
        bot_stats.init_stats(path)
        user_storage.init_db(path)
        user_storage.upsert_user_poll(path, 101, 1, "инженер")
        user_storage.upsert_user_poll(path, 102, 0, "учитель")
        user_storage.upsert_user_poll(path, 103, 1, "врач")
        html = bot_stats.format_all_stats_html(path)
        assert "█" in html
        assert "░" in html
        assert "Математика нравится" in html
        assert "✅ Верных" not in html
        assert "✅ Частично верных" in html


def test_academic_year_moscow_dates() -> None:
    assert bot_stats.academic_year_for_datetime(
        datetime(2024, 8, 31, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow")),
    ) == 2023
    assert bot_stats.academic_year_for_datetime(
        datetime(2024, 9, 1, 0, 0, 0, tzinfo=ZoneInfo("Europe/Moscow")),
    ) == 2024


def test_migrate_legacy_aggregate_table() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "legacy.sqlite")
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE bot_aggregate_stats (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                photos_uploaded INTEGER NOT NULL DEFAULT 0,
                checks_completed INTEGER NOT NULL DEFAULT 0,
                checks_technical_failed INTEGER NOT NULL DEFAULT 0,
                verdict_correct INTEGER NOT NULL DEFAULT 0,
                verdict_partial INTEGER NOT NULL DEFAULT 0,
                verdict_absent INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO bot_aggregate_stats VALUES (1, 10, 4, 2, 1, 2, 1)",
        )
        conn.commit()
        conn.close()

        bot_stats.init_stats(path)
        t = bot_stats.get_totals(path)
        assert t["photos_uploaded"] == 10
        assert t["checks_completed"] == 4
        assert t["checks_technical_failed"] == 2
        assert t["verdict_correct"] == 1
        assert t["verdict_partial"] == 2
        assert t["verdict_absent"] == 1
        assert t["verdict_percent_sum"] == 0

        conn = sqlite3.connect(path)
        legacy = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bot_aggregate_stats'",
        ).fetchone()
        conn.close()
        assert legacy is None


def test_record_user_visit_day_idempotent() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "v.sqlite")
        bot_stats.init_stats(path)
        bot_stats.record_user_visit_day(path, 42)
        bot_stats.record_user_visit_day(path, 42)
        conn = sqlite3.connect(path)
        n = conn.execute("SELECT COUNT(*) FROM bot_user_visit_day").fetchone()[0]
        conn.close()
        assert n == 1


def test_daily_unique_counts_last_days() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "d.sqlite")
        bot_stats.init_stats(path)
        tz = bot_stats._stats_tz()
        today = datetime.now(tz).date()
        d0 = (today - timedelta(days=2)).isoformat()
        d1 = (today - timedelta(days=1)).isoformat()
        d2 = today.isoformat()
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO bot_user_visit_day (user_id, visit_day) VALUES (1, ?)", (d0,))
        conn.execute("INSERT INTO bot_user_visit_day (user_id, visit_day) VALUES (10, ?)", (d1,))
        conn.execute("INSERT INTO bot_user_visit_day (user_id, visit_day) VALUES (11, ?)", (d1,))
        conn.execute("INSERT INTO bot_user_visit_day (user_id, visit_day) VALUES (20, ?)", (d2,))
        conn.execute("INSERT INTO bot_user_visit_day (user_id, visit_day) VALUES (21, ?)", (d2,))
        conn.execute("INSERT INTO bot_user_visit_day (user_id, visit_day) VALUES (22, ?)", (d2,))
        conn.commit()
        conn.close()
        series = bot_stats.daily_unique_counts_last_days(path, 3)
        by = dict(series)
        assert by[d0] == 1
        assert by[d1] == 2
        assert by[d2] == 3


def test_unique_visitors_last_days() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "u30.sqlite")
        bot_stats.init_stats(path)
        tz = bot_stats._stats_tz()
        today = datetime.now(tz).date()
        d0 = (today - timedelta(days=2)).isoformat()
        d1 = (today - timedelta(days=1)).isoformat()
        d2 = today.isoformat()
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO bot_user_visit_day (user_id, visit_day) VALUES (1, ?)", (d0,))
        conn.execute("INSERT INTO bot_user_visit_day (user_id, visit_day) VALUES (10, ?)", (d1,))
        conn.execute("INSERT INTO bot_user_visit_day (user_id, visit_day) VALUES (11, ?)", (d1,))
        conn.execute("INSERT INTO bot_user_visit_day (user_id, visit_day) VALUES (10, ?)", (d2,))
        conn.commit()
        conn.close()
        assert bot_stats.unique_visitors_last_days(path, 3) == 3
