"""Трассировка обращений: идентификатор запроса и латентность.

Текст пользователя в лог не попадает — только SHA-256. Восстановить переписку из логов
не должно быть возможно.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from tnved_bot.db.audit import text_digest
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)


class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        request_id = uuid.uuid4().hex[:8]
        data["request_id"] = request_id
        started = time.monotonic()

        log.info(
            "update_received",
            request_id=request_id,
            user_id=data.get("user_id"),
            kind=_kind(event),
            digest=_digest(event),
        )
        try:
            return await handler(event, data)
        finally:
            log.info(
                "update_handled",
                request_id=request_id,
                latency_ms=int((time.monotonic() - started) * 1000),
            )


def _kind(event: TelegramObject) -> str:
    if isinstance(event, CallbackQuery):
        return "callback"
    if isinstance(event, Message):
        if event.photo:
            return "photo"
        if event.document:
            return "document"
        if (event.text or "").startswith("/"):
            return "command"
        return "text"
    return type(event).__name__


def _digest(event: TelegramObject) -> str | None:
    """Хеш вместо текста: связать события между собой можно, прочитать переписку — нет."""
    if isinstance(event, Message) and event.text:
        return text_digest(event.text)[:16]
    return None
