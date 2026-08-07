"""Сборка бота: зависимости, middleware, роутеры, polling.

Единственное место, где связываются слои. Порядок middleware важен:
`auth` → `ratelimit` → `logging`. Whitelist стоит первым, чтобы посторонний не расходовал
ни счётчики, ни ИИ, ни место в логе.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from tnved_bot.bot.handlers import admin, callbacks, commands, errors, photo, text
from tnved_bot.bot.middlewares.auth import AuthMiddleware
from tnved_bot.bot.middlewares.logging import LoggingMiddleware
from tnved_bot.bot.middlewares.ratelimit import RateLimitMiddleware
from tnved_bot.bot.service import DialogService, DialogSettings
from tnved_bot.config import Settings
from tnved_bot.core.classifier import Classifier, ClassifierSettings
from tnved_bot.db.audit import AuditLog
from tnved_bot.db.counters import UsageCounters
from tnved_bot.db.engine import Database
from tnved_bot.db.nomenclature import NomenclatureRepository
from tnved_bot.db.photos import PhotoRepository
from tnved_bot.db.search import NomenclatureSearch
from tnved_bot.db.sessions import SessionRepository
from tnved_bot.db.users import UserRepository
from tnved_bot.llm.client import LlmClient
from tnved_bot.llm.vision import VisionReader
from tnved_bot.logging_setup import get_logger
from tnved_bot.storage.photo_store import PhotoStore

log = get_logger(__name__)

RECONNECT_START = 1.0
RECONNECT_MAX = 60.0


@dataclass(slots=True)
class BotRuntime:
    bot: Bot
    dispatcher: Dispatcher
    llm: LlmClient
    sessions: SessionRepository
    nomenclature: NomenclatureRepository
    audit: AuditLog
    users: UserRepository
    service: DialogService


def build(settings: Settings, db: Database) -> BotRuntime:
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    audit = AuditLog(db)
    counters = UsageCounters(db)
    sessions = SessionRepository(db)
    users = UserRepository(db)
    nomenclature = NomenclatureRepository(db)
    search = NomenclatureSearch(db)

    llm = LlmClient(
        binary=settings.claude_bin,
        model=settings.claude_model,
        max_concurrency=settings.claude_max_concurrency,
        max_retries=settings.claude_max_retries,
        breaker_failures=settings.claude_breaker_failures,
        breaker_cooldown_minutes=settings.claude_breaker_cooldown_minutes,
    )

    classifier = Classifier(
        search,
        nomenclature,
        llm,
        ClassifierSettings(
            candidates=settings.fts_candidates,
            accept=settings.confidence_accept,
            clarify=settings.confidence_clarify,
            max_rounds=settings.max_clarify_rounds,
            timeout_text=settings.claude_timeout_text,
        ),
    )

    service = DialogService(
        classifier,
        sessions,
        audit,
        DialogSettings(
            timeout_minutes=settings.session_timeout_minutes,
            max_rounds=settings.max_clarify_rounds,
            accept=settings.confidence_accept,
            clarify=settings.confidence_clarify,
        ),
    )

    photo_store = PhotoStore(settings.abs_path(settings.photo_dir), settings.max_photo_mb)
    photos = PhotoRepository(db)
    vision = VisionReader(llm, timeout=settings.claude_timeout_vision)

    dispatcher = Dispatcher()
    dispatcher.workflow_data.update(
        service=service,
        sessions=sessions,
        nomenclature=nomenclature,
        audit=audit,
        users=users,
        llm=llm,
        photos=photos,
        photo_store=photo_store,
        vision=vision,
        admins=settings.admin_user_ids,
        invite_ttl_hours=settings.invite_ttl_hours,
        photo_ttl_hours=settings.photo_ttl_hours,
        session_timeout_minutes=settings.session_timeout_minutes,
        db_path_parent=str(settings.abs_path(settings.db_path).parent),
    )

    auth = AuthMiddleware(settings.admin_user_ids, users, audit)
    limits = RateLimitMiddleware(
        counters,
        audit,
        per_hour=settings.rate_limit_per_hour,
        per_day=settings.rate_limit_per_day,
        global_per_day=settings.global_limit_per_day,
    )
    tracing = LoggingMiddleware()

    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.middleware(auth)
        observer.middleware(limits)
        observer.middleware(tracing)

    # Обработчик ошибок вешается на сам диспетчер, а не отдельным дочерним роутером:
    # observer дочернего роутера ловит только ошибки собственных хендлеров, поэтому
    # сбои в командах и в тексте прошли бы мимо него.
    dispatcher.errors.register(errors.handle_error)

    # Админский роутер — до общего обработчика кнопок: он забирает `adm:` себе.
    dispatcher.include_router(admin.build_router())
    dispatcher.include_router(commands.build_router())
    dispatcher.include_router(callbacks.build_router())
    dispatcher.include_router(photo.build_router())
    dispatcher.include_router(text.build_router())

    return BotRuntime(bot, dispatcher, llm, sessions, nomenclature, audit, users, service)


USER_COMMANDS = [
    ("start", "Начать работу"),
    ("help", "Как описывать товар"),
    ("code", "Наименование по коду"),
    ("cancel", "Прервать уточнения"),
    ("forget", "Удалить мои данные"),
    ("version", "Версия бота и справочника"),
]

ADMIN_COMMANDS = [
    ("admin", "Панель администратора"),
    ("users", "Список пользователей"),
    ("invite", "Код приглашения"),
    ("adduser", "Выдать доступ по ID"),
    ("deluser", "Отозвать доступ"),
    ("health", "Состояние системы"),
    ("stats", "Статистика за сутки"),
]


async def publish_commands(runtime: BotRuntime, admins: frozenset[int]) -> None:
    """Заполняет меню команд в интерфейсе Telegram.

    Без этого кнопка «Меню» в чате пуста, и о командах можно узнать только из /help.
    Админские команды публикуются точечно, для чатов администраторов: обычному
    пользователю незачем видеть в меню то, чем он не может воспользоваться.

    Сбой не критичен — это оформление, а не работа бота.
    """
    user_menu = [BotCommand(command=name, description=title) for name, title in USER_COMMANDS]
    admin_menu = user_menu + [
        BotCommand(command=name, description=title) for name, title in ADMIN_COMMANDS
    ]
    try:
        await runtime.bot.set_my_commands(user_menu, scope=BotCommandScopeDefault())
        for admin_id in admins:
            await runtime.bot.set_my_commands(
                admin_menu, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        log.info("commands_published", admins=len(admins))
    except Exception as exc:  # noqa: BLE001 — меню не главное, бот работает и без него
        log.warning("commands_publish_failed", error=str(exc)[:200])


async def run_polling(runtime: BotRuntime, shutdown: asyncio.Event) -> None:
    """Long polling с бесконечным переподключением.

    Обрыв сети — норма для домашнего компьютера, а не повод останавливать бота. Пауза
    растёт до минуты, чтобы не долбить недоступный API.
    """
    delay = RECONNECT_START
    polling = asyncio.create_task(_poll_forever(runtime, delay))
    stopper = asyncio.create_task(shutdown.wait())
    try:
        await asyncio.wait({polling, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        polling.cancel()
        stopper.cancel()
        await runtime.dispatcher.stop_polling()


async def _poll_forever(runtime: BotRuntime, delay: float) -> None:
    while True:
        try:
            # drop_pending_updates: накопленные за время простоя компьютера сообщения
            # отвечать поздно, а обрабатывать их пачкой — значит выжечь лимиты ИИ.
            await runtime.dispatcher.start_polling(
                runtime.bot, handle_signals=False, drop_pending_updates=True
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — сеть недоступна, ждём и пробуем снова
            log.warning("polling_failed", error=str(exc)[:200], retry_in=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)
