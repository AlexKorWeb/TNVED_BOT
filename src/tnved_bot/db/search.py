"""Подбор кандидатов по справочнику через FTS5.

Задача поиска — не угадать код, а сузить 13 тысяч позиций до нескольких десятков, из которых
выбирает ИИ. Промах фатален: кода, не попавшего в кандидаты, модель предложить не сможет.

Три решения, каждое — следствие проверки на настоящем справочнике:

1. **Индексируются основы слов, а не исходный текст.** Поиск по словам не связывал формы
   («часы» и «часов» — разные токены), а префиксный поиск по обрубку давал ложные совпадения:
   «горный» → «горн»* → «горностая». Основа, вычисленная одинаково при индексации и при
   запросе, решает обе задачи.
2. **`ё` сворачивается в `е`.** Токенизатор FTS этого не делает, и запрос «мёд» не находил
   ничего, хотя «мед» находил.
3. **Ранжирование по числу совпавших слов, а не по bm25.** Голый bm25 предпочитает короткие
   наименования: «велосипед горный» выдавал «горностая».

Чего поиск принципиально не может: связать бытовое слово с официальным, если общего корня нет.
«Ноутбука» и «кроссовок» в номенклатуре нет вообще — там «машины вычислительные портативные»
и «обувь с верхом из кожи». Этот разрыв закрывает отдельный шаг ИИ (см. `core/classifier.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tnved_bot.db.engine import Database

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")
_WORD = re.compile(r"\w+", re.UNICODE)

STOPWORDS = frozenset(
    # ruff: noqa: SIM905 — строка читаемее, чем список из полусотни коротких литералов
    "и в во на для из с со по от до при как что это или а но же бы ли не ни к у о об "
    "the a of for and with from to in on "
    "шт штук штука кг гр мм см метр литр".split()
)

_ENDINGS = (
    "ования",
    "ованию",
    "иями",
    "ями",
    "ами",
    "ого",
    "его",
    "ому",
    "ему",
    "ыми",
    "ими",
    "ие",
    "ые",
    "ая",
    "яя",
    "ое",
    "ее",
    "ый",
    "ий",
    "ой",
    "ем",
    "ом",
    "ах",
    "ях",
    "ов",
    "ев",
    "ей",
    "ии",
    "ия",
    "ию",
    "ам",
    "ям",
    "ух",
    "у",
    "ю",
    "а",
    "я",
    "о",
    "е",
    "ы",
    "и",
    "ь",
    "й",
)

MIN_STEM = 3
MIN_TOKEN = 2
PER_TOKEN = 60
MAX_TOKENS = 10

# Ступень широкого корня. Длина три, а не четыре: индекс содержит уже усечённые основы,
# и «кофе»* не совпало бы с проиндексированным «коф». Шум компенсируется тем, что такие
# кандидаты получают нулевой вес и оседают в конце выдачи.
BROAD_PREFIX = 3
BROAD_MIN_TOKEN = 6
BROAD_LIMIT = 20

# Соседи по ветке: сколько мест выдачи под них отвести, от скольких лучших находок
# отталкиваться и по скольким знакам кода считать «соседством» (6 = товарная подпозиция).
SIBLING_SLOTS = 12
SIBLING_SEEDS = 4
SIBLING_PREFIX = 6

# Число совпавших слов важнее места совпадения: два совпадения в пути должны побеждать
# одно в собственном наименовании. Отсюда разрыв на порядок между весами.
COUNT_WEIGHT = 10
NAME_BONUS = 1


@dataclass(frozen=True, slots=True)
class SearchHit:
    code: str
    name: str
    name_full: str
    tariff: str | None
    score: float


def fold(text: str) -> str:
    """`ё` → `е`. Иначе «мёд» и «мед» — разные слова, и пользователь остаётся без кандидатов."""
    return text.replace("ё", "е").replace("Ё", "Е")


def stem(token: str) -> str:
    """Грубое усечение окончания. Не лингвистика, а способ склеить формы одного слова."""
    if len(token) <= MIN_STEM:
        return token
    for ending in _ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= MIN_STEM:
            return token[: -len(ending)]
    return token


def stems_of(text: str) -> str:
    """Строка основ для индексации. Применяется и к справочнику, и к запросу."""
    cleaned = _NON_WORD.sub(" ", fold(text).lower())
    seen: list[str] = []
    for word in _SPACES.split(cleaned):
        if not word or word in STOPWORDS:
            continue
        root = stem(word)
        if root not in seen:
            seen.append(root)
    return " ".join(seen)


def tokenize(query: str) -> list[str]:
    """Основы значимых слов запроса.

    Whitelist по буквам и цифрам, а не чёрный список: синтаксис FTS5 (`" * : ^ - NEAR`)
    физически не может попасть в запрос.
    """
    cleaned = _NON_WORD.sub(" ", fold(query).lower())
    tokens: list[str] = []
    for word in _SPACES.split(cleaned):
        if not word or word in STOPWORDS:
            continue
        if len(word) < MIN_TOKEN and not word.isdigit():
            continue
        root = stem(word)
        if root not in tokens:
            tokens.append(root)
    return tokens


def broad_prefixes(tokens: list[str]) -> list[str]:
    """Корни длинных слов: «кофеварк» → «кофе», «электрочайник» → «элек»."""
    prefixes: list[str] = []
    for token in tokens:
        if len(token) >= BROAD_MIN_TOKEN:
            prefix = token[:BROAD_PREFIX]
            if prefix not in prefixes and prefix not in tokens:
                prefixes.append(prefix)
    return prefixes


def build_match(tokens: list[str], operator: str) -> str:
    """Выражение MATCH. Основы ищутся точно: индекс уже содержит основы, а не слова."""
    return f" {operator} ".join(f'"{token}"' for token in tokens)


def match_score(hit: SearchHit, tokens: list[str]) -> int:
    """Сколько слов запроса совпало; совпадение в собственном наименовании чуть весомее."""
    own = {stem(w) for w in _WORD.findall(fold(hit.name).lower())}
    inherited = {stem(w) for w in _WORD.findall(fold(hit.name_full).lower())} - own
    matched = 0
    in_name = 0
    for token in tokens:
        if token in own:
            matched += 1
            in_name += 1
        elif token in inherited:
            matched += 1
    return matched * COUNT_WEIGHT + in_name * NAME_BONUS


def rerank(hits: list[SearchHit], tokens: list[str]) -> list[SearchHit]:
    return sorted(hits, key=lambda h: (-match_score(h, tokens), h.score))


class NomenclatureSearch:
    """Поиск кандидатов в активной версии справочника."""

    # Веса bm25 по колонкам (code, stems_name, stems_full).
    _WEIGHTS = "0.0, 10.0, 4.0"

    def __init__(self, db: Database) -> None:
        self._db = db

    async def search(
        self, query: str, limit: int = 40, chapters: list[str] | None = None
    ) -> list[SearchHit]:
        """`chapters` — двузначные группы ТН ВЭД для сужения выдачи (подсказка от ИИ)."""
        tokens = tokenize(query)
        if not tokens:
            return []

        hits = await self._pool(tokens, chapters)
        if not hits and chapters:
            # Подсказка о группе оказалась неверной — ищем без неё, а не отдаём пустоту.
            hits = await self._pool(tokens, None)
        if not hits:
            hits = await self._like(tokens, limit)

        # max(1, ...) обязателен: при limit меньше числа мест под соседей срез уходил
        # в отрицательный индекс и возвращал почти весь пул вместо limit позиций.
        keep = max(1, limit - SIBLING_SLOTS)
        ranked = rerank(hits, tokens)[:keep]
        siblings = await self._siblings(ranked, limit - len(ranked))
        return ranked + siblings

    async def _pool(self, tokens: list[str], chapters: list[str] | None) -> list[SearchHit]:
        """Пул кандидатов: отдельный запрос на каждое слово плюс запрос на все сразу.

        Одним OR-запросом нельзя: его выдача режется по bm25 **до** того, как кандидатов
        оценят по числу совпавших слов, и нужная позиция вылетает из пула.
        """
        hits: list[SearchHit] = []
        seen: set[str] = set()

        if len(tokens) > 1:
            _merge(hits, seen, await self._match(build_match(tokens, "AND"), PER_TOKEN, chapters))
        for token in tokens[:MAX_TOKENS]:
            _merge(hits, seen, await self._match(build_match([token], "OR"), PER_TOKEN, chapters))

        # Корни длинных слов префиксом: «кофеварк» → «кофе»*. Частично закрывает разрыв
        # между бытовым словом и официальным наименованием. Такие кандидаты получают
        # нулевой вес при переранжировании и оседают в конце списка — но они там есть,
        # а это лучше, чем не показать модели ничего.
        for prefix in broad_prefixes(tokens):
            if len(hits) >= PER_TOKEN * 2:
                break
            _merge(hits, seen, await self._match(f'"{prefix}"*', BROAD_LIMIT, chapters))
        return hits

    async def _match(
        self, expression: str, limit: int, chapters: list[str] | None
    ) -> list[SearchHit]:
        params: list[object] = [expression]
        chapter_filter = ""
        if chapters:
            valid = [c for c in chapters if len(c) == 2 and c.isdigit()]
            if valid:
                placeholders = ", ".join("?" * len(valid))
                chapter_filter = f" AND SUBSTR(n.code, 1, 2) IN ({placeholders})"
                params.extend(valid)
        params.append(limit)

        # S608: в запрос подставляются только константа весов bm25 и плейсхолдеры «?».
        # Ни одно значение от пользователя в текст SQL не попадает.
        rows = await self._db.fetch_all(
            "SELECT f.code, n.name, n.name_full, n.tariff,"  # noqa: S608
            f" bm25(nomenclature_fts, {self._WEIGHTS}) AS score"
            " FROM nomenclature_fts f"
            " JOIN nomenclature n ON n.code = f.code"
            " JOIN nomenclature_version v ON v.id = n.version_id AND v.is_active = 1"
            f" WHERE nomenclature_fts MATCH ?{chapter_filter}"
            " ORDER BY score LIMIT ?",
            params,
        )
        return [_to_hit(row) for row in rows]

    async def _siblings(self, hits: list[SearchHit], limit: int) -> list[SearchHit]:
        """Добирает соседей по ветке для лучших находок.

        Источник не содержит промежуточных уровней иерархии: у кода «3917 23 100 9» в пути
        нет слов «трубы жёсткие из полимеров винилхлорида», они живут в шестизначной позиции,
        которой в выгрузке нет. Поэтому запрос про ПВХ-трубу находит соседнюю ветку, но не
        нужный код. Соседи по шести знакам закрывают этот разрыв — и это ровно то, как
        рассуждает декларант: определил позицию, дальше выбирай подсубпозицию.
        """
        if not hits or limit <= 0:
            return []
        prefixes = list(dict.fromkeys(hit.code[:SIBLING_PREFIX] for hit in hits[:SIBLING_SEEDS]))
        known = {hit.code for hit in hits}

        placeholders = ", ".join("?" * len(prefixes))
        # S608: подставляются только константа SIBLING_PREFIX и плейсхолдеры «?».
        rows = await self._db.fetch_all(
            "SELECT n.code, n.name, n.name_full, n.tariff, 0.0 AS score"  # noqa: S608
            " FROM nomenclature n"
            " JOIN nomenclature_version v ON v.id = n.version_id AND v.is_active = 1"
            f" WHERE SUBSTR(n.code, 1, {SIBLING_PREFIX}) IN ({placeholders})"
            " ORDER BY n.code LIMIT ?",
            (*prefixes, limit + len(known)),
        )
        return [_to_hit(row) for row in rows if row["code"] not in known][:limit]

    async def _like(self, tokens: list[str], limit: int) -> list[SearchHit]:
        """Запасной путь: подстрока по самому длинному слову запроса."""
        longest = max(tokens, key=len)
        if len(longest) < MIN_STEM + 1:
            return []
        rows = await self._db.fetch_all(
            "SELECT n.code, n.name, n.name_full, n.tariff, 0.0 AS score"
            " FROM nomenclature n"
            " JOIN nomenclature_version v ON v.id = n.version_id AND v.is_active = 1"
            " WHERE n.stems_full LIKE ? ESCAPE '\\'"
            " ORDER BY LENGTH(n.name_full) LIMIT ?",
            (f"%{_escape_like(longest)}%", limit),
        )
        return [_to_hit(row) for row in rows]


def _escape_like(value: str) -> str:
    """Экранирует шаблонные символы LIKE, чтобы `%` из запроса не стал маской."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _merge(hits: list[SearchHit], seen: set[str], new: list[SearchHit]) -> None:
    for hit in new:
        if hit.code not in seen:
            seen.add(hit.code)
            hits.append(hit)


def _to_hit(row: object) -> SearchHit:
    return SearchHit(
        code=row["code"],  # type: ignore[index]
        name=row["name"],  # type: ignore[index]
        name_full=row["name_full"],  # type: ignore[index]
        tariff=row["tariff"],  # type: ignore[index]
        score=float(row["score"]),  # type: ignore[index]
    )
