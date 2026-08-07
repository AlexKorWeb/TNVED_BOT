"""Общие фикстуры тестов."""

from __future__ import annotations

from pathlib import Path

import pytest

from tnved_bot.config import Settings

# Заведомо ненастоящий токен нужного формата — используется во всех тестах конфига.
FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_0123456789abc"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """Изолированное окружение: настоящий `.env` проекта не читается, пути ведут в tmp_path.

    Без этого тесты подхватили бы `.env` разработчика и стали бы зависеть от его содержимого.
    """
    values = {
        "BOT_TOKEN": FAKE_TOKEN,
        "ADMIN_USER_IDS": "111,222",
        "DB_PATH": str(tmp_path / "db" / "tnved.db"),
        "PHOTO_DIR": str(tmp_path / "photos"),
        "NOMENCLATURE_DIR": str(tmp_path / "nomenclature"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "LOG_DIR": str(tmp_path / "logs"),
        # Тесты не ходят в сеть. Без этого сборка бота создала бы настоящий HTTP-клиент,
        # и первый же ответ с кодом полез бы на ifcg.ru — за справкой, которую тест не
        # проверяет. Кто проверяет справку, подставляет фейковый клиент явно.
        "REFERENCE_ENABLED": "false",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    # Подменяем путь к .env на заведомо несуществующий файл в tmp_path.
    monkeypatch.setitem(Settings.model_config, "env_file", tmp_path / "absent.env")
    return values
