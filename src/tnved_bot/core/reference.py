"""Получение таможенной справки: кеш → сеть → кеш.

Справка **дополняет** ответ, а не образует его. Поэтому у всего слоя одно правило: любая
неудача — отсутствие интернета, изменившаяся вёрстка, таймаут — заканчивается значением
`None`, а не исключением. Классификация обязана работать на компьютере без сети ровно так
же, как работала до появления этого модуля.
"""

from __future__ import annotations

import asyncio

from tnved_bot.customs.ifcg import IfcgClient
from tnved_bot.customs.reference import CodeReference
from tnved_bot.db.reference import ReferenceCache
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

MAX_PARALLEL_CODES = 6


class ReferenceService:
    def __init__(
        self,
        cache: ReferenceCache,
        client: IfcgClient | None,
        *,
        enabled: bool = True,
    ) -> None:
        self._cache = cache
        self._client = client
        self._enabled = enabled and client is not None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def get(self, code: str) -> CodeReference | None:
        cached = await self._cache.get(code)
        if cached is not None:
            # Отрицательный результат тоже ответ: в сеть за этим кодом сейчас не идём.
            return CodeReference.from_json(code, cached.payload) if cached.ok else None

        if not self._enabled or self._client is None:
            return None

        fetched = await self._client.fetch(code)
        # Пустая справка означает, что разбор ничего не дал — вёрстка могла измениться.
        # Запоминаем как неудачу: иначе пустышка залипла бы в кеше на месяц.
        usable = fetched if fetched is not None and not fetched.is_empty else None
        await self._cache.put(code, usable.to_json() if usable else None)
        if usable is None:
            log.info("reference_empty", code=code)
        return usable

    async def get_many(self, codes: list[str]) -> dict[str, CodeReference]:
        """Справки на несколько кодов сразу.

        Параллельно, потому что иначе четыре кода в ответе — это четыре последовательных
        сетевых запроса поверх и без того небыстрой классификации.
        """
        unique = list(dict.fromkeys(codes))[:MAX_PARALLEL_CODES]
        if not unique:
            return {}
        results = await asyncio.gather(*(self.get(code) for code in unique), return_exceptions=True)

        found: dict[str, CodeReference] = {}
        for code, result in zip(unique, results, strict=True):
            if isinstance(result, BaseException):
                # gather с return_exceptions не должен молча съедать сбои.
                log.warning("reference_gather_failed", code=code, error=str(result)[:200])
                continue
            if result is not None:
                found[code] = result
        return found

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
