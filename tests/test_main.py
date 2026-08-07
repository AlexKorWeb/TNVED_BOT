"""Тесты точки входа: коды выхода, отказ второй копии, graceful shutdown."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import FAKE_TOKEN
from tnved_bot.__main__ import EXIT_ALREADY_RUNNING, EXIT_CONFIG, main
from tnved_bot.lockfile import SingleInstanceLock

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_bad_config_exits_with_readable_message(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BOT_TOKEN")

    assert main() == EXIT_CONFIG

    stderr = capsys.readouterr().err
    assert "BOT_TOKEN" in stderr
    assert "@BotFather" in stderr
    assert "Traceback" not in stderr


def test_second_instance_refused(env: dict[str, str], tmp_path: Path) -> None:
    """Вторая копия завершается кодом 3, первая (держатель лока) не страдает."""
    lock = SingleInstanceLock(tmp_path / "db" / "bot.lock")
    (tmp_path / "db").mkdir(parents=True, exist_ok=True)
    lock.acquire()
    try:
        assert main() == EXIT_ALREADY_RUNNING
    finally:
        lock.release()


def _child_env(tmp_path: Path) -> dict[str, str]:
    """Окружение для дочернего процесса бота: всё в tmp_path, настоящий .env перекрыт."""
    return {
        **os.environ,
        "BOT_TOKEN": FAKE_TOKEN,
        "ADMIN_USER_IDS": "111",
        "DB_PATH": str(tmp_path / "db" / "tnved.db"),
        "PHOTO_DIR": str(tmp_path / "photos"),
        "NOMENCLATURE_DIR": str(tmp_path / "nomenclature"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "LOG_DIR": str(tmp_path / "logs"),
        "PYTHONIOENCODING": "utf-8",
    }


def _wait_for_log(log_file: Path, needle: str, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_file.exists():
            content = log_file.read_text(encoding="utf-8", errors="replace")
            if needle in content:
                return content
        time.sleep(0.1)
    existing = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
    pytest.fail(f"не дождался '{needle}' за {timeout} с. Лог:\n{existing}")


def test_starts_and_shuts_down_gracefully(tmp_path: Path) -> None:
    """Полный цикл: старт → сигнал завершения → корректная остановка и освобождение лока.

    Это единственный тест, поднимающий процесс целиком — им проверяется то, чего не видно
    в unit-тестах: что лок реально снимается и повторный запуск не требует ручной уборки.
    """
    log_file = tmp_path / "logs" / "bot.log"
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

    proc = subprocess.Popen(  # noqa: S603 — фиксированный интерпретатор и аргументы
        [sys.executable, "-m", "tnved_bot"],
        cwd=PROJECT_ROOT,
        env=_child_env(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        content = _wait_for_log(log_file, "bot_started")
        assert FAKE_TOKEN not in content, "токен утёк в лог при старте"

        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGTERM)

        assert proc.wait(timeout=30) == 0, "завершение по сигналу должно быть штатным"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    content = log_file.read_text(encoding="utf-8", errors="replace")
    assert "shutdown_signal" in content
    assert "bot_stopping" in content
    assert "bot_stopped" in content
    assert FAKE_TOKEN not in content

    # Лок освобождён — следующий запуск не требует ручной уборки.
    SingleInstanceLock(tmp_path / "db" / "bot.lock").acquire()
