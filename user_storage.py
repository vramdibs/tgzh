"""
Профиль пользователя (учебник + привязка к заданию: параграф, упражнение или страница).
Хранилище: SQLite по USER_DB_PATH или PostgreSQL при заданном DATABASE_URL.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from tgzh_db import connect, db_path_usable, execute as _e, use_postgres


@dataclass(frozen=True)
class UserProfile:
    user_id: int
    grade: int
    textbook_slug: str
    textbook_url: str
    textbook_label: str
    is_premium: bool
    hw_paragraph: str | None
    hw_exercise: str | None
    hw_page: int | None
    subject_slug: str


@dataclass(frozen=True)
class UserPollRow:
    user_id: int
    likes_math: int
    career_text: str
    updated_at: str


# Разделитель фрагментов в user_feedback.body: редкая последовательность (не вводится с клавиатуры)
FEEDBACK_ENTRY_SEP = "\n\fTGZH_FB\v\n"


@dataclass(frozen=True)
class UserFeedbackRow:
    """Одна строка БД на пользователя; body - несколько отзывов через FEEDBACK_ENTRY_SEP."""

    user_id: int
    username: str | None
    body: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class UserFeedbackEntry:
    """Один отзыв после разбора body."""

    at: str
    text: str


@dataclass(frozen=True)
class FeedbackTicketRow:
    """Одно обращение в поддержку (строка в feedback_ticket)."""

    id: int
    user_id: int
    username: str | None
    body: str
    status: str
    staff_response: str | None
    created_at: str
    status_updated_at: str | None
    archived: bool


@dataclass(frozen=True)
class FeedbackNlpRow:
    ticket_id: int
    sentiment_label: str
    sentiment_score: float
    emotions_json: str
    badges: str
    error: str | None


@dataclass(frozen=True)
class UserConsentRow:
    user_id: int
    disclaimer_version: int
    quiz_passed_at: str | None
    accepted_at: str | None


def _migrate_user_profile_grade_subject(conn: Any) -> None:
    if use_postgres():
        return
    row = _e(
        conn,
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_profile'",
    ).fetchone()
    if not row:
        return
    create_sql = (row[0] or "").replace(" ", "")
    cols = {r[1] for r in _e(conn, "PRAGMA table_info(user_profile)").fetchall()}
    need_subject = "subject_slug" not in cols
    need_rebuild = "6,7" in create_sql or "IN(6,7)" in create_sql
    if need_subject:
        _e(conn, "ALTER TABLE user_profile ADD COLUMN subject_slug TEXT DEFAULT 'matematika'")
        _e(
            conn,
            "UPDATE user_profile SET subject_slug = 'matematika' "
            "WHERE subject_slug IS NULL OR TRIM(subject_slug) = ''",
        )
    if need_rebuild:
        _e(
            conn,
            """
            CREATE TABLE user_profile__new (
                user_id BIGINT PRIMARY KEY NOT NULL,
                grade INTEGER NOT NULL CHECK (grade >= 6 AND grade <= 11),
                textbook_slug TEXT NOT NULL,
                textbook_url TEXT NOT NULL,
                textbook_label TEXT NOT NULL,
                is_premium INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                hw_paragraph TEXT,
                hw_exercise TEXT,
                hw_page INTEGER,
                subject_slug TEXT NOT NULL DEFAULT 'matematika'
            )
            """,
        )
        _e(
            conn,
            """
            INSERT INTO user_profile__new (
                user_id, grade, textbook_slug, textbook_url, textbook_label,
                is_premium, updated_at, hw_paragraph, hw_exercise, hw_page, subject_slug
            )
            SELECT user_id, grade, textbook_slug, textbook_url, textbook_label,
                   is_premium, updated_at, hw_paragraph, hw_exercise, hw_page,
                   COALESCE(NULLIF(TRIM(subject_slug), ''), 'matematika')
            FROM user_profile
            """,
        )
        _e(conn, "DROP TABLE user_profile")
        _e(conn, "ALTER TABLE user_profile__new RENAME TO user_profile")


def _migrate_feedback_ticket_archived(conn: Any) -> None:
    """Колонка archived: рассмотренные/отклоненные не показываются пользователю в /my_support."""
    if use_postgres():
        return
    row = _e(
        conn,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='feedback_ticket'",
    ).fetchone()
    if not row:
        return
    cols = {r[1] for r in _e(conn, "PRAGMA table_info(feedback_ticket)").fetchall()}
    if "archived" not in cols:
        _e(conn, "ALTER TABLE feedback_ticket ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    _e(
        conn,
        """
        UPDATE feedback_ticket SET archived = 1
        WHERE status IN ('reviewed', 'rejected')
        """
    )


def _migrate(conn: Any) -> None:
    if use_postgres():
        return
    info = _e(conn, "PRAGMA table_info(user_profile)").fetchall()
    colnames = {row[1] for row in info}
    if "hw_paragraph" not in colnames:
        _e(conn, "ALTER TABLE user_profile ADD COLUMN hw_paragraph TEXT")
    if "hw_exercise" not in colnames:
        _e(conn, "ALTER TABLE user_profile ADD COLUMN hw_exercise TEXT")
    if "hw_page" not in colnames:
        _e(conn, "ALTER TABLE user_profile ADD COLUMN hw_page INTEGER")


def _migrate_user_feedback_table(conn: Any) -> None:
    """Со схемы (id, user_id, ...) на одну строку на user_id с накоплением body."""
    if use_postgres():
        return
    info = _e(conn, "PRAGMA table_info(user_feedback)").fetchall()
    colnames = {row[1] for row in info}
    if "id" not in colnames:
        return
    _e(conn, "ALTER TABLE user_feedback RENAME TO user_feedback_legacy")
    _e(
        conn,
        """
        CREATE TABLE user_feedback (
            user_id BIGINT PRIMARY KEY NOT NULL,
            username TEXT,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    legacy = _e(
        conn,
        "SELECT user_id, username, body, created_at FROM user_feedback_legacy ORDER BY id ASC",
    ).fetchall()
    by_user: dict[int, list[tuple[str | None, str, str]]] = defaultdict(list)
    for uid, un, body, cat in legacy:
        by_user[int(uid)].append((un, str(body), str(cat)))
    for uid, items in by_user.items():
        chunks: list[str] = []
        for un, b, cat in items:
            chunks.append(f"{cat}\n{b.strip()}")
        merged = FEEDBACK_ENTRY_SEP.join(chunks)
        first_ca = items[0][2]
        last_ca = items[-1][2]
        last_un = items[-1][0]
        last_un = (last_un or "").strip() or None
        _e(
            conn,
            "INSERT INTO user_feedback (user_id, username, body, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, last_un, merged, first_ca, last_ca),
        )
    _e(conn, "DROP TABLE user_feedback_legacy")


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        _e(
            conn,
            """
            CREATE TABLE IF NOT EXISTS user_profile (
                user_id BIGINT PRIMARY KEY,
                grade INTEGER NOT NULL CHECK (grade >= 6 AND grade <= 11),
                textbook_slug TEXT NOT NULL,
                textbook_url TEXT NOT NULL,
                textbook_label TEXT NOT NULL,
                is_premium INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                hw_paragraph TEXT,
                hw_exercise TEXT,
                hw_page INTEGER,
                subject_slug TEXT NOT NULL DEFAULT 'matematika'
            )
            """,
        )
        if use_postgres():
            _e(
                conn,
                """
                CREATE TABLE IF NOT EXISTS user_feedback (
                    user_id BIGINT PRIMARY KEY NOT NULL,
                    username TEXT,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
            )
        else:
            row = _e(
                conn,
                "SELECT name FROM sqlite_master WHERE type='table' AND name='user_feedback'",
            ).fetchone()
            if row is None:
                _e(
                    conn,
                    """
                    CREATE TABLE user_feedback (
                        user_id BIGINT PRIMARY KEY NOT NULL,
                        username TEXT,
                        body TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """,
                )
            else:
                _migrate_user_feedback_table(conn)
        _e(
            conn,
            """
            CREATE TABLE IF NOT EXISTS user_consent (
                user_id BIGINT PRIMARY KEY NOT NULL,
                disclaimer_version INTEGER NOT NULL,
                quiz_passed_at TEXT,
                accepted_at TEXT
            )
            """,
        )
        _e(
            conn,
            """
            CREATE TABLE IF NOT EXISTS user_poll (
                user_id BIGINT PRIMARY KEY NOT NULL,
                likes_math INTEGER NOT NULL CHECK (likes_math IN (0, 1)),
                career_text TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        )
        if use_postgres():
            _e(
                conn,
                """
                CREATE TABLE IF NOT EXISTS feedback_ticket (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    staff_response TEXT,
                    created_at TEXT NOT NULL,
                    status_updated_at TEXT,
                    archived INTEGER NOT NULL DEFAULT 0
                )
                """,
            )
        else:
            _e(
                conn,
                """
                CREATE TABLE IF NOT EXISTS feedback_ticket (
                    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    staff_response TEXT,
                    created_at TEXT NOT NULL,
                    status_updated_at TEXT,
                    archived INTEGER NOT NULL DEFAULT 0
                )
                """,
            )
        if use_postgres():
            _e(
                conn,
                """
                CREATE TABLE IF NOT EXISTS feedback_ticket_nlp (
                    ticket_id BIGINT PRIMARY KEY NOT NULL,
                    sentiment_label TEXT NOT NULL DEFAULT '',
                    sentiment_score DOUBLE PRECISION NOT NULL DEFAULT 0,
                    emotions_json TEXT NOT NULL DEFAULT '[]',
                    badges TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    analyzed_at TEXT NOT NULL
                )
                """,
            )
        else:
            _e(
                conn,
                """
                CREATE TABLE IF NOT EXISTS feedback_ticket_nlp (
                    ticket_id INTEGER PRIMARY KEY NOT NULL,
                    sentiment_label TEXT NOT NULL DEFAULT '',
                    sentiment_score REAL NOT NULL DEFAULT 0,
                    emotions_json TEXT NOT NULL DEFAULT '[]',
                    badges TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    analyzed_at TEXT NOT NULL
                )
                """,
            )
        _e(
            conn,
            """
            CREATE TABLE IF NOT EXISTS check_result_vote (
                chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                user_id BIGINT NOT NULL,
                vote INTEGER NOT NULL CHECK (vote IN (1, -1)),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, message_id, user_id)
            )
            """,
        )
        _e(
            conn,
            """
            CREATE TABLE IF NOT EXISTS user_block (
                user_id BIGINT PRIMARY KEY NOT NULL,
                block_type TEXT NOT NULL CHECK (block_type IN ('temp', 'permanent')),
                reason TEXT,
                until_utc TEXT,
                created_at TEXT NOT NULL
            )
            """,
        )
        if use_postgres():
            _e(
                conn,
                """
                CREATE TABLE IF NOT EXISTS check_sticker_reward (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('reward', 'motivation')),
                    sticker_file_id TEXT,
                    sticker_set_name TEXT,
                    quip TEXT,
                    created_at TEXT NOT NULL
                )
                """,
            )
        else:
            _e(
                conn,
                """
                CREATE TABLE IF NOT EXISTS check_sticker_reward (
                    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                    user_id BIGINT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('reward', 'motivation')),
                    sticker_file_id TEXT,
                    sticker_set_name TEXT,
                    quip TEXT,
                    created_at TEXT NOT NULL
                )
                """,
            )
        conn.commit()
        if not use_postgres():
            _migrate(conn)
            conn.commit()
            _migrate_feedback_ticket_archived(conn)
            conn.commit()
            _migrate_user_profile_grade_subject(conn)
            conn.commit()
    finally:
        conn.close()


def homework_complete(p: UserProfile) -> bool:
    if not (p.hw_paragraph and p.hw_paragraph.strip()):
        return False
    has_ex = bool(p.hw_exercise and p.hw_exercise.strip())
    has_page = p.hw_page is not None
    return has_ex or has_page


def get_profile(path: str, user_id: int) -> UserProfile | None:
    conn = connect(path)
    try:
        row = _e(
            conn,
            "SELECT user_id, grade, textbook_slug, textbook_url, textbook_label, is_premium, "
            "hw_paragraph, hw_exercise, hw_page, subject_slug "
            "FROM user_profile WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    subj = row[9] if len(row) > 9 else "matematika"
    if not (subj and str(subj).strip()):
        subj = "matematika"
    return UserProfile(
        user_id=row[0],
        grade=row[1],
        textbook_slug=row[2],
        textbook_url=row[3],
        textbook_label=row[4],
        is_premium=bool(row[5]),
        hw_paragraph=row[6],
        hw_exercise=row[7],
        hw_page=row[8],
        subject_slug=str(subj),
    )


def set_textbook(
    path: str,
    user_id: int,
    grade: int,
    slug: str,
    url: str,
    label: str,
    is_premium: bool,
    subject_slug: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    existing = get_profile(path, user_id)
    sub = (subject_slug or "").strip() or (existing.subject_slug if existing else "") or "matematika"
    conn = connect(path)
    try:
        _e(
            conn,
            """
            INSERT INTO user_profile (
                user_id, grade, textbook_slug, textbook_url, textbook_label, is_premium,
                updated_at, hw_paragraph, hw_exercise, hw_page, subject_slug
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                grade = excluded.grade,
                textbook_slug = excluded.textbook_slug,
                textbook_url = excluded.textbook_url,
                textbook_label = excluded.textbook_label,
                is_premium = excluded.is_premium,
                updated_at = excluded.updated_at,
                hw_paragraph = NULL,
                hw_exercise = NULL,
                hw_page = NULL,
                subject_slug = excluded.subject_slug
            """,
            (user_id, grade, slug, url, label, 1 if is_premium else 0, now, sub),
        )
        conn.commit()
    finally:
        conn.close()


def set_homework_meta(
    path: str,
    user_id: int,
    paragraph: str,
    exercise: str | None,
    page: int | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = connect(path)
    try:
        _e(
            conn,
            """
            UPDATE user_profile SET
                hw_paragraph = ?,
                hw_exercise = ?,
                hw_page = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (paragraph.strip(), exercise.strip() if exercise else None, page, now, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def textbook_popularity_by_grade(path: str, grade: int) -> dict[str, int]:
    """Сколько пользователей сейчас с привязкой к учебнику (slug) в данном классе."""
    if not db_path_usable(path):
        return {}
    conn = connect(path)
    try:
        rows = _e(
            conn,
            "SELECT textbook_slug, COUNT(*) FROM user_profile WHERE grade = ? "
            "GROUP BY textbook_slug",
            (grade,),
        ).fetchall()
    finally:
        conn.close()
    return {str(r[0]): int(r[1]) for r in rows}


def clear_homework_meta(path: str, user_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = connect(path)
    try:
        _e(
            conn,
            "UPDATE user_profile SET hw_paragraph = NULL, hw_exercise = NULL, "
            "hw_page = NULL, updated_at = ? WHERE user_id = ?",
            (now, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def parse_feedback_entries(body: str) -> list[UserFeedbackEntry]:
    """Разбор накопленного body на отзывы: первая строка фрагмента - ISO-время, остальное - текст."""
    if not (body or "").strip():
        return []
    parts = body.split(FEEDBACK_ENTRY_SEP)
    out: list[UserFeedbackEntry] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        lines = p.split("\n", 1)
        at = lines[0].strip()
        text = lines[1].strip() if len(lines) > 1 else ""
        if not text:
            text = at
            at = ""
        out.append(UserFeedbackEntry(at=at, text=text))
    return out


_FEEDBACK_BODY_MAX = 120_000

FEEDBACK_TICKET_STATUS_PENDING = "pending"
FEEDBACK_TICKET_STATUS_REVIEWED = "reviewed"
FEEDBACK_TICKET_STATUS_REJECTED = "rejected"


def _feedback_ticket_row_from_fetch(r: Any) -> FeedbackTicketRow:
    arch = r[8]
    if arch is None:
        archived = False
    else:
        archived = bool(int(arch))
    return FeedbackTicketRow(
        id=int(r[0]),
        user_id=int(r[1]),
        username=r[2],
        body=str(r[3]),
        status=str(r[4]),
        staff_response=str(r[5]) if r[5] is not None else None,
        created_at=str(r[6]),
        status_updated_at=str(r[7]) if r[7] is not None else None,
        archived=archived,
    )


def _insert_feedback_ticket_get_id(
    conn: Any,
    user_id: int,
    username: str | None,
    body: str,
    created_at: str,
) -> int:
    if use_postgres():
        row = _e(
            conn,
            """
            INSERT INTO feedback_ticket (user_id, username, body, status, staff_response, created_at, status_updated_at, archived)
            VALUES (?, ?, ?, ?, NULL, ?, NULL, 0)
            RETURNING id
            """,
            (user_id, username, body, FEEDBACK_TICKET_STATUS_PENDING, created_at),
        ).fetchone()
        if row is None:
            raise RuntimeError("feedback_ticket INSERT RETURNING id failed")
        return int(row[0])
    cur = _e(
        conn,
        """
        INSERT INTO feedback_ticket (user_id, username, body, status, staff_response, created_at, status_updated_at, archived)
        VALUES (?, ?, ?, ?, NULL, ?, NULL, 0)
        """,
        (user_id, username, body, FEEDBACK_TICKET_STATUS_PENDING, created_at),
    )
    lid = int(getattr(cur, "lastrowid", None) or 0)
    if lid <= 0:
        raise RuntimeError("feedback_ticket lastrowid missing")
    return lid


def append_user_feedback(path: str, user_id: int, username: str | None, text: str) -> tuple[int, int]:
    """Дописывает отзыв в body этому user_id (одна строка в БД) и создает строку тикета. Возвращает (user_id, ticket_id)."""
    now = datetime.now(timezone.utc).isoformat()
    un = (username or "").strip() or None
    b = text.strip()
    if not b:
        raise ValueError("empty feedback")
    if len(b) > 8000:
        b = b[:8000]
    block = f"{now}\n{b}"
    conn = connect(path)
    try:
        ticket_id = _insert_feedback_ticket_get_id(conn, user_id, un, b, now)
        _e(
            conn,
            """
            INSERT INTO user_feedback (user_id, username, body, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                body = user_feedback.body || ? || excluded.body,
                updated_at = excluded.updated_at
            """,
            (user_id, un, block, now, now, FEEDBACK_ENTRY_SEP),
        )
        row = _e(conn, "SELECT body FROM user_feedback WHERE user_id = ?", (user_id,)).fetchone()
        if row and len(row[0] or "") > _FEEDBACK_BODY_MAX:
            full = row[0]
            entries = parse_feedback_entries(full)
            while entries and len(full) > _FEEDBACK_BODY_MAX:
                entries = entries[1:]
                blocks: list[str] = []
                for e in entries:
                    blocks.append(f"{e.at}\n{e.text}" if e.at else e.text)
                full = FEEDBACK_ENTRY_SEP.join(blocks)
            _e(
                conn,
                "UPDATE user_feedback SET body = ? WHERE user_id = ?",
                (full, user_id),
            )
        conn.commit()
    finally:
        conn.close()
    return user_id, ticket_id


def upsert_feedback_ticket_nlp(
    path: str,
    ticket_id: int,
    *,
    sentiment_label: str,
    sentiment_score: float,
    emotions_json: str,
    badges: str,
    error: str | None = None,
) -> None:
    """Результат TEI /predict для тикета обратной связи."""
    if not db_path_usable(path):
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = connect(path)
    try:
        if use_postgres():
            _e(
                conn,
                """
                INSERT INTO feedback_ticket_nlp (
                    ticket_id, sentiment_label, sentiment_score, emotions_json, badges, error, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (ticket_id) DO UPDATE SET
                    sentiment_label = excluded.sentiment_label,
                    sentiment_score = excluded.sentiment_score,
                    emotions_json = excluded.emotions_json,
                    badges = excluded.badges,
                    error = excluded.error,
                    analyzed_at = excluded.analyzed_at
                """,
                (
                    ticket_id,
                    sentiment_label,
                    sentiment_score,
                    emotions_json,
                    badges,
                    error,
                    now,
                ),
            )
        else:
            _e(
                conn,
                """
                INSERT INTO feedback_ticket_nlp (
                    ticket_id, sentiment_label, sentiment_score, emotions_json, badges, error, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                    sentiment_label = excluded.sentiment_label,
                    sentiment_score = excluded.sentiment_score,
                    emotions_json = excluded.emotions_json,
                    badges = excluded.badges,
                    error = excluded.error,
                    analyzed_at = excluded.analyzed_at
                """,
                (
                    ticket_id,
                    sentiment_label,
                    sentiment_score,
                    emotions_json,
                    badges,
                    error,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def get_feedback_nlp_by_ticket_ids(path: str, ticket_ids: list[int]) -> dict[int, FeedbackNlpRow]:
    if not db_path_usable(path) or not ticket_ids:
        return {}
    conn = connect(path)
    try:
        ph = ",".join("?" * len(ticket_ids))
        rows = _e(
            conn,
            f"""
            SELECT ticket_id, sentiment_label, sentiment_score, emotions_json, badges, error
            FROM feedback_ticket_nlp WHERE ticket_id IN ({ph})
            """,
            tuple(ticket_ids),
        ).fetchall()
    finally:
        conn.close()
    out: dict[int, FeedbackNlpRow] = {}
    for r in rows:
        tid = int(r[0])
        out[tid] = FeedbackNlpRow(
            ticket_id=tid,
            sentiment_label=str(r[1] or ""),
            sentiment_score=float(r[2] or 0.0),
            emotions_json=str(r[3] or "[]"),
            badges=str(r[4] or ""),
            error=str(r[5]) if r[5] is not None else None,
        )
    return out


def list_feedback_ticket_ids_for_user_recent_asc(path: str, user_id: int, limit: int) -> list[int]:
    """Последние limit тикетов пользователя по времени создания, в порядке от старых к новым (как фрагменты в user_feedback.body)."""
    if not db_path_usable(path) or limit <= 0:
        return []
    conn = connect(path)
    try:
        rows = _e(
            conn,
            """
            SELECT id FROM feedback_ticket WHERE user_id = ? ORDER BY id DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    finally:
        conn.close()
    ids = [int(r[0]) for r in rows]
    ids.reverse()
    return ids


def list_feedback_tickets_for_user(
    path: str,
    user_id: int,
    *,
    limit: int = 50,
) -> list[FeedbackTicketRow]:
    if not db_path_usable(path):
        return []
    conn = connect(path)
    try:
        rows = _e(
            conn,
            """
            SELECT id, user_id, username, body, status, staff_response, created_at, status_updated_at, archived
            FROM feedback_ticket WHERE user_id = ? AND archived = 0 ORDER BY id DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_feedback_ticket_row_from_fetch(r) for r in rows]


def get_feedback_ticket_by_id(path: str, ticket_id: int) -> FeedbackTicketRow | None:
    if not db_path_usable(path):
        return None
    conn = connect(path)
    try:
        r = _e(
            conn,
            """
            SELECT id, user_id, username, body, status, staff_response, created_at, status_updated_at, archived
            FROM feedback_ticket WHERE id = ?
            """,
            (ticket_id,),
        ).fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    return _feedback_ticket_row_from_fetch(r)


def list_feedback_tickets_for_admin_user(
    path: str,
    user_id: int,
    *,
    archived: bool = False,
    limit: int = 30,
) -> list[FeedbackTicketRow]:
    """Тикеты одного пользователя для админки. archived=False - только активные (на экране); archived=True - архив."""
    if not db_path_usable(path):
        return []
    conn = connect(path)
    try:
        a = 1 if archived else 0
        order = "DESC" if archived else "ASC"
        rows = _e(
            conn,
            f"""
            SELECT id, user_id, username, body, status, staff_response, created_at, status_updated_at, archived
            FROM feedback_ticket WHERE user_id = ? AND archived = ? ORDER BY id {order} LIMIT ?
            """,
            (user_id, a, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_feedback_ticket_row_from_fetch(r) for r in rows]


def count_feedback_tickets_for_admin_user(path: str, user_id: int, *, archived: bool) -> int:
    if not db_path_usable(path):
        return 0
    conn = connect(path)
    try:
        a = 1 if archived else 0
        row = _e(
            conn,
            "SELECT COUNT(*) FROM feedback_ticket WHERE user_id = ? AND archived = ?",
            (user_id, a),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def resolve_feedback_ticket(
    path: str,
    ticket_id: int,
    status: str,
    staff_response: str,
) -> bool:
    """Переводит pending -> reviewed/rejected. Возвращает True, если строка обновлена."""
    if status not in (FEEDBACK_TICKET_STATUS_REVIEWED, FEEDBACK_TICKET_STATUS_REJECTED):
        raise ValueError("invalid status")
    note = staff_response.strip()
    if not note:
        raise ValueError("empty staff_response")
    if len(note) > 4000:
        note = note[:4000]
    now = datetime.now(timezone.utc).isoformat()
    conn = connect(path)
    try:
        cur = _e(
            conn,
            """
            UPDATE feedback_ticket SET status = ?, staff_response = ?, status_updated_at = ?, archived = 1
            WHERE id = ? AND status = ?
            """,
            (status, note, now, ticket_id, FEEDBACK_TICKET_STATUS_PENDING),
        )
        n = int(getattr(cur, "rowcount", -1) or 0)
        conn.commit()
    finally:
        conn.close()
    return n > 0


def count_user_feedback(path: str) -> int:
    if not db_path_usable(path):
        return 0
    conn = connect(path)
    try:
        row = _e(conn, "SELECT COUNT(*) FROM user_feedback").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def list_user_feedback(path: str, *, limit: int, offset: int) -> list[UserFeedbackRow]:
    if not db_path_usable(path):
        return []
    conn = connect(path)
    try:
        rows = _e(
            conn,
            "SELECT user_id, username, body, created_at, updated_at FROM user_feedback "
            "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    finally:
        conn.close()
    out: list[UserFeedbackRow] = []
    for r in rows:
        out.append(
            UserFeedbackRow(
                user_id=int(r[0]),
                username=r[1],
                body=str(r[2]),
                created_at=str(r[3]),
                updated_at=str(r[4]),
            ),
        )
    return out


def get_user_feedback_by_user_id(path: str, uid: int) -> UserFeedbackRow | None:
    if not db_path_usable(path):
        return None
    conn = connect(path)
    try:
        r = _e(
            conn,
            "SELECT user_id, username, body, created_at, updated_at FROM user_feedback WHERE user_id = ?",
            (uid,),
        ).fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    return UserFeedbackRow(
        user_id=int(r[0]),
        username=r[1],
        body=str(r[2]),
        created_at=str(r[3]),
        updated_at=str(r[4]),
    )


def get_user_consent(path: str, user_id: int) -> UserConsentRow | None:
    if not db_path_usable(path):
        return None
    conn = connect(path)
    try:
        r = _e(
            conn,
            "SELECT user_id, disclaimer_version, quiz_passed_at, accepted_at "
            "FROM user_consent WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    qp = r[2]
    ac = r[3]
    return UserConsentRow(
        user_id=int(r[0]),
        disclaimer_version=int(r[1]),
        quiz_passed_at=str(qp) if qp else None,
        accepted_at=str(ac) if ac else None,
    )


def upsert_disclaimer_quiz_passed(path: str, user_id: int, version: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = connect(path)
    try:
        _e(
            conn,
            """
            INSERT INTO user_consent (user_id, disclaimer_version, quiz_passed_at, accepted_at)
            VALUES (?, ?, ?, NULL)
            ON CONFLICT(user_id) DO UPDATE SET
                disclaimer_version = excluded.disclaimer_version,
                quiz_passed_at = excluded.quiz_passed_at,
                accepted_at = NULL
            """,
            (user_id, version, now),
        )
        conn.commit()
    finally:
        conn.close()


def set_disclaimer_accepted(path: str, user_id: int, version: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = connect(path)
    try:
        # PRIMARY KEY = user_id; UPSERT по user_id, версию обновляем (новая версия = новый «приём»).
        _e(
            conn,
            """
            INSERT INTO user_consent (user_id, disclaimer_version, quiz_passed_at, accepted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                disclaimer_version = excluded.disclaimer_version,
                accepted_at = excluded.accepted_at,
                quiz_passed_at = COALESCE(user_consent.quiz_passed_at, excluded.quiz_passed_at)
            """,
            (user_id, version, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def consent_fully_accepted(row: UserConsentRow | None, required_version: int) -> bool:
    if row is None:
        return False
    return row.disclaimer_version == required_version and bool((row.accepted_at or "").strip())


def consent_awaiting_final_accept(row: UserConsentRow | None, required_version: int) -> bool:
    if row is None:
        return False
    return (
        row.disclaimer_version == required_version
        and bool((row.quiz_passed_at or "").strip())
        and not (row.accepted_at or "").strip()
    )


def upsert_user_poll(path: str, user_id: int, likes_math: int, career_text: str) -> None:
    if likes_math not in (0, 1):
        raise ValueError("likes_math must be 0 or 1")
    ct = career_text.strip()
    if not ct:
        raise ValueError("empty career_text")
    if len(ct) > 2000:
        ct = ct[:2000]
    now = datetime.now(timezone.utc).isoformat()
    conn = connect(path)
    try:
        _e(
            conn,
            """
            INSERT INTO user_poll (user_id, likes_math, career_text, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                likes_math = excluded.likes_math,
                career_text = excluded.career_text,
                updated_at = excluded.updated_at
            """,
            (user_id, likes_math, ct, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_poll(path: str, user_id: int) -> UserPollRow | None:
    if not db_path_usable(path):
        return None
    conn = connect(path)
    try:
        r = _e(
            conn,
            "SELECT user_id, likes_math, career_text, updated_at FROM user_poll WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    return UserPollRow(
        user_id=int(r[0]),
        likes_math=int(r[1]),
        career_text=str(r[2]),
        updated_at=str(r[3]),
    )


def count_user_poll_rows(path: str) -> int:
    if not db_path_usable(path):
        return 0
    conn = connect(path)
    try:
        row = _e(conn, "SELECT COUNT(*) FROM user_poll").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def poll_math_yes_no_counts(path: str) -> tuple[int, int]:
    """Число ответов Да / Нет по математике."""
    if not db_path_usable(path):
        return (0, 0)
    conn = connect(path)
    try:
        yes_r = _e(
            conn,
            "SELECT COUNT(*) FROM user_poll WHERE likes_math = 1",
        ).fetchone()
        no_r = _e(
            conn,
            "SELECT COUNT(*) FROM user_poll WHERE likes_math = 0",
        ).fetchone()
    finally:
        conn.close()
    return (int(yes_r[0]) if yes_r else 0, int(no_r[0]) if no_r else 0)


def upsert_check_result_vote(
    path: str,
    *,
    chat_id: int,
    message_id: int,
    user_id: int,
    vote: int,
) -> bool:
    """
    Сохранить оценку сообщения с результатом проверки (1 - лайк, -1 - дизлайк).
    Возвращает True, если запись создана или значение vote изменилось.
    """
    if vote not in (1, -1):
        raise ValueError("vote must be 1 or -1")
    if not db_path_usable(path):
        return False
    chat_s = str(chat_id)
    now = datetime.now(timezone.utc).isoformat()
    conn = connect(path)
    try:
        # Один запрос вместо SELECT + INSERT/UPDATE: RETURNING вернёт строку только если
        # запись фактически создана или vote реально поменялся (фильтр WHERE на DO UPDATE).
        # vote — NOT NULL, поэтому `<>` работает и в SQLite, и в PostgreSQL.
        # SQLite >= 3.35 и PostgreSQL >= 9.5 поддерживают INSERT ... RETURNING.
        row = _e(
            conn,
            """
            INSERT INTO check_result_vote (chat_id, message_id, user_id, vote, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (chat_id, message_id, user_id) DO UPDATE SET
                vote = excluded.vote,
                updated_at = excluded.updated_at
                WHERE check_result_vote.vote <> excluded.vote
            RETURNING vote
            """,
            (chat_s, message_id, user_id, vote, now),
        ).fetchone()
        conn.commit()
        return row is not None
    finally:
        conn.close()


def check_result_vote_totals(path: str) -> tuple[int, int]:
    """Текущие итоги: (лайки, дизлайки) по последнему голосу каждой пары (чат, сообщение, пользователь)."""
    if not db_path_usable(path):
        return (0, 0)
    conn = connect(path)
    try:
        up_r = _e(conn, "SELECT COUNT(*) FROM check_result_vote WHERE vote = 1").fetchone()
        down_r = _e(conn, "SELECT COUNT(*) FROM check_result_vote WHERE vote = -1").fetchone()
    finally:
        conn.close()
    return (int(up_r[0]) if up_r else 0, int(down_r[0]) if down_r else 0)


USER_BLOCK_TEMP = "temp"
USER_BLOCK_PERMANENT = "permanent"

CHECK_STICKER_KIND_REWARD = "reward"
CHECK_STICKER_KIND_MOTIVATION = "motivation"


def _parse_until_utc(s: str | None) -> datetime | None:
    if not s or not str(s).strip():
        return None
    raw = str(s).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def blocked_state(path: str, user_id: int) -> dict[str, Any] | None:
    """
    None - нет блока или истекла временная.
    Иначе {"kind": "temp"|"permanent", "reason": str|None, "until_utc": str|None}.
    Истёкший temp удаляется атомарно в той же транзакции, что и SELECT.
    """
    if not db_path_usable(path):
        return None
    conn = connect(path)
    try:
        row = _e(
            conn,
            "SELECT block_type, reason, until_utc FROM user_block WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        typ = str(row[0])
        reason = row[1]
        until_s = row[2]
        if typ == USER_BLOCK_TEMP:
            until_dt = _parse_until_utc(until_s)
            now = datetime.now(timezone.utc)
            if until_dt is None or now >= until_dt:
                # атомарно удаляем только этот же истёкший блок (ровно ту же строку),
                # чтобы не затереть свежепоставленный блок гонкой
                _e(
                    conn,
                    "DELETE FROM user_block WHERE user_id = ? AND block_type = ? "
                    "AND COALESCE(until_utc, '') = ?",
                    (user_id, USER_BLOCK_TEMP, until_s or ""),
                )
                conn.commit()
                return None
            return {
                "kind": USER_BLOCK_TEMP,
                "reason": (str(reason).strip() if reason else None) or None,
                "until_utc": until_dt.isoformat(),
            }
        if typ == USER_BLOCK_PERMANENT:
            return {"kind": USER_BLOCK_PERMANENT, "reason": None, "until_utc": None}
        return None
    finally:
        conn.close()


def _clear_user_block_row(path: str, user_id: int) -> None:
    conn = connect(path)
    try:
        _e(conn, "DELETE FROM user_block WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def clear_user_block(path: str, user_id: int) -> None:
    """Снять блок (админ или самопомощь)."""
    if not db_path_usable(path):
        return
    _clear_user_block_row(path, user_id)


def set_temp_user_block(path: str, user_id: int, reason: str, hours: float = 1.0) -> str:
    """Блок на hours часов. Возвращает until_utc ISO."""
    if not db_path_usable(path):
        return ""
    now = datetime.now(timezone.utc)
    until = now + timedelta(hours=float(hours))
    until_s = until.isoformat()
    created = now.isoformat()
    r = (reason or "").strip() or "не указана"
    conn = connect(path)
    try:
        _e(
            conn,
            """
            INSERT INTO user_block (user_id, block_type, reason, until_utc, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                block_type = excluded.block_type,
                reason = excluded.reason,
                until_utc = excluded.until_utc,
                created_at = excluded.created_at
            """,
            (user_id, USER_BLOCK_TEMP, r, until_s, created),
        )
        conn.commit()
    finally:
        conn.close()
    return until_s


def set_permanent_user_block(path: str, user_id: int) -> None:
    if not db_path_usable(path):
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = connect(path)
    try:
        _e(
            conn,
            """
            INSERT INTO user_block (user_id, block_type, reason, until_utc, created_at)
            VALUES (?, ?, NULL, NULL, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                block_type = excluded.block_type,
                reason = NULL,
                until_utc = NULL,
                created_at = excluded.created_at
            """,
            (user_id, USER_BLOCK_PERMANENT, now),
        )
        conn.commit()
    finally:
        conn.close()


def record_check_sticker_reward(
    path: str,
    user_id: int,
    kind: str,
    sticker_file_id: str | None,
    sticker_set_name: str | None,
    quip: str | None,
) -> None:
    """Стикер и/или ироничная фраза после проверки (награда или мотивация)."""
    if not db_path_usable(path):
        return
    k = (kind or "").strip()
    if k not in (CHECK_STICKER_KIND_REWARD, CHECK_STICKER_KIND_MOTIVATION):
        return
    fid = (sticker_file_id or "").strip() or None
    sn = (sticker_set_name or "").strip() or None
    q = (quip or "").strip() or None
    if q and len(q) > 4000:
        q = q[:3997] + "..."
    if not fid and not q:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = connect(path)
    try:
        _e(
            conn,
            """
            INSERT INTO check_sticker_reward (user_id, kind, sticker_file_id, sticker_set_name, quip, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, k, fid, sn, q, now),
        )
        conn.commit()
    finally:
        conn.close()
