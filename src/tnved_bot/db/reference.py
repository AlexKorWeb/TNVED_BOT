"""Кеш внешней справки по коду.

Хранит справку как непрозрачную строку: разбор и сборка — дело `core/reference.py`.
Так слой БД не зависит от адаптера сети, и правка формата справки не трогает хранилище.

Ставки пошлин и перечни документов меняются решениями коллегии ЕЭК — раз в месяцы, не в
минуты. Поэтому кеш живёт долго, а неудачная попытка запоминается ненадолго: сайт мог
лежать десять минут, и наказывать за это на месяц нечестно.
"""

from __future__ import annotations

from dataclasses import dataclass

from tnved_bot.clock import iso_ago, now_iso
from tnved_bot.db.engine import Database
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_TTL_DAYS = 30
FAILURE_TTL_HOURS = 6


@dataclass(frozen=True, slots=True)
class CachedReference:
    """`ok=False` — «недавно пробовали, не вышло».

    Отрицательный результат кешируется намеренно: без него бот ходил бы в сеть за кодом,
    которого на сайте нет, при каждом его упоминании.
    """

    ok: bool
    payload: str


class ReferenceCache:
    def __init__(self, db: Database, ttl_days: int = DEFAULT_TTL_DAYS) -> None:
        self._db = db
        self._ttl_days = ttl_days

    async def get(self, code: str) -> CachedReference | None:
        """Свежая запись кеша или `None`, если её нет или она устарела."""
        row = await self._db.fetch_one(
            "SELECT fetched_at, ok, payload_json FROM code_reference WHERE code = ?", (code,)
        )
        if row is None:
            return None

        ok = bool(row["ok"])
        fresh_since = iso_ago(days=self._ttl_days) if ok else iso_ago(hours=FAILURE_TTL_HOURS)
        if row["fetched_at"] < fresh_since:
            return None
        return CachedReference(ok=ok, payload=row["payload_json"])

    async def put(self, code: str, payload: str | None) -> None:
        """`payload=None` записывает отрицательный результат."""
        await self._db.execute(
            "INSERT INTO code_reference (code, fetched_at, ok, payload_json)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (code) DO UPDATE SET"
            "   fetched_at = excluded.fetched_at, ok = excluded.ok,"
            "   payload_json = excluded.payload_json",
            (code, now_iso(), 1 if payload else 0, payload or "{}"),
        )

    async def count(self) -> int:
        row = await self._db.fetch_one("SELECT COUNT(*) AS n FROM code_reference WHERE ok = 1")
        return int(row["n"]) if row else 0

    async def clear(self) -> int:
        """Полный сброс кеша. Нужен, когда ставки пересмотрели и ждать TTL не хочется."""
        removed = await self._db.execute("DELETE FROM code_reference")
        log.info("reference_cache_cleared", rows=removed)
        return removed
