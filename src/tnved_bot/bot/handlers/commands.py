"""Команды бота: пользовательские и административные."""

from __future__ import annotations

from pathlib import Path

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from tnved_bot import __version__
from tnved_bot.bot import keyboards, texts
from tnved_bot.core.models import Candidate
from tnved_bot.db.audit import AuditLog
from tnved_bot.db.nomenclature import NomenclatureRepository, format_code, normalize_code
from tnved_bot.db.photos import PhotoRepository
from tnved_bot.db.sessions import SessionRepository
from tnved_bot.db.users import UserRepository
from tnved_bot.llm.client import LlmClient
from tnved_bot.logging_setup import get_logger
from tnved_bot.storage.photo_store import PhotoStore

log = get_logger(__name__)

CODE_LENGTH = 10
MIN_FREE_MB = 500


async def cmd_start(
    message: Message,
    command: CommandObject,
    user_id: int,
    users: UserRepository,
    audit: AuditLog,
    invite_ttl_hours: int,
    is_admin: bool = False,
    invite_only: bool = False,
) -> None:
    code = (command.args or "").strip()
    if code:
        await _redeem(message, code, user_id, users, audit)
        return
    if invite_only:
        # Сюда доходит только тот, кого нет в списке: middleware пропустил его ради
        # активации кода, но кода в сообщении не оказалось.
        await message.answer(texts.access_denied(user_id))
        return

    await message.answer(texts.START)
    if is_admin:
        # Администратору сразу показываем вход в панель: иначе о ней можно не узнать.
        await message.answer(texts.ADMIN_HINT, reply_markup=keyboards.admin_entry())


async def _redeem(
    message: Message, code: str, user_id: int, users: UserRepository, audit: AuditLog
) -> None:
    username = message.from_user.username if message.from_user else None
    result = await users.redeem(code, user_id, username)
    if result.ok:
        await audit.record("invite_redeemed", user_id=user_id)
        await message.answer("✅ Доступ открыт.\n\n" + texts.START)
        return

    reasons = {
        "not_found": "Код не найден. Проверьте, что скопировали его целиком.",
        "already_used": "Код уже активирован.",
        "expired": "Срок действия кода истёк — попросите новый.",
    }
    await audit.record(f"invite_{result.reason}", user_id=user_id, ok=False)
    await message.answer(reasons.get(result.reason, "Код не подошёл."))


async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


async def cmd_cancel(message: Message, user_id: int, sessions: SessionRepository) -> None:
    closed = await sessions.close_user_sessions(user_id)
    await message.answer(texts.CANCELLED if closed else texts.NOTHING_TO_CANCEL)


async def cmd_version(message: Message, nomenclature: NomenclatureRepository) -> None:
    version = await nomenclature.active_version()
    if version is None:
        await message.answer(f"Бот {__version__}\nСправочник: не загружен")
        return
    await message.answer(
        f"Бот {__version__}\n"
        f"Справочник: {version.rows} кодов, актуальность {version.source_date or '—'}\n"
        f"Импортирован: {version.imported_at[:10]}"
    )


async def cmd_code(
    message: Message, command: CommandObject, nomenclature: NomenclatureRepository
) -> None:
    raw = (command.args or "").strip()
    normalized = normalize_code(raw)
    if len(normalized) != CODE_LENGTH:
        await message.answer(texts.CODE_FORMAT_HINT)
        return

    row = await nomenclature.verify_code(normalized)
    if row is None:
        await message.answer(texts.code_not_found(format_code(normalized)))
        return
    await message.answer(
        texts.code_info(
            Candidate(code=row.code, name=row.name, name_full=row.name_full, tariff=row.tariff)
        )
    )


async def cmd_forget(
    message: Message,
    user_id: int,
    sessions: SessionRepository,
    photos: PhotoRepository,
    photo_store: PhotoStore,
) -> None:
    """Немедленное удаление данных пользователя, не дожидаясь TTL.

    Файлы стираются с диска, а не просто помечаются: обещание удалить должно означать
    удаление.
    """
    records = await photos.list_by_user(user_id)
    removed = 0
    for record in records:
        if photo_store.delete(Path(record.path)):
            await photos.mark_deleted(record.id)
            removed += 1
    closed = await sessions.close_user_sessions(user_id)

    await message.answer(f"Готово. Удалено фотографий: {removed}. Закрыто диалогов: {closed}.")


# ---------------------------------------------------------------- админские


async def cmd_users(message: Message, is_admin: bool, users: UserRepository) -> None:
    if not await _require_admin(message, is_admin):
        return
    people = await users.list_users()
    if not people:
        await message.answer("Список пуст. Добавьте: /adduser или /invite")
        return
    lines = ["<b>Пользователи</b>", ""]
    for person in people:
        note = f" — {texts.esc(person.note)}" if person.note else ""
        seen = person.last_seen_at[:10] if person.last_seen_at else "не заходил"
        lines.append(
            f"<code>{person.user_id}</code>{note}\n   добавлен {person.added_at[:10]}, был {seen}"
        )
    await message.answer("\n".join(lines))


