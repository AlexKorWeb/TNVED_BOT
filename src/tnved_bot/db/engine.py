"""Соединение с SQLite, применение схемы и восстановление после повреждения БД.

Одно соединение на процесс. Пул не нужен: бот однопроцессный, нагрузка — единицы запросов
в минуту, а `aiosqlite` и так сериализует обращения через собственный поток. Запись
дополнительно защищена `asyncio.Lock`, чтобы конкурентные транзакции не переплетались.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from tnved_bot.clock import utc_now
from tnved_bot.core.errors import StorageError
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Паузы между повторами при «database is locked». Суммарно ~1.3 с — этого хватает,
# чтобы переждать чужую запись, и мало, чтобы пользователь заметил задержку.
RETRY_DELAYS = (0.1, 0.3, 0.9)

# Признаки повреждения файла БД. Проверяем по тексту: отдельного типа исключения у sqlite нет.
_CORRUPTION_MARKERS = (
    "database disk image is malformed",
    "file is not a database",
    "database corruption",
    "malformed database schema",
)

Params = Sequence[Any]


def _is_locked(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text


def _is_corruption(exc: sqlite3.Error) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _CORRUPTION_MARKERS)


class Database:
    """Обёртка над `aiosqlite` с повторами, транзакциями и восстановлением из бэкапа."""

    def __init__(self, path: Path, backup_dir: Path | None = None) -> None:
        self.path = path
        self.backup_dir = backup_dir
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ подключение

    async def connect(self) -> None:
        """Открывает БД, при необходимости восстанавливает её и применяет схему.

        Повреждение файла не должно ронять бота: если есть бэкап — восстанавливаем из него,
        если нет — отводим битый файл в сторону и начинаем с чистой БД. Справочник и в том,
        и в другом случае переимпортируется, а пользовательские сессии одноразовые.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self._open()
        except sqlite3.DatabaseError as exc:
            if not _is_corruption(exc):
                raise
            log.critical("db_corrupted", path=str(self.path), error=str(exc))
            self._recover_corrupted()
            await self._open()

        await self._apply_schema()

    async def _open(self) -> None:
        conn = await aiosqlite.connect(self.path)
        # Любая ошибка ниже обязана закрыть соединение: sqlite открывает файл лениво,
        # и повреждение вылезает уже на первой PRAGMA. С незакрытым дескриптором Windows
        # не даст переименовать битый файл, и восстановление упадёт с PermissionError.
        try:
            conn.row_factory = aiosqlite.Row
            # busy_timeout — первая линия обороны при конкурентном доступе; RETRY_DELAYS
            # добирают то, что она не покрыла.
            await conn.execute("PRAGMA busy_timeout = 5000")
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute("PRAGMA synchronous = NORMAL")
            # Быстрая проверка целостности: полный integrity_check дорог, quick_check
            # находит повреждение страниц и останавливается на первой ошибке.
            row = await (await conn.execute("PRAGMA quick_check(1)")).fetchone()
            if row is not None and str(row[0]).lower() != "ok":
                msg = f"database disk image is malformed: {row[0]}"
                raise sqlite3.DatabaseError(msg)
        except BaseException:
            await conn.close()
            raise
        self._conn = conn

    def _recover_corrupted(self) -> None:
        stamp = utc_now().strftime("%Y%m%d-%H%M%S")
        broken = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        # Битый файл не удаляем: возможно, из него ещё удастся что-то достать вручную.
        self.path.replace(broken)
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)

        backup = self._latest_backup()
        if backup is None:
            log.critical("db_recovered_empty", broken=str(broken))
            return
        shutil.copy2(backup, self.path)
        log.critical("db_recovered_from_backup", broken=str(broken), backup=str(backup))

    def _latest_backup(self) -> Path | None:
        if self.backup_dir is None or not self.backup_dir.is_dir():
            return None
        backups = sorted(self.backup_dir.glob(f"{self.path.stem}-*.db"))
        return backups[-1] if backups else None

    async def close(self) -> None:
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            msg = "БД не подключена — вызовите connect() до обращений"
            raise StorageError(msg)
        return self._conn

    # ------------------------------------------------------------------ схема

    async def _apply_schema(self) -> None:
        conn = self.connection
        current = await self.user_version()
        await conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        # executescript коммитит и сбрасывает PRAGMA foreign_keys — возвращаем.
        await conn.execute("PRAGMA foreign_keys = ON")
        if current != SCHEMA_VERSION:
            await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await conn.commit()
            log.info("schema_applied", was=current, now=SCHEMA_VERSION)

    async def user_version(self) -> int:
        row = await (await self.connection.execute("PRAGMA user_version")).fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------ запросы

    async def _with_retry(self, action: str, coro_factory: Any) -> Any:
        """Повторяет операцию при блокировке БД.

        Блокировка — временное состояние, а не ошибка приложения: пользователь не должен
        видеть её вообще.
        """
        last: sqlite3.Error | None = None
        for attempt, delay in enumerate((*RETRY_DELAYS, None)):
            try:
                return await coro_factory()
            except sqlite3.Error as exc:
                if not _is_locked(exc) or delay is None:
                    last = exc
                    break
                log.warning("db_locked_retry", action=action, attempt=attempt + 1)
                await asyncio.sleep(delay)
                last = exc
        msg = f"Операция с БД не удалась ({action}): {last}"
        raise StorageError(msg) from last

    async def execute(self, sql: str, params: Params = ()) -> int:
        """Возвращает число затронутых строк."""

        async def run() -> int:
            async with self._write_lock:
                cursor = await self.connection.execute(sql, params)
                await self.connection.commit()
                return cursor.rowcount

        result = await self._with_retry("execute", run)
        return int(result)

    async def executemany(self, sql: str, rows: Iterable[Params]) -> None:
        async def run() -> None:
            async with self._write_lock:
                await self.connection.executemany(sql, rows)
                await self.connection.commit()

        await self._with_retry("executemany", run)

    async def fetch_one(self, sql: str, params: Params = ()) -> aiosqlite.Row | None:
        async def run() -> aiosqlite.Row | None:
            cursor = await self.connection.execute(sql, params)
            try:
                return await cursor.fetchone()
            finally:
                await cursor.close()

        return await self._with_retry("fetch_one", run)  # type: ignore[no-any-return]

    async def fetch_all(self, sql: str, params: Params = ()) -> list[aiosqlite.Row]:
        async def run() -> list[aiosqlite.Row]:
            cursor = await self.connection.execute(sql, params)
            try:
                return list(await cursor.fetchall())
            finally:
                await cursor.close()

        result = await self._with_retry("fetch_all", run)
        return list(result)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Атомарная группа операций.

        Нужна там, где промежуточное состояние недопустимо: переключение активной версии
        справочника и гашение кода приглашения одновременно с выдачей доступа.
        """
        async with self._write_lock:
            conn = self.connection
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    # ------------------------------------------------------------------ обслуживание

    async def backup(self, backup_dir: Path, keep: int) -> Path:
        """Горячий бэкап средствами SQLite — безопасен на работающей БД.

        Операции с файловой системой здесь синхронные (`mkdir`, `glob`, `unlink`). Это
        осознанно: метаданные каталога читаются за микросекунды, а бэкап делается раз в сутки
        джанитором. Выносить их в поток — шум без выигрыша.
        """
        backup_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — см. docstring
        # Миллисекунды в имени: два бэкапа внутри одной секунды иначе перезаписали бы
        # друг друга, и хранилось бы меньше копий, чем заказано.
        stamp = utc_now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        target = backup_dir / f"{self.path.stem}-{stamp}.db"

        async with aiosqlite.connect(target) as dest:
            await self.connection.backup(dest)

        stale = sorted(backup_dir.glob(f"{self.path.stem}-*.db"))[:-keep]  # noqa: ASYNC240
        for old in stale:
            old.unlink(missing_ok=True)
        log.info("db_backup_created", path=str(target), removed=len(stale))
        return target

    async def vacuum(self) -> None:
        await self.execute("VACUUM")
