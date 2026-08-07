"""Нажатия кнопок.

Два правила, нарушение которых пользователь замечает сразу:

1. `answer()` вызывается **всегда**, включая ветки ошибок — иначе у пользователя бесконечно
   крутятся «часики» на кнопке.
2. `callback_data` — недоверенные данные: их присылает клиент. Сессия проверяется на
   принадлежность пользователю, индекс варианта — на попадание в сохранённый список.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from tnved_bot.bot import keyboards, texts
from tnved_bot.bot.service import DialogService
from tnved_bot.db.audit import AuditLog
from tnved_bot.db.sessions import OPEN_STATES, Session, SessionRepository
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)


async def handle_callback(
    callback: CallbackQuery,
    user_id: int,
    service: DialogService,
    sessions: SessionRepository,
    audit: AuditLog,
) -> None:
    parsed = keyboards.parse_callback(callback.data)
    if parsed is None:
        await callback.answer()
        return

    action, session_id, index = parsed

    if action == keyboards.RESTART:
        await callback.answer()
        await _reply(callback, "Отправьте новое описание товара.")
        return

    session = await sessions.get(session_id)
    if session is None or session.user_id != user_id:
        # Чужая или несуществующая сессия. Логируем: это либо подделка callback_data,
        # либо кнопка из очень старого сообщения.
        log.warning("callback_foreign_session", user_id=user_id, session=session_id)
        await callback.answer(texts.SESSION_CLOSED, show_alert=True)
        return

    if action in (keyboards.FEEDBACK_GOOD, keyboards.FEEDBACK_BAD):
        await _feedback(
            callback, session, service, audit, positive=action == keyboards.FEEDBACK_GOOD
        )
        return

    if session.state not in OPEN_STATES:
        await callback.answer(texts.SESSION_CLOSED, show_alert=True)
        return

    if action == keyboards.CANCEL:
        await sessions.close(session.id, "expired")
        await callback.answer()
        await _strip_buttons(callback)
        await _reply(callback, texts.CANCELLED)
        return

    if action in (keyboards.CUSTOM, keyboards.EDIT_PHOTO):
        await service.await_custom_answer(session)
        await callback.answer()
        await _strip_buttons(callback)
        await _reply(callback, texts.CUSTOM_ANSWER_PROMPT)
        return

    if action == keyboards.CONFIRM_PHOTO:
        await callback.answer()
        await _strip_buttons(callback)
        await service.run(_as_message(callback), session)
        return

    if action == keyboards.OPTION:
        # Отвечаем Telegram до классификации: она занимает десяток секунд, всё это время
        # кнопка иначе показывала бы «часики».
        await callback.answer()
        await _strip_buttons(callback)
        if not await service.continue_with_option(_as_message(callback), session, index):
            await _reply(callback, texts.SESSION_CLOSED)
        return

    await callback.answer()


async def _feedback(
    callback: CallbackQuery,
    session: Session,
    service: DialogService,
    audit: AuditLog,
    *,
    positive: bool,
) -> None:
    await audit.record(
        "feedback",
        user_id=session.user_id,
        payload={"positive": positive, "chapter": str(session.context.get("answer_code", ""))[:2]},
        ok=positive,
    )
    await callback.answer("Спасибо, учту." if positive else "Спасибо.")
    await _strip_buttons(callback)

    if positive:
        return
    # «Неверно» без продолжения — потерянная информация: бот знает, что ошибся, но не знает,
    # в чём. Здесь и только здесь можно спросить, пока пользователь помнит свой товар.
    await service.await_correction(session)
    await _reply(callback, texts.CORRECTION_ASK)


async def _strip_buttons(callback: CallbackQuery) -> None:
    """Убирает клавиатуру, чтобы по ней нельзя было нажать повторно.

    Двойное нажатие иначе запустило бы вторую классификацию того же вопроса.
    """
    message = callback.message
    if isinstance(message, Message):
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception as exc:  # noqa: BLE001 — сообщение могли удалить, это не ошибка
            log.debug("strip_buttons_failed", error=str(exc)[:120])


async def _reply(callback: CallbackQuery, text: str) -> None:
    message = callback.message
    if isinstance(message, Message):
        await message.answer(text)


def _as_message(callback: CallbackQuery) -> Message:
    message = callback.message
    if not isinstance(message, Message):  # pragma: no cover — inline-режим не используется
        msg = "callback без доступного сообщения"
        raise RuntimeError(msg)
    return message


def build_router() -> Router:
    router = Router(name="callbacks")
    router.callback_query.register(handle_callback, F.data)
    return router
