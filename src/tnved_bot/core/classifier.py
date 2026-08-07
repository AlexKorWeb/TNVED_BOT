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

from dataclasses import dataclass, replace

from tnved_bot.core.confidence import Decision, decide
from tnved_bot.core.models import (
    Answer,
    Candidate,
    Clarification,
    CodeSuggestion,
    NoResult,
    Outcome,
    Saving,
)
from tnved_bot.core.reference import ReferenceService
from tnved_bot.core.sanitize import wrap_user_data
from tnved_bot.core.savings import cheaper_options
from tnved_bot.customs import marking
from tnved_bot.db.corrections import CorrectionRepository
from tnved_bot.db.nomenclature import NomenclatureRepository
from tnved_bot.db.search import NomenclatureSearch
from tnved_bot.llm.client import LlmClient
from tnved_bot.llm.prompts import load
from tnved_bot.llm.schema import ClassifyResponse, parse_classify, parse_keywords
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

MAX_ALTERNATIVES = 2
DEGRADED_CONFIDENCE = 0.3
MAX_SAMPLE_CODES = 5
MAX_SAMPLES_PER_CODE = 4


@dataclass(frozen=True, slots=True)
class ClassifierSettings:
    candidates: int = 40
    accept: float = 0.80
    clarify: float = 0.45
    max_rounds: int = 3
    timeout_text: int = 90
    max_savings: int = 2
    use_samples: bool = True
    """Повторный проход с примерами декларирования, когда модель не уверена."""


