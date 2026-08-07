"""Список доступа и коды приглашений.

Пользователи живут в БД, а не в `.env`: добавление человека не должно требовать правки файла
и перезапуска бота под планировщиком задач. В `.env` остаются только администраторы — это
аварийный вход на случай, если список в БД испорчен или админ удалил сам себя.

Авторизация только по числовому `user_id`. Привязка к `@username` запрещена: его можно сменить
или передать другому человеку, и доступ уедет к постороннему.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from tnved_bot.clock import iso_in, now_iso
from tnved_bot.db.engine import Database
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

# Алфавит без похожих символов: 0/O, 1/I/L. Код диктуют голосом и переписывают руками.
_ALPHABET = "ACDEFGHJKMNPQRTUVWXYZ2346789"
CODE_PREFIX = "TNVED"
CODE_GROUPS = 2
CODE_GROUP_LEN = 4


@dataclass(frozen=True, slots=True)
class AllowedUser:
    user_id: int
    username: str | None
    note: str | None
    added_by: int
    added_at: str
    last_seen_at: str | None


@dataclass(frozen=True, slots=True)
class Invite:
    code: str
    note: str | None
    created_by: int
    created_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class InviteResult:
    ok: bool
    reason: str = ""
    user_id: int | None = None
    note: str | None = None
    created_by: int | None = None


def generate_code() -> str:
    """Криптостойкий код вида `TNVED-7K2M-9XQP`."""
    groups = [
        "".join(secrets.choice(_ALPHABET) for _ in range(CODE_GROUP_LEN))
        for _ in range(CODE_GROUPS)
    ]
    return "-".join([CODE_PREFIX, *groups])


def normalize_code(raw: str) -> str:
    """Приводит введённый код к каноническому виду: регистр и лишние пробелы не важны."""
    return raw.strip().upper().replace(" ", "")


class UserRepository:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._cache: set[int] | None = None

    # ------------------------------------------------------------------ доступ

    async def allowed_ids(self) -> set[int]:
        """Кешируется в памяти: запрос к БД на каждое сообщение не нужен.

        Кеш сбрасывается при любом изменении списка, поэтому перезапуск для применения
        изменений не требуется.
        """
        if self._cache is None:
            rows = await self._db.fetch_all("SELECT user_id FROM allowed_users WHERE is_active = 1")
            self._cache = {int(row["user_id"]) for row in rows}
        return self._cache

    def invalidate(self) -> None:
        self._cache = None

    async def is_allowed(self, user_id: int) -> bool:
        return user_id in await self.allowed_ids()

    async def touch(self, user_id: int) -> None:
        """Отметка активности. Нужна только для `/users`, поэтому кеш не трогает."""
        await self._db.execute(
            "UPDATE allowed_users SET last_seen_at = ? WHERE user_id = ?", (now_iso(), user_id)
        )

    # ------------------------------------------------------------------ управление

    async def add(
        self, user_id: int, added_by: int, note: str | None = None, username: str | None = None
    ) -> None:
        await self._db.execute(
            "INSERT INTO allowed_users (user_id, username, note, added_by, added_at, is_active)"
            " VALUES (?, ?, ?, ?, ?, 1)"
            " ON CONFLICT (user_id) DO UPDATE SET"
            "   is_active = 1, note = COALESCE(excluded.note, allowed_users.note),"
            "   username = COALESCE(excluded.username, allowed_users.username)",
            (user_id, username, note, added_by, now_iso()),
        )
        self.invalidate()
        log.info("user_added", user_id=user_id, added_by=added_by)

    async def remove(self, user_id: int) -> bool:
        """Мягкое удаление: история о том, кто и когда имел доступ, сохраняется."""
        changed = await self._db.execute(
            "UPDATE allowed_users SET is_active = 0 WHERE user_id = ? AND is_active = 1",
            (user_id,),
        )
        self.invalidate()
        if changed:
            log.info("user_removed", user_id=user_id)
        return bool(changed)

    async def list_users(self) -> list[AllowedUser]:
        rows = await self._db.fetch_all(
            "SELECT user_id, username, note, added_by, added_at, last_seen_at"
            " FROM allowed_users WHERE is_active = 1 ORDER BY added_at"
        )
        return [
            AllowedUser(
                user_id=row["user_id"],
                username=row["username"],
                note=row["note"],
                added_by=row["added_by"],
                added_at=row["added_at"],
                last_seen_at=row["last_seen_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ приглашения

    async def create_invite(self, created_by: int, note: str | None, ttl_hours: int) -> str:
        code = generate_code()
        await self._db.execute(
            "INSERT INTO invite_codes (code, note, created_by, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (code, note, created_by, now_iso(), iso_in(hours=ttl_hours)),
        )
        log.info("invite_created", created_by=created_by)
        return code

    async def list_active_invites(self) -> list[Invite]:
        """Невостребованные и непросроченные коды.

        Нужны в панели: выданный и забытый код — это открытая дверь на сутки, и админ
        должен видеть, сколько их и кому они предназначались.
        """
        rows = await self._db.fetch_all(
            "SELECT code, note, created_by, created_at, expires_at FROM invite_codes"
            " WHERE used_by IS NULL AND expires_at > ? ORDER BY created_at DESC",
            (now_iso(),),
        )
        return [
            Invite(
                code=row["code"],
                note=row["note"],
                created_by=row["created_by"],
                created_at=row["created_at"],
                expires_at=row["expires_at"],
            )
            for row in rows
        ]

    async def revoke_invite(self, code: str) -> bool:
        """Отзывает невостребованный код.

        Код удаляется, а не помечается: использованные коды хранят историю активаций,
        а неиспользованный отозванный не несёт никакой информации.
        """
        removed = await self._db.execute(
            "DELETE FROM invite_codes WHERE code = ? AND used_by IS NULL",
            (normalize_code(code),),
        )
        if removed:
            log.info("invite_revoked")
        return bool(removed)

    async def redeem(self, raw_code: str, user_id: int, username: str | None) -> InviteResult:
        """Активирует код приглашения.

        Гашение и выдача доступа — одной атомарной операцией `UPDATE ... WHERE used_by IS NULL`.
        Схема «прочитал → проверил → записал» позволила бы двум людям с одним кодом пройти
        обоим: между чтением и записью успел бы вклиниться второй.
        """
        code = normalize_code(raw_code)
        now = now_iso()

        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE invite_codes SET used_by = ?, used_at = ?"
                " WHERE code = ? AND used_by IS NULL AND expires_at > ?",
                (user_id, now, code, now),
            )
            if cursor.rowcount == 0:
                row = await (
                    await conn.execute(
                        "SELECT used_by, expires_at FROM invite_codes WHERE code = ?", (code,)
                    )
                ).fetchone()
                if row is None:
                    return InviteResult(False, "not_found")
                if row["used_by"] is not None:
                    return InviteResult(False, "already_used")
                return InviteResult(False, "expired")

            note_row = await (
                await conn.execute(
                    "SELECT note, created_by FROM invite_codes WHERE code = ?", (code,)
                )
            ).fetchone()
            note = note_row["note"] if note_row else None
            created_by = int(note_row["created_by"]) if note_row else 0

            await conn.execute(
                "INSERT INTO allowed_users"
                " (user_id, username, note, added_by, added_at, is_active)"
                " VALUES (?, ?, ?, 0, ?, 1)"
                " ON CONFLICT (user_id) DO UPDATE SET is_active = 1",
                (user_id, username, note, now),
            )

        self.invalidate()
        log.info("invite_redeemed", user_id=user_id)
        return InviteResult(True, "ok", user_id=user_id, note=note, created_by=created_by)
