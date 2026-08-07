"""Конфигурация из `.env`, валидируемая при старте.

Ошибка конфигурации — фатальна, но пользователь должен увидеть понятный текст на русском,
а не traceback pydantic. Разбор ошибок в человекочитаемый вид — `format_config_error`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Человекочитаемые подсказки для полей, без которых бот не стартует.
_REQUIRED_HINTS = {
    "BOT_TOKEN": "токен бота от @BotFather",
    "ADMIN_USER_IDS": "числовые Telegram ID администраторов через запятую",
}


class Settings(BaseSettings):
    """Настройки бота. Имена полей совпадают с переменными в `.env`."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ---
    bot_token: SecretStr = Field(alias="BOT_TOKEN")

    # ВАЖНО: строка, а не list[int]. pydantic-settings разбирает поля составных типов
    # из переменных окружения как JSON, поэтому `ADMIN_USER_IDS=123,456` упал бы
    # с ошибкой парсинга. Разбираем сами — см. admin_user_ids ниже.
    admin_user_ids_raw: str = Field(alias="ADMIN_USER_IDS")

    invite_ttl_hours: Annotated[int, Field(gt=0, le=720)] = Field(24, alias="INVITE_TTL_HOURS")
    invite_attempts_per_hour: Annotated[int, Field(gt=0)] = Field(
        5, alias="INVITE_ATTEMPTS_PER_HOUR"
    )

    # --- Пути ---
    db_path: Path = Field(Path("data/tnved.db"), alias="DB_PATH")
    photo_dir: Path = Field(Path("data/photos"), alias="PHOTO_DIR")
    nomenclature_dir: Path = Field(Path("data/nomenclature"), alias="NOMENCLATURE_DIR")
    backup_dir: Path = Field(Path("data/backups"), alias="BACKUP_DIR")
    log_dir: Path = Field(Path("logs"), alias="LOG_DIR")

    # --- ИИ через CLI claude ---
    claude_bin: str = Field("claude", alias="CLAUDE_BIN")
    claude_model: str = Field("claude-sonnet-5", alias="CLAUDE_MODEL")
    claude_timeout_text: Annotated[int, Field(gt=0)] = Field(90, alias="CLAUDE_TIMEOUT_TEXT")
    claude_timeout_vision: Annotated[int, Field(gt=0)] = Field(120, alias="CLAUDE_TIMEOUT_VISION")
    claude_max_retries: Annotated[int, Field(ge=0, le=5)] = Field(2, alias="CLAUDE_MAX_RETRIES")
    claude_max_concurrency: Annotated[int, Field(gt=0, le=8)] = Field(
        2, alias="CLAUDE_MAX_CONCURRENCY"
    )
    claude_breaker_failures: Annotated[int, Field(gt=0)] = Field(5, alias="CLAUDE_BREAKER_FAILURES")
    claude_breaker_cooldown_minutes: Annotated[int, Field(gt=0)] = Field(
        5, alias="CLAUDE_BREAKER_COOLDOWN_MINUTES"
    )

    # --- Логика классификации ---
    fts_candidates: Annotated[int, Field(gt=0, le=200)] = Field(40, alias="FTS_CANDIDATES")
    confidence_accept: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        0.80, alias="CONFIDENCE_ACCEPT"
    )
    confidence_clarify: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        0.45, alias="CONFIDENCE_CLARIFY"
    )
    max_clarify_rounds: Annotated[int, Field(gt=0, le=10)] = Field(3, alias="MAX_CLARIFY_ROUNDS")
    session_timeout_minutes: Annotated[int, Field(gt=0)] = Field(
        30, alias="SESSION_TIMEOUT_MINUTES"
    )

    # --- Хранение данных ---
    photo_ttl_hours: Annotated[int, Field(gt=0)] = Field(48, alias="PHOTO_TTL_HOURS")
    photo_meta_retention_days: Annotated[int, Field(gt=0)] = Field(
        30, alias="PHOTO_META_RETENTION_DAYS"
    )
    session_retention_days: Annotated[int, Field(gt=0)] = Field(7, alias="SESSION_RETENTION_DAYS")
    audit_retention_days: Annotated[int, Field(gt=0)] = Field(90, alias="AUDIT_RETENTION_DAYS")
    backup_keep: Annotated[int, Field(gt=0)] = Field(7, alias="BACKUP_KEEP")
    max_photo_mb: Annotated[int, Field(gt=0, le=50)] = Field(10, alias="MAX_PHOTO_MB")

    # --- Лимиты ---
    rate_limit_per_hour: Annotated[int, Field(gt=0)] = Field(20, alias="RATE_LIMIT_PER_HOUR")
    rate_limit_per_day: Annotated[int, Field(gt=0)] = Field(100, alias="RATE_LIMIT_PER_DAY")
    global_limit_per_day: Annotated[int, Field(gt=0)] = Field(300, alias="GLOBAL_LIMIT_PER_DAY")

    # --- Прочее ---
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    admin_alerts: bool = Field(True, alias="ADMIN_ALERTS")
    stale_update_minutes: Annotated[int, Field(gt=0)] = Field(5, alias="STALE_UPDATE_MINUTES")

    # ------------------------------------------------------------------ валидаторы

    @field_validator("admin_user_ids_raw")
    @classmethod
    def _check_admin_ids(cls, value: str) -> str:
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if not parts:
            msg = "список пуст — ботом будет некому управлять"
            raise ValueError(msg)
        bad = [p for p in parts if not p.isdigit()]
        if bad:
            msg = (
                f"не числовые значения: {', '.join(bad)} (нужен числовой Telegram ID, не @username)"
            )
            raise ValueError(msg)
        return value

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.strip().upper()
        if upper not in allowed:
            msg = f"должен быть одним из {', '.join(sorted(allowed))}"
            raise ValueError(msg)
        return upper

    @model_validator(mode="after")
    def _check_thresholds(self) -> Settings:
        if self.confidence_clarify >= self.confidence_accept:
            msg = (
                f"CONFIDENCE_CLARIFY ({self.confidence_clarify}) должен быть строго меньше "
                f"CONFIDENCE_ACCEPT ({self.confidence_accept}), иначе гейт уверенности "
                f"никогда не задаст уточняющий вопрос"
            )
            raise ValueError(msg)
        return self

    # ------------------------------------------------------------------ производные

    @computed_field  # type: ignore[prop-decorator]
    @property
    def admin_user_ids(self) -> frozenset[int]:
        """Разобранный список ID администраторов."""
        return frozenset(int(p) for p in self.admin_user_ids_raw.split(",") if p.strip())

    @property
    def data_dirs(self) -> tuple[Path, ...]:
        """Каталоги, которые создаются при старте."""
        return (
            self.abs_path(self.db_path).parent,
            self.abs_path(self.photo_dir),
            self.abs_path(self.nomenclature_dir),
            self.abs_path(self.backup_dir),
            self.abs_path(self.log_dir),
        )

    @property
    def lock_path(self) -> Path:
        return self.abs_path(self.db_path).parent / "bot.lock"

    @staticmethod
    def abs_path(path: Path) -> Path:
        """Относительные пути из `.env` считаются от корня проекта, а не от cwd.

        Иначе бот, запущенный планировщиком задач из System32, создал бы `data/` там.
        """
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    def ensure_dirs(self) -> None:
        for directory in self.data_dirs:
            directory.mkdir(parents=True, exist_ok=True)


