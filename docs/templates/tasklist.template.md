---
type: tasklist
ticket: T-XXX
timestamp: YYYY-MM-DD
---

# Список задач — T-XXX: <название>

**План:** [[../plan/T-XXX|docs/plan/T-XXX.md]]
**PRD:** [[../prd/T-XXX|docs/prd/T-XXX.md]]

## Реализация

- [ ] <шаг 1> → `src/tnved_bot/<файл>.py`
- [ ] <шаг 2> → `src/tnved_bot/<файл>.py`
- [ ] <шаг 3>

## Тесты

- [ ] happy path → `tests/<файл>.py::test_<имя>`
- [ ] ветка ошибки: <какая> → `tests/<файл>.py::test_<имя>`
- [ ] инъекционные кейсы (если есть пользовательский ввод) → `tests/test_sanitize.py`

## Проверки перед закрытием

- [ ] `ruff check src tests` — чисто
- [ ] `mypy src` — чисто
- [ ] `pytest -q` — зелёный
- [ ] `/local-test` — бот стартует и отвечает
- [ ] Ни один путь не отдаёт код ТН ВЭД без `verify_code()`
- [ ] Секретов в diff нет
- [ ] `CHANGELOG.md` обновлён

## Заметки

<Решения, принятые по ходу; отклонения от плана и их причина.>
