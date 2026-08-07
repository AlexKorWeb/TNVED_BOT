"""Журнал событий: кто, что, когда, сколько заняло.

Сырой текст пользователя сюда не попадает — только SHA-256. Этого достаточно, чтобы
опознать повторный запрос и связать события между собой, но недостаточно, чтобы
восстановить содержимое переписки из БД.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from tnved_bot.clock import iso_ago, now_iso
from tnved_bot.db.engine import Database

# Значения, которые нельзя писать в журнал даже случайно.
_FORBIDDEN_KEYS = frozenset({"text", "caption", "description", "code", "token", "prompt"})


def text_digest(text: str) -> str:
    """SHA-256 от текста пользователя — идентификатор без раскрытия содержимого."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        event: str,
        *,
        user_id: int | None = None,
        payload: dict[str, Any] | None = None,
        latency_ms: int | None = None,
        ok: bool = True,
    ) -> None:
        if payload:
            leaked = _FORBIDDEN_KEYS & payload.keys()
            if leaked:
                msg = (
                    f"в audit_log нельзя писать поля {sorted(leaked)} — "
                    f"это содержимое запроса пользователя, используйте text_digest()"
                )
                raise ValueError(msg)

        await self._db.execute(
            "INSERT INTO audit_log (ts, user_id, event, payload_json, latency_ms, ok)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                now_iso(),
                user_id,
                event,
                json.dumps(payload, ensure_ascii=False) if payload else None,
                latency_ms,
                int(ok),
            ),
        )

    async def count_since(self, event: str, since_iso: str) -> int:
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM audit_log WHERE event = ? AND ts >= ?",
            (event, since_iso),
        )
        return int(row["n"]) if row else 0

    async def purge_older_than(self, days: int) -> int:
        return await self._db.execute("DELETE FROM audit_log WHERE ts < ?", (iso_ago(days=days),))
