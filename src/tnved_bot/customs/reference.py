"""Модель таможенной справки по коду и её сериализация для кеша.

Справка приходит из интернета, то есть является **недоверенными данными** ровно так же,
как текст пользователя. Поэтому здесь только простые типы, никакого исполняемого содержимого,
а на выходе в Telegram всё проходит через `texts.esc()`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

MAX_TITLE = 120
MAX_KIND = 80
MAX_SAMPLE = 400
MAX_KINDS = 8
MAX_GROUPS = 8
MAX_REGS = 8
MAX_SAMPLES = 12


def _cut(value: object, limit: int) -> str:
    """Строка ограниченной длины из чего угодно, что пришло снаружи.

    Обрезанное помечается многоточием: без него текст обрывается на середине слова и
    выглядит как ошибка разбора, а не как сокращение.
    """
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class DocGroup:
    """Группа разрешительных документов.

    `required` различает два заголовка на странице источника: «Документы, необходимые для
    ввоза» и «могут потребоваться … требуются редко или только при определённых условиях».
    Разница существенная: на ней строится подбор варианта без разрешительной документации.
    """

    title: str
    kinds: list[str] = field(default_factory=list)
    required: bool = True

    def as_line(self) -> str:
        kinds = ", ".join(self.kinds)
        return f"{self.title}: {kinds}" if kinds else self.title


@dataclass(frozen=True, slots=True)
class CodeReference:
    """Справка по одному коду ТН ВЭД."""

    code: str
    duty: str | None = None
    vat: str | None = None
    docs: list[DocGroup] = field(default_factory=list)
    tech_regs: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)
    source_url: str = ""

    @property
    def required_docs(self) -> list[DocGroup]:
        return [group for group in self.docs if group.required]

    @property
    def has_required_docs(self) -> bool:
        return bool(self.required_docs)

    @property
    def is_empty(self) -> bool:
        """Пустая справка — признак того, что разбор ничего не дал: кешировать её надолго нельзя."""
        return not (self.duty or self.vat or self.docs)

    # ---------------------------------------------------------------- сериализация

    def to_json(self) -> str:
        return json.dumps(
            {
                "code": self.code,
                "duty": self.duty,
                "vat": self.vat,
                "docs": [
                    {"title": d.title, "kinds": d.kinds, "required": d.required} for d in self.docs
                ],
                "tech_regs": self.tech_regs,
                "samples": self.samples,
                "source_url": self.source_url,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, code: str, payload: str) -> CodeReference | None:
        """Разбор кеша. Битая запись — это `None`, а не исключение: кеш восстановим."""
        try:
            data: Any = json.loads(payload)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        return cls.build(
            code=code,
            duty=data.get("duty"),
            vat=data.get("vat"),
            docs=data.get("docs") or [],
            tech_regs=data.get("tech_regs") or [],
            samples=data.get("samples") or [],
            source_url=data.get("source_url") or "",
        )

    @classmethod
    def build(
        cls,
        code: str,
        duty: object = None,
        vat: object = None,
        docs: object = (),
        tech_regs: object = (),
        samples: object = (),
        source_url: str = "",
    ) -> CodeReference:
        """Единственный конструктор для данных извне: обрезает и приводит типы.

        Ограничения нужны не для красоты вывода: справка попадает и в сообщение Telegram
        (лимит 4096 символов), и в промт, где чужой длинный текст — это ещё и деньги.
        """
        groups: list[DocGroup] = []
        if isinstance(docs, list):
            for item in docs[:MAX_GROUPS]:
                group = _to_group(item)
                if group is not None:
                    groups.append(group)

        return cls(
            code=code,
            duty=_cut(duty, MAX_KIND) or None if duty else None,
            vat=_cut(vat, MAX_KIND) or None if vat else None,
            docs=groups,
            tech_regs=_to_lines(tech_regs, MAX_REGS, MAX_TITLE),
            samples=_to_lines(samples, MAX_SAMPLES, MAX_SAMPLE),
            source_url=_cut(source_url, 200),
        )


def _to_group(item: object) -> DocGroup | None:
    if isinstance(item, DocGroup):
        return item
    if not isinstance(item, dict):
        return None
    title = _cut(item.get("title", ""), MAX_TITLE)
    if not title:
        return None
    return DocGroup(
        title=title,
        kinds=_to_lines(item.get("kinds") or [], MAX_KINDS, MAX_KIND),
        required=bool(item.get("required", True)),
    )


def _to_lines(value: object, count: int, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    lines = [_cut(item, limit) for item in value[:count]]
    return [line for line in lines if line]
