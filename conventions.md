# Conventions — TNVED_BOT

Соглашения по коду для Python 3.12+ / aiogram 3 / SQLite. Обязательны для `implementer`,
проверяются `reviewer`.

---

## 1. Архитектура

### Слои (зависимости идут строго вниз)

```text
bot/        — транспорт: aiogram, хендлеры, клавиатуры, тексты
   ↓
core/       — бизнес-логика: оркестрация, санитизация, гейт уверенности
   ↓
llm/  db/  storage/   — адаптеры: CLI claude, SQLite, файловая система
```

**Правила слоёв:**

- `bot/` **не знает** о SQLite и о `claude`. Он вызывает `core.classifier` и форматирует ответ.
- `core/` **не импортирует** `aiogram`. Ни `Message`, ни `CallbackQuery` внутрь `core` не попадают —
  только собственные dataclass'ы из `core/models.py`.
- `llm/`, `db/`, `storage/` не импортируют друг друга и не импортируют `core/`.
- Единственная точка сборки зависимостей — `bot/setup.py` и `__main__.py`.

### Поток данных (текстовый запрос)

```text
Message
  → middlewares (auth → ratelimit → logging)
  → handlers/text.py
  → core.sanitize.clean_user_text()        # → SanitizedText | RejectReason
  → core.classifier.classify()
        ├→ db.nomenclature.search()        # FTS5 → list[Candidate]
        ├→ llm.client.run_json()           # claude -p → dict
        ├→ llm.schema.parse_classify()     # → ClassifyResult (валидированный)
        └→ db.nomenclature.verify_code()   # обязательная сверка каждого кода
  → core.confidence.decide()               # → Answer | Clarification
  → bot/texts.py + keyboards.py            # форматирование + экранирование
  → Telegram
```

**Инвариант, который нельзя нарушать:** между `llm` и отправкой пользователю всегда стоит
`db.nomenclature.verify_code()`. Новый код-путь без этой сверки — блокирующее замечание на ревью.

---

## 2. Кодстайл

- **Форматирование:** `ruff format` (аналог black), длина строки **100**.
- **Линтер:** `ruff check` с правилами `E, F, W, I, N, UP, B, ASYNC, S, A, C4, DTZ, T20, RET, SIM, PTH`.
  - `S` (bandit) — обязательно, это проект с trust boundary.
  - `T20` — никаких `print()` в `src/`, только логгер.
  - `PTH` — работа с путями только через `pathlib`.
- **Типизация:** аннотации обязательны для всех публичных функций. `mypy` в режиме
  `strict = true` для `src/tnved_bot/core` и `src/tnved_bot/llm`, обычный режим для остального.
- **Именование:**
  - модули и функции — `snake_case`; классы — `PascalCase`; константы — `UPPER_SNAKE`
  - приватное — с `_` префиксом; хендлеры — `handle_<что>`; middleware — `<Что>Middleware`
  - булевы — `is_/has_/should_`; функции с побочным эффектом — глагол (`save_photo`, не `photo`)
- **Async:**
  - Всё I/O — асинхронное. `time.sleep`, синхронный `sqlite3`, `requests` — запрещены.
  - CPU-bound (обработка изображений `Pillow`) — через `asyncio.to_thread`.
  - Фоновые задачи — только через `apscheduler` или `asyncio.TaskGroup`; «голый»
    `asyncio.create_task` без хранения ссылки запрещён (задача может быть собрана GC).
  - Любой `await` на внешнем ресурсе обёрнут в `asyncio.timeout`.
- **Ошибки:**
  - Собственная иерархия в `core/errors.py`: `TnvedError` → `UserInputError`, `LlmError`,
    `NomenclatureError`, `StorageError`.
  - `except Exception` допустим только в `handlers/errors.py` и в джаниторе — и обязан логировать.
  - `except: pass` запрещён везде.
- **Строки:** f-strings. Для логов — структурные поля (`log.info("event", user_id=...)`),
  а не форматирование сообщения.
- **Даты:** только timezone-aware UTC (`datetime.now(UTC)`). Naive-datetime запрещены (`DTZ`).
- **Комментарии:** объясняют «почему», а не «что». Комментарий длиннее объясняемого кода —
  признак, что код надо упростить. Намеренный срез угла помечается `# ponytail:`.

---

## 3. Структура файлов

```text
src/tnved_bot/
├── __main__.py                # точка входа: конфиг, лок, БД, планировщик, polling
├── config.py                  # pydantic-settings, валидация при старте
├── logging_setup.py           # structlog + ротация
├── bot/
│   ├── setup.py               # Dispatcher, регистрация роутеров и middleware
│   ├── keyboards.py           # inline-клавиатуры (уточнения, фидбек)
│   ├── texts.py               # ВСЕ пользовательские тексты (RU) — одно место
│   ├── middlewares/
│   │   ├── auth.py            # whitelist, первый в цепочке
│   │   ├── ratelimit.py       # лимиты час/сутки/глобально
│   │   └── logging.py         # request_id, латентность
│   └── handlers/
│       ├── commands.py        # /start /help /cancel /code /version /health /stats /forget
│       ├── text.py            # текстовое описание товара
│       ├── photo.py           # фото и документ-изображение
│       ├── callbacks.py       # нажатия кнопок уточнений и фидбека
│       └── errors.py          # глобальный error handler
├── core/
│   ├── classifier.py          # оркестратор пайплайна
│   ├── session.py             # состояние диалога, раунды уточнений, таймаут
│   ├── sanitize.py            # L1–L2 защиты от инъекций
│   ├── confidence.py          # гейт: ответ / уточнение / деградация
│   ├── models.py              # Candidate, ClassifyResult, Answer, Clarification
│   └── errors.py              # иерархия исключений
├── llm/
│   ├── client.py              # subprocess claude -p, семафор, таймаут, ретраи, breaker
│   ├── schema.py              # pydantic-схемы ответа модели + извлечение JSON
│   └── prompts/               # vision.md, classify.md, clarify.md
├── db/
│   ├── engine.py              # пул соединений, WAL, миграции
│   ├── schema.sql
│   ├── nomenclature.py        # FTS-поиск, verify_code, версии справочника
│   ├── sessions.py
│   ├── photos.py
│   ├── audit.py
│   └── counters.py            # rate limit
├── storage/
│   └── photo_store.py         # приём, валидация, ре-энкод, TTL-удаление
└── jobs/
    └── janitor.py             # фото 48 ч, сессии 30 мин, аудит 90 дн, бэкапы
```

