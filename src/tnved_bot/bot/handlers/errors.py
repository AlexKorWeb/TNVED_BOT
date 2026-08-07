"""Глобальный обработчик ошибок.

Пользователь получает человекочитаемый текст и `error_id`; трассировка уходит в лог с тем же
`error_id`. Ни путь файла, ни содержимое промта, ни стек наружу не попадают.

Ошибка одного пользователя не должна ронять процесс: aiogram продолжит принимать апдейты,
если обработчик вернул управление.
"""

from __future__ import annotations

import uuid

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import CallbackQuery, ErrorEvent, Message

from tnved_bot.bot import texts
from tnved_bot.core.errors import TnvedError
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)


async def handle_error(event: ErrorEvent) -> bool:
    error = event.exception
    error_id = uuid.uuid4().hex[:8]

    # Пользователь заблокировал бота или удалил чат — писать ему некуда и незачем.
    if isinstance(error, TelegramForbiddenError):
        log.info("user_blocked_bot", error_id=error_id)
        return True

    # Telegram просит подождать: это не наша ошибка, отвечать пользователю нечем.
    if isinstance(error, TelegramRetryAfter):
        log.warning("telegram_flood", retry_after=error.retry_after, error_id=error_id)
        return True

    # «Сообщение не изменено» возникает при повторной правке одним и тем же текстом.
    if isinstance(error, TelegramBadRequest) and "message is not modified" in str(error):
        log.debug("message_not_modified", error_id=error_id)
        return True

    log.exception("unhandled_error", error_id=error_id, kind=type(error).__name__)
    await _notify(event, error, error_id)
    return True


async def _notify(event: ErrorEvent, error: BaseException, error_id: str) -> None:
    # Для своих исключений есть заготовленный текст; для чужих — общий, чтобы наружу
    # случайно не ушли внутренние подробности.
    text = (
        error.user_message
        if isinstance(error, TnvedError) and error.user_message
        else texts.ERROR_GENERIC.format(error_id=error_id)
    )

    target = event.update.message or (
        event.update.callback_query.message if event.update.callback_query else None
    )
    try:
        if event.update.callback_query is not None:
            # Без answer() у пользователя навсегда останутся «часики» на кнопке.
            await event.update.callback_query.answer()
        if isinstance(target, Message):
            await target.answer(text)
    except Exception as exc:  # noqa: BLE001 — сообщить не вышло, но падать из-за этого нельзя
        log.warning("error_notify_failed", error_id=error_id, error=str(exc)[:200])


def build_router() -> Router:
    router = Router(name="errors")
    router.errors.register(handle_error)
    return router


__all__ = ["CallbackQuery", "build_router", "handle_error"]
