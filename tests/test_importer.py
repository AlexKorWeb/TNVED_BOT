"""Тесты разбора и валидации справочника (без БД)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tnved_bot.core.errors import NomenclatureImportError
from tnved_bot.importer import (
    ImportReport,
    build_row,
    file_sha256,
    find_columns,
    find_source_date,
    parse_file,
    parse_rows,
    split_hierarchy,
)

SEP = "\U0001f83a"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nomenclature_sample.csv"


def _report() -> ImportReport:
    return ImportReport(source="test", sha256="0" * 64)


# ---------------------------------------------------------------- колонки


@pytest.mark.parametrize(
    "header",
    [
        ["Код", "Наименование", "Тариф", "Подробности"],
        ["код тн вэд", "описание", "ставка"],
        ["  КОД  ", "Название"],
        ["Code", "Name"],
    ],
)
def test_finds_required_columns(header: list[str]) -> None:
    found = find_columns(header)
    assert found["code"] == 0
    assert found["name"] == 1


def test_column_order_does_not_matter() -> None:
    found = find_columns(["Тариф", "Наименование", "Код"])
    assert found["code"] == 2
    assert found["name"] == 1
    assert found["tariff"] == 0


def test_optional_columns_absent() -> None:
    found = find_columns(["Код", "Наименование"])
    assert "tariff" not in found
    assert "unit" not in found


# ---------------------------------------------------------------- иерархия


def test_split_hierarchy() -> None:
    name = f"лошади: {SEP} прочие: {SEP} убойные"
    assert split_hierarchy(name) == ["лошади:", "прочие:", "убойные"]


def test_name_is_last_segment_and_full_is_path() -> None:
    row = build_row(
        "8516710000", f"Приборы бытовые {SEP} прочие {SEP} для кофе или чая", "8.5%", None, None
    )
    assert row.name == "для кофе или чая"
    assert row.name_full == "Приборы бытовые / прочие / для кофе или чая"
    assert row.tariff == "8.5%"


def test_name_without_separator() -> None:
    row = build_row("0101300000", "ослы", None, None, None)
    assert row.name == "ослы"
    assert row.name_full == "ослы"


def test_code_normalized_and_level_derived() -> None:
    row = build_row("8516 71 000 0", "кофеварки", None, None, None)
    assert row.code == "8516710000"
    assert row.level == 10
    assert row.parent_code == "85167100"


# ---------------------------------------------------------------- валидация


def test_rejects_bad_rows_without_aborting_import() -> None:
    """Выгрузки регулярно содержат подытоги и служебные строки — они не должны срывать импорт."""
    rows: list[list[str | None]] = [
        ["Код", "Наименование"],
        ["0101210000", "лошади"],
        ["", "строка без кода"],
        ["12345", "код нечётной длины"],
        ["0101291000", ""],
        ["0101210000", "дубликат"],
        ["ИТОГО", "подытог"],
        ["0101300000", "ослы"],
    ]
    report = _report()
    parsed = list(parse_rows(rows, report))

    assert [r.code for r in parsed] == ["0101210000", "0101300000"]
    assert report.rows_read == 7
    assert report.rows_accepted == 2
    assert report.rows_rejected == 5

    reasons = report.reason_summary()
    assert reasons["дубликат кода"] == 1
    assert reasons["пустое наименование"] == 1
    assert "длина кода 5, ожидалась 2/4/6/8/10" in reasons


def test_rejected_rows_carry_line_numbers() -> None:
    rows: list[list[str | None]] = [["Код", "Наименование"], ["плохо", "x"]]
    report = _report()
    list(parse_rows(rows, report))
    assert report.rejected[0].line == 2
    assert report.rejected[0].raw_code == "плохо"


def test_blank_rows_ignored_not_rejected() -> None:
    rows: list[list[str | None]] = [
        ["Код", "Наименование"],
        [None, None],
        ["", ""],
        ["0101210000", "лошади"],
    ]
    report = _report()
    parsed = list(parse_rows(rows, report))
    assert len(parsed) == 1
    assert report.rows_rejected == 0


def test_header_can_be_below_cover_rows() -> None:
    rows: list[list[str | None]] = [
        ["Справочник ТН ВЭД", None],
        ["Актуальность данных", "07.08.2026"],
        [None, None],
        ["Код", "Наименование"],
        ["0101210000", "лошади"],
    ]
    report = _report()
    assert len(list(parse_rows(rows, report))) == 1


def test_missing_header_is_a_clear_error() -> None:
    rows: list[list[str | None]] = [["что-то", "другое"] for _ in range(30)]
    with pytest.raises(NomenclatureImportError, match="не найдены колонки"):
        list(parse_rows(rows, _report()))


def test_empty_file_is_a_clear_error() -> None:
    with pytest.raises(NomenclatureImportError, match="пуст"):
        list(parse_rows([], _report()))


# ---------------------------------------------------------------- дата и хеш


@pytest.mark.parametrize(
    ("cells", "expected"),
    [
        (["Актуальность данных", "07.08.2026"], "07.08.2026"),
        (["актуальность", "2026-08-07"], "2026-08-07"),
        (["Данные на", "01.01.2026"], "01.01.2026"),
        (["Всего кодов", "13293"], None),
    ],
)
def test_find_source_date(cells: list[str], expected: str | None) -> None:
    assert find_source_date([cells]) == expected  # type: ignore[list-item]


def test_sha256_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_text("Код,Наименование\n0101210000,лошади\n", encoding="utf-8")
    first = file_sha256(path)
    assert first == file_sha256(path)
    path.write_text("Код,Наименование\n0101210000,кони\n", encoding="utf-8")
    assert file_sha256(path) != first


# ---------------------------------------------------------------- файлы


def test_parses_csv_fixture() -> None:
    rows, report = parse_file(FIXTURE)

    assert report.rows_accepted == len(rows) > 200
    assert report.rows_rejected == 0
    assert all(len(r.code) == 10 for r in rows)
    assert all(r.name for r in rows)

    by_code = {r.code: r for r in rows}
    assert "для приготовления кофе или чая" in by_code["8516710000"].name
    assert by_code["8516710000"].tariff == "8.5%"
    assert " / " in by_code["8516710000"].name_full


def test_unsupported_format(tmp_path: Path) -> None:
    path = tmp_path / "spravochnik.pdf"
    path.write_bytes(b"%PDF-1.4")
    with pytest.raises(NomenclatureImportError, match="неподдерживаемый формат"):
        parse_file(path)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(NomenclatureImportError, match="не найден"):
        parse_file(tmp_path / "нет.csv")


def test_parses_real_xlsx_with_cover_sheet() -> None:
    """Данные TWS лежат на второй вкладке, дата актуальности — на первой."""
    real = Path(__file__).resolve().parents[1] / "data/nomenclature/tnved_tws_2026-08-07.xlsx"
    if not real.exists():
        pytest.skip("файл справочника не загружен")

    rows, report = parse_file(real)

    assert report.sheet == "ТНВЭД"
    assert report.source_date == "07.08.2026"
    assert report.rows_accepted == 13293
    assert report.rows_rejected == 0
    assert {r.code for r in rows} >= {"8516710000", "9105210000", "3917231009"}
