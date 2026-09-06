-- Первичная схема PostgreSQL для tgzh (профили, отзывы, согласия, опрос, статистика).
-- Применение: psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/postgres_schema.sql
-- Либо: alembic upgrade head (ревизия вызывает тот же SQL).

CREATE TABLE IF NOT EXISTS user_profile (
    user_id BIGINT NOT NULL,
    subject_slug TEXT NOT NULL DEFAULT 'matematika',
    grade INTEGER NOT NULL CHECK (grade >= 6 AND grade <= 11),
    textbook_slug TEXT NOT NULL,
    textbook_url TEXT NOT NULL,
    textbook_label TEXT NOT NULL,
    is_premium INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    hw_paragraph TEXT,
    hw_exercise TEXT,
    hw_page INTEGER,
    PRIMARY KEY (user_id, subject_slug)
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id BIGINT PRIMARY KEY NOT NULL,
    active_subject_slug TEXT NOT NULL DEFAULT 'matematika',
    updated_at TEXT NOT NULL
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

-- /chat: пароль (CHAT_PASSWORD/ADMIN_PASSWORD) → запись сессии на CHAT_SESSION_TTL_DAYS суток.
CREATE TABLE IF NOT EXISTS chat_session (
    user_id BIGINT PRIMARY KEY NOT NULL,
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- /begemot: пароль ADMIN_PASSWORD → сессия на ADMIN_SESSION_TTL_DAYS суток.
-- Отдельная таблица от chat_session, чтобы logout одного раздела не выкидывал
-- из другого, и чтобы пароли можно было разводить.
CREATE TABLE IF NOT EXISTS admin_session (
    user_id BIGINT PRIMARY KEY NOT NULL,
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- /chat: до 10 сохранённых диалогов на пользователя (история {role, content} в JSON).
-- Прорежается при insert (помещается всё, что больше 10 — старейшие удаляются).
CREATE TABLE IF NOT EXISTS chat_dialog (
    dialog_id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    history_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_dialog_user_updated
    ON chat_dialog (user_id, updated_at DESC);

-- Настройки/счётчики «сна памяти» в /chat (одна строка на пользователя).
CREATE TABLE IF NOT EXISTS chat_memory_pref (
    user_id BIGINT PRIMARY KEY NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    msgs_since_sleep INTEGER NOT NULL DEFAULT 0,
    last_sleep_at TEXT,
    last_sleep_status TEXT,
    chat_model_slug TEXT,
    updated_at TEXT NOT NULL
);
