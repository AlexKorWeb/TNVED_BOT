"""Текстовое описание товара.

Роутер собирается фабрикой, а не создаётся на уровне модуля: aiogram запрещает подключать
один и тот же объект роутера к двум диспетчерам, а это нужно и тестам, и потенциально
второму экземпляру бота в одном процессе.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from tnved_bot.bot.service import DialogService


async def handle_text(message: Message, user_id: int, service: DialogService) -> None:
    await service.handle_text(message, user_id)


def build_router() -> Router:
    router = Router(name="text")
    router.message.register(handle_text, F.text & ~F.text.startswith("/"))
    return router
