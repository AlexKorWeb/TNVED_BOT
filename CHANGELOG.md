# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [SemVer](https://semver.org/lang/ru/).

## [Unreleased]

### Added — T-002 Слой БД

- `db/schema.sql` — 9 таблиц: справочник и его версии, FTS5-индекс, `allowed_users`,
  `invite_codes`, `photos`, `sessions`, `audit_log`, `usage_counters`. Единственность
  активной версии справочника обеспечена уникальным частичным индексом, состояния и
  перечисления — `CHECK`-ограничениями.
- `db/engine.py` — соединение `aiosqlite` с WAL, `foreign_keys=ON`, повторами при
  `database is locked` (0.1/0.3/0.9 с), транзакциями, горячим бэкапом и восстановлением
  после повреждения файла.
- `db/audit.py` — журнал событий; отказывается принимать поля с текстом пользователя,
  вместо содержимого — `text_digest()`.
- `db/counters.py` — счётчики лимитов в БД (переживают перезапуск), инкремент через
  `UPSERT ... RETURNING` без гонок.
- `db/photos.py`, `db/sessions.py` — учёт фото с обязательным TTL и состояние диалогов.
- `clock.py` — единая точка времени (UTC), чтобы тесты TTL могли подменять его.
- БД подключается при старте: событие `bot_started` теперь содержит версию схемы.
- Тестов стало 76, покрытие 93 %. Отчёт: [reports/qa/T-002.md](reports/qa/T-002.md).

### Fixed — T-002

- **Восстановление после повреждения БД падало с `PermissionError`:** соединение не
  закрывалось при ошибке открытия, и Windows не давал переименовать битый файл.
  Повреждение вылезает уже на первой `PRAGMA`, когда объект соединения создан.
- **Бэкапы внутри одной секунды перезаписывали друг друга** — в имя добавлены миллисекунды,
  иначе хранилось меньше копий, чем заказано.

### Added — T-001 Каркас проекта, конфигурация, логирование

- `config.py` — конфигурация на `pydantic-settings`, 34 переменные, валидация при старте.
  `ADMIN_USER_IDS` разбирается из CSV собственным валидатором: составные типы
  `pydantic-settings` читает из окружения как JSON, и `ADMIN_USER_IDS=123,456` упал бы.
  Ошибки конфигурации выводятся человекочитаемым текстом на русском, без traceback.
- `logging_setup.py` — `structlog` + JSON + ротация 10 МБ × 5, маскирование секретов
  на двух уровнях (процессор structlog и `MaskingFormatter` на handler'ах).
- `core/errors.py` — иерархия `TnvedError` с полем `user_message`.
- `lockfile.py` — защита от второй копии через блокировку ОС (`msvcrt` / `fcntl`).
  Лок и PID разнесены по разным файлам: `bot.lock` и `bot.pid`.
- `__main__.py` — старт, graceful shutdown по SIGINT/SIGTERM/SIGBREAK, коды выхода
  0/1/2/3.
- `scripts/smoke_check.py` — проверка запущенного бота (процесс, `claude`, лог, диск,
  справочник).
- `pyproject.toml` — ruff (14 наборов правил), mypy (strict для `core/` и `llm/`), pytest.
- 44 теста, покрытие 93 %. Отчёт: [reports/qa/T-001.md](reports/qa/T-001.md).

### Fixed — T-001 (найдено тестами и локальной проверкой)

- **Утечка токена в лог из URL:** `\b` в шаблоне маскирования не срабатывал на
  `.../bot<token>/getMe` — именно в таком виде aiogram логирует запросы к Telegram API.
- **Утечка токена в лог из traceback:** `logger.exception()` дописывал traceback мимо
  процессоров structlog. Добавлен `MaskingFormatter` на уровне handler'ов.
- **PID был нечитаем при живом боте:** обязательная блокировка Windows не давала
  прочитать `bot.lock`, где лежал PID. Файлы разделены.
- **Устаревший PID мог привести к убийству чужого процесса:** `bot.pid` удаляется при
  штатной остановке, перед `Stop-Process` требуется проверка `CommandLine`.
- **`already_running` логировался как ERROR** — понижено до WARNING: это штатный исход
  при автоперезапуске планировщиком.

### Added

- Техническое задание: [docs/TZ.md](docs/TZ.md) — архитектура, пайплайн классификации,
  7 слоёв защиты от prompt-инъекций, матрица обработки ошибок, TTL данных, автозапуск.
- Последовательность из 19 тикетов: [docs/tasklist/tickets.md](docs/tasklist/tickets.md).
- Проектная заготовка AIDD: 7 агентов (`.claude/agents/`), 7 slash-команд (`.claude/commands/`),
  4 набора правил (`.claude/rules/`), шаблоны PRD и tasklist.
- `CLAUDE.md`, `conventions.md`, `README.md`, `.env.example`, `.gitignore`.
- Разведка источников справочника: [docs/research/nomenclature-sources.md](docs/research/nomenclature-sources.md).
  Подтверждена XLSX-выгрузка TWS.BY (13 293 кода уровня 10, 96 групп, 0 дубликатов);
  файл скачан в `data/nomenclature/` (в git не попадает).

### Changed

- Модель доступа: `ALLOWED_USER_IDS` → `ADMIN_USER_IDS` (только администраторы, аварийный вход).
  Обычные пользователи вынесены в таблицу `allowed_users` и управляются командами бота
  (`/users`, `/adduser`, `/deluser`, `/invite`) без перезапуска. Добавлен тикет **T-010a**
  и таблицы `allowed_users` / `invite_codes` в T-002.

### Fixed

- Пример кода в ТЗ: `3917 23 100 0` не существует в номенклатуре, заменён на реальный
  `3917 23 100 9`. Найдено сверкой с загруженным справочником.

## [0.1.0] — 2026-08-07

### Added

- Инициализация проекта TNVED_BOT.
