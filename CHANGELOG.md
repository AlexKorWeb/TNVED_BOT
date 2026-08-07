# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [SemVer](https://semver.org/lang/ru/).

## [Unreleased]

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

### Fixed

- Пример кода в ТЗ: `3917 23 100 0` не существует в номенклатуре, заменён на реальный
  `3917 23 100 9`. Найдено сверкой с загруженным справочником.

## [0.1.0] — 2026-08-07

### Added

- Инициализация проекта TNVED_BOT.
