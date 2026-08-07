"""Оркестратор классификации: описание товара → верифицированный код ТН ВЭД.

Пайплайн из двух обращений к ИИ, а не из одного. Первое переводит описание на язык
справочника, второе выбирает код из найденных кандидатов:

    санитизация → ИИ: ключевые слова и группы → поиск → ИИ: выбор из кандидатов
    → **сверка кода со справочником** → гейт уверенности

Шаг ключевых слов появился по результату проверки на настоящих данных: в номенклатуре нет
слов «ноутбук» и «кроссовки», там «машины вычислительные портативные» и «обувь с верхом
из кожи». Поиск по бытовому слову возвращал пустоту, и модели было не из чего выбирать.

Инвариант, который держит весь проект: **ни один код не уходит наружу без `verify_code`**.
Модель может ошибиться, может попасть под инъекцию, может выдумать код — сверка со справочником
стоит после неё и не зависит от её поведения.
"""

from __future__ import annotations

from dataclasses import dataclass

from tnved_bot.core.confidence import Decision, decide
from tnved_bot.core.models import (
    Answer,
    Candidate,
    Clarification,
    CodeSuggestion,
    NoResult,
    Outcome,
)
from tnved_bot.core.sanitize import wrap_user_data
from tnved_bot.db.nomenclature import NomenclatureRepository
from tnved_bot.db.search import NomenclatureSearch
from tnved_bot.llm.client import LlmClient
from tnved_bot.llm.prompts import load
from tnved_bot.llm.schema import ClassifyResponse, parse_classify, parse_keywords
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

MAX_ALTERNATIVES = 2
DEGRADED_CONFIDENCE = 0.3


@dataclass(frozen=True, slots=True)
class ClassifierSettings:
    candidates: int = 40
    accept: float = 0.80
    clarify: float = 0.45
    max_rounds: int = 3
    timeout_text: int = 90