async def cmd_adduser(
    message: Message, command: CommandObject, user_id: int, is_admin: bool, users: UserRepository
) -> None:
    if not await _require_admin(message, is_admin):
        return
    parts = (command.args or "").split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        await message.answer("Формат: <code>/adduser 123456789 Иванов, отдел ВЭД</code>")
        return
    target = int(parts[0])
    note = parts[1] if len(parts) > 1 else None
    await users.add(target, added_by=user_id, note=note)
    await message.answer(
        f"Доступ выдан: <code>{target}</code>.\n"
        "Если ID указан неверно, человек в списке появится, но войти не сможет."
    )


async def cmd_deluser(
    message: Message,
    command: CommandObject,
    is_admin: bool,
    admins: frozenset[int],
    users: UserRepository,
    sessions: SessionRepository,
) -> None:
    if not await _require_admin(message, is_admin):
        return
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer("Формат: <code>/deluser 123456789</code>")
        return

    target = int(raw)
    if target in admins:
        await message.answer(
            "Это администратор из <code>.env</code>. Его доступ отзывается только правкой файла "
            "и перезапуском — так задумано: это аварийный вход."
        )
        return

    removed = await users.remove(target)
    closed = await sessions.close_user_sessions(target)
    await message.answer(
        f"Доступ отозван: <code>{target}</code>. Закрыто диалогов: {closed}."
        if removed
        else f"Пользователя <code>{target}</code> в списке не было."
    )


async def cmd_invite(
    message: Message,
    command: CommandObject,
    user_id: int,
    is_admin: bool,
    users: UserRepository,
    invite_ttl_hours: int,
) -> None:
    if not await _require_admin(message, is_admin):
        return
    note = (command.args or "").strip() or None
    code = await users.create_invite(user_id, note, invite_ttl_hours)
    await message.answer(texts.invite_created(code, invite_ttl_hours, await _username(message.bot)))


async def _username(bot: object) -> str | None:
    """Имя бота для готового текста приглашения. `me()` кешируется aiogram."""
    if bot is None:
        return None
    try:
        me = await bot.me()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — без имени текст просто короче
        return None
    return str(me.username) if me.username else None


async def cmd_health(
    message: Message,
    is_admin: bool,
    nomenclature: NomenclatureRepository,
    llm: LlmClient,
    db_path_parent: str,
) -> None:
    if not await _require_admin(message, is_admin):
        return
    # Тот же текст, что и в панели: иначе команда и кнопка со временем разъедутся.
    await message.answer(await texts.health_text(nomenclature, llm, db_path_parent))


async def cmd_stats(message: Message, is_admin: bool, audit: AuditLog) -> None:
    if not await _require_admin(message, is_admin):
        return
    from tnved_bot.clock import iso_ago

    since = iso_ago(days=1)
    rows = [
        ("Запросов", await audit.count_since("classify_started", since)),
        ("Ответов с кодом", await audit.count_since("classified", since)),
        ("Уточнений", await audit.count_since("clarification_asked", since)),
        ("Без результата", await audit.count_since("no_result", since)),
        ("Отказов в доступе", await audit.count_since("access_denied", since)),
        ("Упёрлись в лимит", await audit.count_since("rate_limited", since)),
        ("Подозрений на инъекцию", await audit.count_since("injection_suspected", since)),
        ("Оценок 👍/👎", await audit.count_since("feedback", since)),
    ]
    lines = ["<b>За сутки</b>", ""] + [f"{name}: {value}" for name, value in rows]
    await message.answer("\n".join(lines))


async def cmd_reload_db(
    message: Message, is_admin: bool, nomenclature: NomenclatureRepository
) -> None:
    if not await _require_admin(message, is_admin):
        return
    version = await nomenclature.active_version()
    if version is None:
        await message.answer(
            "Справочник не загружен. Импорт выполняется скриптом:\n"
            "<code>python scripts\\import_nomenclature.py файл.xlsx</code>"
        )
        return
    await message.answer(
        f"Активная версия {version.id}: {version.rows} кодов, "
        f"актуальность {version.source_date or '—'}.\n"
        "Справочник читается из БД при каждом запросе — перезагрузка не требуется."
    )


async def _require_admin(message: Message, is_admin: bool) -> bool:
    if is_admin:
        return True
    await message.answer("Команда доступна только администратору.")
    return False


def build_router() -> Router:
    """Новый роутер на каждый вызов: один и тот же объект нельзя подключить к двум
    диспетчерам, а это нужно тестам."""
    router = Router(name="commands")
    # Именно `CommandStart()` без аргументов: `CommandStart(deep_link=False)` не срабатывает
    # на `/start КОД` — проверено, фильтр возвращает False. С ним активация приглашений
    # молча не работала бы.
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.message.register(cmd_version, Command("version"))
    router.message.register(cmd_code, Command("code"))
    router.message.register(cmd_forget, Command("forget"))
    router.message.register(cmd_users, Command("users"))
    router.message.register(cmd_adduser, Command("adduser"))
    router.message.register(cmd_deluser, Command("deluser"))
    router.message.register(cmd_invite, Command("invite"))
    router.message.register(cmd_health, Command("health"))
    router.message.register(cmd_stats, Command("stats"))
    router.message.register(cmd_reload_db, Command("reload_db"))
    return router
