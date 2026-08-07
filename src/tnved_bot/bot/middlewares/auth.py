"""Whitelist. Первый в цепочке: до него не должно происходить ничего затратного.

Доступ разрешён, если `user_id` есть в `ADMIN_USER_IDS` или в таблице `allowed_users`.
Исключение — команда `/start` с кодом приглашения: её обязан пропускать и тот, кого
в списке ещё нет, иначе активировать код было бы невозможно.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from tnved_bot.bot import texts
from tnved_bot.db.audit import AuditLog
from tnved_bot.db.users import UserRepository
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)


class AuthMiddleware(BaseMiddleware):
    def __init__(self, admins: frozenset[int], users: UserRepository, audit: AuditLog) -> None:
        self._admins = admins
        self._users = users
        self._audit = audit

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return None

        if isinstance(event, Message) and event.chat.type != "private":
            # Групповые чаты игнорируются молча: бот работает один на один.
            return None

        is_admin = user.id in self._admins
        allowed = is_admin or await self._users.is_allowed(user.id)

        data["user_id"] = user.id
        data["is_admin"] = is_admin

        if allowed:
            return await handler(event, data)

        if _is_invite_attempt(event):
            data["invite_only"] = True
            return await handler(event, data)

        await self._audit.record("access_denied", user_id=user.id)
        log.info("access_denied", user_id=user.id)
        await _refuse(event, user.id)
        return None


def _is_invite_attempt(event: TelegramObject) -> bool:
    """`/start КОД` обязан проходить: иначе новый человек не сможет активировать приглашение."""
    if not isinstance(event, Message):
        return False
    text = event.text or ""
    return text.startswith("/start") and len(text.split()) > 1


async def _refuse(event: TelegramObject, user_id: int) -> None:
    text = texts.access_denied(user_id)
    if isinstance(event, Message):
        await event.answer(text)
    elif isinstance(event, CallbackQuery):
        # answer() обязателен даже при отказе, иначе у пользователя висят «часики».
        await event.answer("Доступ ограничен", show_alert=True)
