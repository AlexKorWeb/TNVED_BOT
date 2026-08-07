"""Граница доверия: всё, что пришло от Telegram, проходит здесь до любого использования.

Слои L1 и L2 из раздела 9.1 ТЗ. Важно понимать их место: это гигиена, а **не** основная защита
от prompt-инъекций. Пытаться распознать «злой умысел» в тексте — заведомо проигрышная игра,
обходится перефразированием. Настоящая защита дальше по цепочке и не зависит от этого модуля:

* у модели нет инструментов (`--allowedTools ""`), выполнить она ничего не может;
* ответ принимается только строгим JSON по схеме;
* каждый код сверяется со справочником и со списком переданных кандидатов.

Поэтому здесь: убрать то, что ломает структуру промта и разметку, и отклонить заведомый мусор.
Порог отклонения намеренно низкий — ложный отказ настоящему описанию товара вреднее, чем
пропущенная попытка инъекции, которую всё равно остановят следующие слои.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_LENGTH = 2000
MIN_LENGTH = 3
MAX_NON_LETTER_RATIO = 0.7

# Невидимые символы: склеивают слова для человека, но меняют текст для модели.
_INVISIBLE = re.compile(r"[​-‏⁠﻿­]")
# Переопределение направления письма: показывает одно, содержит другое.
_BIDI = re.compile(r"[‪-‮⁦-⁩]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[ \t]+")
_NEWLINES = re.compile(r"\n{3,}")

# Разметка, имитирующая структуру промта. Вырезается молча — в описании товара её быть не может.
_STRUCTURE = re.compile(
    r"</?\s*(?:user_data|system|assistant|human|instructions?|prompt)\b[^>]*>"
    r"|```|~~~"
    r"|\[/?INST\]"
    r"|<\|[^|]*\|>",
    re.IGNORECASE,
)
# Ролевые префиксы диалога. Обезвреживаются заменой двоеточия — смысл слова сохраняется.
_ROLE_PREFIX = re.compile(r"\b(human|assistant|system|user|ассистент|система)\s*:", re.IGNORECASE)
# Прочие теги: в описании товара HTML не нужен, а закрывающий тег может рвать структуру.
_TAGS = re.compile(r"</?[a-zA-Zа-яА-Я][^<>]{0,60}>")

_LETTER_OR_DIGIT = re.compile(r"[^\W_]", re.UNICODE)


@dataclass(frozen=True, slots=True)
class Sanitized:
    """Текст, пригодный для дальнейшей обработки."""

    text: str
    suspicious: bool = False
    """Найдена и обезврежена разметка, имитирующая структуру промта."""


@dataclass(frozen=True, slots=True)
class Rejected:
    """Текст отклонён. `reason` — для лога, `user_message` — для пользователя."""

    reason: str
    user_message: str


Result = Sanitized | Rejected

_TOO_SHORT = Rejected(
    "too_short",
    "Опишите товар подробнее: что это, из чего сделано, для чего используется.",
)
_TOO_LONG = Rejected(
    "too_long",
    f"Слишком длинное описание. Сократите до {MAX_LENGTH} символов.",
)
_GARBAGE = Rejected(
    "garbage",
    "Не удалось разобрать описание. Опишите товар обычными словами.",
)


def clean_user_text(raw: str | None) -> Result:
    """Единственная точка входа для текста пользователя."""
    if not raw:
        return _TOO_SHORT

    # NFKC до всего остального: иначе «ｋｏｆｅ» и «kofe» — разные строки, и фильтры
    # ниже можно обойти полноширинными или составными символами.
    text = unicodedata.normalize("NFKC", raw)
    text = _CONTROL.sub("", text)
    text = _INVISIBLE.sub("", text)
    text = _BIDI.sub("", text)

    without_structure = _STRUCTURE.sub(" ", text)
    without_structure = _TAGS.sub(" ", without_structure)
    suspicious = without_structure != text
    text = _ROLE_PREFIX.sub(r"\1 ", without_structure)

    text = _SPACES.sub(" ", text)
    text = _NEWLINES.sub("\n\n", text)
    text = "\n".join(line.strip() for line in text.split("\n")).strip()

    if len(text) > MAX_LENGTH:
        return _TOO_LONG
    if len(text) < MIN_LENGTH:
        return _TOO_SHORT
    if _non_letter_ratio(text) > MAX_NON_LETTER_RATIO:
        return _GARBAGE

    return Sanitized(text=text, suspicious=suspicious)


def _non_letter_ratio(text: str) -> float:
    """Доля символов, не являющихся буквами или цифрами.

    Отсекает наборы эмодзи и пунктуации, из которых описание товара не построить.
    """
    compact = text.replace(" ", "").replace("\n", "")
    if not compact:
        return 1.0
    letters = len(_LETTER_OR_DIGIT.findall(compact))
    return 1.0 - letters / len(compact)


def wrap_user_data(text: str) -> str:
    """Оборачивает текст пользователя для передачи модели.

    Закрывающего тега внутри быть не может: он вырезан на предыдущем шаге. Дополнительная
    страховка — экранирование любых оставшихся угловых скобок вокруг слова user_data.
    """
    safe = text.replace("</user_data>", " ").replace("<user_data>", " ")
    return f"<user_data>\n{safe}\n</user_data>"
