"""Тесты оркестратора классификации. Настоящий `claude` не вызывается."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tnved_bot.core.classifier import Classifier, ClassifierSettings
from tnved_bot.core.confidence import Decision, decide
from tnved_bot.core.errors import LlmError
from tnved_bot.core.models import Answer, Candidate, Clarification, NoResult
from tnved_bot.db.engine import Database
from tnved_bot.db.nomenclature import NomenclatureRepository
from tnved_bot.db.search import NomenclatureSearch
from tnved_bot.importer import parse_file
from tnved_bot.llm.client import LlmClient, LlmResult

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nomenclature_sample.csv"


class FakeLlm(LlmClient):
    """Подставной ИИ: отдаёт заранее заданные ответы по очереди."""

    def __init__(self, *responses: dict[str, object] | Exception, available: bool = True) -> None:
        super().__init__()
        self.responses = list(responses)
        self._available = available
        self.calls: list[str] = []

    @property
    def available(self) -> bool:
        return self._available

    async def run_json(self, prompt, system, *, timeout, allow_read_dir=None):  # type: ignore[no-untyped-def, override]
        self.calls.append(prompt)
        if not self.responses:
            msg = "у подставного ИИ закончились ответы"
            raise LlmError(msg)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return LlmResult(payload=item, latency_ms=10, input_tokens=1, output_tokens=1)


KEYWORDS_OK = {"keywords": "приборы электронагревательные кофе", "chapters": ["85"]}


@pytest.fixture
async def parts(tmp_path: Path) -> AsyncIterator[tuple[NomenclatureSearch, NomenclatureRepository]]:
    db = Database(tmp_path / "tnved.db")
    await db.connect()
    rows, report = parse_file(FIXTURE)
    repo = NomenclatureRepository(db)
    await repo.import_version(report.source, report.sha256, report.source_date, rows)
    yield NomenclatureSearch(db), repo
    await db.close()


def build(parts, llm: LlmClient, **overrides: object) -> Classifier:  # type: ignore[no-untyped-def]
    search, repo = parts
    return Classifier(search, repo, llm, ClassifierSettings(**overrides))  # type: ignore[arg-type]


# ---------------------------------------------------------------- гейт уверенности


@pytest.mark.parametrize(
    ("confidence", "wants", "rounds", "expected"),
    [
        (0.95, False, 0, Decision.ANSWER),
        (0.80, False, 0, Decision.ANSWER),
        # 0.45–0.79: отвечаем с пометкой «средняя достоверность», а не спрашиваем —
        # своего осмысленного вопроса у бота нет (см. комментарий в confidence.decide).
        (0.60, False, 0, Decision.ANSWER),
        (0.10, False, 0, Decision.CLARIFY),
        (0.95, True, 0, Decision.CLARIFY),  # модель сама попросила уточнить
        (0.10, False, 3, Decision.ANSWER),  # раунды исчерпаны — обязаны ответить
        (0.10, True, 3, Decision.ANSWER),
    ],
)
def test_confidence_gate(confidence: float, wants: bool, rounds: int, expected: Decision) -> None:
    assert (
        decide(
            confidence=confidence,
            has_code=True,
            wants_clarification=wants,
            rounds_used=rounds,
            max_rounds=3,
            accept=0.8,
            clarify=0.45,
        )
        is expected
    )


def test_gate_clarifies_without_code() -> None:
    assert (
        decide(
            confidence=0.99,
            has_code=False,
            wants_clarification=False,
            rounds_used=0,
            max_rounds=3,
            accept=0.8,
            clarify=0.45,
        )
        is Decision.CLARIFY
    )


# ---------------------------------------------------------------- метка кандидата


def test_candidate_label_keeps_distinguishing_tail() -> None:
    """Обрезка пути с начала стоила модели правильного ответа на живой проверке:
    у кода 8516 71 000 0 общий заголовок занимает сотни символов, а различающее
    «для приготовления кофе или чая» стоит последним."""
    candidate = Candidate(
        code="8516710000",
        name="для приготовления кофе или чая",
        name_full=("Электрические водонагреватели " + "очень длинный заголовок " * 20)
        + " / приборы электронагревательные прочие / для приготовления кофе или чая",
    )
    label = candidate.label()
    assert "для приготовления кофе или чая" in label
    assert len(label) <= 220


# ---------------------------------------------------------------- пайплайн


async def test_happy_path(parts) -> None:  # type: ignore[no-untyped-def]
    llm = FakeLlm(
        KEYWORDS_OK,
        {
            "code": "8516710000",
            "confidence": 0.91,
            "reasoning": ["бытовой электронагревательный прибор"],
            "alternatives": [{"code": "8516108000", "why": "прочие"}],
        },
    )
    outcome = await build(parts, llm).classify("кофеварка капельная бытовая")

    assert isinstance(outcome, Answer)
    assert outcome.code == "8516710000"
    assert outcome.confidence == 0.91
    assert outcome.degraded is None
    assert outcome.reasoning


async def test_two_llm_calls_are_made(parts) -> None:  # type: ignore[no-untyped-def]
    """Первый вызов переводит запрос на язык справочника, второй выбирает код."""
    llm = FakeLlm(KEYWORDS_OK, {"code": "8516710000", "confidence": 0.9})
    await build(parts, llm).classify("кофеварка")
    assert len(llm.calls) == 2
    assert "<user_data>" in llm.calls[0]
    assert "Кандидаты из справочника" in llm.calls[1]


async def test_clarification_returned(parts) -> None:  # type: ignore[no-untyped-def]
    llm = FakeLlm(
        {"keywords": "трубы", "chapters": []},
        {
            "code": None,
            "confidence": 0.2,
            "clarifying_question": "Из какого материала труба?",
            "options": ["Сталь", "Пластик"],
        },
    )
    outcome = await build(parts, llm).classify("труба")

    assert isinstance(outcome, Clarification)
    assert outcome.question == "Из какого материала труба?"
    assert outcome.options == ["Сталь", "Пластик"]


async def test_answers_are_appended_to_query(parts) -> None:  # type: ignore[no-untyped-def]
    llm = FakeLlm(KEYWORDS_OK, {"code": "8516710000", "confidence": 0.9})
    await build(parts, llm).classify("труба", answers=["пластик", "жёсткая"])
    assert "пластик" in llm.calls[0]
    assert "жёсткая" in llm.calls[0]


async def test_rounds_exhausted_forces_answer(parts) -> None:  # type: ignore[no-untyped-def]
    """Бесконечно спрашивать хуже, чем ответить с честной пометкой."""
    llm = FakeLlm(
        KEYWORDS_OK,
        {
            "code": "8516710000",
            "confidence": 0.2,
            "clarifying_question": "ещё вопрос?",
            "options": ["да", "нет"],
        },
    )
    outcome = await build(parts, llm).classify("кофеварка", rounds_used=3)
    assert isinstance(outcome, Answer)
    assert outcome.confidence == 0.2


# ---------------------------------------------------------------- инвариант кода


async def test_invented_code_is_rejected(parts) -> None:  # type: ignore[no-untyped-def]
    """Ключевой инвариант проекта: код не из справочника не может уйти пользователю."""
    llm = FakeLlm(KEYWORDS_OK, {"code": "9999999999", "confidence": 0.99})
    outcome = await build(parts, llm).classify("кофеварка")

    assert isinstance(outcome, Answer)
    assert outcome.code != "9999999999"
    assert outcome.degraded is not None


async def test_code_outside_candidates_is_rejected(parts) -> None:  # type: ignore[no-untyped-def]
    """Код существует в справочнике, но его не было в списке — модель его не выбирала."""
    llm = FakeLlm(KEYWORDS_OK, {"code": "0101210000", "confidence": 0.99})
    outcome = await build(parts, llm).classify("кофеварка капельная")

    assert isinstance(outcome, Answer)
    assert outcome.code != "0101210000" or outcome.degraded is not None


async def test_every_returned_code_exists_in_nomenclature(parts) -> None:  # type: ignore[no-untyped-def]
    """Сотня разных ответов модели — ни один выдуманный код не должен просочиться."""
    search, repo = parts
    fabrications = [{"code": f"99999999{i:02d}", "confidence": 0.99} for i in range(20)] + [
        {"code": "не код", "confidence": 1.0},
        {"code": "", "confidence": 1.0},
    ]

    for payload in fabrications:
        llm = FakeLlm(KEYWORDS_OK, payload)
        outcome = await build(parts, llm).classify("кофеварка бытовая")
        if isinstance(outcome, Answer):
            assert await repo.verify_code(outcome.code) is not None, (
                f"код {outcome.code} отсутствует в справочнике"
            )


async def test_alternatives_are_verified_too(parts) -> None:  # type: ignore[no-untyped-def]
    llm = FakeLlm(
        KEYWORDS_OK,
        {
            "code": "8516710000",
            "confidence": 0.9,
            "alternatives": [{"code": "9999999999"}, {"code": "8516710000"}],
        },
    )
    outcome = await build(parts, llm).classify("кофеварка")
    assert isinstance(outcome, Answer)
    assert all(alt.code != "9999999999" for alt in outcome.alternatives)
    assert all(alt.code != outcome.code for alt in outcome.alternatives)


# ---------------------------------------------------------------- деградации


async def test_llm_unavailable_degrades_to_search(parts) -> None:  # type: ignore[no-untyped-def]
    llm = FakeLlm(available=False)
    outcome = await build(parts, llm).classify("кофеварка капельная бытовая")

    assert isinstance(outcome, Answer)
    assert outcome.degraded is not None
    assert outcome.confidence < 0.5
    assert llm.calls == [], "недоступный ИИ не должен вызываться"


async def test_llm_error_degrades_not_crashes(parts) -> None:  # type: ignore[no-untyped-def]
    llm = FakeLlm(KEYWORDS_OK, LlmError("таймаут"))
    outcome = await build(parts, llm).classify("кофеварка капельная бытовая")
    assert isinstance(outcome, Answer)
    assert outcome.degraded is not None


async def test_keywords_failure_does_not_break_pipeline(parts) -> None:  # type: ignore[no-untyped-def]
    """Шаг ключевых слов вспомогательный: его сбой не должен ломать классификацию."""
    llm = FakeLlm(LlmError("сбой"), {"code": "8516710000", "confidence": 0.9})
    outcome = await build(parts, llm).classify("кофеварка капельная бытовая")
    assert isinstance(outcome, Answer)
    assert outcome.code == "8516710000"
    assert outcome.degraded is None


async def test_empty_llm_answer_degrades(parts) -> None:  # type: ignore[no-untyped-def]
    """Ни кода, ни вопроса — своего вопроса у бота нет, отдаём находки поиска."""
    llm = FakeLlm(KEYWORDS_OK, {"совсем": "не то"})
    outcome = await build(parts, llm).classify("кофеварка капельная бытовая")
    assert isinstance(outcome, Answer)
    assert outcome.degraded is not None


async def test_low_confidence_without_question_still_answers(parts) -> None:  # type: ignore[no-untyped-def]
    """Пометка о достоверности полезнее вопроса «уточните описание»."""
    llm = FakeLlm(KEYWORDS_OK, {"code": "8516710000", "confidence": 0.6})
    outcome = await build(parts, llm).classify("кофеварка капельная бытовая")
    assert isinstance(outcome, Answer)
    assert outcome.confidence == 0.6
    assert outcome.degraded is None


async def test_no_candidates_returns_no_result(parts) -> None:  # type: ignore[no-untyped-def]
    # Слова короче шести букв не порождают широких корней, поэтому поиск честно пуст.
    llm = FakeLlm({"keywords": "", "chapters": []}, {"code": None})
    outcome = await build(parts, llm).classify("щщщщ ъъъъ")
    assert isinstance(outcome, NoResult)
    assert outcome.reason == "no_candidates"


async def test_no_nomenclature_returns_no_result(tmp_path: Path) -> None:
    db = Database(tmp_path / "empty.db")
    await db.connect()
    try:
        classifier = Classifier(
            NomenclatureSearch(db), NomenclatureRepository(db), FakeLlm(KEYWORDS_OK)
        )
        outcome = await classifier.classify("кофеварка")
        assert isinstance(outcome, NoResult)
        assert outcome.reason == "no_nomenclature"
    finally:
        await db.close()


async def test_core_does_not_import_aiogram() -> None:
    """Слои: ядро не должно знать о транспорте."""
    import tnved_bot.core.classifier as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "aiogram" not in source
