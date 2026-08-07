---
description: Прогнать автотесты и проверить покрытие (агент qa)
---

# /test — автотесты

Вызови агента `qa` для активного тикета.

Команды, которые должны быть выполнены и чей вывод приложен целиком:

```powershell
.\.venv\Scripts\Activate.ps1
pytest -q
pytest --cov=src/tnved_bot --cov-report=term-missing
pytest tests\test_sanitize.py tests\test_security.py -v
ruff check src tests
mypy src
```

Требования:

- Результат фиксируется честно: упавшие тесты перечисляются с выводом.
- Набор защиты от инъекций прогоняется явно и отдельно.
- Непокрытые ветки обработки ошибок в изменённых файлах перечисляются поимённо.
- Отчёт сохраняется в `reports/qa/<ticket-id>.md`.

При `FAIL` — вернись к `/implement` с описанием бага, не переходи дальше.

Следующий шаг при `PASS`: `/local-test`.
