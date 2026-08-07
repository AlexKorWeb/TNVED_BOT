"""Иерархия исключений проекта.

Правило: `except Exception` допустим только в глобальном error handler и в джаниторе.
Всё остальное ловит конкретный подтип и обрабатывает осмысленно.
"""

from __future__ import annotations


class TnvedError(Exception):
    """Базовое исключение проекта.

    `user_message` — то, что можно показать пользователю: без путей, стектрейсов
    и внутренних деталей. Если его нет, пользователю уходит общий текст об ошибке.
    """

    user_message: str | None = None

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        if user_message is not None:
            self.user_message = user_message


class ConfigError(TnvedError):
    """Конфигурация невалидна. Фатально — бот не должен стартовать."""


class AlreadyRunningError(TnvedError):
    """Другая копия бота уже держит файловый лок."""


class UserInputError(TnvedError):
    """Ввод пользователя отклонён: пустой, слишком длинный, подозрительный."""

    user_message = "Не удалось разобрать сообщение. Опишите товар обычными словами."


class LlmError(TnvedError):
    """Сбой обращения к ИИ: недоступен, таймаут, невалидный ответ."""

    user_message = "ИИ временно недоступен. Попробуйте позже."


class NomenclatureError(TnvedError):
    """Проблема со справочником ТН ВЭД: не загружен, повреждён, недоступен."""

    user_message = "Справочник ТН ВЭД недоступен. Обратитесь к администратору."


class StorageError(TnvedError):
    """Проблема с хранением данных: файловая система, место на диске, БД."""

    user_message = "Не удалось сохранить данные. Попробуйте ещё раз."
