"""Точка входа бота.

Порядок старта важен: конфиг → логи → каталоги → лок → работа. Пока конфиг не прочитан,
логировать некуда; пока лок не взят, работать нельзя.

Коды выхода: 0 — штатно, 2 — ошибка конфигурации, 3 — бот уже запущен, 1 — прочее.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from types import FrameType

from pydantic import ValidationError

from tnved_bot import __version__
from tnved_bot.bot.setup import build, run_polling
from tnved_bot.config import Settings, format_config_error, load_settings
from tnved_bot.core.errors import AlreadyRunningError
from tnved_bot.db.engine import Database
from tnved_bot.jobs.janitor import Janitor
from tnved_bot.lockfile import SingleInstanceLock
from tnved_bot.logging_setup import get_logger, setup_logging

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_CONFIG = 2
EXIT_ALREADY_RUNNING = 3

log = get_logger(__name__)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown: asyncio.Event) -> None:
    """Ставит обработчики сигналов завершения.

    `loop.add_signal_handler` на Windows не поддерживается (ProactorEventLoop), поэтому
    используется обычный `signal.signal` + `call_soon_threadsafe`.
    """

    def handler(signum: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(shutdown.set)
        log.info("shutdown_signal", signal=signal.Signals(signum).name)

    signals = [signal.SIGINT, signal.SIGTERM]
    if sys.platform == "win32":
        signals.append(signal.SIGBREAK)

    for sig in signals:
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError) as exc:
            # Не главный поток или сигнал недоступен — не повод падать.
            log.warning("signal_handler_failed", signal=sig, error=str(exc))


async def run(settings: Settings) -> None:
    """Основной цикл работы бота."""
    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    _install_signal_handlers(loop, shutdown)

    db = Database(
        settings.abs_path(settings.db_path),
        backup_dir=settings.abs_path(settings.backup_dir),
    )
    await db.connect()

    runtime = build(settings, db)
    nomenclature = await runtime.nomenclature.active_version()
    claude_path = runtime.llm.check_binary()

    log.info(
        "bot_started",
        version=__version__,
        python=sys.version.split()[0],
        admins=len(settings.admin_user_ids),
        schema=await db.user_version(),
        nomenclature=nomenclature.rows if nomenclature else 0,
        claude=bool(claude_path),
    )
    # Предупреждения при старте, а не при первом запросе: администратор должен узнать
    # о проблеме сразу, а не от пользователя.
    if nomenclature is None:
        log.error("nomenclature_not_loaded", hint="scripts/import_nomenclature.py")
    if claude_path is None:
        log.error("claude_not_found", binary=settings.claude_bin)

    janitor = Janitor(db, settings)
    await janitor.start(runtime.bot)

    try:
        await run_polling(runtime, shutdown)
    finally:
        log.info("bot_stopping")
        await janitor.stop()
        await runtime.bot.session.close()
        await db.close()


def main() -> int:
    try:
        settings = load_settings()
    except ValidationError as exc:
        sys.stderr.write(format_config_error(exc) + "\n")
        return EXIT_CONFIG

    settings.ensure_dirs()
    setup_logging(settings.abs_path(settings.log_dir), settings.log_level)

    lock = SingleInstanceLock(settings.lock_path)
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        # WARNING, а не ERROR: это штатный исход, а не поломка. Планировщик задач при
        # автоперезапуске регулярно попадает в живой лок, и такие записи не должны
        # выглядеть в логе и в smoke-проверке как отказ работающего бота.
        log.warning("already_running", detail=str(exc))
        sys.stderr.write(f"{exc}\n")
        return EXIT_ALREADY_RUNNING

    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        log.info("interrupted")
    except Exception:
        # Единственное место, где ловится всё: иначе процесс упадёт с traceback в консоль,
        # а планировщик задач молча перезапустит его без диагностики в логе.
        log.exception("fatal_error")
        return EXIT_UNEXPECTED
    finally:
        lock.release()
        log.info("bot_stopped")

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