def format_config_error(exc: Exception) -> str:
    """Превращает ошибку валидации pydantic в понятный русский текст."""
    lines = ["Конфигурация некорректна — бот не запущен.", ""]

    errors = getattr(exc, "errors", None)
    if callable(errors):
        for err in errors():
            loc = err.get("loc") or ("?",)
            field = str(loc[0])
            env_name = _field_to_env_name(field)
            reason = err.get("msg", "некорректное значение")
            reason = reason.removeprefix("Value error, ")
            if err.get("type") == "missing":
                hint = _REQUIRED_HINTS.get(env_name, "обязательная переменная")
                lines.append(f"  • {env_name} — не задан ({hint})")
            else:
                lines.append(f"  • {env_name} — {reason}")
    else:
        lines.append(f"  • {exc}")

    lines += [
        "",
        "Проверьте файл .env в корне проекта.",
        "Если его нет — скопируйте .env.example и заполните BOT_TOKEN и ADMIN_USER_IDS.",
    ]
    return "\n".join(lines)


def _field_to_env_name(field: str) -> str:
    """`admin_user_ids_raw` → `ADMIN_USER_IDS`."""
    return field.removesuffix("_raw").upper()


def load_settings() -> Settings:
    """Читает и валидирует конфигурацию. Бросает `ValidationError` при ошибке."""
    return Settings()  # type: ignore[call-arg]  # значения приходят из .env