**Правила размера:** файл > 300 строк или функция > 50 строк — повод разделить, но не самоцель.
Не плодить модули ради «красоты».

---

## 4. Тесты

- **Расположение:** `tests/`, зеркалит `src/` (`tests/core/test_sanitize.py`).
- **Инструменты:** `pytest`, `pytest-asyncio` (`asyncio_mode = auto`), `pytest-cov`.
- **Типы:**
  - `unit` — санитизация, парсинг JSON модели, гейт уверенности, FTS-запрос, TTL. Без I/O.
  - `integration` — пайплайн с замоканным `llm.client` и временной БД (`tmp_path`).
  - `security` — `tests/test_sanitize.py` + `tests/test_security.py`: инъекции, path traversal,
    polyglot-файлы, FTS-спецсимволы. **Этот набор нельзя ослаблять** — только дополнять.
- **Фикстуры:** общие в `tests/conftest.py` — временная БД со срезом справочника (~200 кодов),
  фейковый `llm.client`, фабрика aiogram-объектов.
- **Запрещено в тестах:** реальные вызовы `claude`, реальный Telegram API, сеть, `sleep` для
  синхронизации (использовать `freezegun`/подмену времени).
- **Покрытие:** ≥ 85 % для `core/` и `llm/`, ≥ 70 % общее. Метрика — не самоцель; непокрытая
  ветка обработки ошибок — блокирующее замечание.

---

## 5. Git

- **Ветки:** `main` — рабочая. Фича-ветки `feat/T-007-photo-pipeline`, `fix/T-012-janitor-lock`.
- **Коммиты:** Conventional Commits, на английском, тело — при необходимости на русском.
  ```text
  feat(llm): add claude -p client with timeout and circuit breaker

  Реализует T-005. Семафор на 2 параллельных вызова, ретраи 2/6 с,
  breaker открывается после 5 подряд неудач на 5 минут.
  ```
  Типы: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `security`.
- **Один тикет — один коммит** (или несколько атомарных, но без смешивания фичи и рефакторинга).
- **Перед коммитом:** `ruff check` + `mypy` + `pytest` зелёные.
- **Запрещено коммитить:** `.env`, `data/`, `logs/`, `.venv/`, любые токены.
  Перед `git add` — проверка `git diff --staged | Select-String "BOT_TOKEN|api_key|token="`.
- **PR:** описание = что, зачем, как проверялось. Обязательна ссылка на тикет.

---

## 6. Зависимости

- Файл `requirements.txt` — прямые зависимости с точными версиями (`==`).
  `requirements.lock` — полное дерево (`pip freeze`).
- **Новая зависимость добавляется только если:** stdlib не справляется, нативной возможности нет,
  и уже установленная библиотека не решает задачу. Обоснование — в описании коммита.
- Утверждённый список: `aiogram`, `aiosqlite`, `pydantic`, `pydantic-settings`, `Pillow`,
  `apscheduler`, `structlog`. Всё сверх — через обсуждение.
- Обновление зависимостей — отдельным `chore`-коммитом, никогда вместе с фичей.

---

## 7. Запрещённые паттерны

| Паттерн | Почему | Вместо этого |
|---|---|---|
| `subprocess.run(cmd, shell=True)` | shell-инъекция | `asyncio.create_subprocess_exec(*args)` |
| Пользовательский текст в argv `claude` | утечка в логи процессов, инъекция | передача через stdin |
| `f"SELECT ... WHERE code = '{code}'"` | SQL-инъекция | параметризованный запрос `?` |
| Сырой пользовательский ввод в FTS-запрос | FTS5-спецсимволы ломают запрос | whitelist-токенизация + экранирование |
| Отправка кода без `verify_code()` | галлюцинация уйдёт пользователю | обязательная сверка с БД |
| `parse_mode=Markdown` для текста модели | инъекция разметки, падение парсера | HTML + `html.escape()` |
| `except Exception: pass` | молчаливая потеря ошибки | логировать + деградировать |
| Синхронный `sqlite3` / `time.sleep` в async | блокировка event loop | `aiosqlite`, `asyncio.sleep` |
| `os.path` + конкатенация путей | path traversal | `pathlib` + `resolve()` + проверка родителя |
| Хранение фото без `expires_at` | нарушение TTL 48 ч | запись в `photos` с TTL при сохранении |
| Глобальные мутабельные синглтоны | тесты становятся зависимыми | явная передача зависимостей |
| Абстракция с одной реализацией | оверинжиниринг | конкретный класс, интерфейс — когда появится вторая |
| Код «на будущее», спекулятивные фичи | YAGNI | реализуй только тикет |
| Тексты сообщений, размазанные по хендлерам | нельзя вычитать и локализовать | `bot/texts.py` |
