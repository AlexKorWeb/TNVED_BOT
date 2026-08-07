"""Метаданные фотографий.

Сам файл лежит на диске (`storage/photo_store.py`, T-013), здесь только учёт и срок жизни.
Запись без `expires_at` создать нельзя: TTL 48 часов — обязательство перед пользователем,
а не настройка, которую можно забыть проставить.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tnved_bot.clock import iso_ago, iso_in, now_iso
from tnved_bot.db.engine import Database


@dataclass(frozen=True, slots=True)
class PhotoRecord:
    id: str
    user_id: int
    path: str
    expires_at: str


def _to_record(row: Any) -> PhotoRecord:
    return PhotoRecord(
        id=row["id"],
        user_id=row["user_id"],
        path=row["path"],
        expires_at=row["expires_at"],
    )


class PhotoRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self,
        photo_id: str,
        user_id: int,
        path: str,
        sha256: str,
        size_bytes: int,
        ttl_hours: int,
    ) -> PhotoRecord:
        expires_at = iso_in(hours=ttl_hours)
        await self._db.execute(
            "INSERT INTO photos (id, user_id, path, sha256, bytes, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (photo_id, user_id, path, sha256, size_bytes, now_iso(), expires_at),
        )
        return PhotoRecord(id=photo_id, user_id=user_id, path=path, expires_at=expires_at)

    async def list_expired(self, limit: int = 500) -> list[PhotoRecord]:
        """Фото, у которых истёк срок, но файл ещё не удалён.

        Используется джанитором, в том числе догоняющей очисткой после простоя компьютера.
        """
        rows = await self._db.fetch_all(
            "SELECT id, user_id, path, expires_at FROM photos"
            " WHERE deleted_at IS NULL AND expires_at < ?"
            " ORDER BY expires_at LIMIT ?",
            (now_iso(), limit),
        )
        return [_to_record(r) for r in rows]

    async def list_by_user(self, user_id: int) -> list[PhotoRecord]:
        rows = await self._db.fetch_all(
            "SELECT id, user_id, path, expires_at FROM photos"
            " WHERE user_id = ? AND deleted_at IS NULL",
            (user_id,),
        )
        return [_to_record(r) for r in rows]

    async def mark_deleted(self, photo_id: str) -> None:
        """Отметка ставится только после фактического удаления файла с диска."""
        await self._db.execute(
            "UPDATE photos SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (now_iso(), photo_id),
        )

    async def purge_metadata_older_than(self, days: int) -> int:
        """Удаляет строки уже удалённых фото. Файлы к этому моменту давно стёрты."""
        return await self._db.execute(
            "DELETE FROM photos WHERE deleted_at IS NOT NULL AND deleted_at < ?",
            (iso_ago(days=days),),
        )
