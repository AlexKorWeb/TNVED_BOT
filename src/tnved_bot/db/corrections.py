"""Исправления кодов пользователями — память бота о собственных ошибках.

Смысл таблицы: если человек однажды сказал «на этот запрос правильный код такой», то при
похожем запросе бот обязан хотя бы рассмотреть этот код, а не наступать на те же грабли.
Дообучения модели тут нет и быть не может — есть подсказка в промте и принудительное
попадание кода в список кандидатов.

Инвариант не ослабляется: код из исправления проходит `verify_code` **до** записи, и всё
равно проверяется ещё раз перед отправкой пользователю. Испорченная запись в этой таблице
не даёт способа протащить несуществующий код.
"""

from __future__ import annotations

from dataclasses import dataclass

from tnved_bot.clock import now_iso
from tnved_bot.db.engine import Database
from tnved_bot.db.search import stems_of
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

MAX_QUERY_LEN = 500
SCAN_LIMIT = 300
MIN_OVERLAP = 2
"""Меньше двух общих основ — это совпадение по предлогам, а не по смыслу."""


@dataclass(frozen=True, slots=True)
class Correction:
    id: int
    user_id: int
    query: str
    wrong_code: str | None
    correct_code: str
    created_at: str


class CorrectionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self, user_id: int, query: str, correct_code: str, wrong_code: str | None = None
    ) -> None:
        text = query.strip()[:MAX_QUERY_LEN]
        await self._db.execute(
            "INSERT INTO corrections"
            " (user_id, query, query_stems, wrong_code, correct_code, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, text, stems_of(text), wrong_code, correct_code, now_iso()),
        )
        log.info("correction_saved", user_id=user_id, correct_code=correct_code)

    async def similar(self, query: str, limit: int = 3) -> list[Correction]:
        """Исправления по похожим запросам, самые похожие первыми.

        Похожесть считается по числу общих основ слов и разбирается в Python, а не в SQL:
        исправлений здесь единицы и десятки — ради них незачем заводить ещё один
        полнотекстовый индекс.
        """
        wanted = set(stems_of(query).split())
        if not wanted:
            return []

        rows = await self._db.fetch_all(
            "SELECT id, user_id, query, query_stems, wrong_code, correct_code, created_at"
            " FROM corrections ORDER BY id DESC LIMIT ?",
            (SCAN_LIMIT,),
        )

        scored: list[tuple[int, Correction]] = []
        for row in rows:
            overlap = len(wanted & set(str(row["query_stems"]).split()))
            if overlap >= MIN_OVERLAP:
                scored.append((overlap, _to_correction(row)))

        scored.sort(key=lambda pair: (-pair[0], -pair[1].id))
        return [correction for _, correction in scored[:limit]]

    async def recent(self, limit: int = 20) -> list[Correction]:
        rows = await self._db.fetch_all(
            "SELECT id, user_id, query, query_stems, wrong_code, correct_code, created_at"
            " FROM corrections ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [_to_correction(row) for row in rows]

    async def count(self) -> int:
        row = await self._db.fetch_one("SELECT COUNT(*) AS n FROM corrections")
        return int(row["n"]) if row else 0


def _to_correction(row: object) -> Correction:
    return Correction(
        id=int(row["id"]),  # type: ignore[index]
        user_id=int(row["user_id"]),  # type: ignore[index]
        query=row["query"],  # type: ignore[index]
        wrong_code=row["wrong_code"],  # type: ignore[index]
        correct_code=row["correct_code"],  # type: ignore[index]
        created_at=row["created_at"],  # type: ignore[index]
    )
