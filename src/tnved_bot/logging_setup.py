"""Настройка логирования: structlog + ротация файлов + маскирование секретов.

Инвариант: токен бота не должен попасть в лог ни при каких обстоятельствах — ни в сообщении,
ни в структурных полях, ни в тексте исключения. Маскирование стоит последним процессором,
после всех остальных, чтобы отработать на итоговом тексте.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any

import structlog

MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# Токен бота: <цифры>:<30+ символов>. Без \b по краям намеренно: aiogram логирует URL вида
# `https://api.telegram.org/bot<token>/getMe`, где перед цифрами стоит буква «t» из «bot»
# и границы слова нет — с \b токен в URL не маскировался бы.
_TOKEN_PATTERN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")

# Поля, значение которых не логируется никогда, чем бы оно ни было.
_SECRET_KEYS = frozenset({"bot_token", "token", "api_key", "password", "secret", "invite_code"})

_MASK = "***"


def mask_secrets(text: str) -> str:
    """Заменяет похожее на токен бота на маску."""
    return _TOKEN_PATTERN.sub(_MASK, text)


def _mask_processor(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Маскирует секреты в сообщении и во всех строковых полях события."""
    for key, value in list(event_dict.items()):
        if key.lower() in _SECRET_KEYS:
            event_dict[key] = _MASK
        elif isinstance(value, str):
            event_dict[key] = mask_secrets(value)
    return event_dict


class MaskingFormatter(logging.Formatter):
    """Маскирует секреты в **итоговой** строке лога, после всех форматирований.

    Только процессора structlog недостаточно: `logger.exception()` передаёт `exc_info`
    и в stdlib-логгер, который дописывает traceback уже после JSON-строки. Токен из текста
    исключения утекал бы в файл мимо structlog. Здесь же перехватывается всё, включая
    сообщения сторонних библиотек (aiogram/aiohttp логируют URL с токеном).
    """

    def format(self, record: logging.LogRecord) -> str:
        return mask_secrets(super().format(record))


def setup_logging(log_dir: Path, level: str = "INFO", *, console: bool = True) -> None:
    """Инициализирует логирование. Вызывать один раз при старте, до всего остального."""
    log_dir.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            log_dir / "bot.log",
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    ]
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))

    formatter = MaskingFormatter("%(message)s")
    for handler in handlers:
        handler.setFormatter(formatter)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )

    # aiohttp и aiogram шумят на DEBUG и могут вывалить заголовки запросов вместе с токеном.
    for noisy in ("aiogram.event", "aiohttp.access", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _mask_processor,  # последним: маскируем уже отформатированный текст
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
