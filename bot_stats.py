"""
Глобальная статистика бота (все пользователи) в том же хранилище, что USER_DB_PATH (SQLite или PostgreSQL).
Счетчики ведутся отдельно по учебному году: с 1 сентября по 31 августа следующего года
(граница дат — выбранный часовой пояс, по умолчанию Europe/Moscow).
Посещаемость по дням: таблица bot_user_visit_day (уникальный user_id на календарный день в STATS_TIMEZONE).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import homework_check_status
from tgzh_db import connect, execute as _e, use_postgres


def _stats_tz_name() -> str:
    return (os.getenv("STATS_TIMEZONE") or "Europe/Moscow").strip() or "Europe/Moscow"


def _stats_tz() -> ZoneInfo:
    try:
        return ZoneInfo(_stats_tz_name())
    except Exception:
        return ZoneInfo("Europe/Moscow")


def academic_year_for_datetime(dt: datetime) -> int:
    """
    Номер учебного года = календарный год сентября начала.
    Например сен 2025 — авг 2026 соответствует 2025 (подпись «2025/2026»).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    loc = dt.astimezone(_stats_tz())
    y, m = loc.year, loc.month
    if m >= 9:
        return y
    return y - 1


def academic_year_now() -> int:
    return academic_year_for_datetime(datetime.now(_stats_tz()))


def _year_label(ys: int) -> str:
    return f"{ys}/{ys + 1}"


def _zero_row_dict() -> dict[str, int]:
    return {
        "photos_uploaded": 0,
        "checks_completed": 0,
        "checks_technical_failed": 0,
        "verdict_correct": 0,
        "verdict_partial": 0,
        "verdict_absent": 0,
        "verdict_percent_sum": 0,
    }


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, int]:
    return {
        "photos_uploaded": int(row[0]),
        "checks_completed": int(row[1]),
        "checks_technical_failed": int(row[2]),
        "verdict_correct": int(row[3]),
        "verdict_partial": int(row[4]),
        "verdict_absent": int(row[5]),
        "verdict_percent_sum": int(row[6]),
    }


_CREATE_BY_YEAR = """
CREATE TABLE IF NOT EXISTS bot_stats_by_year (
    academic_year INTEGER NOT NULL PRIMARY KEY,
    photos_uploaded INTEGER NOT NULL DEFAULT 0,
    checks_completed INTEGER NOT NULL DEFAULT 0,
    checks_technical_failed INTEGER NOT NULL DEFAULT 0,
    verdict_correct INTEGER NOT NULL DEFAULT 0,
    verdict_partial INTEGER NOT NULL DEFAULT 0,
    verdict_absent INTEGER NOT NULL DEFAULT 0,
    verdict_percent_sum INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_VISIT_DAY_SQLITE = """
