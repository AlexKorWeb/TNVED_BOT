"""Тесты логирования: токен не должен попасть в лог ни при каких обстоятельствах."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from tests.conftest import FAKE_TOKEN
from tnved_bot.logging_setup import get_logger, mask_secrets, setup_logging


@pytest.mark.parametrize(
    "text",
    [
        FAKE_TOKEN,
        f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe",
        f"Unauthorized for token {FAKE_TOKEN} while polling",
        f"prefix {FAKE_TOKEN} suffix",
    ],
)
def test_mask_secrets_removes_token(text: str) -> None:
    masked = mask_secrets(text)
    assert FAKE_TOKEN not in masked
    assert "***" in masked


def test_mask_keeps_ordinary_text() -> None:
    text = "код 8516710000 найден, уверенность 0.91"
    assert mask_secrets(text) == text


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "logs"
    setup_logging(directory, "INFO", console=False)
    yield directory
    logging.shutdown()


def _read_log(log_dir: Path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return (log_dir / "bot.log").read_text(encoding="utf-8")


def test_token_masked_in_message(log_dir: Path) -> None:
    get_logger("test").error("telegram_error", detail=f"failed with {FAKE_TOKEN}")
    content = _read_log(log_dir)
    assert FAKE_TOKEN not in content
    assert "***" in content


def test_secret_field_masked_by_name(log_dir: Path) -> None:
    """Поле с секретным именем маскируется целиком, чем бы оно ни было."""
    get_logger("test").info("started", bot_token="whatever-value-here", invite_code="TNVED-AAAA")
    content = _read_log(log_dir)
    assert "whatever-value-here" not in content
    assert "TNVED-AAAA" not in content


def test_token_masked_inside_exception(log_dir: Path) -> None:
    """Токен, попавший в текст чужого исключения, тоже не должен утечь."""
    try:
        msg = f"Unauthorized: bot token {FAKE_TOKEN} is invalid"
        raise RuntimeError(msg)
    except RuntimeError:
        get_logger("test").exception("fatal_error")

    content = _read_log(log_dir)
    assert FAKE_TOKEN not in content


def test_log_is_valid_json(log_dir: Path) -> None:
    get_logger("test").info("bot_started", version="0.1.0", admins=2)
    line = _read_log(log_dir).strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "bot_started"
    assert payload["admins"] == 2
    assert payload["level"] == "info"
    assert "timestamp" in payload
