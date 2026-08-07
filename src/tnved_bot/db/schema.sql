-- Схема БД TNVED_BOT. Применяется идемпотентно при каждом старте.
-- Версия схемы хранится в PRAGMA user_version (см. SCHEMA_VERSION в engine.py).
--
-- Все временные метки — строки ISO 8601 в UTC ('2026-08-07T11:22:33.444444+00:00').
-- Так они корректно сравниваются лексикографически, что позволяет отбирать просроченные
-- записи обычным `WHERE expires_at < ?` без функций даты.

-- ---------------------------------------------------------------- справочник ТН ВЭД

CREATE TABLE IF NOT EXISTS nomenclature_version (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,          -- имя файла-источника
    sha256      TEXT    NOT NULL,          -- контроль повторного импорта того же файла
    source_date TEXT,                      -- дата актуальности, заявленная источником
    imported_at TEXT    NOT NULL,
    rows        INTEGER NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1))
);

-- Активная версия может быть только одна. Это гарантия на уровне БД, а не соглашение
-- в коде: рассинхрон здесь означал бы выдачу кодов из неактуального справочника.
CREATE UNIQUE INDEX IF NOT EXISTS idx_version_single_active
    ON nomenclature_version (is_active) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS nomenclature (
    code        TEXT    NOT NULL,          -- только цифры, без пробелов
    parent_code TEXT,
    level       INTEGER NOT NULL CHECK (level IN (2, 4, 6, 8, 10)),
    name        TEXT    NOT NULL,          -- последний сегмент пути
    name_full   TEXT    NOT NULL,          -- полный путь иерархии
    unit        TEXT,                      -- доп. единица измерения (может отсутствовать)
    notes       TEXT,                      -- примечания к группе/позиции
    tariff      TEXT,                      -- ставка пошлины; в v1 не используется
    version_id  INTEGER NOT NULL REFERENCES nomenclature_version (id) ON DELETE CASCADE,
    PRIMARY KEY (code, version_id)
);

CREATE INDEX IF NOT EXISTS idx_nomenclature_version ON nomenclature (version_id);
CREATE INDEX IF NOT EXISTS idx_nomenclature_parent ON nomenclature (parent_code, version_id);

-- Полнотекстовый индекс. Наполняется импортёром (T-003), используется поиском (T-004).
-- Хранит только активную версию: держать все версии в индексе незачем, а поиск по чужой
-- версии выдал бы неактуальные коды.
CREATE VIRTUAL TABLE IF NOT EXISTS nomenclature_fts USING fts5 (
    code UNINDEXED,
    name,
    name_full,
    notes,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- ---------------------------------------------------------------- доступ

CREATE TABLE IF NOT EXISTS allowed_users (
    user_id      INTEGER PRIMARY KEY,
    username     TEXT,                       -- только для отображения, НЕ для авторизации
    note         TEXT,
    added_by     INTEGER NOT NULL,           -- user_id админа; 0 = активация по коду
    added_at     TEXT    NOT NULL,
    last_seen_at TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS invite_codes (
    code       TEXT PRIMARY KEY,
    note       TEXT,
    created_by INTEGER NOT NULL,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL,
    used_by    INTEGER,                      -- NULL пока не активирован
    used_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_invite_unused
    ON invite_codes (expires_at) WHERE used_by IS NULL;

-- ---------------------------------------------------------------- данные пользователей

CREATE TABLE IF NOT EXISTS photos (
    id         TEXT    PRIMARY KEY,          -- UUID4, он же имя файла
    user_id    INTEGER NOT NULL,
    path       TEXT    NOT NULL,
    sha256     TEXT    NOT NULL,
    bytes      INTEGER NOT NULL,
    created_at TEXT    NOT NULL,
    expires_at TEXT    NOT NULL,             -- created_at + PHOTO_TTL_HOURS
    deleted_at TEXT                          -- проставляется после удаления файла с диска
);

CREATE INDEX IF NOT EXISTS idx_photos_pending_delete
    ON photos (expires_at) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_photos_user ON photos (user_id);

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT    PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    chat_id         INTEGER NOT NULL,
    state           TEXT    NOT NULL
                    CHECK (state IN ('collecting', 'clarifying', 'done', 'expired')),
    description     TEXT,
    photo_id        TEXT    REFERENCES photos (id) ON DELETE SET NULL,
    answers_json    TEXT    NOT NULL DEFAULT '[]',
    candidates_json TEXT,
    round           INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    expires_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_open
    ON sessions (expires_at) WHERE state IN ('collecting', 'clarifying');
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id, updated_at);

-- ---------------------------------------------------------------- аудит и лимиты

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    user_id      INTEGER,
    event        TEXT    NOT NULL,
    payload_json TEXT,                       -- без сырого текста пользователя, только SHA-256
    latency_ms   INTEGER,
    ok           INTEGER NOT NULL DEFAULT 1 CHECK (ok IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts);
CREATE INDEX IF NOT EXISTS idx_audit_user_event ON audit_log (user_id, event);

CREATE TABLE IF NOT EXISTS usage_counters (
    user_id      INTEGER NOT NULL,           -- 0 = глобальный счётчик
    kind         TEXT    NOT NULL CHECK (kind IN ('hour', 'day')),
    window_start TEXT    NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, kind, window_start)
);

CREATE INDEX IF NOT EXISTS idx_counters_window ON usage_counters (window_start);
