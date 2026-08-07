"""Лимиты обращений.

Бот работает на личном компьютере и расходует подписку владельца, поэтому лимит защищает
не сервер, а машину и лимиты подписки. Счётчики живут в БД: в памяти они обнулялись бы
при каждом автоперезапуске планировщиком и не значили бы ничего.

Служебные команды не считаются: `/start`, `/help`, `/cancel` не обращаются к ИИ.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from tnved_bot.bot import texts
from tnved_bot.clock import utc_now
from tnved_bot.db.audit import AuditLog
from tnved_bot.db.counters import GLOBAL_USER_ID, UsageCounters
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

FREE_COMMANDS = ("/start", "/help", "/cancel", "/version", "/forget")
MINUTES_PER_HOUR = 60


class RateLimitMiddleware(BaseMiddleware):
    def __init__(
        self,
        counters: UsageCounters,
        audit: AuditLog,
        *,
        per_hour: int,
        per_day: int,
        global_per_day: int,
    ) -> None:
        self._counters = counters
        self._audit = audit
        self._per_hour = per_hour
        self._per_day = per_day
        self._global_per_day = global_per_day

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id: int | None = data.get("user_id")
        if user_id is None or not _is_billable(event):
            return await handler(event, data)

        hourly = await self._counters.bump(user_id, "hour")
        daily = await self._counters.bump(user_id, "day")
        total = await self._counters.bump(GLOBAL_USER_ID, "day")

        if hourly > self._per_hour:
            await self._reject(event, user_id, "hour", _minutes_left_in_hour())
            return None
        if daily > self._per_day or total > self._global_per_day:
            await self._reject(event, user_id, "day", _minutes_left_in_day())
            return None

        return await handler(event, data)

    async def _reject(self, event: TelegramObject, user_id: int, window: str, minutes: int) -> None:
        await self._audit.record("rate_limited", user_id=user_id, payload={"window": window})
        log.info("rate_limited", user_id=user_id, window=window)
        if isinstance(event, Message):
            await event.answer(texts.rate_limited(minutes))


def _is_billable(event: TelegramObject) -> bool:
    """Считаются только обращения, которые могут дойти до ИИ."""
    if not isinstance(event, Message):
        return False
    if event.photo or event.document:
        return True
    text = (event.text or "").strip()
    if not text:
        return False
    return not text.startswith(FREE_COMMANDS)


def _minutes_left_in_hour() -> int:
    now = utc_now()
    return max(1, MINUTES_PER_HOUR - now.minute)


def _minutes_left_in_day() -> int:
    now = utc_now()
    return max(1, (23 - now.hour) * MINUTES_PER_HOUR + (MINUTES_PER_HOUR - now.minute))
