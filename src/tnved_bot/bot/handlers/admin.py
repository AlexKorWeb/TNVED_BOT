"""Панель администратора на кнопках.

Команды `/users`, `/adduser`, `/deluser`, `/invite` остаются — они удобны, когда ID уже
известен. Панель нужна для другого: увидеть, кто имеет доступ и сколько выданных приглашений
висит невостребованными, и отозвать лишнее, не набирая ID руками.

Права проверяются в **каждом** обработчике, а не только при открытии панели: `callback_data`
приходит от клиента, и кнопку из чужого пересланного сообщения нажать можно.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from tnved_bot.bot import broadcast, keyboards, texts
from tnved_bot.clock import iso_ago
from tnved_bot.db.audit import AuditLog
from tnved_bot.db.corrections import CorrectionRepository
from tnved_bot.db.nomenclature import NomenclatureRepository
from tnved_bot.db.sessions import SessionRepository
from tnved_bot.db.users import UserRepository
from tnved_bot.llm.client import LlmClient
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

MAX_LISTED = 20
AWAITING = ""
"""Пустая строка в черновике — «ждём текст рассылки», а не «текст пустой»."""


async def cmd_admin(
    message: Message, is_admin: bool, users: UserRepository, audit: AuditLog
) -> None:
    if not is_admin:
        await message.answer(texts.ADMIN_ONLY)
        return
    await message.answer(await _menu_text(users, audit), reply_markup=keyboards.admin_menu())


async def handle_admin_callback(  # noqa: PLR0913 — панель связывает много репозиториев
    callback: CallbackQuery,
    user_id: int,
    is_admin: bool,
    users: UserRepository,
    sessions: SessionRepository,
    audit: AuditLog,
    nomenclature: NomenclatureRepository,
    corrections: CorrectionRepository,
    llm: LlmClient,
    admins: frozenset[int],
    invite_ttl_hours: int,
    db_path_parent: str,
    broadcast_drafts: dict[int, str],
) -> None:
    parsed = keyboards.parse_admin_callback(callback.data)
    if parsed is None:
        await callback.answer()
        return

    if not is_admin:
        # Кнопку могли переслать: панель — не место, где право проверяется однажды.
        log.warning("admin_callback_from_non_admin", user_id=user_id)
        await callback.answer(texts.ADMIN_ONLY, show_alert=True)
        return

    action, argument = parsed
    await callback.answer()

    if action == keyboards.ADM_MENU:
        await _show(callback, await _menu_text(users, audit), keyboards.admin_menu())

    elif action == keyboards.ADM_USERS:
        await _show_users(callback, users, admins)

    elif action == keyboards.ADM_CONFIRM_USER:
        await _confirm_user(callback, argument, users, admins)

    elif action == keyboards.ADM_REMOVE_USER:
        await _remove_user(callback, argument, users, sessions, admins)

    elif action == keyboards.ADM_INVITES:
        await _show_invites(callback, users)

    elif action == keyboards.ADM_NEW_INVITE:
        await _new_invite(callback, user_id, users, invite_ttl_hours)

    elif action == keyboards.ADM_REVOKE_INVITE:
        revoked = await users.revoke_invite(argument)
        await _show_invites(
            callback, users, note="Код отозван." if revoked else "Код уже использован или истёк."
        )

    elif action == keyboards.ADM_STATS:
        await _show(callback, await _stats_text(audit), keyboards.admin_back())

    elif action == keyboards.ADM_HEALTH:
        await _show(
            callback,
            await texts.health_text(nomenclature, llm, db_path_parent),
            keyboards.admin_back(),
        )

    elif action == keyboards.ADM_CORRECTIONS:
        await _show(callback, await _corrections_text(corrections), keyboards.admin_back())

    elif action == keyboards.ADM_BROADCAST:
        broadcast_drafts[user_id] = AWAITING
        await _show(callback, texts.BROADCAST_PROMPT, keyboards.admin_broadcast())

    elif action == keyboards.ADM_RELEASE:
        broadcast_drafts[user_id] = texts.RELEASE_NOTE
        await _show(
            callback,
            texts.broadcast_preview(texts.RELEASE_NOTE, len(await users.list_users())),
            keyboards.admin_broadcast_confirm(),
        )

    elif action == keyboards.ADM_BROADCAST_SEND:
        await _send_broadcast(callback, user_id, users, broadcast_drafts)


# ---------------------------------------------------------------- экраны


async def _menu_text(users: UserRepository, audit: AuditLog) -> str:
    people = await users.list_users()
    invites = await users.list_active_invites()
    since = iso_ago(days=1)
    return texts.admin_menu_text(
        users=len(people),
        invites=len(invites),
        requests=await audit.count_since("classify_started", since),
    )


async def _show_users(
    callback: CallbackQuery, users: UserRepository, admins: frozenset[int]
) -> None:
    people = await users.list_users()
    listed = people[:MAX_LISTED]
    buttons = [(p.user_id, p.note or str(p.user_id)) for p in listed]
    await _show(
        callback,
        texts.admin_users_text(listed, total=len(people), admins=admins),
        keyboards.admin_users(buttons),
    )


async def _confirm_user(
    callback: CallbackQuery, argument: str, users: UserRepository, admins: frozenset[int]
) -> None:
    if not argument.isdigit():
        await _show_users(callback, users, admins)
        return
    target = int(argument)
    person = next((p for p in await users.list_users() if p.user_id == target), None)
    label = person.note if person and person.note else str(target)
    await _show(
        callback,
        texts.admin_confirm_user_text(target, label),
        keyboards.admin_confirm_user(target),
    )


async def _remove_user(
    callback: CallbackQuery,
    argument: str,
    users: UserRepository,
    sessions: SessionRepository,
    admins: frozenset[int],
) -> None:
    if not argument.isdigit():
        await _show_users(callback, users, admins)
        return
    target = int(argument)

    if target in admins:
        # Администратор из .env — аварийный вход. Отзыв через панель оставил бы бота
        # без управления, если список в БД окажется пуст.
        await _show(callback, texts.ADMIN_FROM_ENV, keyboards.admin_back())
        return

    removed = await users.remove(target)
    closed = await sessions.close_user_sessions(target)
    log.info("admin_removed_user", target=target, closed=closed)
    await _show_users(callback, users, admins)
    await _notify(callback, texts.admin_user_removed(target, closed, removed))


async def _show_invites(
    callback: CallbackQuery, users: UserRepository, note: str | None = None
) -> None:
    invites = await users.list_active_invites()
    listed = invites[:MAX_LISTED]
    buttons = [(inv.code, inv.note or inv.code) for inv in listed]
    await _show(
        callback,
        texts.admin_invites_text(listed, total=len(invites), note=note),
        keyboards.admin_invites(buttons),
    )


async def _new_invite(
    callback: CallbackQuery, user_id: int, users: UserRepository, ttl_hours: int
) -> None:
    code = await users.create_invite(user_id, note=None, ttl_hours=ttl_hours)
    username = await _username(callback.bot)
    # Двумя сообщениями: первое — админу (что за код, до какого времени), второе —
    # самодостаточное приглашение со ссылкой, которое пересылается человеку как есть.
    # Правкой экрана тут не обойтись: перерисованная панель унесла бы код из чата.
    await _notify(callback, texts.invite_created(code, ttl_hours, username))
    await _notify(callback, texts.invite_share(code, username))
    await _show_invites(callback, users)


async def _corrections_text(corrections: CorrectionRepository) -> str:
    rows = await corrections.recent(MAX_LISTED)
    return texts.corrections_list(
        [(item.created_at, item.query, item.wrong_code or "", item.correct_code) for item in rows]
    )


async def _send_broadcast(
    callback: CallbackQuery,
    user_id: int,
    users: UserRepository,
    drafts: dict[int, str],
) -> None:
    text = drafts.pop(user_id, AWAITING)
    if not text:
        await _show(callback, texts.BROADCAST_PROMPT, keyboards.admin_broadcast())
        return

    bot = callback.bot
    if not isinstance(bot, Bot):  # pragma: no cover — вне Telegram не бывает
        return
    recipients = [person.user_id for person in await users.list_users()]
    result = await broadcast.send_to_all(bot, recipients, text)
    await _show(
        callback, texts.broadcast_report(result.sent, result.failed), keyboards.admin_back()
    )


async def handle_broadcast_text(
    message: Message,
    user_id: int,
    is_admin: bool,
    users: UserRepository,
    broadcast_drafts: dict[int, str],
) -> None:
    """Ловит текст рассылки, набранный администратором.

    Право проверяется повторно, хотя фильтр уже отсеял всех, у кого нет черновика:
    черновик — состояние, а состояние может пережить отзыв прав.
    """
    if not is_admin or broadcast_drafts.get(user_id) != AWAITING:
        raise SkipHandler
    text = (message.text or "").strip()
    if not text:
        raise SkipHandler

    broadcast_drafts[user_id] = text
    await message.answer(
        texts.broadcast_preview(text, len(await users.list_users())),
        reply_markup=keyboards.admin_broadcast_confirm(),
    )


async def _stats_text(audit: AuditLog) -> str:
    since = iso_ago(days=1)
    rows = [
        ("Запросов", "classify_started"),
        ("Ответов с кодом", "classified"),
        ("Уточнений", "clarification_asked"),
        ("Без результата", "no_result"),
        ("Отказов в доступе", "access_denied"),
        ("Упёрлись в лимит", "rate_limited"),
        ("Подозрений на инъекцию", "injection_suspected"),
        ("Оценок пользователей", "feedback"),
    ]
    values = [(name, await audit.count_since(event, since)) for name, event in rows]
    return texts.admin_stats_text(values)


# ---------------------------------------------------------------- вспомогательное


async def _show(callback: CallbackQuery, text: str, markup: object) -> None:
    """Перерисовывает то же сообщение: панель не должна засорять чат новыми экранами."""
    message = callback.message
    if not isinstance(message, Message):
        return
    try:
        await message.edit_text(text, reply_markup=markup)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 — «сообщение не изменено» не ошибка
        log.debug("admin_screen_unchanged", error=str(exc)[:120])


async def _notify(callback: CallbackQuery, text: str) -> None:
    message = callback.message
    if isinstance(message, Message):
        await message.answer(text)


async def _username(bot: object) -> str | None:
    """Имя бота для готового текста приглашения. `me()` кешируется aiogram."""
    if bot is None:
        return None
    try:
        me = await bot.me()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — без имени текст просто короче
        return None
    return str(me.username) if me.username else None


async def cmd_broadcast(
    message: Message, user_id: int, is_admin: bool, broadcast_drafts: dict[int, str]
) -> None:
    if not is_admin:
        await message.answer(texts.ADMIN_ONLY)
        return
    broadcast_drafts[user_id] = AWAITING
    await message.answer(texts.BROADCAST_PROMPT, reply_markup=keyboards.admin_broadcast())


async def cmd_corrections(
    message: Message, is_admin: bool, corrections: CorrectionRepository
) -> None:
    if not is_admin:
        await message.answer(texts.ADMIN_ONLY)
        return
    await message.answer(await _corrections_text(corrections))


def build_router(broadcast_drafts: dict[int, str] | None = None) -> Router:
    router = Router(name="admin")
    router.message.register(cmd_admin, Command("admin"))
    router.message.register(cmd_broadcast, Command("broadcast"))
    router.message.register(cmd_corrections, Command("corrections"))

    drafts = broadcast_drafts if broadcast_drafts is not None else {}

    def awaiting_draft(message: Message) -> bool:
        """Ждёт ли этот пользователь ввода текста рассылки.

        Проверка вынесена в фильтр, а не в тело обработчика, намеренно. Фильтры
        отрабатывают до внутренних middleware, а `SkipHandler` из обработчика заставил бы
        цепочку `auth → ratelimit → logging` пройти дважды на каждое сообщение
        администратора — то есть списать с него два запроса лимита вместо одного.
        Заполнять словарь может только админский путь, так что фильтр не даёт доступа.
        """
        sender = message.from_user
        return sender is not None and drafts.get(sender.id) == AWAITING

    router.message.register(handle_broadcast_text, F.text, ~F.text.startswith("/"), awaiting_draft)
    router.callback_query.register(
        handle_admin_callback, F.data.startswith(f"{keyboards.ADMIN_PREFIX}:")
    )
    return router
