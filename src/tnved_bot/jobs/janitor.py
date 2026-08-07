"""Фоновая уборка: TTL фотографий, закрытие сессий, чистка журналов, бэкапы.

Два свойства, без которых джанитор бесполезен:

1. **Догоняющая очистка при старте.** Компьютер выключают на ночь и на выходные. Если чистить
   только по расписанию, фотография, у которой срок истёк во время простоя, останется лежать
   до следующего попадания в цикл — то есть обещание «48 часов» будет нарушено.
2. **Падение одной задачи не останавливает остальные.** Занятый файл не должен приводить
   к тому, что перестанут чиститься сессии и журнал.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]

from tnved_bot.bot import texts
from tnved_bot.config import Settings
from tnved_bot.db.audit import AuditLog
from tnved_bot.db.engine import Database
from tnved_bot.db.photos import PhotoRepository
from tnved_bot.db.sessions import SessionRepository
from tnved_bot.logging_setup import get_logger
from tnved_bot.storage.photo_store import PhotoStore

log = get_logger(__name__)

PHOTO_INTERVAL_MINUTES = 60
SESSION_INTERVAL_MINUTES = 5
DAILY_HOUR = 4


class Janitor:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._photos = PhotoRepository(db)
        self._sessions = SessionRepository(db)
        self._audit = AuditLog(db)
        self._store = PhotoStore(settings.abs_path(settings.photo_dir), settings.max_photo_mb)
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._bot: object | None = None

    async def start(self, bot: object | None = None) -> None:
        self._bot = bot

        # Догоняющая очистка — до расписания: накопившееся за время простоя должно уйти
        # немедленно, а не через час.
        await self.sweep_photos()
        await self.close_expired_sessions()

        self._scheduler.add_job(
            self._guard(self.sweep_photos), "interval", minutes=PHOTO_INTERVAL_MINUTES
        )
        self._scheduler.add_job(
            self._guard(self.close_expired_sessions),
            "interval",
            minutes=SESSION_INTERVAL_MINUTES,
        )
        self._scheduler.add_job(self._guard(self.daily), "cron", hour=DAILY_HOUR)
        self._scheduler.start()
        log.info("janitor_started")

    async def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def _guard(self, job: Callable[[], Awaitable[object]]) -> Callable[[], Awaitable[None]]:
        """Оборачивает задачу так, чтобы её падение не сняло остальные с расписания."""

        async def wrapped() -> None:
            try:
                await job()
            except Exception:  # noqa: BLE001 — джанитор обязан пережить любую задачу
                log.exception("janitor_job_failed", job=getattr(job, "__name__", "?"))

        wrapped.__name__ = getattr(job, "__name__", "job")
        return wrapped

    # ------------------------------------------------------------------ задачи

    async def sweep_photos(self) -> int:
        """Удаляет файлы фотографий с истёкшим сроком."""
        removed = 0
        for record in await self._photos.list_expired():
            if self._store.delete(Path(record.path)):
                await self._photos.mark_deleted(record.id)
                removed += 1
        if removed:
            log.info("photos_swept", removed=removed)
        return removed

    async def close_expired_sessions(self) -> int:
        """Закрывает диалоги, в которых пользователь не ответил на уточнение.

        Пользователю уходит лучший из имеющихся вариантов с явной пометкой, что ответ дан
        без уточнений: молча выбросить начатый диалог — значит оставить человека без ответа.
        """
        closed = 0
        for session in await self._sessions.list_expired():
            await self._sessions.close(session.id, "expired")
            closed += 1
            await self._notify_timeout(session)
        if closed:
            log.info("sessions_expired", closed=closed)
        return closed

    async def _notify_timeout(self, session: object) -> None:
        bot = self._bot
        if bot is None:
            return
        chat_id = getattr(session, "chat_id", None)
        if chat_id is None:
            return
        try:
            await bot.send_message(  # type: ignore[attr-defined]
                chat_id,
                "⏳ Не дождался уточнения — диалог закрыт.\n"
                "Отправьте описание заново, если код всё ещё нужен.\n\n"
                f"{texts.DISCLAIMER}",
            )
        except Exception as exc:  # noqa: BLE001 — пользователь мог заблокировать бота
            log.info("timeout_notify_failed", error=str(exc)[:150])

    async def daily(self) -> None:
        """Суточная уборка: метаданные, сессии, журнал, бэкап, сжатие БД."""
        photos = await self._photos.purge_metadata_older_than(
            self._settings.photo_meta_retention_days
        )
        sessions = await self._sessions.purge_older_than(self._settings.session_retention_days)
        audit = await self._audit.purge_older_than(self._settings.audit_retention_days)
        await self._db.backup(
            self._settings.abs_path(self._settings.backup_dir), self._settings.backup_keep
        )
        await self._db.vacuum()
        log.info("daily_cleanup", photos=photos, sessions=sessions, audit=audit)


@contextlib.asynccontextmanager
async def running(db: Database, settings: Settings, bot: object | None = None):  # type: ignore[no-untyped-def]
    janitor = Janitor(db, settings)
    await janitor.start(bot)
    try:
        yield janitor
    finally:
        await janitor.stop()


__all__ = ["Janitor", "running", "asyncio"]
