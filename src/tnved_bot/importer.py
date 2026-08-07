"""Импорт справочника ТН ВЭД из CSV/XLSX.

Разбор отделён от записи в БД: парсинг и валидация — чистые функции над строками,
их можно проверить без базы. Запись выполняет `NomenclatureRepository.import_version`
одной транзакцией.

Формат источника не зашит: колонки ищутся по заголовку среди синонимов, поэтому переход
на другую выгрузку (например, официальную) не требует переписывания импортёра.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from tnved_bot.core.errors import NomenclatureImportError
from tnved_bot.db.nomenclature import VALID_LEVELS, NomenclatureRow, normalize_code
from tnved_bot.xlsx import read_rows, sheet_names

# Разделитель уровней иерархии в выгрузке TWS. Символ редкий, поэтому распознаём и его,
# и более привычные стрелки — на случай другого источника.
HIERARCHY_SEPARATORS = ("\U0001f83a", "➤", "→", "►", ">>")
PATH_JOINER = " / "

# Заголовки колонок и их синонимы. Сравнение по нормализованному (lower, без пунктуации) виду.
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "code": ("код", "код тн вэд", "код тнвэд", "тнвэд", "code", "hs code"),
    "name": ("наименование", "название", "описание", "name", "description"),
    "tariff": ("тариф", "ставка", "пошлина", "ставка пошлины"),
    "unit": ("единица", "ед изм", "доп ед", "дополнительная единица измерения", "unit"),
    "notes": ("примечание", "примечания", "notes"),
}

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")

# Дата актуальности на обложке выгрузки TWS: строка «Актуальность данных» + дата рядом.
_ACTUALITY_LABELS = ("актуальность", "дата актуальности", "данные на")
_DATE = re.compile(r"\d{2}[.\-/]\d{2}[.\-/]\d{4}|\d{4}-\d{2}-\d{2}")

MAX_HEADER_SEARCH_ROWS = 20


@dataclass(frozen=True, slots=True)
class RejectedRow:
    line: int
    reason: str
    raw_code: str


@dataclass(slots=True)
class ImportReport:
    source: str
    sha256: str
    source_date: str | None = None
    sheet: str | None = None
    rows_read: int = 0
    rows_accepted: int = 0
    rejected: list[RejectedRow] = field(default_factory=list)
    version_id: int | None = None

    @property
    def rows_rejected(self) -> int:
        return len(self.rejected)

    def reason_summary(self) -> dict[str, int]:
        summary: dict[str, int] = {}
        for item in self.rejected:
            summary[item.reason] = summary.get(item.reason, 0) + 1
        return summary


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_header(value: str | None) -> str:
    if not value:
        return ""
    cleaned = _PUNCT.sub(" ", value.strip().lower())
    return _SPACES.sub(" ", cleaned).strip()


def find_columns(header: list[str | None]) -> dict[str, int]:
    """Сопоставляет роли колонок их номерам. Обязательны только `code` и `name`."""
    normalized = [normalize_header(cell) for cell in header]
    mapping: dict[str, int] = {}
    for role, aliases in HEADER_ALIASES.items():
        for index, cell in enumerate(normalized):
            if cell and (cell in aliases or any(cell.startswith(a) for a in aliases)):
                mapping[role] = index
                break
    return mapping


def split_hierarchy(name: str) -> list[str]:
    """Разбивает наименование с путём иерархии на сегменты.

    В выгрузке TWS полный путь лежит прямо в наименовании — это готовый `name_full`
    для полнотекстового поиска, разбирать отдельные уровни не нужно.
    """
    text = name
    for separator in HIERARCHY_SEPARATORS:
        text = text.replace(separator, "\x00")
    return [part.strip() for part in text.split("\x00") if part.strip()]


def build_row(
    raw_code: str, raw_name: str, tariff: str | None, unit: str | None, notes: str | None
) -> NomenclatureRow:
    code = normalize_code(raw_code)
    segments = split_hierarchy(raw_name)
    return NomenclatureRow(
        code=code,
        level=len(code),
        name=segments[-1] if segments else raw_name.strip(),
        name_full=PATH_JOINER.join(segments) if segments else raw_name.strip(),
        # Источник содержит только уровень 10, строк-предков в нём нет. Код родителя
        # вычисляем усечением: это связь для группировки, внешнего ключа на неё нет.
        parent_code=code[:-2] if len(code) > 2 else None,
        unit=unit or None,
        notes=notes or None,
        tariff=tariff or None,
    )


def _cell(row: list[str | None], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return (row[index] or "").strip()


def parse_rows(rows: Iterable[list[str | None]], report: ImportReport) -> Iterator[NomenclatureRow]:
    """Находит заголовок, проверяет строки и отдаёт валидные.

    Битая строка не срывает импорт: она попадает в `report.rejected` с номером и причиной.
    Выгрузки справочника регулярно содержат служебные строки и подытоги.
    """
    columns: dict[str, int] | None = None
    seen: set[str] = set()

    for line, raw in enumerate(rows, start=1):
        if columns is None:
            found = find_columns(raw)
            if "code" in found and "name" in found:
                columns = found
                continue
            if line > MAX_HEADER_SEARCH_ROWS:
                msg = (
                    f"не найдены колонки с кодом и наименованием в первых "
                    f"{MAX_HEADER_SEARCH_ROWS} строках"
                )
                raise NomenclatureImportError(msg)
            continue

        raw_code = _cell(raw, columns.get("code"))
        raw_name = _cell(raw, columns.get("name"))
        if not raw_code and not raw_name:
            continue  # пустая строка-разделитель

        report.rows_read += 1
        code = normalize_code(raw_code)

        if not code:
            report.rejected.append(RejectedRow(line, "код пуст или не содержит цифр", raw_code))
            continue
        if len(code) not in VALID_LEVELS:
            report.rejected.append(
                RejectedRow(line, f"длина кода {len(code)}, ожидалась 2/4/6/8/10", raw_code)
            )
            continue
        if not raw_name:
            report.rejected.append(RejectedRow(line, "пустое наименование", raw_code))
            continue
        if code in seen:
            report.rejected.append(RejectedRow(line, "дубликат кода", raw_code))
            continue

        seen.add(code)
        report.rows_accepted += 1
        yield build_row(
            raw_code,
            raw_name,
            _cell(raw, columns.get("tariff")),
            _cell(raw, columns.get("unit")),
            _cell(raw, columns.get("notes")),
        )

    if columns is None:
        msg = "файл пуст или не содержит колонок с кодом и наименованием"
        raise NomenclatureImportError(msg)


def find_source_date(rows: Iterable[list[str | None]]) -> str | None:
    """Ищет дату актуальности на обложке выгрузки.

    У TWS данные лежат на второй вкладке, а дата — на первой. Без неё нельзя понять,
    насколько справочник свежий, поэтому вытаскиваем её отдельно.
    """
    for raw in rows:
        cells = [(cell or "").strip() for cell in raw]
        joined = " ".join(cells).lower()
        if not any(label in joined for label in _ACTUALITY_LABELS):
            continue
        match = _DATE.search(" ".join(cells))
        if match:
            return match.group(0)
    return None


def _pick_data_sheet(path: Path) -> tuple[str, str | None]:
    """Выбирает вкладку с данными и достаёт дату актуальности с остальных.

    В выгрузке TWS первая вкладка — обложка, данные на второй. Ориентируемся не на номер,
    а на наличие колонок с кодом и наименованием: номер вкладки может измениться.
    """
    names = sheet_names(path)
    source_date: str | None = None
    data_sheet: str | None = None

    for name in names:
        head = []
        for index, row in enumerate(read_rows(path, name)):
            head.append(row)
            if index >= MAX_HEADER_SEARCH_ROWS:
                break
        if data_sheet is None and any(
            "code" in find_columns(row) and "name" in find_columns(row) for row in head
        ):
            data_sheet = name
        if source_date is None:
            source_date = find_source_date(head)

    if data_sheet is None:
        available = ", ".join(names)
        msg = f"ни на одной вкладке нет колонок с кодом и наименованием. Вкладки: {available}"
        raise NomenclatureImportError(msg)
    return data_sheet, source_date


def _read_csv(path: Path) -> Iterator[list[str | None]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(8192)
        handle.seek(0)
        try:
            dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(sample, ";,\t")
        except csv.Error:
            dialect = csv.excel
        for row in csv.reader(handle, dialect):
            yield list(row)


def read_source(path: Path) -> tuple[Iterator[list[str | None]], str | None, str | None]:
    """Возвращает строки данных, имя листа и дату актуальности."""
    if path.suffix.lower() in {".csv", ".txt"}:
        return _read_csv(path), None, None
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        msg = f"неподдерживаемый формат: {path.suffix or 'без расширения'} (нужен .csv или .xlsx)"
        raise NomenclatureImportError(msg)

    sheet, source_date = _pick_data_sheet(path)
    return read_rows(path, sheet), sheet, source_date


def parse_file(path: Path) -> tuple[list[NomenclatureRow], ImportReport]:
    """Читает и проверяет файл целиком, не трогая БД."""
    if not path.is_file():
        msg = f"файл не найден: {path}"
        raise NomenclatureImportError(msg)

    rows_iter, sheet, source_date = read_source(path)
    report = ImportReport(
        source=path.name, sha256=file_sha256(path), sheet=sheet, source_date=source_date
    )
    parsed = list(parse_rows(rows_iter, report))
    return parsed, report
