"""Сравнение ставок пошлин и подбор менее затратного соседнего кода.

Зачем это нужно. Одна и та же на вид вещь в номенклатуре разложена по признакам, и ставка
у соседних позиций отличается кратно: полотна для пил по металлу и по прочим материалам
стоят рядом в 8202, а платить по ним придётся по-разному. Человек, который не знает
номенклатуру, об этой развилке просто не догадывается.

Что здесь **не** делается. Бот не предлагает «взять код подешевле»: код определяется
свойствами товара, а не желанием сэкономить, и подгонка под ставку — это недостоверное
декларирование. Поэтому результат подаётся как вопрос «не подходит ли ваш товар сюда»,
а сами коды берутся только из справочника — как и ставки.

Сравнимость ставок. Адвалорная ставка (`5%`) сравнима с адвалорной. Специфическая
(`1,5 EUR за 1 пар`) не сравнима ни с чем без цены и веса партии, а комбинированная
(`50%, но не менее 1 EUR за 1 кг`) может обернуться чем угодно из двух. Поэтому
сравниваются только чистые проценты — остальное честнее не сравнивать вовсе.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from tnved_bot.core.models import Candidate

_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_CURRENCY = re.compile(r"\b(?:eur|usd|евро|долл)", re.IGNORECASE)

HEADING_LEN = 4
SUBHEADING_LEN = 6
MAX_OPTIONS = 2
# Позиции «части» и «принадлежности» ставке готового изделия не альтернатива: у них
# почти всегда ставка ниже, и предложить их для целого товара значит подсказать
# классическое недостоверное декларирование. Найдено на живой проверке — кофеварке
# предлагалось «8516 90 000 0 части».
_PARTS = ("части", "часть", "принадлежност")
# Разница меньше этого не стоит отдельного блока в ответе: 5% против 4,9% — не находка.
MIN_GAP = 1.0


@dataclass(frozen=True, slots=True)
class Duty:
    raw: str
    percent: float | None
    """Заполнено только для чистой адвалорной ставки — иначе сравнивать нечего."""

    @property
    def comparable(self) -> bool:
        return self.percent is not None


def parse_duty(raw: str | None) -> Duty:
    text = (raw or "").strip()
    if not text:
        return Duty(raw="", percent=None)

    match = _PERCENT.search(text)
    if match is None or _CURRENCY.search(text):
        # Либо ставка вообще не в процентах, либо в ней есть валютная составляющая:
        # «50%, но не менее 1 EUR за 1 кг» — это не 50%.
        return Duty(raw=text, percent=None)
    try:
        return Duty(raw=text, percent=float(match.group(1).replace(",", ".")))
    except ValueError:  # pragma: no cover — регулярка уже гарантирует число
        return Duty(raw=text, percent=None)


@dataclass(frozen=True, slots=True)
class SavingOption:
    """Соседний код с меньшей ставкой."""

    candidate: Candidate
    duty: Duty
    gap: float
    """На сколько процентных пунктов ставка ниже выбранной."""

    docs_free: bool | None = None
    """Известно ли, что для кода нет обязательных разрешительных документов."""


def cheaper_options(
    chosen: Candidate,
    pool: Iterable[Candidate],
    limit: int = MAX_OPTIONS,
    relevant: set[str] | None = None,
) -> list[SavingOption]:
    """Соседние коды с меньшей ставкой, от самой низкой.

    Два ограничения, и второе появилось после живой проверки. Первое — товарная позиция
    (четыре знака): это уровень, на котором один и тот же товар разложен по материалу и
    назначению. Второе — код должен быть либо найден поиском по описанию товара, либо
    делить с выбранным субпозицию (шесть знаков).

    Одной товарной позиции мало. В 8516 рядом с кофеварками лежат нагревательные блоки
    противообледенительных систем воздушных судов — формально сосед, ставка ниже, и в
    ответе про кофеварку это выглядело бы как совет, которому нельзя следовать. Проверка
    на релевантность отсекает ровно такие случаи.
    """
    base = parse_duty(chosen.tariff)
    if not base.comparable:
        # Ставка выбранного кода несравнима — сравнивать с ней тоже нечего.
        return []

    heading = chosen.code[:HEADING_LEN]
    seen: set[str] = {chosen.code}
    options: list[SavingOption] = []

    for candidate in pool:
        if candidate.code in seen or not candidate.code.startswith(heading):
            continue
        if relevant is not None and not (
            candidate.code in relevant or same_subheading(candidate.code, chosen.code)
        ):
            continue
        if is_parts(candidate.name) and not is_parts(chosen.name):
            continue
        duty = parse_duty(candidate.tariff)
        if not duty.comparable or duty.percent is None or base.percent is None:
            continue
        gap = base.percent - duty.percent
        if gap < MIN_GAP:
            continue
        seen.add(candidate.code)
        options.append(SavingOption(candidate=candidate, duty=duty, gap=gap))

    options.sort(key=lambda option: (option.duty.percent or 0.0, option.candidate.code))
    return options[:limit]


def same_subheading(left: str, right: str) -> bool:
    return left[:SUBHEADING_LEN] == right[:SUBHEADING_LEN]


def is_parts(name: str) -> bool:
    """Позиция описывает части изделия, а не изделие целиком."""
    return name.strip().lower().startswith(_PARTS)
