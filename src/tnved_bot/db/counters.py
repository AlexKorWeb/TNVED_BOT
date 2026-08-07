"""Счётчики обращений для лимитов.

Хранятся в БД, а не в памяти: иначе перезапуск бота (в том числе автоперезапуск
планировщиком) обнулял бы лимиты и делал бы их бессмысленными.

`user_id = 0` зарезервирован под глобальный счётчик — настоящих Telegram ID со значением 0
не бывает.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from tnved_bot.clock import iso_ago, utc_now
from tnved_bot.db.engine import Database

Window = Literal["hour", "day"]

GLOBAL_USER_ID = 0


def window_start(kind: Window, moment: datetime | None = None) -> str:
    """Начало текущего окна. Служит ключом, поэтому усекается до часа или суток."""
    now = moment or utc_now()
    truncated = now.replace(minute=0, second=0, microsecond=0)
    if kind == "day":
        truncated = truncated.replace(hour=0)
    return truncated.isoformat()


class UsageCounters:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def bump(self, user_id: int, kind: Window) -> int:
        """Увеличивает счётчик и возвращает новое значение.

        Инкремент и чтение — одним запросом через UPSERT ... RETURNING: read-modify-write
        двумя запросами дал бы гонку при параллельных сообщениях одного пользователя.
        """
        row = await self._db.fetch_one(
            "INSERT INTO usage_counters (user_id, kind, window_start, count)"
            " VALUES (?, ?, ?, 1)"
            " ON CONFLICT (user_id, kind, window_start)"
            " DO UPDATE SET count = count + 1"
            " RETURNING count",
            (user_id, kind, window_start(kind)),
        )
        if row is None:  # pragma: no cover — RETURNING всегда отдаёт строку
            msg = "UPSERT не вернул счётчик"
            raise RuntimeError(msg)
        await self._db.connection.commit()
        return int(row["count"])

    async def current(self, user_id: int, kind: Window) -> int:
        row = await self._db.fetch_one(
            "SELECT count FROM usage_counters WHERE user_id = ? AND kind = ? AND window_start = ?",
            (user_id, kind, window_start(kind)),
        )
        return int(row["count"]) if row else 0

    async def purge_older_than(self, days: int) -> int:
        return await self._db.execute(
            "DELETE FROM usage_counters WHERE window_start < ?", (iso_ago(days=days),)
        )
