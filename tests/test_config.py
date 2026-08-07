"""Тесты конфигурации: разбор списка ID, валидация, понятные сообщения об ошибках."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tnved_bot.config import PROJECT_ROOT, Settings, format_config_error, load_settings


def test_loads_minimal_env(env: dict[str, str]) -> None:
    settings = load_settings()
    assert settings.admin_user_ids == frozenset({111, 222})
    assert settings.bot_token.get_secret_value() == env["BOT_TOKEN"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("111", {111}),
        ("111,222", {111, 222}),
        (" 111 , 222 ", {111, 222}),  # пробелы вокруг запятых
        ("111,222,", {111, 222}),  # висящая запятая
        ("111,111", {111}),  # дубликаты схлопываются
    ],
)
def test_admin_ids_comma_separated(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, raw: str, expected: set[int]
) -> None:
    """Список читается как CSV, а не как JSON.

    Это главные грабли pydantic-settings v2: поле типа list[int] разбиралось бы как JSON
    и `ADMIN_USER_IDS=111,222` упало бы с ошибкой парсинга.
    """
    monkeypatch.setenv("ADMIN_USER_IDS", raw)
    assert load_settings().admin_user_ids == expected


@pytest.mark.parametrize("raw", ["", "   ", ",", " , "])
def test_admin_ids_empty_is_fatal(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("ADMIN_USER_IDS", raw)
    with pytest.raises(ValidationError) as exc:
        load_settings()
    assert "ботом будет некому управлять" in format_config_error(exc.value)


@pytest.mark.parametrize("raw", ["@username", "111,@petrov", "abc", "111, 22a"])
def test_admin_ids_must_be_numeric(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """@username запрещён: его можно сменить или передать, и доступ уедет к постороннему."""
    monkeypatch.setenv("ADMIN_USER_IDS", raw)
    with pytest.raises(ValidationError) as exc:
        load_settings()
    message = format_config_error(exc.value)
    assert "ADMIN_USER_IDS" in message
    assert "не числовые значения" in message


def test_missing_token_gives_readable_message(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOT_TOKEN")
    with pytest.raises(ValidationError) as exc:
        load_settings()

    message = format_config_error(exc.value)
    assert "BOT_TOKEN" in message
    assert "@BotFather" in message
    assert "Traceback" not in message
    assert "pydantic" not in message.lower()


def test_confidence_thresholds_must_be_ordered(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLARIFY >= ACCEPT означал бы, что уточняющий вопрос не задаётся никогда."""
    monkeypatch.setenv("CONFIDENCE_ACCEPT", "0.5")
    monkeypatch.setenv("CONFIDENCE_CLARIFY", "0.7")
    with pytest.raises(ValidationError) as exc:
        load_settings()
    assert "CONFIDENCE_CLARIFY" in format_config_error(exc.value)


def test_invalid_log_level_rejected(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
    with pytest.raises(ValidationError) as exc:
        load_settings()
    assert "LOG_LEVEL" in format_config_error(exc.value)


def test_log_level_normalized(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", " debug ")
    assert load_settings().log_level == "DEBUG"


def test_token_not_in_repr(env: dict[str, str]) -> None:
    """SecretStr не должен раскрывать токен при печати настроек."""
    settings = load_settings()
    assert env["BOT_TOKEN"] not in repr(settings)
    assert env["BOT_TOKEN"] not in str(settings)
    assert env["BOT_TOKEN"] not in settings.model_dump_json()


def test_relative_paths_resolve_from_project_root() -> None:
    """Планировщик задач запускает бота с чужим cwd — пути обязаны считаться от корня проекта."""
    assert Settings.abs_path(Path("data/tnved.db")) == (PROJECT_ROOT / "data/tnved.db").resolve()


def test_absolute_paths_preserved(tmp_path: Path) -> None:
    assert Settings.abs_path(tmp_path / "x.db") == tmp_path / "x.db"


def test_ensure_dirs_creates_everything(env: dict[str, str]) -> None:
    settings = load_settings()
    settings.ensure_dirs()
    for directory in settings.data_dirs:
        assert directory.is_dir()


def test_out_of_range_values_rejected(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONFIDENCE_ACCEPT", "1.5")
    with pytest.raises(ValidationError):
        load_settings()
