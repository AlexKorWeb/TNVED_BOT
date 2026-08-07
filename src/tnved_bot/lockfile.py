"""Защита от запуска второй копии бота.

Используется блокировка на уровне ОС, а не «PID-файл + проверка живости процесса»:
ОС снимает такой лок сама при любом завершении процесса, включая аварийное. PID-файл
после падения остался бы висеть и требовал ручной уборки.

Файлов два, и это принципиально:

* `bot.lock` — держится эксклюзивно, содержимое неважно. Пока бот жив, прочитать этот файл
  снаружи нельзя: на Windows `msvcrt.locking` — обязательная блокировка, и `Get-Content`
  падает с «another process has locked a portion of the file».
* `bot.pid` — обычный текстовый файл с PID, свободно читается кем угодно. Именно его читает
  скрипт остановки.

Держать PID внутри залоченного файла нельзя — он оказался бы недоступен ровно тогда, когда нужен.

Замечание по Windows: после аварийного завершения лок освобождается не мгновенно — задержка
до нескольких секунд. Перезапуск сразу после падения может получить `AlreadyRunningError`;
интервал повтора в планировщике задач (1 минута) это перекрывает.

`bot.pid` при аварийном завершении остаётся устаревшим, поэтому перед `Stop-Process` внешний
скрипт обязан убедиться, что процесс с этим PID — действительно бот.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from types import TracebackType

from tnved_bot.core.errors import AlreadyRunningError

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        # Ошибку снятия глушим намеренно: дескриптор всё равно закрывается следом,
        # и ОС освободит лок сама.
        os.lseek(fd, 0, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        # См. комментарий в windows-ветке: дескриптор закрывается следом.
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


def read_pid(pid_path: Path) -> int | None:
    """Читает PID из `bot.pid`. `None`, если файла нет или содержимое мусорное.

    PID может быть устаревшим (осталось от аварийно завершившегося процесса) — вызывающая
    сторона обязана проверить, что процесс действительно принадлежит боту.
    """
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


class SingleInstanceLock:
    """Контекстный менеджер: удерживает лок, пока жив процесс."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.pid_path = path.with_suffix(".pid")
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)

        if not _try_lock(fd):
            os.close(fd)
            holder = read_pid(self.pid_path)
            who = f" (PID {holder})" if holder else ""
            msg = f"Бот уже запущен{who}. Лок: {self.path}"
            raise AlreadyRunningError(msg)

        self._fd = fd
        self.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def release(self) -> None:
        if self._fd is None:
            return
        # Удаляем PID-файл до снятия лока. Иначе после остановки остался бы номер уже мёртвого
        # процесса, а ОС со временем выдаёт этот номер другому — скрипт остановки, доверившись
        # файлу, убил бы постороннюю программу.
        self.pid_path.unlink(missing_ok=True)
        _unlock(self._fd)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
