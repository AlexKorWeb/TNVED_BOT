"""Строгая проверка того, что вернула модель.

Ответ модели — недоверенные данные ровно так же, как текст пользователя: он мог быть
сформирован под влиянием инъекции. Поэтому здесь не «разбор», а фильтр: всё, что не прошло
схему, отбрасывается, а не «чинится на месте».

Главное правило: **код не принимается, если его нет в списке кандидатов**. Модель обязана
выбирать, а не изобретать; сверка со справочником идёт следом, в `NomenclatureRepository`.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

MAX_REASONING_LINES = 5
MAX_REASONING_LEN = 300
MAX_ALTERNATIVES = 3
MAX_OPTION_LEN = 40
MAX_QUESTION_LEN = 200
MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_KEYWORDS_LEN = 200

_DIGITS = re.compile(r"\D")
# Разметка в тексте от модели: markdown сломает HTML-парсер Telegram, ссылки уводят
# пользователя неизвестно куда.
_MARKUP = re.compile(r"[<>`*_\[\]]|https?://\S+")


def _clean(text: str, limit: int) -> str:
    return _MARKUP.sub("", text).strip()[:limit]


class Alternative(BaseModel):
    code: str
    why: str = ""

    @field_validator("code")
    @classmethod
    def _digits(cls, value: str) -> str:
        return _DIGITS.sub("", value)

    @field_validator("why")
    @classmethod
    def _clean_why(cls, value: str) -> str:
        return _clean(value, MAX_REASONING_LEN)


class ClassifyResponse(BaseModel):
    """Ответ шага классификации."""

    code: str | None = None
    confidence: float = 0.0
    reasoning: list[str] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    clarifying_question: str | None = None
    options: list[str] = Field(default_factory=list)

    @field_validator("code", mode="before")
    @classmethod
    def _normalize_code(cls, value: Any) -> str | None:
        if value is None:
            return None
        digits = _DIGITS.sub("", str(value))
        return digits or None

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        # Значение вне диапазона — признак, что модель не следовала инструкции.
        # Зажимаем, а не доверяем: «уверенность 1.5» не должна стать «очень уверен».
        return min(1.0, max(0.0, number))

    @field_validator("reasoning", mode="before")
    @classmethod
    def _clean_reasoning(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [
            _clean(str(item), MAX_REASONING_LEN) for item in value[:MAX_REASONING_LINES] if item
        ]

    @field_validator("alternatives", mode="before")
    @classmethod
    def _limit_alternatives(cls, value: Any) -> list[Any]:
        return value[:MAX_ALTERNATIVES] if isinstance(value, list) else []

    @field_validator("clarifying_question", mode="before")
    @classmethod
    def _clean_question(cls, value: Any) -> str | None:
        if not value or not isinstance(value, str):
            return None
        cleaned = _clean(value, MAX_QUESTION_LEN)
        return cleaned or None

    @field_validator("options", mode="before")
    @classmethod
    def _clean_options(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned = [_clean(str(item), MAX_OPTION_LEN) for item in value[:MAX_OPTIONS]]
        return [item for item in cleaned if item]

    @property
    def wants_clarification(self) -> bool:
        """Вопрос имеет смысл только вместе с вариантами ответа — иначе кнопок не будет."""
        return bool(self.clarifying_question) and len(self.options) >= MIN_OPTIONS


class KeywordsResponse(BaseModel):
    """Ответ шага нормализации запроса.

    Нужен, потому что справочник написан официальным языком: «ноутбука» и «кроссовок» в нём
    нет вообще, там «машины вычислительные портативные» и «обувь с верхом из кожи».
    Поиск по бытовому слову не находит ничего — этот шаг переводит запрос на язык справочника.
    """

    keywords: str = ""
    chapters: list[str] = Field(default_factory=list)

    @field_validator("keywords", mode="before")
    @classmethod
    def _clean_keywords(cls, value: Any) -> str:
        if isinstance(value, list):
            value = " ".join(str(item) for item in value)
        return _clean(str(value or ""), MAX_KEYWORDS_LEN)

    @field_validator("chapters", mode="before")
    @classmethod
    def _clean_chapters(cls, value: Any) -> list[str]:
        if isinstance(value, (str, int)):
            value = [value]
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value[:5]:
            digits = _DIGITS.sub("", str(item))
            # Группа ТН ВЭД — ровно два знака от 01 до 97.
            if len(digits) == 2 and "01" <= digits <= "97" and digits not in result:
                result.append(digits)
        return result


def parse_classify(payload: dict[str, object]) -> ClassifyResponse | None:
    return _parse(ClassifyResponse, payload, "classify")


def parse_keywords(payload: dict[str, object]) -> KeywordsResponse | None:
    return _parse(KeywordsResponse, payload, "keywords")


def _parse(model: type[Any], payload: dict[str, object], step: str) -> Any | None:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        log.warning("llm_schema_invalid", step=step, errors=exc.error_count())
        return None
