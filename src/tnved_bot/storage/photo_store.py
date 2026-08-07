"""Приём и хранение фотографий.

Файл, присланный в Telegram, — недоверенные данные. Проверка расширения ничего не значит:
`.jpg` может оказаться чем угодно. Поэтому три рубежа:

1. **Размер и разрешение** — до открытия картинки, чтобы декомпрессионная бомба не съела память.
2. **`Pillow.verify()`** — файл действительно является изображением.
3. **Обязательный ре-энкод в JPEG** — главный рубеж. Он уничтожает EXIF (в том числе
   геолокацию съёмки), любую нагрузку, приклеенную к валидному изображению, и текст в
   метаданных, который иначе дошёл бы до модели как часть «данных о товаре».

Имена файлов — только UUID4, путь проверяется через `resolve()`. Имя из Telegram не участвует
в построении пути вообще: это самый простой способ не получить `../../.env`.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from tnved_bot.core.errors import StorageError
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

MAX_SIDE = 8000
# Рекомендованный предел для зрения модели: больше не улучшает распознавание, но раздувает
# и вложение, и время ответа.
TARGET_SIDE = 1568
MIN_FREE_MB = 500
JPEG_QUALITY = 85

_MAGIC = (
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"GIF87a",
    b"GIF89a",
    b"BM",  # BMP
    b"RIFF",  # WEBP (проверяется вместе с сигнатурой WEBP ниже)
    b"II*\x00",  # TIFF LE
    b"MM\x00*",  # TIFF BE
)


@dataclass(frozen=True, slots=True)
class StoredPhoto:
    id: str
    path: Path
    sha256: str
    size_bytes: int


class PhotoStore:
    def __init__(self, directory: Path, max_mb: int) -> None:
        self.directory = directory
        self.max_bytes = max_mb * 1024 * 1024

    # ------------------------------------------------------------------ приём

    def has_space(self) -> bool:
        self.directory.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.directory).free // (1024 * 1024) >= MIN_FREE_MB

    async def save(self, raw: bytes, user_id: int) -> StoredPhoto:
        """Проверяет и сохраняет изображение. Тяжёлую часть выполняет отдельный поток."""
        if len(raw) > self.max_bytes:
            msg = f"файл {len(raw) // 1024 // 1024} МБ, допустимо {self.max_bytes // 1024 // 1024}"
            raise StorageError(
                msg,
                user_message="Файл слишком большой. Пришлите фотографию меньшего размера.",
            )
        if not _looks_like_image(raw):
            raise StorageError(
                "магические байты не соответствуют изображению",
                user_message="Не смог прочитать изображение. Пришлите фото ещё раз "
                "или опишите товар текстом.",
            )
        if not self.has_space():
            raise StorageError(
                "мало места на диске",
                user_message="Временно не могу принимать фотографии. Опишите товар текстом.",
            )

        photo_id = uuid.uuid4().hex
        target = self._path_for(photo_id)
        # Pillow — синхронная библиотека и работает с CPU: в event loop ей не место.
        size = await asyncio.to_thread(_reencode, raw, target)

        log.info("photo_stored", user_id=user_id, photo_id=photo_id, bytes=size)
        return StoredPhoto(
            id=photo_id, path=target, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=size
        )

    def _path_for(self, photo_id: str) -> Path:
        """Путь строго внутри каталога фотографий."""
        self.directory.mkdir(parents=True, exist_ok=True)
        base = self.directory.resolve()
        target = (base / f"{photo_id}.jpg").resolve()
        if base not in target.parents:
            msg = f"путь вне каталога фотографий: {target}"
            raise StorageError(msg)
        return target

    # ------------------------------------------------------------------ удаление

    def delete(self, path: Path) -> bool:
        """Удаляет файл, если он внутри управляемого каталога.

        Проверка обязательна: путь приходит из БД, а джанитор не должен уметь удалить
        что-либо за пределами своего каталога, чем бы туда ни попала строка.
        """
        try:
            base = self.directory.resolve()
            target = Path(path).resolve()
        except OSError:
            return False

        if base not in target.parents:
            log.error("photo_delete_outside_dir", path=str(path))
            return False

        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("photo_delete_failed", path=str(path), error=str(exc)[:150])
            return False
        return True

    def delete_all_for(self, paths: list[str]) -> int:
        return sum(1 for path in paths if self.delete(Path(path)))


def _looks_like_image(raw: bytes) -> bool:
    head = raw[:16]
    if head.startswith(b"RIFF"):
        return raw[8:12] == b"WEBP"
    return any(head.startswith(magic) for magic in _MAGIC if magic != b"RIFF")


def _reencode(raw: bytes, target: Path) -> int:
    """Проверяет изображение и пересохраняет в JPEG без метаданных.

    `verify()` расходует объект, поэтому картинка открывается дважды — так предписано
    документацией Pillow.
    """
    import io

    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(raw)) as image:
            if max(image.size) > MAX_SIDE:
                msg = f"изображение {image.size}, допустимо до {MAX_SIDE} px"
                raise StorageError(
                    msg,
                    user_message="Изображение слишком большое. Пришлите фотографию поменьше.",
                )
            image = image.convert("RGB")
            image.thumbnail((TARGET_SIDE, TARGET_SIDE))
            # Новый объект без EXIF и прочих метаданных: сохраняются только пиксели.
            clean = Image.new("RGB", image.size)
            clean.putdata(list(image.getdata()))
            clean.save(target, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        target.unlink(missing_ok=True)
        raise StorageError(
            f"не удалось разобрать изображение: {exc}",
            user_message="Не смог прочитать изображение. Пришлите фото ещё раз "
            "или опишите товар текстом.",
        ) from exc

    return target.stat().st_size
