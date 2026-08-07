"""Вызов ИИ через CLI `claude -p`.

Флаги подобраны замером, а не по документации. На тривиальном запросе:

| Вариант | Вход, токенов | Время |
|---|---|---|
| по умолчанию | 65 000 | 8.7 с |
| `--system-prompt` | 55 700 | 9.9 с |
| `+ --allowedTools ""` | 55 700 | 7.2 с |
| `+ --setting-sources ""` | **33 000** | 5.6 с |

Вдвое меньше входных токенов — это вдвое меньше расхода лимитов подписки на каждый запрос
пользователя. `--setting-sources ""` убирает CLAUDE.md, скиллы и настройки проекта: боту они
не нужны, а грузятся в каждый вызов.

`--allowedTools ""` здесь не только про экономию: у модели не должно быть инструментов.
Даже успешная prompt-инъекция тогда не может ничего выполнить — это четвёртый слой защиты
из раздела 9.1 ТЗ.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from tnved_bot.clock import utc_now
from tnved_bot.core.errors import LlmError
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

# Ретраятся только эти виды сбоев. Отсутствие бинаря или отказ авторизации повтором
# не лечатся — повторять их значит впустую жечь время пользователя.
RETRYABLE = ("timeout", "bad_json", "empty", "exit_code")


@dataclass(frozen=True, slots=True)
class LlmResult:
    payload: dict[str, object]
    latency_ms: int
    input_tokens: int
    output_tokens: int


class CircuitBreaker:
    """Перестаёт дёргать ИИ после серии отказов.

    Без него каждый запрос пользователя ждал бы полный таймаут и три ретрая при том, что
    ответ заведомо не придёт.
    """

    def __init__(self, threshold: int, cooldown_minutes: int) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_minutes * 60
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self._cooldown:
            self._opened_at = None
            self._failures = 0
            log.info("breaker_closed")
            return False
        return True

    @property
    def retry_after_seconds(self) -> int:
        if self._opened_at is None:
            return 0
        return max(0, int(self._cooldown - (time.monotonic() - self._opened_at)))

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold and self._opened_at is None:
            self._opened_at = time.monotonic()
            log.error("breaker_opened", failures=self._failures)


class LlmClient:
    """Обёртка над `claude -p`, отдающая разобранный JSON."""

    def __init__(
        self,
        binary: str = "claude",
        model: str = "claude-sonnet-5",
        *,
        max_concurrency: int = 2,
        max_retries: int = 2,
        breaker_failures: int = 5,
        breaker_cooldown_minutes: int = 5,
    ) -> None:
        self.binary = binary
        self.model = model
        self.max_retries = max_retries
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._breaker = CircuitBreaker(breaker_failures, breaker_cooldown_minutes)
        self._queue_depth = 0

    # ------------------------------------------------------------------ состояние

    @property
    def available(self) -> bool:
        return shutil.which(self.binary) is not None and not self._breaker.is_open

    @property
    def breaker_open(self) -> bool:
        return self._breaker.is_open

    @property
    def retry_after_seconds(self) -> int:
        return self._breaker.retry_after_seconds

    @property
    def queue_depth(self) -> int:
        return self._queue_depth

    def check_binary(self) -> str | None:
        """Путь к `claude` или `None`. Проверяется при старте, чтобы предупредить сразу."""
        return shutil.which(self.binary)

    # ------------------------------------------------------------------ вызов

    async def run_json(
        self,
        prompt: str,
        system: str,
        *,
        timeout: int,  # noqa: ASYNC109 — обоснование в docstring
        allow_read_dir: Path | None = None,
    ) -> LlmResult:
        """Отправляет промт и возвращает разобранный JSON-ответ модели.

        `allow_read_dir` — единственный каталог, который модели разрешено читать (нужен
        только для разбора фотографии). Без него инструментов нет вовсе.

        ASYNC109: параметр `timeout` здесь осознан. Таймаут привязан к виду вызова
        (текст 90 с, фотография 120 с) и обязан приводить к убийству процесса, а не только
        к отмене ожидания — снаружи `asyncio.timeout` этого не даст, останется зомби.
        """
        if self._breaker.is_open:
            msg = f"ИИ временно недоступен, повтор через {self._breaker.retry_after_seconds} с"
            raise LlmError(msg, user_message=msg)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self._queue_depth += 1
                async with self._semaphore:
                    result = await self._invoke(prompt, system, timeout, allow_read_dir)
                self._breaker.record_success()
                return result
            except LlmError as exc:
                last_error = exc
                kind = getattr(exc, "kind", "unknown")
                if kind not in RETRYABLE:
                    self._breaker.record_failure()
                    raise
                if attempt < self.max_retries:
                    delay = 2.0 * (3**attempt)
                    log.warning("llm_retry", attempt=attempt + 1, kind=kind, delay=delay)
                    await asyncio.sleep(delay)
            finally:
                self._queue_depth -= 1

        self._breaker.record_failure()
        raise LlmError(str(last_error)) from last_error

    def _build_args(self, system: str, allow_read_dir: Path | None) -> list[str]:
        """Командная строка вызова. Выделено отдельно, чтобы тесты подменяли бинарь."""
        binary = self.check_binary()
        if binary is None:
            msg = f"'{self.binary}' не найден в PATH"
            raise _kinded(LlmError(msg), "no_binary")

        return [
            binary,
            "-p",
            "--output-format",
            "json",
            "--model",
            self.model,
            "--system-prompt",
            system,
            # Пустая строка — не опечатка: так отключаются инструменты и загрузка
            # CLAUDE.md/скиллов/настроек проекта.
            "--allowedTools",
            "Read" if allow_read_dir else "",
            "--setting-sources",
            "",
            "--strict-mcp-config",
        ]

    async def _invoke(
        self,
        prompt: str,
        system: str,
        timeout: int,  # noqa: ASYNC109 — см. run_json
        allow_read_dir: Path | None,
    ) -> LlmResult:
        args = self._build_args(system, allow_read_dir)
        started = utc_now()
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(allow_read_dir) if allow_read_dir else None,
        )

        try:
            async with asyncio.timeout(timeout):
                # Текст пользователя уходит через stdin и никогда — в аргументы командной
                # строки: argv виден в списке процессов и попадает в логи планировщика.
                stdout, stderr = await proc.communicate(prompt.encode("utf-8"))
        except TimeoutError as exc:
            await _kill(proc)
            msg = f"ИИ не ответил за {timeout} с"
            raise _kinded(LlmError(msg), "timeout") from exc

        latency_ms = int((utc_now() - started).total_seconds() * 1000)

        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace")[:300]
            kind = "auth" if "auth" in detail.lower() or "login" in detail.lower() else "exit_code"
            raise _kinded(LlmError(f"claude завершился с кодом {proc.returncode}: {detail}"), kind)

        raw = stdout.decode("utf-8", "replace").strip()
        if not raw:
            raise _kinded(LlmError("claude вернул пустой вывод"), "empty")

        return self._parse_envelope(raw, latency_ms)

    def _parse_envelope(self, raw: str, latency_ms: int) -> LlmResult:
        """Разбирает конверт CLI и вложенный ответ модели.

        `--output-format json` отдаёт метаданные вызова, а текст модели лежит в поле
        `result` — его и надо парсить как JSON ответа.
        """
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _kinded(LlmError(f"конверт CLI не разобран: {raw[:200]}"), "bad_json") from exc

        if envelope.get("is_error"):
            detail = str(envelope.get("result", ""))[:300]
            raise _kinded(LlmError(f"claude сообщил об ошибке: {detail}"), "exit_code")

        text = str(envelope.get("result", "")).strip()
        if not text:
            raise _kinded(LlmError("модель вернула пустой ответ"), "empty")

        payload = extract_json(text)
        if payload is None:
            raise _kinded(LlmError(f"ответ модели не является JSON: {text[:200]}"), "bad_json")

        usage = envelope.get("usage") or {}
        return LlmResult(
            payload=payload,
            latency_ms=latency_ms,
            input_tokens=int(usage.get("input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens", 0))
            + int(usage.get("cache_read_input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )


def extract_json(text: str) -> dict[str, object] | None:
    """Достаёт первый сбалансированный JSON-объект из текста.

    Модель регулярно добавляет пояснения до и после JSON или заворачивает его в ```json.
    Требовать идеального ответа бессмысленно — дешевле извлечь.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```", 2)[1] if stripped.count("```") >= 2 else stripped
        stripped = stripped.removeprefix("json").strip()

    start = stripped.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(stripped[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _kinded(error: LlmError, kind: str) -> LlmError:
    error.kind = kind  # type: ignore[attr-defined]
    return error


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Убивает процесс и дожидается его — иначе останется зомби на каждом таймауте."""
    if proc.returncode is not None:
        return
    proc.kill()
    try:
        async with asyncio.timeout(10):
            await proc.wait()
    except TimeoutError:  # pragma: no cover — процесс не умер за 10 с
        log.error("llm_process_kill_failed", pid=proc.pid)
