"""Smoke-проверка запущенного бота.

Запускать, пока бот работает. Возвращает 0, если всё в порядке, иначе 1.
Проверки расширяются по мере появления подсистем: пока нет БД (T-002) и справочника (T-003),
соответствующие пункты помечаются как «ещё не реализовано» и не влияют на вердикт.

Использование:
    .\\.venv\\Scripts\\python.exe scripts\\smoke_check.py
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tnved_bot.config import format_config_error, load_settings  # noqa: E402
from tnved_bot.core.errors import AlreadyRunningError  # noqa: E402
from tnved_bot.lockfile import SingleInstanceLock, read_pid  # noqa: E402

OK = "  [ok]"
FAIL = "  [FAIL]"
SKIP = "  [--]"

MIN_FREE_MB = 500


def check_running(lock_path: Path) -> tuple[bool, str]:
    """Пытается взять лок: не вышло — бот работает, вышло — бота нет.

    Это надёжнее чтения PID-файла: тот может остаться от аварийно завершившегося процесса
    и соврать, что бот жив.
    """
    probe = SingleInstanceLock(lock_path)
    try:
        probe.acquire()
    except AlreadyRunningError:
        pid = read_pid(probe.pid_path)
        return True, f"бот запущен, PID {pid if pid else '?'}"
    probe.release()
    return False, "бот не запущен — лок свободен"


def check_claude(binary: str) -> tuple[bool, str]:
    path = shutil.which(binary)
    if path is None:
        return False, f"'{binary}' не найден в PATH — бот будет работать без ИИ"
    try:
        result = subprocess.run(  # noqa: S603 — путь получен из shutil.which, ввода извне нет
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"'{binary}' не отвечает: {exc}"
    if result.returncode != 0:
        return False, f"'{binary} --version' завершился с кодом {result.returncode}"
    return True, result.stdout.strip() or "версия не определена"


def check_log(log_file: Path) -> tuple[bool, str]:
    if not log_file.exists():
        return False, f"лог не найден: {log_file}"

    started = False
    errors: list[str] = []
    for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = entry.get("event", "")
        if event == "bot_started":
            started = True
            errors.clear()  # считаем ошибки только после последнего старта
        elif entry.get("level") in {"error", "critical"}:
            errors.append(event)

    if not started:
        return False, "в логе нет события bot_started"
    if errors:
        return False, f"ошибки после старта: {', '.join(errors[:5])}"
    return True, "bot_started есть, ошибок после старта нет"


def check_nomenclature(db_path: Path) -> tuple[bool | None, str]:
    """None = подсистема ещё не реализована, вердикт не портит."""
    if not db_path.exists():
        return None, "БД ещё не создана (T-002)"
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT rows, imported_at FROM nomenclature_version WHERE is_active = 1"
            ).fetchone()
    except sqlite3.Error as exc:
        if "no such table" in str(exc):
            return None, "таблица справочника ещё не создана (T-002/T-003)"
        return False, f"ошибка чтения БД: {exc}"
    if row is None:
        return False, "справочник не загружен — запустите scripts/import_nomenclature.py"
    return True, f"справочник: {row[0]} кодов, импорт {row[1]}"


def check_autostart() -> tuple[bool | None, str]:
    """Зарегистрирована ли задача планировщика. Не критично: бота можно запускать вручную."""
    if sys.platform != "win32":
        return None, "не применимо на этой ОС"
    schtasks = shutil.which("schtasks")
    if schtasks is None:
        return None, "schtasks не найден"
    try:
        result = subprocess.run(  # noqa: S603 — путь из which, аргументы фиксированы
            [schtasks, "/query", "/tn", "TNVED_BOT"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"не удалось проверить: {exc}"
    if result.returncode != 0:
        return None, "задача не зарегистрирована (scripts\\install_autostart.ps1)"
    return True, "задача TNVED_BOT зарегистрирована"


def check_disk(path: Path) -> tuple[bool, str]:
    free_mb = shutil.disk_usage(path).free // (1024 * 1024)
    if free_mb < MIN_FREE_MB:
        return False, f"мало места: {free_mb} МБ (нужно от {MIN_FREE_MB} МБ)"
    return True, f"свободно {free_mb} МБ"


def main() -> int:
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 — на этом этапе логгера ещё нет
        print(format_config_error(exc))
        return 1

    log_dir = settings.abs_path(settings.log_dir)
    db_path = settings.abs_path(settings.db_path)

    checks: list[tuple[str, tuple[bool | None, str]]] = [
        ("Процесс", check_running(settings.lock_path)),
        ("CLI claude", check_claude(settings.claude_bin)),
        ("Лог", check_log(log_dir / "bot.log")),
        ("Справочник", check_nomenclature(db_path)),
        ("Диск", check_disk(db_path.parent)),
        ("Автозапуск", check_autostart()),
    ]

    print("Smoke-проверка TNVED_BOT")
    print("=" * 60)
    failed = 0
    for name, (status, detail) in checks:
        if status is None:
            mark = SKIP
        elif status:
            mark = OK
        else:
            mark = FAIL
            failed += 1
        print(f"{mark} {name}: {detail}")

    print("=" * 60)
    if failed:
        print(f"РЕЗУЛЬТАТ: провалено проверок — {failed}")
        return 1
    print("РЕЗУЛЬТАТ: всё в порядке")
    return 0


if __name__ == "__main__":
    sys.exit(main())
