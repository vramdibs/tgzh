-- Первичная схема PostgreSQL для tgzh (профили, отзывы, согласия, опрос, статистика).
-- Применение: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/postgres_schema.sql
-- Либо: alembic upgrade head (ревизия вызывает тот же SQL).

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
);

CREATE TABLE IF NOT EXISTS user_feedback (
    user_id BIGINT PRIMARY KEY NOT NULL,
    username TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_consent (
    user_id BIGINT PRIMARY KEY NOT NULL,
    disclaimer_version INTEGER NOT NULL,
    quiz_passed_at TEXT,
    accepted_at TEXT
);

CREATE TABLE IF NOT EXISTS user_poll (
    user_id BIGINT PRIMARY KEY NOT NULL,
    likes_math INTEGER NOT NULL CHECK (likes_math IN (0, 1)),
    career_text TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

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
);

CREATE TABLE IF NOT EXISTS feedback_ticket_nlp (
    ticket_id BIGINT PRIMARY KEY NOT NULL,
    sentiment_label TEXT NOT NULL DEFAULT '',
    sentiment_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    emotions_json TEXT NOT NULL DEFAULT '[]',
    badges TEXT NOT NULL DEFAULT '',
    error TEXT,
    analyzed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_stats_by_year (
    academic_year INTEGER NOT NULL PRIMARY KEY,
    photos_uploaded INTEGER NOT NULL DEFAULT 0,
    checks_completed INTEGER NOT NULL DEFAULT 0,
    checks_technical_failed INTEGER NOT NULL DEFAULT 0,
    verdict_correct INTEGER NOT NULL DEFAULT 0,
    verdict_partial INTEGER NOT NULL DEFAULT 0,
    verdict_absent INTEGER NOT NULL DEFAULT 0,
    verdict_percent_sum INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bot_user_visit_day (
    user_id BIGINT NOT NULL,
    visit_day TEXT NOT NULL,
    PRIMARY KEY (user_id, visit_day)
);

CREATE TABLE IF NOT EXISTS check_result_vote (
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    user_id BIGINT NOT NULL,
    vote INTEGER NOT NULL CHECK (vote IN (1, -1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id, user_id)
);

CREATE TABLE IF NOT EXISTS user_block (
    user_id BIGINT PRIMARY KEY NOT NULL,
    block_type TEXT NOT NULL CHECK (block_type IN ('temp', 'permanent')),
    reason TEXT,
    until_utc TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS check_sticker_reward (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('reward', 'motivation')),
    sticker_file_id TEXT,
    sticker_set_name TEXT,
    quip TEXT,
    created_at TEXT NOT NULL
);
