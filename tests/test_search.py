"""Тесты подбора кандидатов по справочнику."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tnved_bot.db.engine import Database
from tnved_bot.db.nomenclature import NomenclatureRepository
from tnved_bot.db.search import NomenclatureSearch, build_match, stem, tokenize
from tnved_bot.importer import parse_file

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nomenclature_sample.csv"


@pytest.fixture
async def search(tmp_path: Path) -> AsyncIterator[NomenclatureSearch]:
    db = Database(tmp_path / "tnved.db")
    await db.connect()
    rows, report = parse_file(FIXTURE)
    await NomenclatureRepository(db).import_version(
        report.source, report.sha256, report.source_date, rows
    )
    yield NomenclatureSearch(db)
    await db.close()


# ---------------------------------------------------------------- токенизация


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("кофеварка", "кофеварк"),
        ("кофеварки", "кофеварк"),
        ("кофеварке", "кофеварк"),
        ("трубы", "труб"),
        ("часы", "час"),
        ("часов", "час"),
        ("часах", "час"),
        ("дом", "дом"),  # короткое слово не трогаем
    ],
)
def test_stem_collapses_forms(word: str, expected: str) -> None:
    assert stem(word) == expected


def test_tokenize_drops_stopwords_and_punctuation() -> None:
    assert tokenize("кофеварка для дома, 900 Вт!") == ["кофеварк", "дом", "900", "вт"]


def test_tokenize_deduplicates() -> None:
    assert tokenize("труба труба трубы") == ["труб"]


@pytest.mark.parametrize(
    "malicious",
    [
        'труба" OR "1"="1',
        "труба* NEAR/5 кран",
        "труба^2 ИЛИ кран",
        "труба - кран",
        '"" AND ""',
        "NEAR NEAR NEAR",
        "труба: кран",
    ],
)
def test_fts_syntax_cannot_leak_into_query(malicious: str) -> None:
    """Whitelist по буквам и цифрам: синтаксис FTS5 физически не попадает в запрос."""
    tokens = tokenize(malicious)
    for token in tokens:
        assert token.isalnum(), f"в токене {token!r} остался спецсимвол"
    expression = build_match(tokens, "AND") if tokens else ""
    assert expression.count('"') % 2 == 0, "кавычки должны быть парными"


# ---------------------------------------------------------------- поиск


async def test_finds_coffee_maker(search: NomenclatureSearch) -> None:
    hits = await search.search("кофеварка капельная бытовая")
    assert hits
    assert any(h.code == "8516710000" for h in hits[:10]), (
        "нужный код обязан попасть в первую десятку — иначе модель его не увидит"
    )


async def test_finds_by_different_word_form(search: NomenclatureSearch) -> None:
    """«часы» и «часов» должны вести к одной позиции."""
    one = await search.search("часы настенные")
    other = await search.search("часов настенных")
    assert {h.code for h in one} & {h.code for h in other}


async def test_household_word_reaches_official_wording(search: NomenclatureSearch) -> None:
    """Пользователь пишет «кофеварка», а справочник — «для приготовления кофе или чая».

    Без ступени широкого префикса поиск вернул бы пустоту, и модели было бы не из чего
    выбирать. Это разрыв между бытовым и официальным языком, а не опечатка.
    """
    hits = await search.search("кофеварка")
    assert hits, "одиночное бытовое слово не должно давать пустой список"
    assert any(h.code == "8516710000" for h in hits)


async def test_finds_wall_clock(search: NomenclatureSearch) -> None:
    hits = await search.search("часы настенные электрические")
    assert any(h.code == "9105210000" for h in hits[:10])


async def test_empty_query_returns_nothing(search: NomenclatureSearch) -> None:
    assert await search.search("") == []
    assert await search.search("и в на для") == []


async def test_unknown_words_return_empty_not_error(search: NomenclatureSearch) -> None:
    hits = await search.search("абвгдеёжзий клмнопрст")
    assert hits == []


async def test_malicious_query_does_not_break_search(search: NomenclatureSearch) -> None:
    for malicious in ('труба" OR "1"="1', "труба* NEAR/5 кран", "^труба", "труба -кран"):
        hits = await search.search(malicious)
        assert isinstance(hits, list)


async def test_limit_respected(search: NomenclatureSearch) -> None:
    hits = await search.search("прочие", limit=5)
    assert len(hits) <= 5


async def test_results_are_unique(search: NomenclatureSearch) -> None:
    hits = await search.search("прочие изделия из пластмасс")
    codes = [h.code for h in hits]
    assert len(codes) == len(set(codes))


async def test_hit_carries_useful_fields(search: NomenclatureSearch) -> None:
    hits = await search.search("кофеварка капельная бытовая")
    matched = [h for h in hits if h.code == "8516710000"]
    assert matched, "искомая позиция не найдена"
    hit = matched[0]
    assert hit.name
    assert " / " in hit.name_full
    assert hit.tariff == "8.5%"


async def test_search_ignores_inactive_version(tmp_path: Path) -> None:
    """Кандидат из старой версии не должен попасть в выдачу."""
    db = Database(tmp_path / "tnved.db")
    await db.connect()
    repo = NomenclatureRepository(db)
    rows, report = parse_file(FIXTURE)
    await repo.import_version(report.source, report.sha256, report.source_date, rows)

    from tnved_bot.db.nomenclature import NomenclatureRow

    await repo.import_version(
        "other.csv",
        "hash2",
        None,
        [NomenclatureRow(code="0101210000", level=10, name="лошади", name_full="лошади")],
    )

    hits = await NomenclatureSearch(db).search("кофеварка")
    assert hits == [], "поиск обязан жить только в активной версии"
    await db.close()
