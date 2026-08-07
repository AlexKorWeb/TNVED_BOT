"""Единая точка получения времени.

Всё время в проекте — timezone-aware UTC. Один источник нужен, чтобы тесты TTL (фото 48 ч,
сессии 30 мин, аудит 90 дней) могли подменить время вместо реального ожидания.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    """ISO 8601 в UTC. В таком виде метки корректно сравниваются лексикографически,
    поэтому отбор просроченных записей делается обычным `WHERE expires_at < ?`."""
    return moment.astimezone(UTC).isoformat()


def now_iso() -> str:
    return iso(utc_now())


def iso_in(**delta: float) -> str:
    """Момент в будущем: `iso_in(hours=48)`."""
    return iso(utc_now() + timedelta(**delta))


def iso_ago(**delta: float) -> str:
    """Момент в прошлом: `iso_ago(days=90)`."""
    return iso(utc_now() - timedelta(**delta))
