"""Состояние диалога классификации.

Сессия живёт в БД, а не в памяти процесса: перезапуск бота не должен терять начатый диалог,
а джанитору нужно уметь находить просроченные сессии независимо от того, кто их создал.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from tnved_bot.clock import iso_ago, iso_in, now_iso
from tnved_bot.db.engine import Database

SessionState = Literal["collecting", "clarifying", "done", "expired"]
OPEN_STATES = ("collecting", "clarifying")


@dataclass(slots=True)
class Session:
    id: str
    user_id: int
    chat_id: int
    state: SessionState
    description: str | None = None
    photo_id: str | None = None
    answers: list[dict[str, str]] = field(default_factory=list)
    candidates: list[dict[str, Any]] | None = None
    round: int = 0
    expires_at: str = ""


def _to_session(row: Any) -> Session:
    return Session(
        id=row["id"],
        user_id=row["user_id"],
        chat_id=row["chat_id"],
        state=row["state"],
        description=row["description"],
        photo_id=row["photo_id"],
        answers=json.loads(row["answers_json"]),
        candidates=json.loads(row["candidates_json"]) if row["candidates_json"] else None,
        round=row["round"],
        expires_at=row["expires_at"],
    )


class SessionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        session_id: str,
        user_id: int,
        chat_id: int,
        timeout_minutes: int,
        description: str | None = None,
        photo_id: str | None = None,
    ) -> Session:
        now = now_iso()
        expires_at = iso_in(minutes=timeout_minutes)
        await self._db.execute(
            "INSERT INTO sessions"
            " (id, user_id, chat_id, state, description, photo_id, created_at, updated_at,"
            "  expires_at)"
            " VALUES (?, ?, ?, 'collecting', ?, ?, ?, ?, ?)",
            (session_id, user_id, chat_id, description, photo_id, now, now, expires_at),
        )
        return Session(
            id=session_id,
            user_id=user_id,
            chat_id=chat_id,
            state="collecting",
            description=description,
            photo_id=photo_id,
            expires_at=expires_at,
        )

    async def get(self, session_id: str) -> Session | None:
        row = await self._db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return _to_session(row) if row else None

    async def save(self, session: Session, timeout_minutes: int) -> None:
        """Сохраняет состояние и продлевает срок жизни — активность отодвигает таймаут."""
        await self._db.execute(
            "UPDATE sessions SET state = ?, description = ?, photo_id = ?, answers_json = ?,"
            " candidates_json = ?, round = ?, updated_at = ?, expires_at = ?"
            " WHERE id = ?",
            (
                session.state,
                session.description,
                session.photo_id,
                json.dumps(session.answers, ensure_ascii=False),
                json.dumps(session.candidates, ensure_ascii=False) if session.candidates else None,
                session.round,
                now_iso(),
                iso_in(minutes=timeout_minutes),
                session.id,
            ),
        )

    async def close(self, session_id: str, state: SessionState = "done") -> None:
        await self._db.execute(
            "UPDATE sessions SET state = ?, updated_at = ? WHERE id = ?",
            (state, now_iso(), session_id),
        )

    async def close_user_sessions(self, user_id: int) -> int:
        """Закрывает открытые сессии пользователя. Нужно при отзыве доступа (`/deluser`)."""
        placeholders = ", ".join("?" * len(OPEN_STATES))
        return await self._db.execute(
            f"UPDATE sessions SET state = 'expired', updated_at = ?"  # noqa: S608 — плейсхолдеры
            f" WHERE user_id = ? AND state IN ({placeholders})",
            (now_iso(), user_id, *OPEN_STATES),
        )

    async def list_expired(self, limit: int = 100) -> list[Session]:
        placeholders = ", ".join("?" * len(OPEN_STATES))
        rows = await self._db.fetch_all(
            f"SELECT * FROM sessions WHERE expires_at < ?"  # noqa: S608 — плейсхолдеры
            f" AND state IN ({placeholders}) ORDER BY expires_at LIMIT ?",
            (now_iso(), *OPEN_STATES, limit),
        )
        return [_to_session(row) for row in rows]

    async def find_open(self, user_id: int) -> Session | None:
        placeholders = ", ".join("?" * len(OPEN_STATES))
        row = await self._db.fetch_one(
            f"SELECT * FROM sessions WHERE user_id = ?"  # noqa: S608 — плейсхолдеры
            f" AND state IN ({placeholders}) ORDER BY updated_at DESC LIMIT 1",
            (user_id, *OPEN_STATES),
        )
        return _to_session(row) if row else None

    async def purge_older_than(self, days: int) -> int:
        return await self._db.execute(
            "DELETE FROM sessions WHERE updated_at < ?", (iso_ago(days=days),)
        )