CREATE TABLE IF NOT EXISTS bot_user_visit_day (
    user_id BIGINT NOT NULL,
    visit_day TEXT NOT NULL,
    PRIMARY KEY (user_id, visit_day)
)
"""

_CREATE_VISIT_DAY_PG = """
CREATE TABLE IF NOT EXISTS bot_user_visit_day (
    user_id BIGINT NOT NULL,
    visit_day TEXT NOT NULL,
    PRIMARY KEY (user_id, visit_day)
)
"""

_CREATE_LLM_USAGE = """
CREATE TABLE IF NOT EXISTS bot_llm_usage_by_year (
    academic_year INTEGER NOT NULL PRIMARY KEY,
    requests INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_LLM_USER_YEAR = """
CREATE TABLE IF NOT EXISTS bot_llm_user_year (
    user_id BIGINT NOT NULL,
    academic_year INTEGER NOT NULL,
    PRIMARY KEY (user_id, academic_year)
)
"""


def _ensure_verdict_percent_sum_column(conn: Any) -> None:
    if use_postgres():
        r = _e(
            conn,
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'bot_stats_by_year'
            AND column_name = 'verdict_percent_sum'
            """,
        ).fetchone()
        if r is None:
            _e(
                conn,
                "ALTER TABLE bot_stats_by_year ADD COLUMN verdict_percent_sum INTEGER NOT NULL DEFAULT 0",
            )
        return
    cur = _e(conn, "PRAGMA table_info(bot_stats_by_year)")
    names = {str(r[1]) for r in cur.fetchall()}
    if "verdict_percent_sum" not in names:
        _e(
            conn,
            "ALTER TABLE bot_stats_by_year ADD COLUMN verdict_percent_sum INTEGER NOT NULL DEFAULT 0",
        )


def _ensure_year_row(conn: Any, academic_year: int) -> None:
    if use_postgres():
        _e(
            conn,
            "INSERT INTO bot_stats_by_year (academic_year) VALUES (?) "
            "ON CONFLICT (academic_year) DO NOTHING",
            (academic_year,),
        )
    else:
        _e(
            conn,
            "INSERT OR IGNORE INTO bot_stats_by_year (academic_year) VALUES (?)",
            (academic_year,),
        )


def _migrate_legacy_aggregate(conn: Any) -> None:
    if use_postgres():
        return
    row = _e(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bot_aggregate_stats'",
    ).fetchone()
    if row is None:
        return
    old = _e(
        conn,
        "SELECT photos_uploaded, checks_completed, checks_technical_failed, "
        "verdict_correct, verdict_partial, verdict_absent "
        "FROM bot_aggregate_stats WHERE id = 1",
    ).fetchone()
    _e(conn, "DROP TABLE IF EXISTS bot_aggregate_stats")
    if old is None:
        return
    if all(int(old[i]) == 0 for i in range(6)):
        return
    y = academic_year_now()
    _ensure_year_row(conn, y)
    _e(
        conn,
        "UPDATE bot_stats_by_year SET "
        "photos_uploaded = photos_uploaded + ?, "
        "checks_completed = checks_completed + ?, "
        "checks_technical_failed = checks_technical_failed + ?, "
        "verdict_correct = verdict_correct + ?, "
        "verdict_partial = verdict_partial + ?, "
        "verdict_absent = verdict_absent + ? "
        "WHERE academic_year = ?",
        (
            int(old[0]),
            int(old[1]),
            int(old[2]),
            int(old[3]),
            int(old[4]),
            int(old[5]),
            y,
        ),
    )


# Кеш уже выполненных init_stats(path) — каждое обращение к stats-функциям иначе
# повторно открывало соединение и DDL'ило идемпотентные CREATE TABLE, что под нагрузкой
# (десятки apдейтов в секунду) добавляло заметный latency и шум в pg_stat_statements.
_INIT_DONE: set[str] = set()


def init_stats(path: str) -> None:
    if path in _INIT_DONE:
        return
    conn = connect(path)
    try:
        _e(conn, _CREATE_BY_YEAR)
        if use_postgres():
            _e(conn, _CREATE_VISIT_DAY_PG)
        else:
            _e(conn, _CREATE_VISIT_DAY_SQLITE)
        _e(conn, _CREATE_LLM_USAGE)
        _e(conn, _CREATE_LLM_USER_YEAR)
        _ensure_verdict_percent_sum_column(conn)
        _migrate_legacy_aggregate(conn)
        conn.commit()
    finally:
        conn.close()
    _INIT_DONE.add(path)


def list_years_desc(path: str) -> list[tuple[int, dict[str, int]]]:
    init_stats(path)
    conn = connect(path)
    try:
        rows = _e(
            conn,
            "SELECT academic_year, photos_uploaded, checks_completed, checks_technical_failed, "
            "verdict_correct, verdict_partial, verdict_absent, verdict_percent_sum "
            "FROM bot_stats_by_year ORDER BY academic_year DESC",
        ).fetchall()
    finally:
        conn.close()
    out: list[tuple[int, dict[str, int]]] = []
    for r in rows:
        ys = int(r[0])
        out.append((ys, _row_to_dict(r[1:])))
    return out


def get_totals(path: str) -> dict[str, int]:
    """Сумма счетчиков по всем учебным годам (совместимость, тесты)."""
    agg = _zero_row_dict()
    for _, d in list_years_desc(path):
        for k in agg:
            agg[k] += d[k]
    return agg


def record_photo_uploaded(path: str) -> None:
    y = academic_year_now()
    init_stats(path)
    conn = connect(path)
    try:
        _ensure_year_row(conn, y)
        _e(
            conn,
            "UPDATE bot_stats_by_year SET photos_uploaded = photos_uploaded + 1 "
            "WHERE academic_year = ?",
            (y,),
        )
        conn.commit()
    finally:
        conn.close()


def _ensure_llm_year_row(conn: Any, academic_year: int) -> None:
    if use_postgres():
        _e(
            conn,
            "INSERT INTO bot_llm_usage_by_year (academic_year) VALUES (?) "
            "ON CONFLICT (academic_year) DO NOTHING",
            (academic_year,),
        )
    else:
        _e(
            conn,
            "INSERT OR IGNORE INTO bot_llm_usage_by_year (academic_year) VALUES (?)",
            (academic_year,),
        )


def record_llm_spend(
    path: str,
    *,
    user_id: int = 0,
    kind: str = "other",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    requests: int = 1,
) -> None:
    """Учет запросов/токенов LLM и уникальных людей. Пишет БД и Prometheus."""
    if (os.getenv("AI_MOCK") or "").strip() == "1":
        return
    y = academic_year_now()
    init_stats(path)
    prompt_n = max(0, int(prompt_tokens))
    completion_n = max(0, int(completion_tokens))
    req_n = max(0, int(requests))
    if req_n <= 0 and prompt_n <= 0 and completion_n <= 0:
        return
    conn = connect(path)
    users_year = 0
    try:
        _ensure_llm_year_row(conn, y)
        _e(
            conn,
            "UPDATE bot_llm_usage_by_year SET "
            "requests = requests + ?, "
            "prompt_tokens = prompt_tokens + ?, "
            "completion_tokens = completion_tokens + ? "
            "WHERE academic_year = ?",
            (req_n, prompt_n, completion_n, y),
        )
        uid = int(user_id or 0)
        if uid > 0:
            if use_postgres():
                _e(
                    conn,
                    "INSERT INTO bot_llm_user_year (user_id, academic_year) VALUES (?, ?) "
                    "ON CONFLICT (user_id, academic_year) DO NOTHING",
                    (uid, y),
                )
            else:
                _e(
                    conn,
                    "INSERT OR IGNORE INTO bot_llm_user_year (user_id, academic_year) VALUES (?, ?)",
                    (uid, y),
                )
        conn.commit()
        row = _e(
            conn,
            "SELECT COUNT(*) FROM bot_llm_user_year WHERE academic_year = ?",
            (y,),
        ).fetchone()
        users_year = int(row[0]) if row else 0
    finally:
        conn.close()
    try:
        import tgzh_metrics

        tgzh_metrics.record_llm_usage(
            kind=kind,
            prompt_tokens=prompt_n,
            completion_tokens=completion_n,
            requests=req_n,
            users=users_year,
        )
    except Exception:
        pass


def get_llm_usage_totals(path: str) -> dict[str, int]:
    init_stats(path)
    conn = connect(path)
    try:
        row = _e(
            conn,
            "SELECT COALESCE(SUM(requests), 0), COALESCE(SUM(prompt_tokens), 0), "
            "COALESCE(SUM(completion_tokens), 0) FROM bot_llm_usage_by_year",
        ).fetchone()
        users_row = _e(
            conn,
            "SELECT COUNT(DISTINCT user_id) FROM bot_llm_user_year",
        ).fetchone()
    finally:
        conn.close()
    requests = int(row[0]) if row else 0
    prompt = int(row[1]) if row else 0
    completion = int(row[2]) if row else 0
    users = int(users_row[0]) if users_row else 0
    return {
        "requests": requests,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "users": users,
    }


def record_check_technical_failed(path: str) -> None:
    y = academic_year_now()
    init_stats(path)
    conn = connect(path)
    try:
        _ensure_year_row(conn, y)
        _e(
            conn,
            "UPDATE bot_stats_by_year SET checks_technical_failed = checks_technical_failed + 1 "
            "WHERE academic_year = ?",
            (y,),
        )
        conn.commit()
    finally:
        conn.close()


_VERDICT_STATS_COLUMN: dict[str, str] = {
    "absent": "verdict_absent",
    "partial": "verdict_partial",
    "correct": "verdict_correct",
}


def record_check_completed(path: str, result_text: str) -> None:
    y = academic_year_now()
    init_stats(path)
    verdict = homework_check_status.homework_check_stats_result(result_text)
    col = _VERDICT_STATS_COLUMN[verdict]
    conn = connect(path)
    try:
        _ensure_year_row(conn, y)
        _e(
            conn,
            "UPDATE bot_stats_by_year SET checks_completed = checks_completed + 1 "
            "WHERE academic_year = ?",
            (y,),
        )
        _e(
            conn,
            f"UPDATE bot_stats_by_year SET {col} = {col} + 1 WHERE academic_year = ?",
            (y,),
        )
        conn.commit()
    finally:
        conn.close()


_POLL_BAR_WIDTH = 10
_POLL_BAR_ON = "█"
_POLL_BAR_OFF = "░"

_ATTEND_BAR_WIDTH = 14
_ATTEND_WEEK_DAYS = 7
_ATTEND_MONTH_DAYS = 30


def record_user_visit_day(path: str, user_id: int) -> None:
    """Одна строка на пару (user_id, календарный день в STATS_TIMEZONE) в сутки."""
    if user_id <= 0:
        return
    init_stats(path)
    day = datetime.now(_stats_tz()).date().isoformat()
    conn = connect(path)
    try:
        if use_postgres():
            _e(
                conn,
                """
                INSERT INTO bot_user_visit_day (user_id, visit_day) VALUES (?, ?)
                ON CONFLICT DO NOTHING
                """,
                (user_id, day),
            )
        else:
            _e(
                conn,
                "INSERT OR IGNORE INTO bot_user_visit_day (user_id, visit_day) VALUES (?, ?)",
                (user_id, day),
            )
        conn.commit()
    finally:
        conn.close()


def daily_unique_counts_last_days(path: str, n: int) -> list[tuple[str, int]]:
    """Последние n календарных дней в STATS_TIMEZONE: (YYYY-MM-DD, число уникальных user_id). Без пропусков, 0 если нет строк."""
    if n <= 0:
        return []
    init_stats(path)
    tz = _stats_tz()
    today = datetime.now(tz).date()
    start = today - timedelta(days=n - 1)
    days: list[date] = []
    d = start
    while d <= today:
        days.append(d)
        d += timedelta(days=1)
    day_strs = [x.isoformat() for x in days]
    conn = connect(path)
    try:
        if not day_strs:
            return []
        ph = ",".join("?" * len(day_strs))
        rows = _e(
            conn,
            f"SELECT visit_day, COUNT(*) AS c FROM bot_user_visit_day "
            f"WHERE visit_day IN ({ph}) GROUP BY visit_day",
            tuple(day_strs),
        ).fetchall()
    finally:
        conn.close()
    by_day = {str(r[0]): int(r[1]) for r in rows}
    return [(ds, by_day.get(ds, 0)) for ds in day_strs]


def unique_visitors_last_days(path: str, n: int) -> int:
    """Сколько разных user_id заходили хотя бы раз за последние n календарных дней (STATS_TIMEZONE)."""
    if n <= 0:
        return 0
    init_stats(path)
    tz = _stats_tz()
    today = datetime.now(tz).date()
    start = today - timedelta(days=n - 1)
    day_strs: list[str] = []
    d = start
    while d <= today:
        day_strs.append(d.isoformat())
        d += timedelta(days=1)
    if not day_strs:
        return 0
    conn = connect(path)
    try:
        ph = ",".join("?" * len(day_strs))
        row = _e(
            conn,
            f"SELECT COUNT(DISTINCT user_id) FROM bot_user_visit_day "
            f"WHERE visit_day IN ({ph})",
            tuple(day_strs),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row and row[0] is not None else 0


def _visit_day_label_dd_mm(iso_day: str) -> str:
    y, m, d = iso_day.split("-")
    return f"{int(d):02d}.{int(m):02d}"


def _attendance_bar_line(label: str, count: int, max_count: int, width: int) -> str:
    if max_count <= 0:
        max_count = 1
    filled = min(width, max(0, round(width * count / max_count)))
    bar = _POLL_BAR_ON * filled + _POLL_BAR_OFF * (width - filled)
    return f"{label} {bar} {count:>4}"


def format_attendance_charts_html(path: str) -> str:
    """Блок /stats: график посещаемости за 7 дней и число уникальных за 30 дней."""
    tz_name = _stats_tz_name()
    week = daily_unique_counts_last_days(path, _ATTEND_WEEK_DAYS)
    w_max = max((c for _, c in week), default=0)
    month_unique = unique_visitors_last_days(path, _ATTEND_MONTH_DAYS)

    def _block(title: str, series: list[tuple[str, int]], mx: int) -> str:
        if not series:
            return ""
        lines = [_attendance_bar_line(_visit_day_label_dd_mm(ds), c, mx, _ATTEND_BAR_WIDTH) for ds, c in series]
        body = "\n".join(lines)
        return f"<b>{title}</b>\n<pre>{body}</pre>\n"

    out = [
        "<b>Посещаемость</b> (уникальные пользователи в день, "
        f"часовой пояс <code>{tz_name}</code>)\n",
        "<i>Сбор данных с момента обновления бота; прошлые дни без записей — нули.</i>\n\n",
        _block(f"Неделя (последние {_ATTEND_WEEK_DAYS} дней)", week, w_max),
        "\n",
        f"<b>Месяц (последние {_ATTEND_MONTH_DAYS} дней):</b> <b>{month_unique}</b> уникальных пользователей\n",
    ]
    return "".join(out).rstrip() + "\n\n"


def _poll_ratio_bar(count: int, total: int) -> tuple[str, int]:
    """Полоска длиной _POLL_BAR_WIDTH и процент (0-100) для count из total."""
    if total <= 0:
        return _POLL_BAR_OFF * _POLL_BAR_WIDTH, 0
    pct = min(100, max(0, round(100.0 * count / total)))
    filled = min(_POLL_BAR_WIDTH, max(0, round(_POLL_BAR_WIDTH * count / total)))
    bar = _POLL_BAR_ON * filled + _POLL_BAR_OFF * (_POLL_BAR_WIDTH - filled)
    return bar, pct


def _format_poll_section_html(poll_n: int, poll_yes: int, poll_no: int) -> str:
    """Блок опроса: завершившие и полоски Да/Нет по вопросу про математику."""
    lines = [
        "<b>Опрос (/polling):</b>",
        f"Завершили: <b>{poll_n}</b>",
    ]
    if poll_n <= 0:
        lines.append("<i>Пока нет завершенных анкет</i>")
        return "\n".join(lines) + "\n\n"
    lines.append("Математика нравится:")
    bar_y, pct_y = _poll_ratio_bar(poll_yes, poll_n)
    bar_n, pct_n = _poll_ratio_bar(poll_no, poll_n)
    lines.append(
        f"<code>Да   {bar_y} {pct_y:>3}% ({poll_yes})</code>",
    )
    lines.append(
        f"<code>Нет  {bar_n} {pct_n:>3}% ({poll_no})</code>",
    )
    return "\n".join(lines) + "\n\n"


def _format_year_block(ys: int, d: dict[str, int]) -> str:
    c = int(d["checks_completed"])
    vc = int(d["verdict_correct"])
    vp = int(d["verdict_partial"])
    va = int(d["verdict_absent"])
    vsum = vc + vp + va
    note = ""
    if c != vsum:
        note = (
            f"\n<i>сумма оценок {vsum} ≠ проверок {c}</i>"
        )
    return (
        f"<b>{_year_label(ys)}</b>\n"
        f"Загружено фото решений: <b>{d['photos_uploaded']}</b>\n"
        f"Проверок завершено: <b>{c}</b>\n"
        f"Не удалось проверить (техн.): <b>{d['checks_technical_failed']}</b>\n"
        f"✅ Частично верных / с замечаниями: <b>{vp}</b>\n"
        f"❌ Нет решения на листе: <b>{va}</b>{note}"
    )


def format_all_stats_html(path: str) -> str:
    import user_storage

    init_stats(path)
    user_storage.init_db(path)
    years = list_years_desc(path)
    grand = get_totals(path)
    poll_n = user_storage.count_user_poll_rows(path)
    poll_yes, poll_no = user_storage.poll_math_yes_no_counts(path)
    chk_up, chk_down = user_storage.check_result_vote_totals(path)
    llm = get_llm_usage_totals(path)
    head = (
        "<b>📊 Статистика</b> (все пользователи)\n\n"
        "<b>Всего за все годы:</b>\n"
        f"Загружено фото решений: <b>{grand['photos_uploaded']}</b>\n"
        f"Проверок завершено: <b>{grand['checks_completed']}</b>\n"
        f"Не удалось проверить (техн.): <b>{grand['checks_technical_failed']}</b>\n"
        "\n"
        "<b>ИИ (токены и запросы):</b>\n"
        f"Запросов к модели: <b>{llm['requests']}</b>\n"
        f"Токенов вход: <b>{llm['prompt_tokens']}</b>\n"
        f"Токенов выход: <b>{llm['completion_tokens']}</b>\n"
        f"Токенов всего: <b>{llm['total_tokens']}</b>\n"
        f"Людей (уникальные): <b>{llm['users']}</b>\n"
        "\n"
        "<b>По тексту ответа модели (всего):</b>\n"
        f"✅ Частично верных / с замечаниями: <b>{grand['verdict_partial']}</b>\n"
        f"❌ Нет решения на листе: <b>{grand['verdict_absent']}</b>\n\n"
        "<b>Оцени ответ (кнопки под сообщением):</b>\n"
        f"👍 <b>{chk_up}</b>, 👎 <b>{chk_down}</b>\n\n"
        f"{_format_poll_section_html(poll_n, poll_yes, poll_no)}"
        f"{format_attendance_charts_html(path)}"
    )
    if not years:
        y = academic_year_now()
        years = [(y, _zero_row_dict())]
    body = "<b>По учебным годам:</b>\n\n" + "\n\n".join(
        _format_year_block(ys, d) for ys, d in years
    )
    foot = ""
    return head + body + foot


# Совместимость со старым вызовом (если где-то остался)
def format_stats_html(totals: dict[str, Any]) -> str:
    c = int(totals["checks_completed"])
    t = int(totals["checks_technical_failed"])
    vc = int(totals["verdict_correct"])
    vp = int(totals["verdict_partial"])
    va = int(totals["verdict_absent"])
    vsum = vc + vp + va
    note = ""
    if c != vsum:
        note = (
            f"\n\n<i>Заметка: сумма оценок ({vsum}) не совпала с числом завершенных "
            f"проверок ({c}) — возможны старые данные до обновления бота.</i>"
        )
    return (
        "<b>📊 Статистика</b> (сводка)\n\n"
        f"Загружено фото решений: <b>{totals['photos_uploaded']}</b>\n"
        f"Проверок завершено: <b>{c}</b>\n"
        f"Не удалось проверить (технические сбои): <b>{t}</b>\n"
        "<b>По тексту ответа модели:</b>\n"
        f"✅ Частично верных / с замечаниями: <b>{vp}</b>\n"
        f"❌ Неверных (нет решения на листе): <b>{va}</b>"
        f"{note}"
    )
