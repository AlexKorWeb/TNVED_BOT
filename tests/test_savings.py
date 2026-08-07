"""Сравнение ставок пошлин и подбор соседнего кода с меньшей ставкой."""

from __future__ import annotations

import pytest

from tnved_bot.core.models import Candidate
from tnved_bot.core.savings import cheaper_options, parse_duty


def candidate(code: str, tariff: str | None, name: str = "позиция") -> Candidate:
    return Candidate(code=code, name=name, name_full=name, tariff=tariff)


@pytest.mark.parametrize(
    ("raw", "percent"),
    [
        ("0%", 0.0),
        ("5%", 5.0),
        ("8,5%", 8.5),
        ("8.5 %", 8.5),
        (" 12% ", 12.0),
    ],
)
def test_ad_valorem_is_comparable(raw: str, percent: float) -> None:
    duty = parse_duty(raw)
    assert duty.comparable
    assert duty.percent == percent


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "1.5 EUR за 1 пар",
        "0.12 евро за 1 кг",
        "50%, но не менее 1 EUR за 1 кг",
        "беспошлинно",
    ],
)
def test_specific_and_combined_rates_are_not_comparable(raw: str | None) -> None:
    """Специфическую ставку нельзя сравнить с процентом без цены и веса партии.

    «50%, но не менее 1 EUR за 1 кг» — это не 50%: при дешёвом товаре сработает вторая
    часть. Считать такую ставку процентной значило бы врать в блоке про выгоду.
    """
    assert not parse_duty(raw).comparable


def test_finds_cheaper_neighbour_in_same_heading() -> None:
    chosen = candidate("8202910000", "10%", "полотна для пил по металлу")
    pool = [
        candidate("8202990000", "0%", "полотна для пил прочие"),
        candidate("8202200000", "2%", "полотна для ленточных пил"),
    ]

    options = cheaper_options(chosen, pool)

    assert [option.candidate.code for option in options] == ["8202990000", "8202200000"]
    assert options[0].gap == 10.0  # noqa: PLR2004


def test_ignores_codes_from_another_heading() -> None:
    """Позиция из другой товарной позиции — уже другой товар, а не альтернатива."""
    chosen = candidate("8202910000", "10%")
    options = cheaper_options(chosen, [candidate("8516710000", "0%")])
    assert options == []


def test_ignores_equal_and_higher_rates() -> None:
    chosen = candidate("8202910000", "3%")
    pool = [candidate("8202990000", "3%"), candidate("8202200000", "5%")]
    assert cheaper_options(chosen, pool) == []


def test_ignores_negligible_difference() -> None:
    """5% против 4,9% — не находка, а шум в ответе."""
    chosen = candidate("8202910000", "5%")
    assert cheaper_options(chosen, [candidate("8202990000", "4,9%")]) == []


def test_incomparable_base_rate_disables_the_block() -> None:
    chosen = candidate("6403511100", "1,5 EUR за 1 пар")
    assert cheaper_options(chosen, [candidate("6403990000", "0%")]) == []


def test_incomparable_candidate_is_skipped() -> None:
    chosen = candidate("6403511100", "10%")
    pool = [candidate("6403990000", "0.5 EUR за 1 пар"), candidate("6403400000", "5%")]

    options = cheaper_options(chosen, pool)

    assert [option.candidate.code for option in options] == ["6403400000"]


def test_chosen_code_is_never_offered_to_itself() -> None:
    chosen = candidate("8202910000", "10%")
    assert cheaper_options(chosen, [chosen]) == []


def test_limit_is_respected_and_cheapest_first() -> None:
    chosen = candidate("8202910000", "10%")
    pool = [
        candidate("8202200000", "5%"),
        candidate("8202300000", "1%"),
        candidate("8202400000", "3%"),
    ]

    options = cheaper_options(chosen, pool, limit=2)

    assert [option.candidate.code for option in options] == ["8202300000", "8202400000"]


def test_irrelevant_neighbour_is_not_offered() -> None:
    """В одной товарной позиции соседями бывают совершенно разные товары.

    Живая проверка: рядом с кофеварками в 8516 лежат нагревательные блоки
    противообледенительных систем воздушных судов. Формально сосед и дешевле — но в
    ответе про кофеварку это совет, которому нельзя следовать.
    """
    chosen = candidate("8516710000", "8.5%")
    aircraft = candidate("8516802001", "5%", "блоки для воздушных судов")

    # Поиск по описанию товара этот код не нашёл — значит, и предлагать его не за что.
    assert cheaper_options(chosen, [aircraft], relevant=set()) == []


def test_same_subheading_neighbour_is_offered_even_if_search_missed_it() -> None:
    """Внутри субпозиции коды описывают один товар с разными признаками."""
    chosen = candidate("8202910000", "10%")
    sibling = candidate("8202919000", "2%")

    options = cheaper_options(chosen, [sibling], relevant=set())

    assert [option.candidate.code for option in options] == ["8202919000"]


def test_relevant_neighbour_from_search_is_offered() -> None:
    chosen = candidate("8202910000", "10%")
    other = candidate("8202990000", "0%")

    options = cheaper_options(chosen, [other], relevant={"8202990000"})

    assert [option.candidate.code for option in options] == ["8202990000"]


def test_parts_position_is_not_offered_for_a_finished_product() -> None:
    """У «частей» ставка почти всегда ниже — но целое изделие не декларируют как части.

    Живая проверка: кофеварке предлагалось «8516 90 000 0 части» на 3,5 п.п. дешевле.
    """
    chosen = candidate("8516710000", "8.5%", "для приготовления кофе или чая")
    parts = candidate("8516900000", "5%", "части")

    assert cheaper_options(chosen, [parts], relevant={"8516900000"}) == []


def test_parts_position_is_offered_when_the_goods_are_parts_too() -> None:
    chosen = candidate("8516900000", "10%", "части")
    other = candidate("8516901000", "2%", "части прочие")

    options = cheaper_options(chosen, [other], relevant={"8516901000"})

    assert [option.candidate.code for option in options] == ["8516901000"]
