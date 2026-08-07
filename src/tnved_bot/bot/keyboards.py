"""Inline-клавиатуры.

В `callback_data` попадают только идентификатор сессии и индекс варианта. Текст туда не
кладётся: поле ограничено 64 байтами, и, что важнее, оно приходит обратно от клиента —
то есть является недоверенными данными. Индекс сверяется с сохранённой сессией, поэтому
подделать ответ, которого не предлагали, нельзя.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

OPTION = "opt"
CUSTOM = "custom"
CANCEL = "cancel"
CONFIRM_PHOTO = "photo_ok"
EDIT_PHOTO = "photo_edit"
FEEDBACK_GOOD = "fb_good"
FEEDBACK_BAD = "fb_bad"
RESTART = "restart"


def clarification(session_id: str, options: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, option in enumerate(options):
        builder.button(text=option, callback_data=f"{OPTION}:{session_id}:{index}")
    builder.button(text="✍️ Свой вариант", callback_data=f"{CUSTOM}:{session_id}:0")
    builder.button(text="❌ Отмена", callback_data=f"{CANCEL}:{session_id}:0")
    builder.adjust(1)
    return builder.as_markup()


def photo_confirm(session_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Всё верно", callback_data=f"{CONFIRM_PHOTO}:{session_id}:0")
    builder.button(text="✍️ Дополнить описание", callback_data=f"{EDIT_PHOTO}:{session_id}:0")
    builder.button(text="❌ Отмена", callback_data=f"{CANCEL}:{session_id}:0")
    builder.adjust(1)
    return builder.as_markup()


def feedback(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👍 Верно", callback_data=f"{FEEDBACK_GOOD}:{session_id}:0"
                ),
                InlineKeyboardButton(
                    text="👎 Неверно", callback_data=f"{FEEDBACK_BAD}:{session_id}:0"
                ),
            ]
        ]
    )


def restart() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Начать заново", callback_data=f"{RESTART}:-:0")]
        ]
    )


def parse_callback(data: str | None) -> tuple[str, str, int] | None:
    """Разбирает `действие:сессия:индекс`. Мусор отбрасывается, а не приводит к ошибке."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    action, session_id, raw_index = parts
    if not raw_index.isdigit():
        return None
    return action, session_id, int(raw_index)
