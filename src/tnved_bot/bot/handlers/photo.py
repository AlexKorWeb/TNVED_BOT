"""Фотография товара.

Порядок шагов важен: сначала фото превращается в текстовое описание, которое пользователь
подтверждает, и только потом идёт обычный текстовый пайплайн. Так пользователь видит, что
именно бот «разглядел», и может поправить — вместо кода, взявшегося непонятно откуда.
"""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.types import Message

from tnved_bot.bot import keyboards, texts
from tnved_bot.core.errors import StorageError
from tnved_bot.core.sanitize import Rejected, clean_user_text
from tnved_bot.db.photos import PhotoRepository
from tnved_bot.db.sessions import SessionRepository
from tnved_bot.llm.vision import VisionReader
from tnved_bot.logging_setup import get_logger
from tnved_bot.storage.photo_store import PhotoStore

log = get_logger(__name__)

IMAGE_MIME_PREFIX = "image/"


async def handle_photo(message: Message, **data: object) -> None:
    photo = message.photo[-1] if message.photo else None
    if photo is None:  # pragma: no cover — фильтр гарантирует наличие
        return
    await _process(message, photo.file_id, **data)


async def handle_document(message: Message, **data: object) -> None:
    """Фотография, отправленная файлом, — распространённый способ не терять качество."""
    document = message.document
    if document is None or not (document.mime_type or "").startswith(IMAGE_MIME_PREFIX):
        await message.answer(
            "Файл не похож на изображение. Пришлите фотографию или опишите товар текстом."
        )
        return
    await _process(message, document.file_id, **data)


async def _process(message: Message, file_id: str, **data: object) -> None:
    user_id: int = data["user_id"]  # type: ignore[assignment]
    sessions: SessionRepository = data["sessions"]  # type: ignore[assignment]
    photos: PhotoRepository = data["photos"]  # type: ignore[assignment]
    store: PhotoStore = data["photo_store"]  # type: ignore[assignment]
    vision: VisionReader = data["vision"]  # type: ignore[assignment]
    ttl_hours: int = data["photo_ttl_hours"]  # type: ignore[assignment]
    timeout_minutes: int = data["session_timeout_minutes"]  # type: ignore[assignment]

    placeholder = await message.answer(texts.LOOKING_AT_PHOTO)

    try:
        raw = await _download(message, file_id)
        stored = await store.save(raw, user_id)
    except StorageError as exc:
        await placeholder.edit_text(exc.user_message or "Не смог обработать изображение.")
        return

    await photos.add(
        stored.id, user_id, str(stored.path), stored.sha256, stored.size_bytes, ttl_hours
    )

    described = await vision.describe(stored.path)
    if described is None:
        await placeholder.edit_text(
            "Не смог разобрать фотографию. Опишите товар текстом — так получится точнее."
        )
        return

    if described.multiple_items:
        await placeholder.edit_text(
            "На фотографии несколько разных товаров. Пришлите их по одному — "
            "код присваивается каждому отдельно."
        )
        return

    description = described.description
    caption = (message.caption or "").strip()
    if caption:
        # Подпись — такой же пользовательский ввод, как обычное сообщение.
        cleaned = clean_user_text(caption)
        if not isinstance(cleaned, Rejected):
            description = f"{description}. {cleaned.text}"

    session = await sessions.create(
        session_id=uuid.uuid4().hex[:16],
        user_id=user_id,
        chat_id=message.chat.id,
        timeout_minutes=timeout_minutes,
        description=description,
        photo_id=stored.id,
    )

    await placeholder.edit_text(
        f"Вижу на фотографии:\n\n<i>{texts.esc(description)}</i>\n\nЭто верно?",
        reply_markup=keyboards.photo_confirm(session.id),
    )


async def _download(message: Message, file_id: str) -> bytes:
    bot = message.bot
    if bot is None:  # pragma: no cover
        msg = "сообщение без привязанного бота"
        raise StorageError(msg)
    buffer = await bot.download(file_id)
    if buffer is None:
        raise StorageError(
            "Telegram не отдал файл",
            user_message="Не удалось скачать фотографию. Попробуйте ещё раз.",
        )
    return buffer.read()


def build_router() -> Router:
    router = Router(name="photo")
    router.message.register(handle_photo, F.photo)
    router.message.register(handle_document, F.document)
    return router
