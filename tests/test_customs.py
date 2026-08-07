"""Таможенная справка: разбор страницы источника, кеш, маркировка.

Сеть здесь не трогается: фикстуры — куски настоящих страниц ifcg.ru, обрезанные до
значимой части. Клиент подставной.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tnved_bot.core.reference import ReferenceService
from tnved_bot.customs import marking
from tnved_bot.customs.ifcg import IfcgClient, alta_url, page_url, parse
from tnved_bot.customs.reference import CodeReference, DocGroup
from tnved_bot.db.engine import Database
from tnved_bot.db.reference import ReferenceCache

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- разбор страницы


def test_parses_duty_vat_and_documents() -> None:
    reference = parse("8516710000", page("ifcg_8516710000.html"))

    assert reference.duty == "8,5%"
    assert reference.vat == "22%"
    assert reference.has_required_docs
    assert reference.docs[0].kinds == ["Сертификат соответствия", "Декларация о соответствии"]


def test_conditional_documents_are_not_marked_required() -> None:
    """Заголовок «могут потребоваться» — другое требование, чем «необходимые».

    На этой разнице строится подбор варианта без разрешительной документации, поэтому
    смешивать их нельзя.
    """
    reference = parse("8202100000", page("ifcg_8202100000.html"))

    assert reference.docs, "документы на странице есть"
    assert not reference.has_required_docs
    assert all(not group.required for group in reference.docs)


def test_parses_tech_regs_and_samples() -> None:
    reference = parse("8516710000", page("ifcg_8516710000.html"))

    assert any("004/2011" in reg for reg in reference.tech_regs)
    assert reference.samples, "примеры декларирования нужны для подсказок модели"
    assert any("КОФЕВАРКА" in sample for sample in reference.samples)


def test_kinds_do_not_include_unrelated_links() -> None:
    """В перечень документов не должны попадать ссылки на нормативку и на услуги сайта."""
    reference = parse("8202100000", page("ifcg_8202100000.html"))

    kinds = [kind for group in reference.docs for kind in group.kinds]
    assert kinds
    assert not any("раздел" in kind.lower() or "свяжитесь" in kind.lower() for kind in kinds)


@pytest.mark.parametrize(
    "html",
    ["", "<html><body>ничего похожего</body></html>", "<table id='duties'><tr></tr></table>"],
)
def test_broken_markup_gives_empty_reference_without_raising(html: str) -> None:
    """Изменившаяся вёрстка не должна ронять классификацию — только обеднять справку."""
    reference = parse("8516710000", html)
    assert reference.is_empty


def test_fields_from_outside_are_length_limited() -> None:
    reference = CodeReference.build(
        code="8516710000",
        duty="9" * 500,
        docs=[{"title": "т" * 500, "kinds": ["к" * 500] * 50}],
        samples=["s" * 2000] * 100,
    )
    assert reference.duty is not None
    assert len(reference.duty) <= 80  # noqa: PLR2004 — MAX_KIND
    assert len(reference.docs[0].kinds) <= 8  # noqa: PLR2004 — MAX_KINDS
    assert len(reference.samples) <= 12  # noqa: PLR2004 — MAX_SAMPLES


def test_json_round_trip() -> None:
    original = CodeReference.build(
        code="8516710000",
        duty="8,5%",
        vat="22%",
        docs=[DocGroup(title="ТР ЕАЭС", kinds=["Сертификат"], required=True)],
        samples=["КОФЕВАРКА"],
    )
    restored = CodeReference.from_json("8516710000", original.to_json())

    assert restored == original


def test_broken_cache_payload_is_not_fatal() -> None:
    assert CodeReference.from_json("8516710000", "не json") is None


# ---------------------------------------------------------------- сетевой клиент


async def test_non_numeric_code_never_reaches_network() -> None:
    """Код приходит из ответа модели — в URL он попадает только после проверки.

    Без этой проверки строка вида `../../` или чужой адрес ушли бы в запрос.
    """
    client = IfcgClient()
    calls: list[str] = []

    async def spy(url: str) -> str | None:
        calls.append(url)
        return None

    client._get = spy  # type: ignore[method-assign]  # noqa: SLF001
    assert await client.fetch("../../etc/passwd") is None
    assert await client.fetch("851671") is None
    assert calls == []


def test_urls_are_built_from_digits_only() -> None:
    assert page_url("8516710000").endswith("/kb/tnved/8516710000/")
    assert alta_url("8516710000").startswith("https://www.alta.ru/")


# ---------------------------------------------------------------- кеш и сервис


class FakeClient:
    """Клиент, считающий обращения: кеш проверяется именно по их числу."""

    def __init__(self, reference: CodeReference | None) -> None:
        self.reference = reference
        self.calls = 0

    async def fetch(self, code: str) -> CodeReference | None:
        self.calls += 1
        return self.reference

    async def close(self) -> None:
        return None


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "ref.db")
    await database.connect()
    yield database
    await database.close()


async def test_second_request_does_not_go_to_network(db: Database) -> None:
    reference = CodeReference.build(code="8516710000", duty="8,5%", vat="22%")
    client = FakeClient(reference)
    service = ReferenceService(ReferenceCache(db), client)  # type: ignore[arg-type]

    first = await service.get("8516710000")
    second = await service.get("8516710000")

    assert first == second == reference
    assert client.calls == 1, "второй запрос обязан обслуживаться кешем"


async def test_failure_is_cached_too(db: Database) -> None:
    """Иначе бот ходил бы в сеть за отсутствующим кодом при каждом упоминании."""
    client = FakeClient(None)
    service = ReferenceService(ReferenceCache(db), client)  # type: ignore[arg-type]

    assert await service.get("8516710000") is None
    assert await service.get("8516710000") is None
    assert client.calls == 1


async def test_empty_reference_is_stored_as_failure(db: Database) -> None:
    """Пустая справка означает, что вёрстка изменилась. Кешировать её на месяц нельзя."""
    cache = ReferenceCache(db)
    client = FakeClient(CodeReference.build(code="8516710000"))
    service = ReferenceService(cache, client)  # type: ignore[arg-type]

    assert await service.get("8516710000") is None
    stored = await cache.get("8516710000")
    assert stored is not None
    assert stored.ok is False


async def test_disabled_service_never_calls_client(db: Database) -> None:
    client = FakeClient(CodeReference.build(code="8516710000", duty="5%"))
    service = ReferenceService(ReferenceCache(db), client, enabled=False)  # type: ignore[arg-type]

    assert await service.get("8516710000") is None
    assert client.calls == 0


async def test_get_many_survives_a_failing_code(db: Database) -> None:
    class Flaky(FakeClient):
        async def fetch(self, code: str) -> CodeReference | None:
            if code == "0000000000":
                msg = "сеть отвалилась"
                raise OSError(msg)
            return CodeReference.build(code=code, duty="5%")

    service = ReferenceService(ReferenceCache(db), Flaky(None))  # type: ignore[arg-type]
    found = await service.get_many(["8516710000", "0000000000"])

    assert "8516710000" in found
    assert "0000000000" not in found


# ---------------------------------------------------------------- маркировка


def test_marking_matches_by_prefix() -> None:
    rules = marking.rules_for("6403511100")
    assert rules
    assert rules[0].category == "Обувь"


def test_marking_returns_all_matching_groups() -> None:
    """2106 «прочие пищевые продукты» — это и БАД, и спортивное питание сразу."""
    assert marking.rules_for("2106909200")


def test_marking_absent_for_unlisted_code() -> None:
    """Пустой ответ означает «не нашли», а не «маркировка не нужна» — так и в тексте."""
    assert marking.rules_for("8202100000") == []


def test_marking_ignores_formatting() -> None:
    assert marking.rules_for("6403 51 110 0") == marking.rules_for("6403511100")


def test_marking_table_loads() -> None:
    assert marking.reload_table() > 50  # noqa: PLR2004 — таблица заведомо крупнее
