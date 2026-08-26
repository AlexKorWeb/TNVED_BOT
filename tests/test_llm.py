"""Тесты клиента ИИ и валидации его ответов. Настоящий `claude` не вызывается."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest

from tnved_bot.core.errors import LlmError
from tnved_bot.llm.client import CircuitBreaker, LlmClient, extract_json
from tnved_bot.llm.prompts import load
from tnved_bot.llm.schema import parse_classify, parse_keywords


def fake_claude(tmp_path: Path, script: str) -> Path:
    """Подставной `claude`: python-скрипт, который ведёт себя как CLI."""
    path = tmp_path / "fake_claude.py"
    path.write_text(textwrap.dedent(script), encoding="utf-8")
    return path


class ScriptedClient(LlmClient):
    """Клиент, у которого вместо `claude` вызывается подставной скрипт."""

    def __init__(self, script_path: Path, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._script = script_path

    def check_binary(self) -> str | None:
        return sys.executable

    def _build_args(self, system: str, allow_read_dir: Path | None) -> list[str]:
        # `-X utf8`: клиент пишет в stdin строго UTF-8, и настоящий `claude` так его и читает.
        # Дочерний python без этого флага декодирует stdin в кодировке локали — на машине с
        # cp1251/cp1252 кириллица превращается в мусор, и тест на stdin падает не из-за кода,
        # а из-за кодовой страницы раннера. Флаг выравнивает дубль с реальным CLI.
        return [sys.executable, "-X", "utf8", str(self._script)]


# ---------------------------------------------------------------- извлечение JSON


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"code": "1"}', {"code": "1"}),
        ('Вот ответ: {"code": "1"} готово', {"code": "1"}),
        ('```json\n{"code": "1"}\n```', {"code": "1"}),
        ('```\n{"code": "1"}\n```', {"code": "1"}),
        ('{"a": {"b": 1}} хвост', {"a": {"b": 1}}),
        ('{"text": "скобка } внутри строки"}', {"text": "скобка } внутри строки"}),
        ('{"text": "экранированная \\" кавычка"}', {"text": 'экранированная " кавычка'}),
    ],
)
def test_extract_json(raw: str, expected: dict[str, object]) -> None:
    """Модель регулярно добавляет пояснения вокруг JSON — требовать идеала бессмысленно."""
    assert extract_json(raw) == expected


@pytest.mark.parametrize("raw", ["извините, не могу", "", "[1, 2, 3]", "{битый", "null"])
def test_extract_json_returns_none_on_garbage(raw: str) -> None:
    assert extract_json(raw) is None


# ---------------------------------------------------------------- схема ответа


def test_classify_valid() -> None:
    response = parse_classify(
        {
            "code": "8516 71 000 0",
            "confidence": 0.91,
            "reasoning": ["бытовой прибор"],
            "alternatives": [{"code": "8516797000", "why": "прочие"}],
        }
    )
    assert response is not None
    assert response.code == "8516710000"
    assert response.alternatives[0].code == "8516797000"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(1.5, 1.0), (-0.5, 0.0), ("0.7", 0.7), ("не знаю", 0.0), (None, 0.0)],
)
def test_confidence_clamped(raw: object, expected: float) -> None:
    """Уверенность вне диапазона — признак, что модель не следовала инструкции."""
    response = parse_classify({"code": "8516710000", "confidence": raw})
    assert response is not None
    assert response.confidence == expected


def test_markup_stripped_from_text() -> None:
    """Разметка от модели сломала бы HTML-парсер Telegram, ссылки увели бы пользователя."""
    response = parse_classify(
        {
            "code": "8516710000",
            "reasoning": ["*жирный* <b>тег</b> и ссылка https://evil.example/x"],
            "clarifying_question": "Материал? <script>",
            "options": ["Сталь `код`", "Пластик [x]"],
        }
    )
    assert response is not None
    assert "<" not in response.reasoning[0]
    assert "https://" not in response.reasoning[0]
    assert "<" not in (response.clarifying_question or "")
    assert all("`" not in option and "[" not in option for option in response.options)


def test_limits_enforced() -> None:
    response = parse_classify(
        {
            "code": "8516710000",
            "reasoning": [f"строка {i}" for i in range(20)],
            "alternatives": [{"code": f"850000000{i}"} for i in range(9)],
            "options": [f"вариант {i}" for i in range(9)],
        }
    )
    assert response is not None
    assert len(response.reasoning) <= 5
    assert len(response.alternatives) <= 3
    assert len(response.options) <= 4


def test_wants_clarification_requires_options() -> None:
    """Вопрос без вариантов бесполезен: кнопок из него не построить."""
    without = parse_classify({"clarifying_question": "Материал?", "options": []})
    with_options = parse_classify(
        {"clarifying_question": "Материал?", "options": ["Сталь", "Пластик"]}
    )
    assert without is not None and not without.wants_clarification
    assert with_options is not None and with_options.wants_clarification


def test_empty_payload_is_valid_but_useless() -> None:
    response = parse_classify({})
    assert response is not None
    assert response.code is None
    assert response.confidence == 0.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["85"], ["85"]),
        (["85", "84"], ["85", "84"]),
        ("85", ["85"]),
        (85, ["85"]),
        (["85", "85"], ["85"]),
        (["99"], []),
        (["00"], []),
        (["8"], []),
        (["абв"], []),
        ("мусор", []),
    ],
)
def test_chapters_validated(raw: object, expected: list[str]) -> None:
    """Групп ТН ВЭД ровно 01–97; всё остальное отбрасывается, а не «чинится»."""
    response = parse_keywords({"keywords": "трубы", "chapters": raw})
    assert response is not None
    assert response.chapters == expected


def test_keywords_joined_from_list() -> None:
    response = parse_keywords({"keywords": ["трубы", "пластик"]})
    assert response is not None
    assert response.keywords == "трубы пластик"


# ---------------------------------------------------------------- промты


@pytest.mark.parametrize("name", ["keywords", "classify", "vision"])
def test_prompts_exist(name: str) -> None:
    text = load(name)
    assert len(text) > 200
    assert "JSON" in text


def test_prompt_states_user_data_is_data_not_instructions() -> None:
    """Изоляция данных — третий слой защиты; без этой фразы промт её не обеспечивает."""
    for name in ("keywords", "classify"):
        text = load(name)
        assert "user_data" in text
        assert "не инструкции" in text


def test_classify_prompt_forbids_inventing_codes() -> None:
    text = load("classify").lower()
    assert "только из списка" in text
    assert "не выдумывай" in text


def test_missing_prompt_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load("нет-такого-промта")


# ---------------------------------------------------------------- circuit breaker


def test_breaker_opens_after_threshold() -> None:
    breaker = CircuitBreaker(threshold=3, cooldown_minutes=5)
    for _ in range(2):
        breaker.record_failure()
    assert not breaker.is_open
    breaker.record_failure()
    assert breaker.is_open
    assert breaker.retry_after_seconds > 0


def test_breaker_resets_on_success() -> None:
    breaker = CircuitBreaker(threshold=2, cooldown_minutes=5)
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert not breaker.is_open


def test_breaker_closes_after_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    breaker = CircuitBreaker(threshold=1, cooldown_minutes=5)
    breaker.record_failure()
    assert breaker.is_open

    import tnved_bot.llm.client as client_module

    base = client_module.time.monotonic()
    monkeypatch.setattr(client_module.time, "monotonic", lambda: base + 301)
    assert not breaker.is_open


# ---------------------------------------------------------------- вызов


async def test_successful_call(tmp_path: Path) -> None:
    script = fake_claude(
        tmp_path,
        """
        import json, sys
        sys.stdin.read()
        print(json.dumps({"is_error": False, "result": '{"code": "8516710000"}',
                          "usage": {"input_tokens": 10, "output_tokens": 5}}))
        """,
    )
    client = ScriptedClient(script)
    result = await client.run_json("опиши товар", "система", timeout=30)
    assert result.payload == {"code": "8516710000"}
    assert result.input_tokens == 10
    assert result.output_tokens == 5


async def test_user_text_goes_through_stdin_not_argv(tmp_path: Path) -> None:
    """argv виден в списке процессов и попадает в логи планировщика — текста там быть не должно."""
    script = fake_claude(
        tmp_path,
        """
        import json, sys
        received = sys.stdin.read()
        print(json.dumps({"is_error": False,
                          "result": json.dumps({"got": received.strip(), "argv": sys.argv[1:]})}))
        """,
    )
    client = ScriptedClient(script)
    secret = "СЕКРЕТНОЕ ОПИСАНИЕ ТОВАРА"
    result = await client.run_json(secret, "система", timeout=30)
    assert secret in str(result.payload["got"])
    assert secret not in json.dumps(result.payload["argv"], ensure_ascii=False)


async def test_timeout_kills_process(tmp_path: Path) -> None:
    script = fake_claude(
        tmp_path,
        """
        import sys, time
        sys.stdin.read()
        time.sleep(60)
        """,
    )
    client = ScriptedClient(script, max_retries=0)
    with pytest.raises(LlmError) as exc:
        await client.run_json("промт", "система", timeout=2)
    assert "не ответил" in str(exc.value)


async def test_retries_on_bad_json_then_succeeds(tmp_path: Path) -> None:
    marker = tmp_path / "attempts.txt"
    script = fake_claude(
        tmp_path,
        f"""
        import json, sys
        from pathlib import Path
        sys.stdin.read()
        marker = Path({str(marker)!r})
        n = int(marker.read_text()) if marker.exists() else 0
        marker.write_text(str(n + 1))
        if n == 0:
            print("это не json вовсе")
        else:
            print(json.dumps({{"is_error": False, "result": '{{"code": "1"}}'}}))
        """,
    )
    client = ScriptedClient(script, max_retries=2)
    result = await client.run_json("промт", "система", timeout=30)
    assert result.payload == {"code": "1"}
    assert marker.read_text() == "2"


async def test_missing_binary_is_not_retried() -> None:
    """Отсутствие бинаря повтором не лечится — ждать ретраи значит зря жечь время пользователя."""
    client = LlmClient(binary="заведомо-несуществующий-бинарь", max_retries=2)
    started = asyncio.get_running_loop().time()
    with pytest.raises(LlmError) as exc:
        await client.run_json("промт", "система", timeout=30)
    assert "не найден" in str(exc.value)
    assert asyncio.get_running_loop().time() - started < 1.0


async def test_error_envelope_reported(tmp_path: Path) -> None:
    script = fake_claude(
        tmp_path,
        """
        import json, sys
        sys.stdin.read()
        print(json.dumps({"is_error": True, "result": "rate limit exceeded"}))
        """,
    )
    client = ScriptedClient(script, max_retries=0)
    with pytest.raises(LlmError) as exc:
        await client.run_json("промт", "система", timeout=30)
    assert "rate limit" in str(exc.value)


async def test_concurrency_is_limited(tmp_path: Path) -> None:
    """Каждый вызов — отдельный процесс; без ограничения десяток запросов положит машину.

    Считаем не общий счётчик в файле (дочерние процессы гонялись бы за него), а интервалы
    жизни каждого процесса — и ищем максимальное перекрытие.
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    script = fake_claude(
        tmp_path,
        f"""
        import json, os, sys, time
        from pathlib import Path
        sys.stdin.read()
        started = time.monotonic()
        time.sleep(0.4)
        # Отдельный файл на процесс — никакой гонки за общий ресурс.
        Path({str(runs)!r}, f"{{os.getpid()}}.txt").write_text(f"{{started}} {{time.monotonic()}}")
        print(json.dumps({{"is_error": False, "result": '{{"ok": 1}}'}}))
        """,
    )
    client = ScriptedClient(script, max_concurrency=2)
    await asyncio.gather(*(client.run_json("промт", "s", timeout=30) for _ in range(5)))

    intervals = []
    for file in runs.iterdir():
        start, end = (float(x) for x in file.read_text().split())
        intervals.append((start, end))
    assert len(intervals) == 5

    events = sorted([(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals])
    live = peak = 0
    for _, delta in events:
        live += delta
        peak = max(peak, live)
    assert peak <= 2, f"одновременно работало {peak} процессов при лимите 2"


async def test_open_breaker_refuses_fast(tmp_path: Path) -> None:
    script = fake_claude(tmp_path, "import sys; sys.stdin.read(); print('мусор')")
    client = ScriptedClient(script, max_retries=0, breaker_failures=1)
    with pytest.raises(LlmError):
        await client.run_json("промт", "s", timeout=30)
    assert client.breaker_open

    with pytest.raises(LlmError) as exc:
        await client.run_json("промт", "s", timeout=30)
    assert "временно недоступен" in str(exc.value)
