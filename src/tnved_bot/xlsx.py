"""Минимальный потоковый читатель XLSX на stdlib.

Зачем свой, а не `openpyxl`: нужна одна операция — прочитать лист как строки текста.
`openpyxl` тянет за собой поддержку формул, стилей, графиков и записи файлов — всё это
пришлось бы держать в зависимостях ради ~120 строк работы (см. `.claude/rules/ponytail.md`,
ступень «справится stdlib»).

Ограничения, осознанные: только чтение, только значения; формулы отдаются как их последний
вычисленный результат; даты возвращаются как исходные числа (в справочнике ТН ВЭД дат нет).

Безопасность разбора XML (проверено на этой версии Python, а не взято из документации):

* **XXE — не проходит.** `ElementTree` не разрешает внешние сущности и падает с
  `undefined entity`.
* **Billion laughs — проходил.** Внутренние сущности разворачиваются, и десяток строк XML
  превращается в гигабайты в памяти. Закрыто отказом от любого DTD: в XLSX, созданном любым
  реальным инструментом, `<!DOCTYPE>` не встречается, поэтому запрет ничего не ломает.
* **Zip-бомба** — ограничение на заявленный размер распакованной записи.

Это дешевле подключения `defusedxml` и покрывает обе реальные угрозы. Файл справочника кладёт
администратор вручную, так что вектор и без того узкий, но полагаться на это не стоит.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from xml.etree import ElementTree as ET

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_ROW = f"{{{_MAIN_NS}}}row"
_CELL = f"{{{_MAIN_NS}}}c"
_VALUE = f"{{{_MAIN_NS}}}v"
_INLINE = f"{{{_MAIN_NS}}}is"
_TEXT = f"{{{_MAIN_NS}}}t"
_SHARED_ITEM = f"{{{_MAIN_NS}}}si"
_SHEET = f"{{{_MAIN_NS}}}sheet"
_REL_ID = f"{{{_REL_NS}}}id"


class XlsxError(Exception):
    """Файл не читается как XLSX или отклонён проверками безопасности."""


# Распакованный лист справочника на 13 тыс. строк — единицы мегабайт. Порог с большим
# запасом, но конечный: без него архив на 600 КБ мог бы развернуться в гигабайты.
MAX_ENTRY_BYTES = 256 * 1024 * 1024
_DOCTYPE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)
_HEAD_BYTES = 8192


def _reject_dtd(head: bytes) -> None:
    if _DOCTYPE.search(head[:_HEAD_BYTES]):
        msg = "XML содержит DTD — в XLSX такого не бывает, файл отклонён как небезопасный"
        raise XlsxError(msg)


def _read_entry(archive: zipfile.ZipFile, name: str) -> bytes:
    """Читает запись архива с проверкой размера и запретом DTD."""
    info = archive.getinfo(name)
    if info.file_size > MAX_ENTRY_BYTES:
        msg = f"запись {name} заявляет {info.file_size} байт после распаковки — отклонено"
        raise XlsxError(msg)
    data = archive.read(name)
    _reject_dtd(data)
    return data


def column_index(ref: str) -> int:
    """`A1` → 0, `B7` → 1, `AA3` → 26.

    Нужна, потому что пустые ячейки в XLSX просто отсутствуют: без разбора ссылки
    разреженная строка «съехала» бы на колонку влево.
    """
    index = 0
    for char in ref:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - ord("A") + 1)
    return index - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(_read_entry(archive, "xl/sharedStrings.xml"))  # noqa: S314 — DTD запрещён
    return ["".join(t.text or "" for t in si.iter(_TEXT)) for si in root.iter(_SHARED_ITEM)]


def _sheet_paths(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Пары (имя листа, путь внутри архива) в порядке вкладок."""
    try:
        workbook = ET.fromstring(_read_entry(archive, "xl/workbook.xml"))  # noqa: S314
        rels_root = ET.fromstring(_read_entry(archive, "xl/_rels/workbook.xml.rels"))  # noqa: S314
    except (KeyError, ET.ParseError) as exc:
        msg = f"не похоже на XLSX: {exc}"
        raise XlsxError(msg) from exc

    targets = {
        rel.get("Id"): rel.get("Target", "")
        for rel in rels_root.iter(f"{{{_PKG_REL_NS}}}Relationship")
    }

    sheets: list[tuple[str, str]] = []
    for sheet in workbook.iter(_SHEET):
        name = sheet.get("name") or ""
        target = targets.get(sheet.get(_REL_ID) or "", "")
        if not target:
            continue
        path = target[1:] if target.startswith("/") else f"xl/{target}"
        sheets.append((name, path.replace("xl/xl/", "xl/")))
    return sheets


def sheet_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return [name for name, _ in _sheet_paths(archive)]


def _cell_text(cell: ET.Element, shared: list[str]) -> str | None:
    kind = cell.get("t")
    if kind == "inlineStr":
        inline = cell.find(_INLINE)
        return "".join(t.text or "" for t in inline.iter(_TEXT)) if inline is not None else None

    value = cell.find(_VALUE)
    if value is None or value.text is None:
        return None
    if kind == "s":
        try:
            return shared[int(value.text)]
        except (ValueError, IndexError):
            return None
    return value.text


def read_rows(path: Path, sheet: str | int = 0) -> Iterator[list[str | None]]:
    """Отдаёт строки листа как списки значений. `sheet` — имя вкладки или её номер с нуля.

    Строки выравниваются по реальным номерам колонок из ссылок ячеек, поэтому пропуски
    в середине превращаются в `None`, а не сдвигают данные.
    """
    with zipfile.ZipFile(path) as archive:
        sheets = _sheet_paths(archive)
        if not sheets:
            msg = "в книге нет листов"
            raise XlsxError(msg)

        if isinstance(sheet, int):
            if sheet >= len(sheets):
                msg = f"листа №{sheet} нет, всего листов: {len(sheets)}"
                raise XlsxError(msg)
            target = sheets[sheet][1]
        else:
            matched = [p for name, p in sheets if name == sheet]
            if not matched:
                available = ", ".join(name for name, _ in sheets)
                msg = f"лист '{sheet}' не найден. Доступные: {available}"
                raise XlsxError(msg)
            target = matched[0]

        shared = _shared_strings(archive)

        info = archive.getinfo(target)
        if info.file_size > MAX_ENTRY_BYTES:
            msg = f"лист заявляет {info.file_size} байт после распаковки — отклонено"
            raise XlsxError(msg)

        # Потоковый разбор через XMLPullParser, а не iterparse: нужно самому проверить
        # первый блок на DTD, не читая лист целиком в память.
        parser = ET.XMLPullParser(events=("end",))
        row: list[str | None] = []

        def drain() -> Iterator[list[str | None]]:
            nonlocal row
            # read_events() типизирован под все виды событий (включая start-ns, где вместо
            # элемента приходит кортеж), но запрошены только "end" — там всегда Element.
            events = cast("Iterator[tuple[str, ET.Element]]", parser.read_events())
            for _event, element in events:
                if element.tag == _CELL:
                    ref = element.get("r")
                    index = column_index(ref) if ref else len(row)
                    if index < 0:  # pragma: no cover — защита от битой ссылки
                        index = len(row)
                    row.extend([None] * (index + 1 - len(row)))
                    row[index] = _cell_text(element, shared)
                elif element.tag == _ROW:
                    yield row
                    row = []
                    element.clear()

        with archive.open(target) as stream:
            first = True
            while chunk := stream.read(1 << 16):
                if first:
                    _reject_dtd(chunk)
                    first = False
                parser.feed(chunk)
                yield from drain()

        parser.close()
        yield from drain()