class Classifier:
    def __init__(
        self,
        search: NomenclatureSearch,
        repo: NomenclatureRepository,
        llm: LlmClient,
        settings: ClassifierSettings | None = None,
    ) -> None:
        self._search = search
        self._repo = repo
        self._llm = llm
        self._settings = settings or ClassifierSettings()

    async def classify(
        self, description: str, answers: list[str] | None = None, rounds_used: int = 0
    ) -> Outcome:
        """`answers` — ответы пользователя на предыдущие уточняющие вопросы."""
        if await self._repo.active_version() is None:
            return NoResult(
                "no_nomenclature",
                "Справочник ТН ВЭД не загружен. Обратитесь к администратору.",
            )

        query = " ".join([description, *(answers or [])]).strip()

        keywords, chapters = await self._keywords(query)
        hits = await self._search.search(
            f"{query} {keywords}".strip(), self._settings.candidates, chapters=chapters
        )
        candidates = [
            Candidate(code=h.code, name=h.name, name_full=h.name_full, tariff=h.tariff)
            for h in hits
        ]

        if not candidates:
            return NoResult(
                "no_candidates",
                "Не нашёл подходящих позиций. Уточните: что это за товар и из чего сделан?",
            )

        if not self._llm.available:
            return self._degraded(candidates, "ИИ недоступен, показан поиск по справочнику")

        response = await self._classify_step(query, candidates)
        if response is None:
            return self._degraded(candidates, "ИИ не дал разборчивого ответа")

        return await self._finish(response, candidates, rounds_used)

    # ------------------------------------------------------------------ шаги

    async def _keywords(self, query: str) -> tuple[str, list[str]]:
        """Перевод запроса на язык справочника. Сбой не критичен — ищем по исходным словам."""
        if not self._llm.available:
            return "", []
        try:
            result = await self._llm.run_json(
                wrap_user_data(query), load("keywords"), timeout=self._settings.timeout_text
            )
        except Exception as exc:  # noqa: BLE001 — шаг вспомогательный, падать из-за него нельзя
            log.warning("keywords_step_failed", error=str(exc)[:200])
            return "", []

        parsed = parse_keywords(result.payload)
        if parsed is None:
            return "", []
        log.info("keywords_ready", chapters=parsed.chapters, latency_ms=result.latency_ms)
        return parsed.keywords, parsed.chapters

    async def _classify_step(
        self, query: str, candidates: list[Candidate]
    ) -> ClassifyResponse | None:
        listing = "\n".join(f"{i + 1}. {c.code} — {c.label()}" for i, c in enumerate(candidates))
        prompt = (
            f"{wrap_user_data(query)}\n\n"
            f"Кандидаты из справочника ТН ВЭД:\n{listing}\n\n"
            f"Выбери наиболее подходящий код из списка выше и верни JSON."
        )
        try:
            result = await self._llm.run_json(
                prompt, load("classify"), timeout=self._settings.timeout_text
            )
        except Exception as exc:  # noqa: BLE001 — деградируем на поиск, а не падаем
            log.warning("classify_step_failed", error=str(exc)[:200])
            return None
        return parse_classify(result.payload)

    async def _finish(
        self, response: ClassifyResponse, candidates: list[Candidate], rounds_used: int
    ) -> Outcome:
        allowed = {c.code for c in candidates}
        code = await self._verify(response.code, allowed)

        # Модель назвала код, но он не прошёл проверку — это её сбой, а не нехватка данных.
        # Спрашивать пользователя тут бессмысленно: он ничего не исправит. Полезнее отдать
        # то, что нашёл поиск, честно пометив, что ИИ не справился.
        if response.code and code is None:
            return self._degraded(candidates, "модель назвала код, которого нет в справочнике")

        decision = decide(
            confidence=response.confidence,
            has_code=code is not None,
            wants_clarification=response.wants_clarification,
            rounds_used=rounds_used,
            max_rounds=self._settings.max_rounds,
            accept=self._settings.accept,
            clarify=self._settings.clarify,
        )

        if decision is Decision.CLARIFY and response.wants_clarification:
            return Clarification(
                question=response.clarifying_question or "",
                options=response.options,
                candidates=candidates,
            )

        if code is None:
            # Ни кода, ни вопроса. Своего вопроса у нас нет — придумать хороший, не видя
            # кандидатов глазами, невозможно, а «уточните описание» пользователю ничего
            # не даёт. Отдаём находки поиска с честной пометкой.
            return self._degraded(candidates, "модель не выбрала код из справочника")

        by_code = {c.code: c for c in candidates}
        chosen = by_code[code]
        return Answer(
            code=code,
            name=chosen.name,
            name_full=chosen.name_full,
            tariff=chosen.tariff,
            confidence=response.confidence,
            reasoning=response.reasoning,
            alternatives=await self._alternatives(response, allowed, by_code, exclude=code),
        )

    # ------------------------------------------------------------------ проверки

    async def _verify(self, code: str | None, allowed: set[str]) -> str | None:
        """Код обязан быть и в списке кандидатов, и в активной версии справочника.

        Двойная проверка не избыточна: список кандидатов защищает от выдумывания,
        сверка со справочником — от того, что кандидаты устарели или подменены.
        """
        if not code:
            return None
        if code not in allowed:
            log.warning("llm_code_not_in_candidates", code=code)
            return None
        if await self._repo.verify_code(code) is None:
            log.error("hallucinated_code", code=code)
            return None
        return code

    async def _alternatives(
        self,
        response: ClassifyResponse,
        allowed: set[str],
        by_code: dict[str, Candidate],
        exclude: str,
    ) -> list[CodeSuggestion]:
        result: list[CodeSuggestion] = []
        for alt in response.alternatives:
            if alt.code == exclude or len(result) >= MAX_ALTERNATIVES:
                continue
            verified = await self._verify(alt.code, allowed)
            if verified is None:
                continue
            candidate = by_code[verified]
            result.append(
                CodeSuggestion(
                    code=verified,
                    name=candidate.name,
                    name_full=candidate.name_full,
                    tariff=candidate.tariff,
                    why=alt.why,
                )
            )
        return result

    def _degraded(self, candidates: list[Candidate], reason: str) -> Answer:
        """Ответ без участия ИИ: лучший кандидат поиска с честной пометкой.

        Отдать что-то полезное лучше, чем отказать: пользователь видит и код, и причину
        снижения достоверности, и может решить сам.
        """
        best = candidates[0]
        log.info("degraded_answer", reason=reason, code=best.code)
        return Answer(
            code=best.code,
            name=best.name,
            name_full=best.name_full,
            tariff=best.tariff,
            confidence=DEGRADED_CONFIDENCE,
            reasoning=["Ответ получен поиском по справочнику, без анализа ИИ."],
            alternatives=[
                CodeSuggestion(code=c.code, name=c.name, name_full=c.name_full, tariff=c.tariff)
                for c in candidates[1 : 1 + MAX_ALTERNATIVES]
            ],
            degraded=reason,
        )
