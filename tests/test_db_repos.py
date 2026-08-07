"""Тесты репозиториев: аудит, счётчики, фото, сессии."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tnved_bot.clock import iso_ago, iso_in
from tnved_bot.db.audit import AuditLog, text_digest
from tnved_bot.db.counters import GLOBAL_USER_ID, UsageCounters, window_start
from tnved_bot.db.engine import Database
from tnved_bot.db.photos import PhotoRepository
from tnved_bot.db.sessions import SessionRepository


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "tnved.db")
    await database.connect()
    yield database
    await database.close()


# ---------------------------------------------------------------- аудит


async def test_audit_records_event(db: Database) -> None:
    audit = AuditLog(db)
    await audit.record("access_denied", user_id=42, payload={"digest": text_digest("труба")})

    row = await db.fetch_one("SELECT * FROM audit_log")
    assert row is not None
    assert row["event"] == "access_denied"
    assert row["user_id"] == 42
    assert row["ok"] == 1


async def test_audit_refuses_to_store_user_text(db: Database) -> None:
    """Сырой текст пользователя в журнале — утечка переписки в БД. Ловим на входе."""
    audit = AuditLog(db)
    for forbidden in ("text", "caption", "description", "code"):
        with pytest.raises(ValueError, match="text_digest"):
            await audit.record("classified", payload={forbidden: "кофеварка бытовая"})


async def test_text_digest_is_stable_and_opaque() -> None:
    digest = text_digest("кофеварка капельная")
    assert digest == text_digest("кофеварка капельная")
    assert digest != text_digest("кофеварка капельная ")
    assert "кофеварка" not in digest


async def test_audit_purge_removes_only_old(db: Database) -> None:
    await db.execute("INSERT INTO audit_log (ts, event) VALUES (?, 'old')", (iso_ago(days=200),))
    await db.execute("INSERT INTO audit_log (ts, event) VALUES (?, 'new')", (iso_ago(days=1),))

    removed = await AuditLog(db).purge_older_than(90)

    assert removed == 1
    rows = await db.fetch_all("SELECT event FROM audit_log")
    assert [r["event"] for r in rows] == ["new"]


# ---------------------------------------------------------------- счётчики


async def test_counter_increments(db: Database) -> None:
    counters = UsageCounters(db)
    assert await counters.bump(1, "hour") == 1
    assert await counters.bump(1, "hour") == 2
    assert await counters.current(1, "hour") == 2


async def test_counters_isolated_by_user_and_window(db: Database) -> None:
    counters = UsageCounters(db)
    await counters.bump(1, "hour")
    await counters.bump(1, "hour")
    await counters.bump(2, "hour")
    await counters.bump(GLOBAL_USER_ID, "day")

    assert await counters.current(1, "hour") == 2
    assert await counters.current(2, "hour") == 1
    assert await counters.current(1, "day") == 0
    assert await counters.current(GLOBAL_USER_ID, "day") == 1


async def test_counter_survives_reconnect(tmp_path: Path) -> None:
    """Лимиты в памяти обнулялись бы при каждом автоперезапуске планировщиком."""
    path = tmp_path / "tnved.db"
    first = Database(path)
    await first.connect()
    await UsageCounters(first).bump(7, "day")
    await first.close()

    second = Database(path)
    await second.connect()
    try:
        assert await UsageCounters(second).current(7, "day") == 1
    finally:
        await second.close()


async def test_concurrent_bumps_do_not_lose_increments(db: Database) -> None:
    """Read-modify-write двумя запросами потерял бы часть инкрементов при параллельных
    сообщениях одного пользователя. Проверяем, что UPSERT ... RETURNING этого не допускает."""
    counters = UsageCounters(db)
    await asyncio.gather(*(counters.bump(5, "hour") for _ in range(20)))
    assert await counters.current(5, "hour") == 20


def test_window_start_truncates() -> None:
    assert window_start("hour").endswith(":00:00+00:00")
    day = window_start("day")
    assert day.endswith("T00:00:00+00:00")


# ---------------------------------------------------------------- фото


async def test_photo_add_sets_expiry(db: Database) -> None:
    record = await PhotoRepository(db).add("uuid-1", 42, "data/photos/uuid-1.jpg", "abc", 1024, 48)
    assert record.expires_at > iso_in(hours=47)

    row = await db.fetch_one("SELECT expires_at, deleted_at FROM photos WHERE id = 'uuid-1'")
    assert row is not None
    assert row["expires_at"] == record.expires_at
    assert row["deleted_at"] is None


async def test_list_expired_returns_only_overdue_and_undeleted(db: Database) -> None:
    repo = PhotoRepository(db)
    await repo.add("fresh", 1, "p1", "h", 10, 48)
    await repo.add("old", 1, "p2", "h", 10, 48)
    await repo.add("already-gone", 1, "p3", "h", 10, 48)

    await db.execute(
        "UPDATE photos SET expires_at = ? WHERE id IN ('old', 'already-gone')", (iso_ago(hours=1),)
    )
    await repo.mark_deleted("already-gone")

    expired = await repo.list_expired()
    assert [p.id for p in expired] == ["old"]


async def test_mark_deleted_is_idempotent(db: Database) -> None:
    repo = PhotoRepository(db)
    await repo.add("uuid-2", 1, "p", "h", 10, 48)
    await repo.mark_deleted("uuid-2")
    first = await db.fetch_one("SELECT deleted_at FROM photos WHERE id = 'uuid-2'")
    await repo.mark_deleted("uuid-2")
    second = await db.fetch_one("SELECT deleted_at FROM photos WHERE id = 'uuid-2'")

    assert first is not None and second is not None
    assert first["deleted_at"] == second["deleted_at"], "повтор не должен переписывать метку"


async def test_purge_metadata_keeps_undeleted(db: Database) -> None:
    repo = PhotoRepository(db)
    await repo.add("live", 1, "p", "h", 10, 48)
    await repo.add("gone", 1, "p", "h", 10, 48)
    await repo.mark_deleted("gone")
    await db.execute("UPDATE photos SET deleted_at = ? WHERE id = 'gone'", (iso_ago(days=40),))

    removed = await repo.purge_metadata_older_than(30)

    assert removed == 1
    rows = await db.fetch_all("SELECT id FROM photos")
    assert [r["id"] for r in rows] == ["live"]


# ---------------------------------------------------------------- сессии


async def test_session_roundtrip(db: Database) -> None:
    repo = SessionRepository(db)
    created = await repo.create(
        "s1", user_id=42, chat_id=42, timeout_minutes=30, description="труба"
    )

    created.state = "clarifying"
    created.answers = [{"q": "материал", "a": "пластик"}]
    created.candidates = [{"code": "3917231009"}]
    created.round = 1
    await repo.save(created, timeout_minutes=30)

    loaded = await repo.get("s1")
    assert loaded is not None
    assert loaded.state == "clarifying"
    assert loaded.answers == [{"q": "материал", "a": "пластик"}]
    assert loaded.candidates == [{"code": "3917231009"}]
    assert loaded.round == 1


async def test_get_missing_session_returns_none(db: Database) -> None:
    assert await SessionRepository(db).get("нет такой") is None


async def test_list_expired_ignores_closed(db: Database) -> None:
    repo = SessionRepository(db)
    await repo.create("open-old", 1, 1, timeout_minutes=30)
    await repo.create("open-fresh", 1, 1, timeout_minutes=30)
    await repo.create("closed-old", 1, 1, timeout_minutes=30)

    await db.execute(
        "UPDATE sessions SET expires_at = ? WHERE id IN ('open-old', 'closed-old')",
        (iso_ago(minutes=5),),
    )
    await repo.close("closed-old")

    expired = await repo.list_expired()
    assert [s.id for s in expired] == ["open-old"]


async def test_close_user_sessions(db: Database) -> None:
    """Отзыв доступа должен немедленно закрывать начатые диалоги пользователя."""
    repo = SessionRepository(db)
    await repo.create("a", user_id=7, chat_id=7, timeout_minutes=30)
    await repo.create("b", user_id=7, chat_id=7, timeout_minutes=30)
    await repo.create("other", user_id=8, chat_id=8, timeout_minutes=30)

    closed = await repo.close_user_sessions(7)

    assert closed == 2
    assert await repo.find_open(7) is None
    assert await repo.find_open(8) is not None


async def test_invalid_state_rejected(db: Database) -> None:
    """CHECK в схеме не даёт записать состояние, которого нет в модели."""
    from tnved_bot.core.errors import StorageError

    await SessionRepository(db).create("s", 1, 1, timeout_minutes=30)
    with pytest.raises(StorageError):
        await db.execute("UPDATE sessions SET state = 'нечто' WHERE id = 's'")


async def test_save_extends_expiry(db: Database) -> None:
    repo = SessionRepository(db)
    session = await repo.create("s", 1, 1, timeout_minutes=30)
    await db.execute("UPDATE sessions SET expires_at = ? WHERE id = 's'", (iso_ago(minutes=1),))

    await repo.save(session, timeout_minutes=30)

    assert await repo.list_expired() == [], "активность должна отодвигать таймаут"