class Classifier:
    def __init__(
        self,
        search: NomenclatureSearch,
        repo: NomenclatureRepository,
        llm: LlmClient,
        settings: ClassifierSettings | None = None,
        reference: ReferenceService | None = None,
        corrections: CorrectionRepository | None = None,
    ) -> None:
        self._search = search
        self._repo = repo
        self._llm = llm
        self._settings = settings or ClassifierSettings()
        self._reference = reference
        self._corrections = corrections

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

        hints = await self._corrections_hint(query)
        candidates = await self._with_corrected(candidates, hints)

        if not candidates:
            return NoResult(
                "no_candidates",
                "Не нашёл подходящих позиций. Уточните: что это за товар и из чего сделан?",
            )

        if not self._llm.available:
            return await self._degraded(candidates, "ИИ недоступен, показан поиск по справочнику")

        response = await self._classify_step(query, candidates, hints=hints)
        if response is None:
            return await self._degraded(candidates, "ИИ не дал разборчивого ответа")

        response = await self._maybe_retry_with_samples(query, candidates, response, hints)
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
        self,
        query: str,
        candidates: list[Candidate],
        hints: str = "",
        samples: str = "",
    ) -> ClassifyResponse | None:
        listing = "\n".join(f"{i + 1}. {c.code} — {c.label()}" for i, c in enumerate(candidates))
        blocks = [wrap_user_data(query), f"Кандидаты из справочника ТН ВЭД:\n{listing}"]
        if hints:
            blocks.append(hints)
        if samples:
            blocks.append(samples)
        blocks.append("Выбери наиболее подходящий код из списка выше и верни JSON.")

        try:
            result = await self._llm.run_json(
                "\n\n".join(blocks), load("classify"), timeout=self._settings.timeout_text
            )
        except Exception as exc:  # noqa: BLE001 — деградируем на поиск, а не падаем
            log.warning("classify_step_failed", error=str(exc)[:200])
            return None
        return parse_classify(result.payload)

    async def _maybe_retry_with_samples(
        self,
        query: str,
        candidates: list[Candidate],
        response: ClassifyResponse,
        hints: str,
    ) -> ClassifyResponse:
        """Второй проход с примерами деклараций, если модель не уверена.

        Примеры — обезличенная графа 31 настоящих деклараций: там товар назван так, как его
        называют люди («КОФЕВАРКА», «КОФЕМАШИНА»), а в номенклатуре таких слов нет вообще.
        Именно этот разрыв в словаре и мешает выбрать между похожими позициями.

        Проход не бесплатный — ещё одно обращение к ИИ и несколько сетевых запросов, —
        поэтому он делается только там, где приносит пользу: при уверенном ответе выбор
        уже сделан, а при полном отсутствии кода примеры не помогут.
        """
        if not self._settings.use_samples or self._reference is None:
            return response
        if not response.code or response.confidence >= self._settings.accept:
            return response

        codes = [candidate.code for candidate in candidates[:MAX_SAMPLE_CODES]]
        references = await self._reference.get_many(codes)
        lines: list[str] = []
        for code in codes:
            reference = references.get(code)
            for sample in (reference.samples if reference else [])[:MAX_SAMPLES_PER_CODE]:
                lines.append(f"{code}: {sample}")
        if not lines:
            return response

        retry = await self._classify_step(
            query,
            candidates,
            hints=hints,
            samples=(
                "Примеры реальных деклараций по этим кодам (обезличенные, могут содержать "
                "ошибки — это подсказка, а не правило):\n" + "\n".join(lines)
            ),
        )
        if retry is None or retry.confidence <= response.confidence:
            return response

        log.info(
            "samples_retry_improved",
            was=round(response.confidence, 2),
            now=round(retry.confidence, 2),
        )
        return retry

    async def _corrections_hint(self, query: str) -> str:
        """Прошлые исправления пользователя по похожим запросам — текстом в промт."""
        if self._corrections is None:
            return ""
        try:
            similar = await self._corrections.similar(query)
        except Exception as exc:  # noqa: BLE001 — подсказка не обязана работать
            log.warning("corrections_lookup_failed", error=str(exc)[:200])
            return ""
        if not similar:
            return ""

        lines = [f"«{item.query}» → {item.correct_code}" for item in similar]
        log.info("corrections_hint", count=len(lines))
        return (
            "Ранее пользователь исправлял ответы по похожим запросам. "
            "Учти это, но проверь применимость к текущему товару:\n" + "\n".join(lines)
        )

    async def _with_corrected(self, candidates: list[Candidate], hints: str) -> list[Candidate]:
        """Добавляет коды из исправлений в список кандидатов.

        Одной подсказки в промте мало: выбирать модель может только из кандидатов, и если
        верного кода в списке нет, исправление ни на что не повлияет. Код проходит
        `verify_code` — то есть в кандидаты попадает только существующая позиция.
        """
        if not hints or self._corrections is None:
            return candidates

        known = {candidate.code for candidate in candidates}
        added: list[Candidate] = []
        for line in hints.splitlines():
            code = line.rsplit("→", 1)[-1].strip()
            if not code.isdigit() or code in known:
                continue
            row = await self._repo.verify_code(code)
            if row is None:
                continue
            known.add(code)
            added.append(
                Candidate(code=row.code, name=row.name, name_full=row.name_full, tariff=row.tariff)
            )
        if added:
            log.info("corrections_candidates_added", codes=[c.code for c in added])
        return [*added, *candidates]

    async def _finish(
        self, response: ClassifyResponse, candidates: list[Candidate], rounds_used: int
    ) -> Outcome:
        allowed = {c.code for c in candidates}
        code = await self._verify(response.code, allowed)

        # Модель назвала код, но он не прошёл проверку — это её сбой, а не нехватка данных.
        # Спрашивать пользователя тут бессмысленно: он ничего не исправит. Полезнее отдать
        # то, что нашёл поиск, честно пометив, что ИИ не справился.
        if response.code and code is None:
            return await self._degraded(
                candidates, "модель назвала код, которого нет в справочнике"
            )

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
            return await self._degraded(candidates, "модель не выбрала код из справочника")

        by_code = {c.code: c for c in candidates}
        chosen = by_code[code]
        answer = Answer(
            code=code,
            name=chosen.name,
            name_full=chosen.name_full,
            tariff=chosen.tariff,
            confidence=response.confidence,
            reasoning=response.reasoning,
            alternatives=await self._alternatives(response, allowed, by_code, exclude=code),
        )
        return await self._enrich(answer, candidates)

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

    async def _degraded(self, candidates: list[Candidate], reason: str) -> Answer:
        """Ответ без участия ИИ: лучший кандидат поиска с честной пометкой.

        Отдать что-то полезное лучше, чем отказать: пользователь видит и код, и причину
        снижения достоверности, и может решить сам.
        """
        best = candidates[0]
        log.info("degraded_answer", reason=reason, code=best.code)
        answer = Answer(
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
        return await self._enrich(answer, candidates)

    # ------------------------------------------------------------------ справка

    async def _enrich(self, answer: Answer, candidates: list[Candidate]) -> Answer:
        """Дополняет ответ пошлиной, документами, маркировкой и выгодными соседями.

        Справка не влияет на выбор кода — она приходит после него. Поэтому любая проблема
        со справкой (нет сети, изменилась вёрстка) означает ответ без справки, а не отказ:
        код, ради которого пользователь пришёл, уже найден и проверен.
        """
        savings = await self._savings(answer, candidates)
        codes = [
            answer.code,
            *(alt.code for alt in answer.alternatives),
            *(saving.suggestion.code for saving in savings),
        ]

        references = {}
        if self._reference is not None:
            try:
                references = await self._reference.get_many(codes)
            except Exception as exc:  # noqa: BLE001 — справка необязательна
                log.warning("reference_enrich_failed", error=str(exc)[:200])

        def decorate(item: CodeSuggestion) -> CodeSuggestion:
            return replace(
                item, reference=references.get(item.code), marking=marking.rules_for(item.code)
            )

        return replace(
            answer,
            reference=references.get(answer.code),
            marking=marking.rules_for(answer.code),
            alternatives=[decorate(alt) for alt in answer.alternatives],
            savings=[
                replace(
                    saving,
                    suggestion=decorate(saving.suggestion),
                    docs_free=_docs_free(references.get(saving.suggestion.code)),
                )
                for saving in savings
            ],
        )

    async def _savings(self, answer: Answer, candidates: list[Candidate]) -> list[Saving]:
        """Соседние коды той же товарной позиции с меньшей ставкой.

        Пул собирается из справочника, а не только из результатов поиска: поиск возвращает
        похожее на описание, а разница в ставке живёт у соседей по классификации, которых
        поиск мог и не показать.
        """
        if self._settings.max_savings <= 0:
            return []

        # Коды, которые поиск счёл похожими на описание товара. Соседи по справочнику,
        # которых тут нет, попадут в подбор только если делят субпозицию с выбранным кодом.
        relevant = {candidate.code for candidate in candidates}
        pool = list(candidates)
        try:
            rows = await self._repo.heading_codes(answer.code)
        except Exception as exc:  # noqa: BLE001 — блок необязателен
            log.warning("heading_codes_failed", error=str(exc)[:200])
            rows = []
        known = {candidate.code for candidate in pool}
        pool += [
            Candidate(code=row.code, name=row.name, name_full=row.name_full, tariff=row.tariff)
            for row in rows
            if row.code not in known
        ]

        chosen = Candidate(
            code=answer.code, name=answer.name, name_full=answer.name_full, tariff=answer.tariff
        )
        return [
            Saving(
                suggestion=CodeSuggestion(
                    code=option.candidate.code,
                    name=option.candidate.name,
                    name_full=option.candidate.name_full,
                    tariff=option.candidate.tariff,
                ),
                gap=option.gap,
            )
            for option in cheaper_options(
                chosen, pool, limit=self._settings.max_savings, relevant=relevant
            )
        ]


def _docs_free(reference: object) -> bool | None:
    """`None` — «неизвестно»: отсутствие справки не равно отсутствию требований."""
    if reference is None:
        return None
    return not reference.has_required_docs  # type: ignore[attr-defined]
