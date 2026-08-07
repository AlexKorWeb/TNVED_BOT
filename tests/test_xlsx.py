"""Тесты читателя XLSX."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tnved_bot.xlsx import XlsxError, column_index, read_rows, sheet_names

_RELS = (
    '<?xml version="1.0"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    "{items}</Relationships>"
)
_MAIN = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def make_xlsx(path: Path, sheets: dict[str, str]) -> Path:
    """Собирает минимальный XLSX. `sheets` — имя вкладки → готовый XML её `<sheetData>`."""
    with zipfile.ZipFile(path, "w") as archive:
        sheet_tags = "".join(
            f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>'
            for i, name in enumerate(sheets, start=1)
        )
        archive.writestr(
            "xl/workbook.xml", f"<workbook {_MAIN} {_R}><sheets>{sheet_tags}</sheets></workbook>"
        )
        items = "".join(
            f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            for i in range(1, len(sheets) + 1)
        )
        archive.writestr("xl/_rels/workbook.xml.rels", _RELS.format(items=items))
        for i, data in enumerate(sheets.values(), start=1):
            archive.writestr(
                f"xl/worksheets/sheet{i}.xml",
                f"<worksheet {_MAIN}><sheetData>{data}</sheetData></worksheet>",
            )
    return path


def inline_row(ref_row: int, values: dict[str, str]) -> str:
    cells = "".join(
        f'<c r="{col}{ref_row}" t="inlineStr"><is><t>{text}</t></is></c>'
        for col, text in values.items()
    )
    return f'<row r="{ref_row}">{cells}</row>'


@pytest.mark.parametrize(
    ("ref", "expected"),
    [("A1", 0), ("B7", 1), ("Z2", 25), ("AA3", 26), ("AB1", 27), ("BA10", 52)],
)
def test_column_index(ref: str, expected: int) -> None:
    assert column_index(ref) == expected


def test_reads_inline_strings(tmp_path: Path) -> None:
    path = make_xlsx(
        tmp_path / "book.xlsx",
        {
            "Лист": inline_row(1, {"A": "Код", "B": "Наименование"})
            + inline_row(2, {"A": "0101210000", "B": "лошади"})
        },
    )
    assert list(read_rows(path)) == [["Код", "Наименование"], ["0101210000", "лошади"]]


def test_missing_cells_do_not_shift_columns(tmp_path: Path) -> None:
    """Пустые ячейки в XLSX просто отсутствуют. Без разбора ссылки данные съехали бы влево."""
    path = make_xlsx(
        tmp_path / "book.xlsx",
        {
            "Лист": inline_row(1, {"A": "код", "B": "имя", "C": "тариф"})
            + inline_row(2, {"A": "0101210000", "C": "5%"})
        },  # колонка B отсутствует
    )
    rows = list(read_rows(path))
    assert rows[1] == ["0101210000", None, "5%"]


def test_reads_shared_strings(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    make_xlsx(
        path, {"Лист": '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'}
    )
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            f"<sst {_MAIN}><si><t>Код</t></si>"
            f"<si><r><t>Наиме</t></r><r><t>нование</t></r></si></sst>",
        )
    # Второй элемент собран из двух фрагментов rich text — должен склеиться.
    assert list(read_rows(path)) == [["Код", "Наименование"]]


def test_numbers_returned_as_text(tmp_path: Path) -> None:
    path = make_xlsx(
        tmp_path / "book.xlsx", {"Лист": '<row r="1"><c r="A1"><v>8516710000</v></c></row>'}
    )
    assert list(read_rows(path)) == [["8516710000"]]


def test_select_sheet_by_name_and_index(tmp_path: Path) -> None:
    path = make_xlsx(
        tmp_path / "book.xlsx",
        {
            "Обложка": inline_row(1, {"A": "Актуальность данных", "B": "07.08.2026"}),
            "ТНВЭД": inline_row(1, {"A": "Код"}),
        },
    )
    assert sheet_names(path) == ["Обложка", "ТНВЭД"]
    assert list(read_rows(path, "ТНВЭД")) == [["Код"]]
    assert list(read_rows(path, 1)) == [["Код"]]
    assert list(read_rows(path, 0))[0][0] == "Актуальность данных"


def test_unknown_sheet_lists_available(tmp_path: Path) -> None:
    path = make_xlsx(tmp_path / "book.xlsx", {"Лист1": inline_row(1, {"A": "x"})})
    with pytest.raises(XlsxError) as exc:
        list(read_rows(path, "нет такой"))
    assert "Лист1" in str(exc.value)


def test_not_an_xlsx(tmp_path: Path) -> None:
    path = tmp_path / "fake.xlsx"
    path.write_bytes(b"PK\x03\x04 but not really a workbook")
    with pytest.raises((XlsxError, zipfile.BadZipFile)):
        list(read_rows(path))


def test_rejects_billion_laughs(tmp_path: Path) -> None:
    """Проверено: `ElementTree` разворачивает внутренние сущности, и десяток строк XML
    превращается в гигабайты. Закрыто отказом от любого DTD — в настоящем XLSX его нет."""
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz ['
        '<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
        f']><worksheet {_MAIN}><sheetData><row r="1">'
        '<c r="A1" t="inlineStr"><is><t>&lol2;</t></is></c></row></sheetData></worksheet>'
    )
    path = tmp_path / "bomb.xlsx"
    make_xlsx(path, {"Лист": ""})
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            f"<workbook {_MAIN} {_R}><sheets>"
            f'<sheet name="Лист" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            _RELS.format(
                items='<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="ws"/>'
            ),
        )
        archive.writestr("xl/worksheets/sheet1.xml", bomb)

    with pytest.raises(XlsxError, match="DTD"):
        list(read_rows(path))


def test_rejects_dtd_in_shared_strings(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    make_xlsx(path, {"Лист": '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'})
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            f'<?xml version="1.0"?><!DOCTYPE sst []><sst {_MAIN}><si><t>x</t></si></sst>',
        )
    with pytest.raises(XlsxError, match="DTD"):
        list(read_rows(path))


def test_reads_real_nomenclature_file() -> None:
    """Проверка на настоящей выгрузке TWS, если она лежит в проекте."""
    real = Path(__file__).resolve().parents[1] / "data/nomenclature/tnved_tws_2026-08-07.xlsx"
    if not real.exists():
        pytest.skip("файл справочника не загружен")

    assert "ТНВЭД" in sheet_names(real)
    rows = read_rows(real, "ТНВЭД")
    header = next(rows)
    assert header[:2] == ["Код", "Наименование"]
    first = next(rows)
    assert first[0] == "0101210000"
