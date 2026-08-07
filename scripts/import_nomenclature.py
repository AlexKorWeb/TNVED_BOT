"""Импорт справочника ТН ВЭД в базу бота.

    .\\.venv\\Scripts\\python.exe scripts\\import_nomenclature.py data\\nomenclature\\tnved.xlsx
    .\\.venv\\Scripts\\python.exe scripts\\import_nomenclature.py --rollback
    .\\.venv\\Scripts\\python.exe scripts\\import_nomenclature.py --list

Импорт атомарен: пока новая версия не записана целиком, активной остаётся прежняя.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tnved_bot.config import format_config_error, load_settings  # noqa: E402
from tnved_bot.core.errors import NomenclatureImportError  # noqa: E402
from tnved_bot.db.engine import Database  # noqa: E402
from tnved_bot.db.nomenclature import NomenclatureRepository  # noqa: E402
from tnved_bot.importer import ImportReport, parse_file  # noqa: E402
from tnved_bot.logging_setup import setup_logging  # noqa: E402

MAX_SHOWN_REJECTS = 10


def print_report(report: ImportReport) -> None:
    print(f"Файл:          {report.source}")
    if report.sheet:
        print(f"Вкладка:       {report.sheet}")
    print(f"SHA-256:       {report.sha256[:16]}…")
    print(f"Актуальность:  {report.source_date or 'не указана в источнике'}")
    print(f"Прочитано:     {report.rows_read}")
    print(f"Принято:       {report.rows_accepted}")
    print(f"Отклонено:     {report.rows_rejected}")

    if not report.rejected:
        return
    print("\nПричины отклонения:")
    for reason, count in sorted(report.reason_summary().items(), key=lambda x: -x[1]):
        print(f"  {count:>6}  {reason}")
    print(f"\nПервые {MAX_SHOWN_REJECTS} отклонённых строк:")
    for item in report.rejected[:MAX_SHOWN_REJECTS]:
        print(f"  строка {item.line}: {item.reason} (код: {item.raw_code!r})")


async def run(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 — логгера на этом этапе ещё нет
        print(format_config_error(exc))
        return 1

    setup_logging(settings.abs_path(settings.log_dir), settings.log_level, console=False)
    db = Database(settings.abs_path(settings.db_path), settings.abs_path(settings.backup_dir))
    await db.connect()
    repo = NomenclatureRepository(db)

    try:
        if args.list:
            return await _list_versions(repo)
        if args.rollback:
            return await _rollback(repo)
        return await _import(repo, args)
    finally:
        await db.close()


async def _list_versions(repo: NomenclatureRepository) -> int:
    versions = await repo.list_versions()
    if not versions:
        print("Справочник ещё не загружен.")
        return 0
    active = await repo.active_version()
    print(f"{'':2} {'id':>4}  {'строк':>7}  {'актуальность':<14} {'импорт':<26} источник")
    for version in versions:
        mark = "->" if active and version.id == active.id else "  "
        print(
            f"{mark} {version.id:>4}  {version.rows:>7}  "
            f"{version.source_date or '—':<14} {version.imported_at:<26} {version.source}"
        )
    return 0


async def _rollback(repo: NomenclatureRepository) -> int:
    previous = await repo.rollback()
    if previous is None:
        print("Откатываться не к чему: другой версии справочника нет.")
        return 1
    print(f"Активна версия {previous.id}: {previous.source} ({previous.rows} строк)")
    return 0


async def _import(repo: NomenclatureRepository, args: argparse.Namespace) -> int:
    path = Path(args.path).resolve()  # noqa: ASYNC240 — разовая операция в CLI, не в event loop бота
    try:
        rows, report = parse_file(path)
    except NomenclatureImportError as exc:
        print(f"Импорт невозможен: {exc}")
        return 1

    print_report(report)

    if not rows:
        print("\nНи одной валидной строки — импорт отменён.")
        return 1

    existing = await repo.find_by_sha256(report.sha256)
    if existing is not None and not args.force:
        print(
            f"\nЭтот файл уже импортирован (версия {existing.id} от {existing.imported_at}).\n"
            f"Повторить принудительно: --force"
        )
        return 1

    version_id = await repo.import_version(
        source=report.source,
        sha256=report.sha256,
        source_date=report.source_date,
        rows=rows,
    )
    report.version_id = version_id
    print(f"\nГотово. Активна версия {version_id}, кодов в справочнике: {await repo.count()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Импорт справочника ТН ВЭД ЕАЭС")
    parser.add_argument("path", nargs="?", help="файл CSV или XLSX со справочником")
    parser.add_argument(
        "--force", action="store_true", help="импортировать, даже если этот файл уже загружался"
    )
    parser.add_argument(
        "--rollback", action="store_true", help="вернуть предыдущую версию справочника"
    )
    parser.add_argument("--list", action="store_true", help="показать загруженные версии")
    args = parser.parse_args()

    if not (args.path or args.rollback or args.list):
        parser.error("укажите файл справочника, либо --rollback, либо --list")

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
