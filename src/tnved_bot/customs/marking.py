"""Маркировка «Честный знак» по коду ТН ВЭД.

Локальная таблица, а не запрос в интернет: перечни маркируемых товаров утверждаются
постановлениями Правительства и опубликованы списком кодов — это статичные данные, за
которыми незачем ходить в сеть на каждый запрос.

Осторожность в формулировках здесь не вежливость, а требование к точности. Перечни
привязаны не только к коду, но и к виду товара («отдельные виды радиоэлектронной
продукции»), меняются несколько раз в год и содержат исключения. Поэтому таблица даёт
подсказку «вероятно, подпадает — проверьте», а бот нигде не утверждает обратное:
отсутствие кода в таблице означает «не нашлось у нас», а не «маркировка не нужна».
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

TABLE_PATH = Path(__file__).with_name("marking.csv")
OFFICIAL_URL = "https://честныйзнак.рф/business/projects/"
COMMENT_PREFIX = "--"
MIN_PREFIX_LEN = 2


@dataclass(frozen=True, slots=True)
class MarkingRule:
    prefix: str
    category: str
    since: str

    def as_line(self) -> str:
        return f"{self.category} (с {self.since})"


@lru_cache(maxsize=1)
def _table() -> tuple[MarkingRule, ...]:
    """Читается один раз. Ошибка в файле не роняет бота — она делает справку неполной.

    Падать здесь было бы неправильно: маркировка — дополнение к ответу, а опечатка в
    справочной таблице не повод оставить пользователя без кода ТН ВЭД.
    """
    try:
        lines = [
            line
            for line in TABLE_PATH.read_text(encoding="utf-8").splitlines()
            if not line.startswith(COMMENT_PREFIX)
        ]
    except OSError as exc:
        log.error("marking_table_unreadable", error=str(exc)[:200])
        return ()

    rules: list[MarkingRule] = []
    for row in csv.DictReader(lines):
        prefix = (row.get("prefix") or "").strip()
        category = (row.get("category") or "").strip()
        if not prefix.isdigit() or len(prefix) < MIN_PREFIX_LEN or not category:
            log.warning("marking_row_skipped", prefix=prefix[:20])
            continue
        rules.append(
            MarkingRule(prefix=prefix, category=category, since=(row.get("since") or "").strip())
        )
    log.info("marking_table_loaded", rules=len(rules))
    return tuple(rules)


def rules_for(code: str) -> list[MarkingRule]:
    """Все правила, под префикс которых попадает код.

    Правил может оказаться несколько: 2106 «прочие пищевые продукты» — это и БАД, и
    спортивное питание, и часть кондитерских изделий. Показывать надо все: пользователь
    сам знает, что именно он везёт.
    """
    digits = "".join(ch for ch in code if ch.isdigit())
    if not digits:
        return []
    matched = [rule for rule in _table() if digits.startswith(rule.prefix)]
    # Длинный префикс точнее короткого — он идёт первым.
    return sorted(matched, key=lambda rule: (-len(rule.prefix), rule.category))


def reload_table() -> int:
    """Перечитать таблицу без перезапуска бота — файл правится вручную."""
    _table.cache_clear()
    return len(_table())
