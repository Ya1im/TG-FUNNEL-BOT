-- Все временные метки — целые unix-секунды (UTC).

CREATE TABLE IF NOT EXISTS users (
    tg_id            INTEGER PRIMARY KEY,
    username         TEXT,
    first_name       TEXT,
    started_at       INTEGER NOT NULL,
    source           TEXT,
    status           TEXT    NOT NULL DEFAULT 'active',  -- active | blocked | stopped
    is_subscribed    INTEGER NOT NULL DEFAULT 0,
    sub_checked_at   INTEGER,
    material_sent_at INTEGER,
    funnel_started_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
CREATE INDEX IF NOT EXISTS idx_users_source ON users(source);

CREATE TABLE IF NOT EXISTS media (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    slug           TEXT    NOT NULL UNIQUE,
    kind           TEXT    NOT NULL,   -- photo|video|video_note|document|audio|voice|animation|sticker
    file_id        TEXT    NOT NULL,
    file_unique_id TEXT,
    caption        TEXT,
    created_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS funnel_steps (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    position              INTEGER NOT NULL,
    delay_seconds         INTEGER NOT NULL,
    requires_subscription INTEGER NOT NULL DEFAULT 0,
    on_unsub              TEXT    NOT NULL DEFAULT 'skip',  -- skip | remind: что делать, если не подписан
    text                  TEXT,
    media_id              INTEGER REFERENCES media(id) ON DELETE SET NULL,
    buttons_json          TEXT,
    enabled               INTEGER NOT NULL DEFAULT 1,
    created_at            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_funnel_position ON funnel_steps(position);

CREATE TABLE IF NOT EXISTS material_blocks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    position     INTEGER NOT NULL,
    text         TEXT,
    media_id     INTEGER REFERENCES media(id) ON DELETE SET NULL,
    buttons_json TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL
);

-- То же самое, что material_blocks, но для ответа на повторный /start:
-- админ собирает произвольную последовательность сообщений (текст/медиа/кнопки)
-- вместо одного фиксированного текста.
CREATE TABLE IF NOT EXISTS repeat_start_blocks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    position     INTEGER NOT NULL,
    text         TEXT,
    media_id     INTEGER REFERENCES media(id) ON DELETE SET NULL,
    buttons_json TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_steps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    step_id    INTEGER NOT NULL,
    due_at     INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'pending',  -- pending | sent | skipped | failed
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    sent_at    INTEGER,
    UNIQUE(user_id, step_id)
);
CREATE INDEX IF NOT EXISTS idx_user_steps_due ON user_steps(status, due_at);

CREATE TABLE IF NOT EXISTS broadcasts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    INTEGER NOT NULL,
    created_by    INTEGER,
    segment       TEXT    NOT NULL DEFAULT 'all',
    segment_value TEXT,
    messages_json TEXT    NOT NULL,   -- [{"chat_id":..,"message_id":..}]
    scheduled_at  INTEGER,
    status        TEXT    NOT NULL DEFAULT 'draft',  -- draft|queued|running|done|cancelled
    sub_mode      TEXT    NOT NULL DEFAULT 'off',    -- off | skip | remind: проверка подписки перед отправкой
    started_at    INTEGER,
    finished_at   INTEGER
);

CREATE TABLE IF NOT EXISTS broadcast_targets (
    broadcast_id INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',  -- pending|sent|blocked|failed|skipped|reminded
    error        TEXT,
    PRIMARY KEY (broadcast_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_targets_pending ON broadcast_targets(broadcast_id, status);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
