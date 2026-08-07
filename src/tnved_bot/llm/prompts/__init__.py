"""Промты хранятся в отдельных `.md`, чтобы правились без изменения кода."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


@lru_cache(maxsize=8)
def load(name: str) -> str:
    """Читает промт по имени файла без расширения."""
    path = _DIR / f"{name}.md"
    if not path.is_file():
        msg = f"промт {name!r} не найден: {path}"
        raise FileNotFoundError(msg)
    return path.read_text(encoding="utf-8").strip()
