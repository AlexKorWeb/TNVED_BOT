"""Разбор фотографии моделью.

Отдельный шаг, который **не называет код**. Модели даётся одна задача — описать товар;
дальше описание идёт обычным текстовым пайплайном, где код выбирается из кандидатов
и сверяется со справочником. Если бы зрение сразу называло код, инвариант проверки
пришлось бы дублировать, а модель отвечала бы по картинке, не видя справочника.

Для чтения файла модели нужен инструмент `Read`, поэтому вызов работает в отдельной
временной папке, где лежит **только** это изображение. Ни `.env`, ни исходники, ни другие
фотографии в неё не попадают.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from tnved_bot.llm.client import LlmClient
from tnved_bot.llm.prompts import load
from tnved_bot.llm.schema import _clean  # noqa: PLC2701 — общая очистка текста от разметки
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

MAX_DESCRIPTION_LEN = 400
IMAGE_NAME = "photo.jpg"


class VisionResponse(BaseModel):
    description: str = ""
    confidence: float = 0.0
    multiple_items: bool = False

    @field_validator("description", mode="before")
    @classmethod
    def _clean_description(cls, value: Any) -> str:
        return _clean(str(value or ""), MAX_DESCRIPTION_LEN)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, value: Any) -> float:
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    @field_validator("multiple_items", mode="before")
    @classmethod
    def _as_bool(cls, value: Any) -> bool:
        return bool(value) and str(value).lower() not in {"false", "0", "нет"}


@dataclass(frozen=True, slots=True)
class VisionResult:
    description: str
    confidence: float
    multiple_items: bool


class VisionReader:
    def __init__(self, llm: LlmClient, timeout: int = 120) -> None:
        self._llm = llm
        self._timeout = timeout

    async def describe(self, image: Path) -> VisionResult | None:
        """Описывает товар на фотографии. `None` — разобрать не удалось."""
        workspace = Path(tempfile.mkdtemp(prefix="tnved-vision-"))
        try:
            staged = workspace / IMAGE_NAME
            shutil.copy2(image, staged)

            result = await self._llm.run_json(
                f"Опиши товар на изображении {IMAGE_NAME} в текущей папке.",
                load("vision"),
                timeout=self._timeout,
                allow_read_dir=workspace,
            )
        except Exception as exc:  # noqa: BLE001 — фото не разобрали, но падать нельзя
            log.warning("vision_failed", error=str(exc)[:200])
            return None
        finally:
            # Временную папку убираем в любом случае, включая таймаут и падение:
            # иначе копии пользовательских фотографий копились бы в системном temp
            # мимо TTL и мимо джанитора.
            shutil.rmtree(workspace, ignore_errors=True)

        try:
            parsed = VisionResponse.model_validate(result.payload)
        except ValidationError:
            log.warning("vision_schema_invalid")
            return None

        if not parsed.description:
            return None

        log.info("vision_ready", confidence=parsed.confidence, latency_ms=result.latency_ms)
        return VisionResult(
            description=parsed.description,
            confidence=parsed.confidence,
            multiple_items=parsed.multiple_items,
        )


__all__ = ["Field", "VisionReader", "VisionResponse", "VisionResult"]
