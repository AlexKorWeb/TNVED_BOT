"""Обучение на исправлениях: 👎 → верный код → влияние на следующий подбор.

Инвариант проекта здесь не ослабляется: код из исправления проверяется по справочнику
до записи и ещё раз перед отправкой пользователю.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.test_classifier import KEYWORDS_OK, FakeLlm
from tnved_bot.core.classifier import Classifier, ClassifierSettings
from tnved_bot.core.models import Answer
from tnved_bot.db.corrections import CorrectionRepository
from tnved_bot.db.engine import Database
from tnved_bot.db.nomenclature import NomenclatureRepository
from tnved_bot.db.search import NomenclatureSearch
from tnved_bot.importer import parse_file

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nomenclature_sample.csv"
KNOWN_CODE = "8516710000"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "fix.db")
    await database.connect()
    rows, report = parse_file(FIXTURE)
    await NomenclatureRepository(database).import_version(
        report.source, report.sha256, report.source_date, rows
    )
    yield database
    await database.close()


# ---------------------------------------------------------------- хранилище


async def test_saves_and_finds_similar(db: Database) -> None:
    repo = CorrectionRepository(db)
    await repo.add(1, "кофеварка капельная бытовая", KNOWN_CODE, wrong_code="8509800000")

    found = await repo.similar("кофеварка бытовая на кухню")

    assert [item.correct_code for item in found] == [KNOWN_CODE]
    assert found[0].wrong_code == "8509800000"


async def test_unrelated_query_does_not_match(db: Database) -> None:
    """Одно случайное общее слово — не повод подсовывать модели чужой код."""
    repo = CorrectionRepository(db)
    await repo.add(1, "кофеварка капельная бытовая", KNOWN_CODE)

    assert await repo.similar("детские кожаные ботинки") == []


async def test_empty_query_matches_nothing(db: Database) -> None:
    repo = CorrectionRepository(db)
    await repo.add(1, "кофеварка капельная", KNOWN_CODE)
    assert await repo.similar("") == []


async def test_recent_is_newest_first(db: Database) -> None:
    repo = CorrectionRepository(db)
    await repo.add(1, "первый запрос про кофе", KNOWN_CODE)
    await repo.add(1, "второй запрос про кофе", KNOWN_CODE)

    recent = await repo.recent()

    assert recent[0].query.startswith("второй")
    assert await repo.count() == 2  # noqa: PLR2004


# ---------------------------------------------------------------- влияние на подбор


async def test_correction_reaches_prompt_and_candidates(db: Database) -> None:
    """Подсказки мало: выбирать модель может только из кандидатов.

    Если верный код не попал в список, исправление ни на что не повлияет — поэтому он
    добавляется в кандидаты явно.
    """
    corrections = CorrectionRepository(db)
    await corrections.add(1, "кофеварка капельная бытовая", KNOWN_CODE)

    llm = FakeLlm(KEYWORDS_OK, {"code": KNOWN_CODE, "confidence": 0.9})
    classifier = Classifier(
        NomenclatureSearch(db),
        NomenclatureRepository(db),
        llm,
        ClassifierSettings(use_samples=False, max_savings=0),
        corrections=corrections,
    )

    outcome = await classifier.classify("кофеварка капельная бытовая 900 Вт")

    assert isinstance(outcome, Answer)
    classify_prompt = llm.calls[-1]
    assert "исправлял" in classify_prompt
    assert KNOWN_CODE in classify_prompt


async def test_correction_with_unknown_code_cannot_add_candidate(db: Database) -> None:
    """Испорченная запись в таблице исправлений не даёт протащить несуществующий код."""
    corrections = CorrectionRepository(db)
    await corrections.add(1, "кофеварка капельная бытовая", "9999999999")

    llm = FakeLlm(KEYWORDS_OK, {"code": "9999999999", "confidence": 0.99})
    classifier = Classifier(
        NomenclatureSearch(db),
        NomenclatureRepository(db),
        llm,
        ClassifierSettings(use_samples=False, max_savings=0),
        corrections=corrections,
    )

    outcome = await classifier.classify("кофеварка капельная бытовая")

    assert isinstance(outcome, Answer)
    assert outcome.code != "9999999999"
    assert outcome.degraded, "ответ обязан быть помечен как деградировавший"


async def test_broken_corrections_storage_does_not_break_classification(db: Database) -> None:
    class Broken(CorrectionRepository):
        async def similar(self, query: str, limit: int = 3) -> list:  # type: ignore[override]
            msg = "таблица недоступна"
            raise RuntimeError(msg)

    llm = FakeLlm(KEYWORDS_OK, {"code": KNOWN_CODE, "confidence": 0.9})
    classifier = Classifier(
        NomenclatureSearch(db),
        NomenclatureRepository(db),
        llm,
        ClassifierSettings(use_samples=False, max_savings=0),
        corrections=Broken(db),
    )

    outcome = await classifier.classify("кофеварка капельная бытовая")

    assert isinstance(outcome, Answer)
    assert outcome.code == KNOWN_CODE
