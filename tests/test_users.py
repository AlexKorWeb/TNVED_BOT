"""Тесты списка доступа и кодов приглашений."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tnved_bot.clock import iso_ago
from tnved_bot.db.engine import Database
from tnved_bot.db.users import UserRepository, generate_code, normalize_code

ADMIN = 111


@pytest.fixture
async def users(tmp_path: Path) -> AsyncIterator[UserRepository]:
    db = Database(tmp_path / "tnved.db")
    await db.connect()
    yield UserRepository(db)
    await db.close()


# ---------------------------------------------------------------- коды


def test_code_has_no_ambiguous_characters() -> None:
    """Код диктуют голосом и переписывают руками: 0/O и 1/I/L в нём быть не должно."""
    for _ in range(200):
        code = generate_code()
        body = code.removeprefix("TNVED-").replace("-", "")
        assert not set(body) & set("01OIL")
        assert len(body) == 8


def test_codes_are_unique() -> None:
    codes = {generate_code() for _ in range(500)}
    assert len(codes) == 500


@pytest.mark.parametrize(
    "raw", ["tnved-abcd-efgh", " TNVED-ABCD-EFGH ", "TNVED-abcd-EFGH", "TNVED- ABCD -EFGH"]
)
def test_normalize_code_is_forgiving(raw: str) -> None:
    assert normalize_code(raw) == "TNVED-ABCD-EFGH"


# ---------------------------------------------------------------- список


async def test_add_and_remove(users: UserRepository) -> None:
    assert not await users.is_allowed(42)
    await users.add(42, added_by=ADMIN, note="Иванов")
    assert await users.is_allowed(42)
    assert await users.remove(42)
    assert not await users.is_allowed(42)


async def test_changes_apply_without_restart(users: UserRepository) -> None:
    """Кеш не должен требовать перезапуска: бот работает под планировщиком задач."""
    await users.allowed_ids()  # прогреваем кеш
    await users.add(42, added_by=ADMIN)
    assert await users.is_allowed(42), "добавление обязано применяться сразу"
    await users.remove(42)
    assert not await users.is_allowed(42), "отзыв обязан применяться сразу"


async def test_remove_is_soft(users: UserRepository) -> None:
    """История о том, кто имел доступ, должна сохраняться."""
    await users.add(42, added_by=ADMIN, note="Иванов")
    await users.remove(42)
    db = users._db  # noqa: SLF001
    row = await db.fetch_one("SELECT is_active, note FROM allowed_users WHERE user_id = 42")
    assert row is not None
    assert row["is_active"] == 0
    assert row["note"] == "Иванов"


async def test_remove_unknown_returns_false(users: UserRepository) -> None:
    assert not await users.remove(999)


async def test_readd_restores_access(users: UserRepository) -> None:
    await users.add(42, added_by=ADMIN, note="Иванов")
    await users.remove(42)
    await users.add(42, added_by=ADMIN)
    assert await users.is_allowed(42)
    people = await users.list_users()
    assert people[0].note == "Иванов", "заметка не должна теряться при повторной выдаче"


# ---------------------------------------------------------------- приглашения


async def test_invite_grants_access(users: UserRepository) -> None:
    code = await users.create_invite(ADMIN, "Петров", ttl_hours=24)
    result = await users.redeem(code, 777, "petrov")
    assert result.ok
    assert await users.is_allowed(777)


async def test_invite_is_single_use(users: UserRepository) -> None:
    code = await users.create_invite(ADMIN, None, ttl_hours=24)
    assert (await users.redeem(code, 777, None)).ok
    second = await users.redeem(code, 888, None)
    assert not second.ok
    assert second.reason == "already_used"
    assert not await users.is_allowed(888)


async def test_parallel_redeem_admits_exactly_one(users: UserRepository) -> None:
    """Схема «прочитал → проверил → записал» пропустила бы обоих."""
    code = await users.create_invite(ADMIN, None, ttl_hours=24)
    results = await asyncio.gather(
        *(users.redeem(code, 1000 + i, None) for i in range(10)), return_exceptions=True
    )
    succeeded = [r for r in results if getattr(r, "ok", False)]
    assert len(succeeded) == 1, f"доступ получили {len(succeeded)} человек вместо одного"

    allowed = await users.allowed_ids()
    assert len(allowed) == 1


async def test_expired_invite_rejected(users: UserRepository) -> None:
    code = await users.create_invite(ADMIN, None, ttl_hours=24)
    db = users._db  # noqa: SLF001
    await db.execute(
        "UPDATE invite_codes SET expires_at = ? WHERE code = ?", (iso_ago(hours=1), code)
    )
    result = await users.redeem(code, 777, None)
    assert not result.ok
    assert result.reason == "expired"
    assert not await users.is_allowed(777)


async def test_unknown_code_rejected(users: UserRepository) -> None:
    result = await users.redeem("TNVED-XXXX-YYYY", 777, None)
    assert not result.ok
    assert result.reason == "not_found"


async def test_invite_note_transfers_to_user(users: UserRepository) -> None:
    code = await users.create_invite(ADMIN, "Петров, отдел ВЭД", ttl_hours=24)
    await users.redeem(code, 777, None)
    people = await users.list_users()
    assert people[0].note == "Петров, отдел ВЭД"
    assert people[0].added_by == 0, "активация по коду помечается added_by = 0"


async def test_code_case_insensitive(users: UserRepository) -> None:
    code = await users.create_invite(ADMIN, None, ttl_hours=24)
    assert (await users.redeem(code.lower(), 777, None)).ok
