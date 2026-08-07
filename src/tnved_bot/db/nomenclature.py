"""Работа со справочником ТН ВЭД в БД: версии, запись, верификация кода.

Здесь живёт инвариант проекта: **пользователю не может уйти код, которого нет в активной
версии справочника**. `verify_code` — единственный допустимый способ это проверить.

Полнотекстовый поиск (`search`) добавляется в T-004.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from tnved_bot.clock import now_iso
from tnved_bot.db.engine import Database
from tnved_bot.db.search import stems_of
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

_DIGITS = re.compile(r"\D")
VALID_LEVELS = (2, 4, 6, 8, 10)
HEADING_LEN = 4


def normalize_code(raw: str) -> str:
    """`8516 71 000 0` → `8516710000`. Пользователи пишут коды как угодно."""
    return _DIGITS.sub("", raw or "")


def format_code(code: str) -> str:
    """`8516710000` → `8516 71 000 0` — привычный для декларанта вид."""
    if len(code) != 10:
        return code
    return f"{code[:4]} {code[4:6]} {code[6:9]} {code[9:]}"


@dataclass(frozen=True, slots=True)
class NomenclatureRow:
    code: str
    level: int
    name: str
    name_full: str
    parent_code: str | None = None
    unit: str | None = None
    notes: str | None = None
    tariff: str | None = None

    @property
    def stems_name(self) -> str:
        return stems_of(self.name)

    @property
    def stems_full(self) -> str:
        return stems_of(self.name_full)


@dataclass(frozen=True, slots=True)
class VersionInfo:
    id: int
    source: str
    sha256: str
    source_date: str | None
    imported_at: str
    rows: int


class NomenclatureRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------ версии

    async def active_version(self) -> VersionInfo | None:
        row = await self._db.fetch_one(
            "SELECT id, source, sha256, source_date, imported_at, rows"
            " FROM nomenclature_version WHERE is_active = 1"
        )
        return _to_version(row) if row else None

    async def list_versions(self) -> list[VersionInfo]:
        rows = await self._db.fetch_all(
            "SELECT id, source, sha256, source_date, imported_at, rows"
            " FROM nomenclature_version ORDER BY id DESC"
        )
        return [_to_version(row) for row in rows]

    async def find_by_sha256(self, sha256: str) -> VersionInfo | None:
        row = await self._db.fetch_one(
            "SELECT id, source, sha256, source_date, imported_at, rows"
            " FROM nomenclature_version WHERE sha256 = ? ORDER BY id DESC LIMIT 1",
            (sha256,),
        )
        return _to_version(row) if row else None

    # ------------------------------------------------------------------ запись

    async def import_version(
        self,
        source: str,
        sha256: str,
        source_date: str | None,
        rows: Sequence[NomenclatureRow],
    ) -> int:
        """Записывает новую версию и делает её активной — целиком или никак.

        Всё в одной транзакции: обрыв на середине не должен оставить бота с наполовину
        загруженным справочником или, хуже, с активной версией без строк.
        """
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO nomenclature_version"
                " (source, sha256, source_date, imported_at, rows, is_active)"
                " VALUES (?, ?, ?, ?, ?, 0)",
                (source, sha256, source_date, now_iso(), len(rows)),
            )
            version_id = cursor.lastrowid
            if version_id is None:  # pragma: no cover — INSERT всегда даёт rowid
                msg = "не удалось получить id новой версии справочника"
                raise RuntimeError(msg)

            await conn.executemany(
                "INSERT INTO nomenclature"
                " (code, parent_code, level, name, name_full, unit, notes, tariff,"
                "  stems_name, stems_full, version_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r.code,
                        r.parent_code,
                        r.level,
                        r.name,
                        r.name_full,
                        r.unit,
                        r.notes,
                        r.tariff,
                        r.stems_name,
                        r.stems_full,
                        version_id,
                    )
                    for r in rows
                ],
            )
            await self._activate(conn, version_id)

        log.info("nomenclature_imported", version=version_id, rows=len(rows), source=source)
        return version_id

    async def rollback(self) -> VersionInfo | None:
        """Возвращает предыдущую версию. Данные не удаляются — переключается только флаг."""
        versions = await self.list_versions()
        active = await self.active_version()
        previous = next((v for v in versions if active is None or v.id != active.id), None)
        if previous is None:
            return None

        async with self._db.transaction() as conn:
            await self._activate(conn, previous.id)

        log.info("nomenclature_rolled_back", version=previous.id, source=previous.source)
        return previous

    async def _activate(self, conn: object, version_id: int) -> None:
        """Переключает активную версию и пересобирает FTS-индекс под неё.

        Снятие старого флага обязано идти до установки нового: уникальный частичный индекс
        не допускает двух активных версий одновременно.

        FTS хранит только активную версию — иначе поиск выдавал бы коды из старого
        справочника, а отличить их в выдаче было бы нечем.
        """
        execute = conn.execute  # type: ignore[attr-defined]
        await execute("UPDATE nomenclature_version SET is_active = 0 WHERE is_active = 1")
        await execute("UPDATE nomenclature_version SET is_active = 1 WHERE id = ?", (version_id,))
        await execute("DELETE FROM nomenclature_fts")
        await execute(
            "INSERT INTO nomenclature_fts (code, stems_name, stems_full)"
            " SELECT code, stems_name, stems_full FROM nomenclature WHERE version_id = ?",
            (version_id,),
        )

    # ------------------------------------------------------------------ чтение

    async def verify_code(self, code: str) -> NomenclatureRow | None:
        """Существует ли код в **активной** версии.

        Через эту функцию обязан пройти каждый код перед отправкой пользователю. Ответ
        `None` означает «такого кода нет» — в том числе если он есть, но в неактивной версии.
        """
        normalized = normalize_code(code)
        if len(normalized) not in VALID_LEVELS:
            return None
        row = await self._db.fetch_one(
            "SELECT n.code, n.parent_code, n.level, n.name, n.name_full, n.unit, n.notes, n.tariff"
            " FROM nomenclature n"
            " JOIN nomenclature_version v ON v.id = n.version_id AND v.is_active = 1"
            " WHERE n.code = ?",
            (normalized,),
        )
        return _to_row(row) if row else None

    async def heading_codes(self, code: str, limit: int = 200) -> list[NomenclatureRow]:
        """Все позиции той же товарной позиции (первые четыре знака) из активной версии.

        Нужны для сравнения ставок: соседние подсубпозиции — это тот же товар, разложенный
        по материалу и назначению, и именно там прячется разница в пошлине. Брать их из
        результатов поиска нельзя — поиск возвращает то, что похоже на описание, а не всё,
        что рядом по классификации.
        """
        heading = normalize_code(code)[:HEADING_LEN]
        if len(heading) < HEADING_LEN:
            return []
        rows = await self._db.fetch_all(
            "SELECT n.code, n.parent_code, n.level, n.name, n.name_full, n.unit, n.notes, n.tariff"
            " FROM nomenclature n"
            " JOIN nomenclature_version v ON v.id = n.version_id AND v.is_active = 1"
            " WHERE n.code LIKE ? AND n.level = 10"
            " ORDER BY n.code LIMIT ?",
            (f"{heading}%", limit),
        )
        return [_to_row(row) for row in rows]

    async def count(self) -> int:
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM nomenclature n"
            " JOIN nomenclature_version v ON v.id = n.version_id AND v.is_active = 1"
        )
        return int(row["n"]) if row else 0


def _to_version(row: object) -> VersionInfo:
    return VersionInfo(
        id=row["id"],  # type: ignore[index]
        source=row["source"],  # type: ignore[index]
        sha256=row["sha256"],  # type: ignore[index]
        source_date=row["source_date"],  # type: ignore[index]
        imported_at=row["imported_at"],  # type: ignore[index]
        rows=row["rows"],  # type: ignore[index]
    )


def _to_row(row: object) -> NomenclatureRow:
    return NomenclatureRow(
        code=row["code"],  # type: ignore[index]
        parent_code=row["parent_code"],  # type: ignore[index]
        level=row["level"],  # type: ignore[index]
        name=row["name"],  # type: ignore[index]
        name_full=row["name_full"],  # type: ignore[index]
        unit=row["unit"],  # type: ignore[index]
        notes=row["notes"],  # type: ignore[index]
        tariff=row["tariff"],  # type: ignore[index]
    )
