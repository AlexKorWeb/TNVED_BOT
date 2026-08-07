"""Ответ с таможенной справкой: пошлина и документы у основного кода и у альтернатив.

Проверяется и обратное: без сети ответ остаётся полноценным. Классификация не имеет права
зависеть от доступности чужого сайта.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.test_classifier import KEYWORDS_OK, FakeLlm
from tnved_bot.bot import texts
from tnved_bot.core.classifier import Classifier, ClassifierSettings
from tnved_bot.core.models import Answer
from tnved_bot.core.reference import ReferenceService
from tnved_bot.customs.reference import CodeReference, DocGroup
from tnved_bot.db.engine import Database
from tnved_bot.db.nomenclature import NomenclatureRepository
from tnved_bot.db.reference import ReferenceCache
from tnved_bot.db.search import NomenclatureSearch
from tnved_bot.importer import parse_file

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nomenclature_sample.csv"
CODE = "8516710000"
# Молочная пара: оба кода реально возвращаются поиском по этому запросу и оба есть в таблице
# маркировки. Альтернатива обязана быть среди кандидатов — иначе её отбросит проверка
# (и это правильно, см. test_alternatives_are_verified_too в test_classifier.py).
MILK_KEYWORDS = {"keywords": "молоко сливки несгущенные", "chapters": ["04"]}
MILK_QUERY = "молоко сливки несгущенные"
MILK_CODE = "0401209101"
MILK_ALTERNATIVE = "0403907300"


class StubClient:
    """Отдаёт справку на любой код и считает обращения."""

    def __init__(self, samples: list[str] | None = None, required: bool = True) -> None:
        self.samples = samples or []
        self.required = required
        self.codes: list[str] = []

    async def fetch(self, code: str) -> CodeReference | None:
        self.codes.append(code)
        return CodeReference.build(
            code=code,
            duty="8,5%",
            vat="22%",
            docs=[
                DocGroup(
                    title="Документы о соответствии ТР ЕАЭС",
                    kinds=["Сертификат соответствия"],
                    required=self.required,
                )
            ],
            samples=self.samples,
        )

    async def close(self) -> None:
        return None


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "ref.db")
    await database.connect()
    rows, report = parse_file(FIXTURE)
    await NomenclatureRepository(database).import_version(
        report.source, report.sha256, report.source_date, rows
    )
    yield database
    await database.close()


def make(db: Database, llm: FakeLlm, client: object | None, **overrides: object) -> Classifier:
    reference = ReferenceService(ReferenceCache(db), client, enabled=client is not None)  # type: ignore[arg-type]
    settings = {"use_samples": False, "max_savings": 0, **overrides}
    return Classifier(
        NomenclatureSearch(db),
        NomenclatureRepository(db),
        llm,
        ClassifierSettings(**settings),  # type: ignore[arg-type]
        reference=reference,
    )


async def test_reference_lands_on_answer_and_alternatives(db: Database) -> None:
    llm = FakeLlm(
        MILK_KEYWORDS,
        {
            "code": MILK_CODE,
            "confidence": 0.9,
            "alternatives": [{"code": MILK_ALTERNATIVE, "why": "другой продукт переработки"}],
        },
    )
    outcome = await make(db, llm, StubClient()).classify(MILK_QUERY)

    assert isinstance(outcome, Answer)
    assert outcome.reference is not None
    assert outcome.reference.duty == "8,5%"
    assert outcome.reference.has_required_docs
    assert outcome.alternatives, "альтернатива обязана дойти до ответа"
    assert outcome.alternatives[0].reference is not None, "справка нужна и у альтернатив"
    assert outcome.alternatives[0].marking, "маркировка нужна и у альтернатив"


async def test_answer_survives_without_network(db: Database) -> None:
    class Dead(StubClient):
        async def fetch(self, code: str) -> CodeReference | None:
            msg = "сети нет"
            raise OSError(msg)

    llm = FakeLlm(KEYWORDS_OK, {"code": CODE, "confidence": 0.9})
    outcome = await make(db, llm, Dead()).classify("кофеварка капельная бытовая")

    assert isinstance(outcome, Answer)
    assert outcome.code == CODE
    assert outcome.reference is None
    # Ставка из локального справочника доступна и без интернета.
    assert outcome.tariff


async def test_disabled_reference_makes_no_requests(db: Database) -> None:
    client = StubClient()
    llm = FakeLlm(KEYWORDS_OK, {"code": CODE, "confidence": 0.9})
    classifier = Classifier(
        NomenclatureSearch(db),
        NomenclatureRepository(db),
        llm,
        ClassifierSettings(use_samples=False, max_savings=0),
        reference=ReferenceService(ReferenceCache(db), client, enabled=False),  # type: ignore[arg-type]
    )

    await classifier.classify("кофеварка капельная бытовая")

    assert client.codes == []


async def test_marking_is_attached_from_local_table(db: Database) -> None:
    """Молочная продукция маркируется с 2021 года — это и должно оказаться в ответе."""
    llm = FakeLlm(MILK_KEYWORDS, {"code": MILK_CODE, "confidence": 0.9})

    outcome = await make(db, llm, None).classify(MILK_QUERY)

    assert isinstance(outcome, Answer)
    assert outcome.code == MILK_CODE
    assert outcome.marking, "код молочной продукции есть в таблице маркировки"
    assert "Молочная продукция" in outcome.marking[0].category


# ---------------------------------------------------------------- примеры деклараций


async def test_samples_pass_is_skipped_when_model_is_confident(db: Database) -> None:
    client = StubClient(samples=["КОФЕВАРКА ЭЛЕКТРИЧЕСКАЯ"])
    llm = FakeLlm(KEYWORDS_OK, {"code": CODE, "confidence": 0.95})

    await make(db, llm, client, use_samples=True).classify("кофеварка капельная бытовая")

    assert llm.responses == [], "лишнего обращения к ИИ быть не должно"
    assert "Примеры реальных деклараций" not in llm.calls[-1]


async def test_samples_pass_runs_when_model_hesitates(db: Database) -> None:
    client = StubClient(samples=["КОФЕВАРКА ЭЛЕКТРИЧЕСКАЯ, 800 ВТ"])
    llm = FakeLlm(
        KEYWORDS_OK,
        {"code": CODE, "confidence": 0.5},
        {"code": CODE, "confidence": 0.92},
    )

    outcome = await make(db, llm, client, use_samples=True).classify("кофеварка капельная")

    assert isinstance(outcome, Answer)
    assert outcome.confidence == pytest.approx(0.92)
    assert "Примеры реальных деклараций" in llm.calls[-1]


async def test_samples_pass_keeps_first_answer_if_retry_is_worse(db: Database) -> None:
    client = StubClient(samples=["КОФЕВАРКА"])
    llm = FakeLlm(
        KEYWORDS_OK,
        {"code": CODE, "confidence": 0.6},
        {"code": CODE, "confidence": 0.3},
    )

    outcome = await make(db, llm, client, use_samples=True).classify("кофеварка капельная")

    assert isinstance(outcome, Answer)
    assert outcome.confidence == pytest.approx(0.6)


async def test_samples_cannot_introduce_a_code_outside_candidates(db: Database) -> None:
    """Подсказка остаётся подсказкой: выбор всё равно ограничен списком кандидатов."""
    client = StubClient(samples=["ЧТО-ТО СОВСЕМ ДРУГОЕ"])
    llm = FakeLlm(
        KEYWORDS_OK,
        {"code": CODE, "confidence": 0.5},
        {"code": "0101210000", "confidence": 0.99},
    )

    outcome = await make(db, llm, client, use_samples=True).classify("кофеварка капельная")

    assert isinstance(outcome, Answer)
    assert outcome.code != "0101210000"


# ---------------------------------------------------------------- текст ответа


def answer_with(**overrides: object) -> Answer:
    base = {
        "code": CODE,
        "name": "для приготовления кофе или чая",
        "name_full": "приборы электронагревательные / для приготовления кофе или чая",
        "confidence": 0.9,
        "tariff": "8.5%",
    }
    return Answer(**{**base, **overrides})  # type: ignore[arg-type]


def test_message_shows_payments_documents_and_marking() -> None:
    answer = answer_with(
        reference=CodeReference.build(
            code=CODE,
            duty="8,5%",
            vat="22%",
            docs=[DocGroup(title="ТР ЕАЭС", kinds=["Сертификат соответствия"], required=True)],
        ),
        marking=[],
    )
    text = texts.answer_message(answer, accept=0.8, clarify=0.45)

    assert "22%" in text
    assert "Сертификат соответствия" in text
    assert "не нашёл" in text, "про маркировку бот отвечает и отрицательно"


def test_shown_duty_comes_from_the_local_nomenclature() -> None:
    """Иначе «ниже на N п.п.» считалось бы от одного числа, а показывалось другое."""
    answer = answer_with(
        tariff="10%",
        reference=CodeReference.build(code=CODE, duty="8,5%", vat="22%"),
    )
    text = texts.answer_message(answer, accept=0.8, clarify=0.45)

    assert "10%" in text
    assert "8,5%" not in text


def test_message_says_when_reference_is_unavailable() -> None:
    text = texts.answer_message(answer_with(), accept=0.8, clarify=0.45)
    assert "справка недоступна" in text
    assert "8.5%" in text, "ставка из локального справочника показывается всегда"


def test_message_fits_telegram_limit() -> None:
    """Длинный ответ не обрезается посреди тега — иначе Telegram откажет в отправке целиком."""
    long_docs = [
        DocGroup(title="Группа " + "д" * 100, kinds=["Документ " + "к" * 60] * 8) for _ in range(8)
    ]
    reference = CodeReference.build(
        code=CODE, duty="8,5%", vat="22%", docs=long_docs, tech_regs=["Регламент " * 10] * 8
    )
    answer = answer_with(
        reference=reference,
        reasoning=["Обоснование " * 20] * 5,
        alternatives=[],
    )

    text = texts.answer_message(answer, accept=0.8, clarify=0.45)

    assert len(text) <= texts.MESSAGE_LIMIT
    assert text.count("<b>") == text.count("</b>")
    assert "справочный характер" in text, "дисклеймер не выбрасывается никогда"
