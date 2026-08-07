"""Тесты слоя БД: схема, WAL, повторы при блокировке, восстановление после повреждения."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tnved_bot.core.errors import StorageError
from tnved_bot.db.engine import SCHEMA_VERSION, Database


async def test_creates_database_and_schema(tmp_path: Path) -> None:
    db = Database(tmp_path / "sub" / "tnved.db")
    await db.connect()
    try:
        assert (tmp_path / "sub" / "tnved.db").exists()
        assert await db.user_version() == SCHEMA_VERSION
    finally:
        await db.close()


async def test_all_tables_present(tmp_path: Path) -> None:
    db = Database(tmp_path / "tnved.db")
    await db.connect()
    try:
        rows = await db.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
        names = {row["name"] for row in rows}
    finally:
        await db.close()

    expected = {
        "nomenclature_version",
        "nomenclature",
        "nomenclature_fts",
        "allowed_users",
        "invite_codes",
        "photos",
        "sessions",
        "audit_log",
        "usage_counters",
    }
    assert expected <= names


async def test_wal_and_foreign_keys_enabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "tnved.db")
    await db.connect()
    try:
        mode = await db.fetch_one("PRAGMA journal_mode")
        fk = await db.fetch_one("PRAGMA foreign_keys")
        assert mode is not None
        assert str(mode[0]).lower() == "wal"
        # executescript при применении схемы сбрасывает foreign_keys — проверяем,
        # что он включён обратно, иначе FK молча перестали бы работать.
        assert fk is not None
        assert int(fk[0]) == 1
    finally:
        await db.close()


async def test_restart_preserves_data(tmp_path: Path) -> None:
    path = tmp_path / "tnved.db"
    db = Database(path)
    await db.connect()
    await db.execute(
        "INSERT INTO audit_log (ts, event) VALUES (?, ?)", ("2026-01-01T00:00:00+00:00", "probe")
    )
    await db.close()

    again = Database(path)
    await again.connect()
    try:
        row = await again.fetch_one("SELECT event FROM audit_log")
        assert row is not None
        assert row["event"] == "probe"
        assert await again.user_version() == SCHEMA_VERSION
    finally:
        await again.close()


async def test_only_one_active_nomenclature_version(tmp_path: Path) -> None:
    """Две активные версии означали бы выдачу кодов из неактуального справочника."""
    db = Database(tmp_path / "tnved.db")
    await db.connect()
    try:
        for i in (1, 2):
            await db.execute(
                "INSERT INTO nomenclature_version (source, sha256, imported_at, rows, is_active)"
                " VALUES (?, ?, ?, ?, ?)",
                (f"f{i}.xlsx", f"h{i}", "2026-01-01T00:00:00+00:00", 10, 1 if i == 1 else 0),
            )
        with pytest.raises(StorageError):
            await db.execute(
                "UPDATE nomenclature_version SET is_active = 1 WHERE source = ?", ("f2.xlsx",)
            )
    finally:
        await db.close()


async def test_foreign_key_enforced(tmp_path: Path) -> None:
    db = Database(tmp_path / "tnved.db")
    await db.connect()
    try:
        with pytest.raises(StorageError):
            await db.execute(
                "INSERT INTO nomenclature (code, level, name, name_full, version_id)"
                " VALUES ('0101210000', 10, 'x', 'x', 999)"
            )
    finally:
        await db.close()


async def test_retries_when_database_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Блокировка — временное состояние; пользователь не должен видеть её вообще."""
    db = Database(tmp_path / "tnved.db")
    await db.connect()

    attempts = 0
    original = db.connection.execute

    async def flaky(sql: str, *args: object, **kwargs: object) -> object:
        nonlocal attempts
        if sql.startswith("INSERT INTO audit_log"):
            attempts += 1
            if attempts <= 2:
                msg = "database is locked"
                raise sqlite3.OperationalError(msg)
        return await original(sql, *args, **kwargs)

    monkeypatch.setattr(db.connection, "execute", flaky)
    try:
        await db.execute("INSERT INTO audit_log (ts, event) VALUES (?, ?)", ("t", "probe"))
        assert attempts == 3, "должно быть две неудачи и один успех"
    finally:
        monkeypatch.undo()
        await db.close()


async def test_gives_up_after_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(tmp_path / "tnved.db")
    await db.connect()

    async def always_locked(*_args: object, **_kwargs: object) -> object:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(db.connection, "execute", always_locked)
    try:
        with pytest.raises(StorageError) as exc:
            await db.execute("INSERT INTO audit_log (ts, event) VALUES (?, ?)", ("t", "x"))
        assert "database is locked" in str(exc.value)
    finally:
        monkeypatch.undo()
        await db.close()


async def test_corrupted_database_recovered_from_backup(tmp_path: Path) -> None:
    """Повреждение файла не должно ронять бота: данные берутся из последнего бэкапа."""
    path = tmp_path / "tnved.db"
    backups = tmp_path / "backups"

    db = Database(path, backup_dir=backups)
    await db.connect()
    await db.execute("INSERT INTO audit_log (ts, event) VALUES (?, ?)", ("t", "before_backup"))
    await db.backup(backups, keep=7)
    await db.close()

    path.write_bytes(b"this is definitely not a sqlite database" * 100)

    recovered = Database(path, backup_dir=backups)
    await recovered.connect()
    try:
        row = await recovered.fetch_one("SELECT event FROM audit_log")
        assert row is not None
        assert row["event"] == "before_backup"
    finally:
        await recovered.close()

    assert list(tmp_path.glob("tnved.db.corrupt-*")), "битый файл должен быть сохранён для разбора"


async def test_corrupted_database_without_backup_starts_clean(tmp_path: Path) -> None:
    """Бэкапа нет — начинаем с чистой БД, но процесс всё равно не падает."""
    path = tmp_path / "tnved.db"
    path.write_bytes(b"garbage" * 500)

    db = Database(path, backup_dir=tmp_path / "backups")
    await db.connect()
    try:
        assert await db.user_version() == SCHEMA_VERSION
        rows = await db.fetch_all("SELECT * FROM audit_log")
        assert rows == []
    finally:
        await db.close()

    assert list(tmp_path.glob("tnved.db.corrupt-*"))


async def test_transaction_rolls_back(tmp_path: Path) -> None:
    db = Database(tmp_path / "tnved.db")
    await db.connect()
    try:
        with pytest.raises(RuntimeError):
            async with db.transaction() as conn:
                await conn.execute("INSERT INTO audit_log (ts, event) VALUES ('t', 'inside')")
                msg = "падаем посреди транзакции"
                raise RuntimeError(msg)

        rows = await db.fetch_all("SELECT * FROM audit_log")
        assert rows == [], "данные незавершённой транзакции не должны сохраниться"
    finally:
        await db.close()


async def test_backup_keeps_only_requested_number(tmp_path: Path) -> None:
    db = Database(tmp_path / "tnved.db")
    await db.connect()
    try:
        created = [await db.backup(tmp_path / "backups", keep=3) for _ in range(5)]
        # Имена включают миллисекунды — пять бэкапов подряд не должны схлопнуться в один.
        assert len(set(created)) == 5
        kept = sorted((tmp_path / "backups").glob("tnved-*.db"))
        assert len(kept) == 3
        assert kept == sorted(created[-3:]), "остаться должны три самых свежих"
    finally:
        await db.close()


async def test_query_before_connect_raises_clear_error(tmp_path: Path) -> None:
    db = Database(tmp_path / "tnved.db")
    with pytest.raises(StorageError) as exc:
        await db.fetch_one("SELECT 1")
    assert "не подключена" in str(exc.value)
