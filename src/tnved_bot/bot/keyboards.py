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


# ---------------------------------------------------------------- админ-панель
#
# Отдельный префикс `adm:` и отдельный роутер: админские кнопки не должны разбираться
# тем же кодом, что и пользовательские, — иначе легко перепутать проверки прав.

ADMIN_PREFIX = "adm"
ADM_MENU = "menu"
ADM_USERS = "users"
ADM_INVITES = "invites"
ADM_NEW_INVITE = "new_invite"
ADM_REVOKE_INVITE = "rm_invite"
ADM_CONFIRM_USER = "ask_user"
ADM_REMOVE_USER = "rm_user"
ADM_STATS = "stats"
ADM_HEALTH = "health"

MAX_LABEL = 28


def _cut(text: str) -> str:
    """Подпись кнопки Telegram обрезает сам; лучше сделать это осмысленно."""
    return text if len(text) <= MAX_LABEL else text[: MAX_LABEL - 1] + "…"


def admin_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Пользователи", callback_data=f"{ADMIN_PREFIX}:{ADM_USERS}:-")
    builder.button(text="🎟 Приглашения", callback_data=f"{ADMIN_PREFIX}:{ADM_INVITES}:-")
    builder.button(text="➕ Новое приглашение", callback_data=f"{ADMIN_PREFIX}:{ADM_NEW_INVITE}:-")
    builder.button(text="📊 Статистика", callback_data=f"{ADMIN_PREFIX}:{ADM_STATS}:-")
    builder.button(text="🩺 Состояние", callback_data=f"{ADMIN_PREFIX}:{ADM_HEALTH}:-")
    builder.adjust(2, 1, 2)
    return builder.as_markup()


def admin_users(people: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """`people` — пары (id, подпись). Кнопка на каждого: отзыв в один шаг с подтверждением."""
    builder = InlineKeyboardBuilder()
    for user_id, label in people:
        builder.button(
            text=f"❌ {_cut(label)}",
            callback_data=f"{ADMIN_PREFIX}:{ADM_CONFIRM_USER}:{user_id}",
        )
    builder.button(text="⬅️ Назад", callback_data=f"{ADMIN_PREFIX}:{ADM_MENU}:-")
    builder.adjust(1)
    return builder.as_markup()


def admin_confirm_user(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Да, отозвать", callback_data=f"{ADMIN_PREFIX}:{ADM_REMOVE_USER}:{user_id}"
    )
    builder.button(text="⬅️ Отмена", callback_data=f"{ADMIN_PREFIX}:{ADM_USERS}:-")
    builder.adjust(1)
    return builder.as_markup()


def admin_invites(codes: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for code, label in codes:
        builder.button(
            text=f"❌ {_cut(label)}",
            callback_data=f"{ADMIN_PREFIX}:{ADM_REVOKE_INVITE}:{code}",
        )
    builder.button(text="➕ Новое", callback_data=f"{ADMIN_PREFIX}:{ADM_NEW_INVITE}:-")
    builder.button(text="⬅️ Назад", callback_data=f"{ADMIN_PREFIX}:{ADM_MENU}:-")
    builder.adjust(1)
    return builder.as_markup()


def admin_entry() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛠 Панель администратора",
                    callback_data=f"{ADMIN_PREFIX}:{ADM_MENU}:-",
                )
            ]
        ]
    )


def admin_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ В меню", callback_data=f"{ADMIN_PREFIX}:{ADM_MENU}:-")]
        ]
    )


def parse_admin_callback(data: str | None) -> tuple[str, str] | None:
    """Разбирает `adm:действие:аргумент`. Аргумент может содержать дефисы (код приглашения)."""
    if not data or not data.startswith(f"{ADMIN_PREFIX}:"):
        return None
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None
    return parts[1], parts[2]


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
