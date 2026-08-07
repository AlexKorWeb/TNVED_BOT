"""Тесты фоновой уборки. Время подменяется — реального ожидания нет."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from PIL import Image

from tnved_bot.clock import iso_ago
from tnved_bot.config import Settings, load_settings
from tnved_bot.db.engine import Database
from tnved_bot.db.photos import PhotoRepository
from tnved_bot.db.sessions import SessionRepository
from tnved_bot.jobs.janitor import Janitor
from tnved_bot.storage.photo_store import PhotoStore


def image_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 20, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
async def parts(
    env: dict[str, str], tmp_path: Path
) -> AsyncIterator[tuple[Janitor, Database, Settings]]:
    settings = load_settings()
    db = Database(settings.abs_path(settings.db_path), settings.abs_path(settings.backup_dir))
    await db.connect()
    yield Janitor(db, settings), db, settings
    await db.close()


async def test_expired_photo_file_is_deleted(parts: tuple[Janitor, Database, Settings]) -> None:
    janitor, db, settings = parts
    store = PhotoStore(settings.abs_path(settings.photo_dir), settings.max_photo_mb)
    stored = await store.save(image_bytes(), user_id=42)
    repo = PhotoRepository(db)
    await repo.add(stored.id, 42, str(stored.path), stored.sha256, stored.size_bytes, 48)

    assert await janitor.sweep_photos() == 0, "свежее фото трогать нельзя"
    assert stored.path.exists()

    await db.execute("UPDATE photos SET expires_at = ? WHERE id = ?", (iso_ago(hours=1), stored.id))
    assert await janitor.sweep_photos() == 1
    assert not stored.path.exists(), "файл обязан исчезнуть с диска, а не только пометиться"

    row = await db.fetch_one("SELECT deleted_at FROM photos WHERE id = ?", (stored.id,))
    assert row is not None and row["deleted_at"] is not None


async def test_catch_up_sweep_on_start(parts: tuple[Janitor, Database, Settings]) -> None:
    """Компьютер выключают на ночь: просроченное за время простоя должно уйти при старте,
    а не ждать следующего часа."""
    janitor, db, settings = parts
    store = PhotoStore(settings.abs_path(settings.photo_dir), settings.max_photo_mb)
    stored = await store.save(image_bytes(), user_id=42)
    await PhotoRepository(db).add(
        stored.id, 42, str(stored.path), stored.sha256, stored.size_bytes, 48
    )
    await db.execute("UPDATE photos SET expires_at = ? WHERE id = ?", (iso_ago(days=3), stored.id))

    await janitor.start(bot=None)
    try:
        assert not stored.path.exists()
    finally:
        await janitor.stop()


async def test_missing_file_does_not_break_sweep(
    parts: tuple[Janitor, Database, Settings],
) -> None:
    janitor, db, settings = parts
    repo = PhotoRepository(db)
    await repo.add(
        "нет-файла", 42, str(settings.abs_path(settings.photo_dir) / "x.jpg"), "hash", 10, 48
    )
    await db.execute("UPDATE photos SET expires_at = ?", (iso_ago(hours=1),))
    # Отсутствующий файл считается удалённым: повторять его вечно бессмысленно.
    assert await janitor.sweep_photos() == 1


async def test_expired_session_closed(parts: tuple[Janitor, Database, Settings]) -> None:
    janitor, db, _ = parts
    sessions = SessionRepository(db)
    await sessions.create("s1", user_id=42, chat_id=42, timeout_minutes=30)
    await db.execute("UPDATE sessions SET expires_at = ?", (iso_ago(minutes=1),))

    assert await janitor.close_expired_sessions() == 1
    assert await sessions.find_open(42) is None


async def test_user_is_told_about_timeout(parts: tuple[Janitor, Database, Settings]) -> None:
    """Молча выбросить начатый диалог — значит оставить человека без ответа."""
    janitor, db, _ = parts
    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id: int, text: str) -> None:
            sent.append((chat_id, text))

    await SessionRepository(db).create("s1", user_id=42, chat_id=77, timeout_minutes=30)
    await db.execute("UPDATE sessions SET expires_at = ?", (iso_ago(minutes=1),))

    janitor._bot = FakeBot()  # noqa: SLF001
    await janitor.close_expired_sessions()

    assert sent
    assert sent[0][0] == 77
    assert "не дождался" in sent[0][1].lower()


async def test_blocked_user_does_not_break_janitor(
    parts: tuple[Janitor, Database, Settings],
) -> None:
    janitor, db, _ = parts

    class BlockingBot:
        async def send_message(self, chat_id: int, text: str) -> None:
            msg = "bot was blocked by the user"
            raise RuntimeError(msg)

    await SessionRepository(db).create("s1", user_id=42, chat_id=77, timeout_minutes=30)
    await db.execute("UPDATE sessions SET expires_at = ?", (iso_ago(minutes=1),))

    janitor._bot = BlockingBot()  # noqa: SLF001
    assert await janitor.close_expired_sessions() == 1


async def test_daily_cleanup_and_backup(parts: tuple[Janitor, Database, Settings]) -> None:
    janitor, db, settings = parts
    await db.execute("INSERT INTO audit_log (ts, event) VALUES (?, 'old')", (iso_ago(days=200),))
    await db.execute("INSERT INTO audit_log (ts, event) VALUES (?, 'new')", (iso_ago(days=1),))

    await janitor.daily()

    rows = await db.fetch_all("SELECT event FROM audit_log")
    assert [row["event"] for row in rows] == ["new"]
    assert list(settings.abs_path(settings.backup_dir).glob("*.db")), "бэкап должен появиться"


async def test_failing_job_does_not_stop_others(
    parts: tuple[Janitor, Database, Settings],
) -> None:
    """Занятый файл не должен приводить к тому, что перестанут чиститься сессии и журнал."""
    janitor, _, _ = parts

    async def explode() -> None:
        msg = "файл занят другим процессом"
        raise OSError(msg)

    guarded = janitor._guard(explode)  # noqa: SLF001
    await guarded()  # не должно бросить
