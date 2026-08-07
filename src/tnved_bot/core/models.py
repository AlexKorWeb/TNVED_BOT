"""Модели предметной области. `aiogram` сюда не попадает — это ядро, а не транспорт."""

from __future__ import annotations

from dataclasses import dataclass, field

PATH_SEPARATOR = " / "
# Сколько последних уровней пути показывать модели и пользователю.
# Обрезать путь с начала нельзя: различающая часть находится в конце. У кода 8516 71 000 0
# начало пути — общий заголовок про водонагреватели и утюги на 300 символов, а собственно
# «для приготовления кофе или чая» стоит последним. Обрезка с головы стоила модели
# правильного ответа на живой проверке.
PATH_TAIL_SEGMENTS = 3
MAX_CANDIDATE_LEN = 220


@dataclass(frozen=True, slots=True)
class Candidate:
    """Позиция справочника, предложенная поиском."""

    code: str
    name: str
    name_full: str
    tariff: str | None = None

    def label(self, segments: int = PATH_TAIL_SEGMENTS) -> str:
        """Хвост пути иерархии — самая информативная часть наименования.

        Сегменты добавляются с конца, пока помещаются в лимит. Обрезать готовую строку
        по длине нельзя: срез сверху снова отрезал бы именно различающую концовку.
        """
        parts = [p.strip() for p in self.name_full.split(PATH_SEPARATOR) if p.strip()]
        if not parts:
            return self.name[:MAX_CANDIDATE_LEN]

        tail: list[str] = []
        length = 0
        for part in reversed(parts[-segments:]):
            addition = len(part) + len(PATH_SEPARATOR)
            if tail and length + addition > MAX_CANDIDATE_LEN:
                break
            tail.insert(0, part)
            length += addition
        # Последний сегмент сам по себе может быть длиннее лимита — тогда режем его.
        return PATH_SEPARATOR.join(tail)[-MAX_CANDIDATE_LEN:]


@dataclass(frozen=True, slots=True)
class CodeSuggestion:
    code: str
    name: str
    name_full: str
    tariff: str | None = None
    why: str = ""


@dataclass(frozen=True, slots=True)
class Answer:
    """Готовый ответ пользователю."""

    code: str
    name: str
    name_full: str
    confidence: float
    tariff: str | None = None
    reasoning: list[str] = field(default_factory=list)
    alternatives: list[CodeSuggestion] = field(default_factory=list)
    degraded: str | None = None
    """Причина деградации: ответ получен не полным пайплайном."""


@dataclass(frozen=True, slots=True)
class Clarification:
    """Нужен ответ пользователя, чтобы продолжить."""

    question: str
    options: list[str]
    candidates: list[Candidate] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NoResult:
    """Классифицировать не удалось. `user_message` объясняет пользователю, почему."""

    reason: str
    user_message: str
    candidates: list[Candidate] = field(default_factory=list)


Outcome = Answer | Clarification | NoResult
