"""Тесты защиты от второй копии бота."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tnved_bot.core.errors import AlreadyRunningError
from tnved_bot.lockfile import SingleInstanceLock, read_pid


def test_acquire_and_release(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "bot.lock")
    lock.acquire()
    try:
        assert (tmp_path / "bot.lock").exists()
    finally:
        lock.release()


def test_pid_written_to_separate_file(tmp_path: Path) -> None:
    with SingleInstanceLock(tmp_path / "bot.lock"):
        assert read_pid(tmp_path / "bot.pid") == os.getpid()


def test_pid_readable_while_lock_is_held(tmp_path: Path) -> None:
    """PID должен читаться снаружи именно тогда, когда бот работает.

    Держать PID внутри залоченного файла нельзя: на Windows байт-лок обязательный,
    и внешний скрипт остановки не смог бы прочитать файл (`Get-Content` падает
    с «another process has locked a portion of the file»).
    """
    path = tmp_path / "bot.lock"
    with SingleInstanceLock(path):
        # Читаем как это делает сторонний инструмент — обычным открытием файла.
        assert (tmp_path / "bot.pid").read_text(encoding="utf-8").strip() == str(os.getpid())


def test_second_lock_in_same_process_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    with SingleInstanceLock(path):
        with pytest.raises(AlreadyRunningError) as exc:
            SingleInstanceLock(path).acquire()
        assert str(os.getpid()) in str(exc.value)


def test_pid_file_removed_on_release(tmp_path: Path) -> None:
    """После штатной остановки PID мёртвого процесса не должен остаться на диске.

    Иначе скрипт остановки, прочитав устаревший PID, убьёт постороннюю программу,
    которой ОС успела выдать тот же номер.
    """
    path = tmp_path / "bot.lock"
    with SingleInstanceLock(path):
        assert (tmp_path / "bot.pid").exists()
    assert not (tmp_path / "bot.pid").exists()
    assert read_pid(tmp_path / "bot.pid") is None


def test_lock_released_after_context(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    with SingleInstanceLock(path):
        pass
    SingleInstanceLock(path).acquire()  # не должно бросить
    SingleInstanceLock(path).release()


def test_lock_freed_when_holder_process_dies(tmp_path: Path) -> None:
    """Главное свойство: после аварийного завершения лок снимает ОС, ручная уборка не нужна.

    PID-файл в этой ситуации остался бы висеть и блокировал бы автоперезапуск.
    """
    path = tmp_path / "bot.lock"
    src = Path(__file__).resolve().parents[1] / "src"

    script = textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        sys.path.insert(0, {str(src)!r})
        from tnved_bot.lockfile import SingleInstanceLock
        SingleInstanceLock(Path({str(path)!r})).acquire()
        print("locked", flush=True)
        time.sleep(30)
    """)

    proc = subprocess.Popen(  # noqa: S603 — фиксированный интерпретатор, ввода извне нет
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "locked"

        with pytest.raises(AlreadyRunningError):
            SingleInstanceLock(path).acquire()
    finally:
        proc.kill()
        proc.wait(timeout=10)

    # Windows освобождает лок не синхронно с завершением процесса — ждём до 10 с.
    # Ручная уборка при этом не требуется, а это и есть проверяемое свойство.
    assert _lock_becomes_free(path, timeout=10.0), "лок не освободился после смерти держателя"


def _lock_becomes_free(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lock = SingleInstanceLock(path)
        try:
            lock.acquire()
        except AlreadyRunningError:
            time.sleep(0.2)
            continue
        lock.release()
        return True
    return False


def test_release_is_idempotent(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "bot.lock")
    lock.acquire()
    lock.release()
    lock.release()  # повторный вызов не должен бросать


def test_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "bot.lock"
    with SingleInstanceLock(path):
        assert path.exists()
