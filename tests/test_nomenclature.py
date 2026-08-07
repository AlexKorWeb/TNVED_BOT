"""Тесты справочника в БД: версии, верификация кода, откат."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tnved_bot.db.engine import Database
from tnved_bot.db.nomenclature import (
    NomenclatureRepository,
    NomenclatureRow,
    format_code,
    normalize_code,
)
from tnved_bot.importer import parse_file

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nomenclature_sample.csv"


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[NomenclatureRepository]:
    db = Database(tmp_path / "tnved.db")
    await db.connect()
    yield NomenclatureRepository(db)
    await db.close()


def rows(*codes: str) -> list[NomenclatureRow]:
    return [
        NomenclatureRow(code=c, level=len(c), name=f"товар {c}", name_full=f"группа / товар {c}")
        for c in codes
    ]


# ---------------------------------------------------------------- нормализация


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8516710000", "8516710000"),
        ("8516 71 000 0", "8516710000"),
        ("8516.71.000.0", "8516710000"),
        ("код 8516710000", "8516710000"),
        ("", ""),
    ],
)
def test_normalize_code(raw: str, expected: str) -> None:
    assert normalize_code(raw) == expected


def test_format_code() -> None:
    assert format_code("8516710000") == "8516 71 000 0"
    assert format_code("8516") == "8516"


# ---------------------------------------------------------------- импорт версий


async def test_import_makes_version_active(repo: NomenclatureRepository) -> None:
    version_id = await repo.import_version("f.csv", "hash1", "07.08.2026", rows("0101210000"))

    active = await repo.active_version()
    assert active is not None
    assert active.id == version_id
    assert active.source_date == "07.08.2026"
    assert active.sha256 == "hash1"
    assert await repo.count() == 1


async def test_new_import_replaces_active(repo: NomenclatureRepository) -> None:
    await repo.import_version("old.csv", "h1", None, rows("0101210000"))
    new_id = await repo.import_version("new.csv", "h2", None, rows("0101210000", "0101300000"))

    active = await repo.active_version()
    assert active is not None
    assert active.id == new_id
    assert await repo.count() == 2, "считаться должна только активная версия"


async def test_fts_holds_only_active_version(repo: NomenclatureRepository) -> None:
    """Иначе поиск выдавал бы коды из старого справочника, а отличить их было бы нечем."""
    db = repo._db  # noqa: SLF001 — тест проверяет внутреннее состояние индекса
    await repo.import_version("old.csv", "h1", None, rows("0101210000", "0101291000"))
    await repo.import_version("new.csv", "h2", None, rows("0101300000"))

    fts = await db.fetch_all("SELECT code FROM nomenclature_fts")
    assert [r["code"] for r in fts] == ["0101300000"]


async def test_find_by_sha256(repo: NomenclatureRepository) -> None:
    await repo.import_version("f.csv", "abc123", None, rows("0101210000"))
    assert (await repo.find_by_sha256("abc123")) is not None
    assert (await repo.find_by_sha256("другой")) is None


async def test_import_is_atomic(repo: NomenclatureRepository) -> None:
    """Обрыв на середине не должен оставить бота с наполовину загруженным справочником."""
    await repo.import_version("good.csv", "h1", None, rows("0101210000"))
    before = await repo.active_version()

    broken = [
        *rows("0101300000"),
        NomenclatureRow(code="0101400000", level=7, name="x", name_full="x"),
    ]
    with pytest.raises((sqlite3.Error, Exception)):
        await repo.import_version("bad.csv", "h2", None, broken)

    after = await repo.active_version()
    assert after is not None and before is not None
    assert after.id == before.id, "активная версия должна остаться прежней"
    assert await repo.count() == 1
    assert await repo.verify_code("0101300000") is None, "частичные данные не должны остаться"


# ---------------------------------------------------------------- верификация


async def test_verify_code_finds_active(repo: NomenclatureRepository) -> None:
    await repo.import_version("f.csv", "h", None, rows("8516710000"))

    found = await repo.verify_code("8516710000")
    assert found is not None
    assert found.name == "товар 8516710000"


async def test_verify_code_accepts_formatted_input(repo: NomenclatureRepository) -> None:
    await repo.import_version("f.csv", "h", None, rows("8516710000"))
    assert await repo.verify_code("8516 71 000 0") is not None


async def test_verify_code_rejects_unknown(repo: NomenclatureRepository) -> None:
    await repo.import_version("f.csv", "h", None, rows("8516710000"))
    assert await repo.verify_code("9999999999") is None


@pytest.mark.parametrize("bad", ["", "123", "12345678901", "abcdefghij", "851671000"])
async def test_verify_code_rejects_malformed(repo: NomenclatureRepository, bad: str) -> None:
    await repo.import_version("f.csv", "h", None, rows("8516710000"))
    assert await repo.verify_code(bad) is None


async def test_verify_code_ignores_inactive_version(repo: NomenclatureRepository) -> None:
    """Ключевой инвариант: код из старой версии не должен уйти пользователю."""
    await repo.import_version("old.csv", "h1", None, rows("0101210000"))
    await repo.import_version("new.csv", "h2", None, rows("0101300000"))

    assert await repo.verify_code("0101210000") is None
    assert await repo.verify_code("0101300000") is not None


async def test_verify_code_without_any_version(repo: NomenclatureRepository) -> None:
    assert await repo.verify_code("8516710000") is None


# ---------------------------------------------------------------- откат


async def test_rollback_restores_previous(repo: NomenclatureRepository) -> None:
    old_id = await repo.import_version("old.csv", "h1", None, rows("0101210000"))
    await repo.import_version("new.csv", "h2", None, rows("0101300000"))

    previous = await repo.rollback()

    assert previous is not None
    assert previous.id == old_id
    assert await repo.verify_code("0101210000") is not None
    assert await repo.verify_code("0101300000") is None


async def test_rollback_does_not_delete_data(repo: NomenclatureRepository) -> None:
    await repo.import_version("old.csv", "h1", None, rows("0101210000"))
    new_id = await repo.import_version("new.csv", "h2", None, rows("0101300000"))

    await repo.rollback()

    assert len(await repo.list_versions()) == 2, "версии должны сохраниться"
    db = repo._db  # noqa: SLF001
    kept = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM nomenclature WHERE version_id = ?", (new_id,)
    )
    assert kept is not None and kept["n"] == 1


async def test_rollback_without_previous_version(repo: NomenclatureRepository) -> None:
    await repo.import_version("only.csv", "h1", None, rows("0101210000"))
    assert await repo.rollback() is None


# ---------------------------------------------------------------- срез справочника


async def test_import_fixture_end_to_end(repo: NomenclatureRepository) -> None:
    parsed, report = parse_file(FIXTURE)
    await repo.import_version(report.source, report.sha256, report.source_date, parsed)

    assert await repo.count() == report.rows_accepted

    coffee = await repo.verify_code("8516 71 000 0")
    assert coffee is not None
    assert "кофе" in coffee.name_full.lower()
    assert coffee.tariff == "8.5%"

    # Ровно тот случай, который в первой редакции ТЗ был записан несуществующим кодом.
    assert await repo.verify_code("3917231009") is not None
    assert await repo.verify_code("3917231000") is None
