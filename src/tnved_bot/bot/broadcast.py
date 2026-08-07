"""Рассылка сообщения всем пользователям с доступом.

Одна ошибка не должна ронять рассылку. Заблокировавший бота человек, удалённый чат,
пользователь, ни разу не открывший бота, — всё это нормальные исходы: Telegram отвечает
ошибкой, и она касается только этого получателя.

Темп ограничен намеренно: Telegram режет отправку примерно на тридцати сообщениях в секунду,
а превышение стоит временной блокировки бота — то есть неработающего бота для всех.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aiogram import Bot

from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

PAUSE_SECONDS = 0.06
MAX_LENGTH = 4000


@dataclass(frozen=True, slots=True)
class BroadcastResult:
    sent: int
    failed: int


async def send_to_all(bot: Bot, user_ids: list[int], text: str) -> BroadcastResult:
    sent = failed = 0
    for index, user_id in enumerate(user_ids):
        try:
            await bot.send_message(user_id, text[:MAX_LENGTH], disable_web_page_preview=True)
            sent += 1
        except Exception as exc:  # noqa: BLE001 — недоставка одному не отменяет остальных
            failed += 1
            log.info("broadcast_undelivered", user_id=user_id, error=str(exc)[:120])
        if index + 1 < len(user_ids):
            await asyncio.sleep(PAUSE_SECONDS)

    log.info("broadcast_finished", sent=sent, failed=failed)
    return BroadcastResult(sent=sent, failed=failed)
