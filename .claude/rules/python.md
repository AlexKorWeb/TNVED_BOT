---
description: Правила Python / asyncio для TNVED_BOT
globs: src/**/*.py, scripts/**/*.py
---

# Python / asyncio — TNVED_BOT

## Слои и зависимости

`bot/ → core/ → llm/, db/, storage/`

- `core/` **не импортирует** `aiogram`. Объекты `Message`/`CallbackQuery` внутрь `core` не попадают.
- Адаптеры (`llm/`, `db/`, `storage/`) не импортируют `core/` и друг друга.
- Сборка зависимостей — только в `bot/setup.py` и `__main__.py`.

## Async

- Всё I/O асинхронное. Запрещены: `time.sleep`, синхронный `sqlite3`, `requests`, блокирующее
  чтение файлов в хендлерах.
- CPU-bound (`Pillow`) — через `asyncio.to_thread`.
- Каждый внешний вызов обёрнут в `asyncio.timeout`.
- Фоновая задача либо в `apscheduler`, либо в `asyncio.TaskGroup`, либо ссылка на task сохранена.
  «Голый» `asyncio.create_task(...)` без сохранения ссылки запрещён — GC может её собрать.

## Типы и стиль

- Аннотации обязательны на публичных функциях. `mypy --strict` для `core/` и `llm/`.
- `ruff format`, длина строки 100.
- Только timezone-aware UTC: `datetime.now(UTC)`. Naive-datetime запрещены.
- Пути — только `pathlib`. Никакого `os.path` + конкатенации.
- Логи — `structlog` со структурными полями: `log.info("llm_call", user_id=uid, latency_ms=ms)`.
  `print()` в `src/` запрещён.

## Ошибки

- Иерархия в `core/errors.py`: `TnvedError` → `UserInputError`, `LlmError`, `NomenclatureError`,
  `StorageError`.
- `except Exception` допустим только в `bot/handlers/errors.py` и в `jobs/janitor.py`,
  и обязан логировать с `error_id`.
- `except: pass` запрещён везде. Любое подавление ошибки логируется.
- Пользователю — человекочитаемый русский текст + `error_id`. Traceback только в лог.

## Тексты

Все пользовательские строки — в `bot/texts.py`. Хардкод текста в хендлере — замечание на ревью.

## Ponytail

Перед новым классом/абстракцией/зависимостью — пройти Decision Ladder из
`.claude/rules/ponytail.md`. Интерфейс с одной реализацией не создавать.
