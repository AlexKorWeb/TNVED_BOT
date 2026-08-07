"""Гейт уверенности: отвечать или спросить.

Смысл гейта в том, что уверенный неверный код опаснее лишнего вопроса: пользователь будет
декларировать товар по этому ответу. Поэтому при сомнении бот спрашивает, а когда раунды
уточнений исчерпаны — отвечает, но честно помечает достоверность.
"""

from __future__ import annotations

from enum import Enum


class Decision(Enum):
    ANSWER = "answer"
    CLARIFY = "clarify"


class ConfidenceLevel(Enum):
    HIGH = "высокая"
    MEDIUM = "средняя"
    LOW = "низкая"


def decide(
    *,
    confidence: float,
    has_code: bool,
    wants_clarification: bool,
    rounds_used: int,
    max_rounds: int,
    accept: float,
    clarify: float,
) -> Decision:
    """Решает, отвечать ли сейчас.

    Раунды исчерпаны — отвечаем в любом случае: бесконечно спрашивать хуже, чем дать
    ответ с честной пометкой о низкой достоверности.
    """
    if rounds_used >= max_rounds:
        return Decision.ANSWER
    # Модель сама попросила уточнить — доверяем ей: она видела кандидатов и заметила
    # неоднозначность, которая в одном числе не отражается. Это основной механизм
    # уточнений; порог по confidence — лишь подстраховка.
    if wants_clarification:
        return Decision.CLARIFY
    if not has_code:
        return Decision.CLARIFY
    # Отклонение от ТЗ (раздел 5.2): при уверенности 0.45–0.79 там предписано спрашивать.
    # Но своего осмысленного вопроса у бота нет, а «уточните описание» пользователю ничего
    # не даёт. Полезнее отдать код с честной пометкой об уровне достоверности — он видит
    # обоснование, альтернативы и может уточнить сам.
    return Decision.ANSWER if confidence >= clarify else Decision.CLARIFY


def level(confidence: float, accept: float, clarify: float) -> ConfidenceLevel:
    if confidence >= accept:
        return ConfidenceLevel.HIGH
    if confidence >= clarify:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW
