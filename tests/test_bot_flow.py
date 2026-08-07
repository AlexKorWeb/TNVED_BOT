"""Интеграционные тесты бота: апдейт скармливается диспетчеру целиком.

Настоящий Telegram не вызывается — сессия подставная и записывает исходящие вызовы.
Это единственный способ проверить связку middleware, роутеров и клавиатур: по отдельности
они выглядят исправными, а ломается обычно порядок и передача зависимостей.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Update, User
from aiogram.types import Message as TgMessage

from tests.conftest import FAKE_TOKEN
from tnved_bot.bot.setup import build
from tnved_bot.config import Settings, load_settings
from tnved_bot.db.engine import Database
from tnved_bot.db.nomenclature import NomenclatureRepository
from tnved_bot.importer import parse_file
from tnved_bot.llm.client import LlmClient, LlmResult

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "nomenclature_sample.csv"
ADMIN_ID = 111
GUEST_ID = 999


class RecordingSession(BaseSession):
    """Подставная сессия: ничего не отправляет, всё записывает."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def sent_texts(self) -> list[str]:
        return [data.get("text", "") for name, data in self.calls if "text" in data]

    @property
    def last_markup(self) -> Any:
        for _, data in reversed(self.calls):
            if data.get("reply_markup") is not None:
                return data["reply_markup"]
        return None

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:  # type: ignore[override]
        name = type(method).__name__
        data = method.model_dump(exclude_none=True)
        self.calls.append((name, data))

        if name in {"SendMessage", "EditMessageText", "EditMessageReplyMarkup"}:
            return TgMessage(
                message_id=len(self.calls),
                date=datetime.now(UTC),
                chat=Chat(id=int(data.get("chat_id", 1)), type="private"),
                text=data.get("text", ""),
            ).as_(bot)
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncIterator[bytes]:  # type: ignore[override]
        yield b""

    async def close(self) -> None:
        return None


class StubLlm(LlmClient):
    """ИИ, отвечающий заранее заданным. Каждый вызов берёт следующий ответ."""

    def __init__(self, *responses: dict[str, Any]) -> None:
        super().__init__()
        self.responses = list(responses)

    @property
    def available(self) -> bool:
        return True

    async def run_json(self, prompt, system, *, timeout, allow_read_dir=None):  # type: ignore[no-untyped-def, override]
        payload = self.responses.pop(0) if self.responses else {"code": None}
        return LlmResult(payload=payload, latency_ms=1, input_tokens=1, output_tokens=1)


@pytest.fixture
def settings(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ADMIN_USER_IDS", str(ADMIN_ID))
    return load_settings()


@pytest.fixture
async def harness(settings: Settings, tmp_path: Path) -> AsyncIterator[tuple[Any, ...]]:
    db = Database(tmp_path / "bot.db")
    await db.connect()
    rows, report = parse_file(FIXTURE)
    await NomenclatureRepository(db).import_version(
        report.source, report.sha256, report.source_date, rows
    )

    session = RecordingSession()
    runtime = build(settings, db)
    await runtime.bot.session.close()
    runtime.bot = Bot(
        token=FAKE_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    yield runtime, session, db
    await db.close()


def message_update(text: str, user_id: int, update_id: int = 1) -> Update:
    return Update(
        update_id=update_id,
        message=TgMessage(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="Тест"),
            text=text,
        ),
    )


async def feed(harness: tuple[Any, ...], update: Update) -> RecordingSession:
    runtime, session, _ = harness
    await runtime.dispatcher.feed_update(runtime.bot, update)
    return session


# ---------------------------------------------------------------- доступ


async def test_stranger_is_refused(harness: tuple[Any, ...]) -> None:
    session = await feed(harness, message_update("кофеварка", GUEST_ID))
    joined = " ".join(session.sent_texts)
    assert "Доступ к боту ограничен" in joined
    assert str(GUEST_ID) in joined, "пользователь должен узнать свой ID"


async def test_stranger_does_not_reach_ai(harness: tuple[Any, ...]) -> None:
    """Посторонний не должен расходовать ни ИИ, ни лимиты."""
    runtime, session, _ = harness
    runtime.dispatcher.workflow_data["service"]._classifier._llm = StubLlm()  # noqa: SLF001
    await feed(harness, message_update("кофеварка", GUEST_ID))
    assert not any("Ищу код" in text for text in session.sent_texts)


async def test_admin_gets_start(harness: tuple[Any, ...]) -> None:
    session = await feed(harness, message_update("/start", ADMIN_ID))
    assert "код ТН ВЭД" in " ".join(session.sent_texts)
    assert "справочный характер" in " ".join(session.sent_texts)


async def test_group_chat_ignored(harness: tuple[Any, ...]) -> None:
    runtime, session, _ = harness
    update = Update(
        update_id=5,
        message=TgMessage(
            message_id=5,
            date=datetime.now(UTC),
            chat=Chat(id=-100, type="supergroup"),
            from_user=User(id=ADMIN_ID, is_bot=False, first_name="Тест"),
            text="кофеварка",
        ),
    )
    await runtime.dispatcher.feed_update(runtime.bot, update)
    assert session.calls == []


# ---------------------------------------------------------------- команды


async def test_code_command(harness: tuple[Any, ...]) -> None:
    session = await feed(harness, message_update("/code 8516 71 000 0", ADMIN_ID))
    assert "8516 71 000 0" in " ".join(session.sent_texts)


async def test_code_command_rejects_bad_format(harness: tuple[Any, ...]) -> None:
    session = await feed(harness, message_update("/code абв", ADMIN_ID))
    assert "10 цифр" in " ".join(session.sent_texts)


async def test_code_command_unknown_code(harness: tuple[Any, ...]) -> None:
    session = await feed(harness, message_update("/code 9999999999", ADMIN_ID))
    assert "нет в загруженном справочнике" in " ".join(session.sent_texts)


async def test_version_shows_nomenclature(harness: tuple[Any, ...]) -> None:
    session = await feed(harness, message_update("/version", ADMIN_ID))
    assert "кодов" in " ".join(session.sent_texts)


async def test_admin_commands_refused_for_regular_user(harness: tuple[Any, ...]) -> None:
    runtime, session, _ = harness
    await runtime.users.add(GUEST_ID, added_by=ADMIN_ID)
    await feed(harness, message_update("/adduser 555", GUEST_ID))
    assert "только администратору" in " ".join(session.sent_texts)


async def test_deluser_refuses_env_admin(harness: tuple[Any, ...]) -> None:
    session = await feed(harness, message_update(f"/deluser {ADMIN_ID}", ADMIN_ID))
    assert "аварийный вход" in " ".join(session.sent_texts)


async def test_invite_flow_end_to_end(harness: tuple[Any, ...]) -> None:
    """Админ выдаёт код, посторонний активирует его и получает доступ."""
    runtime, session, _ = harness
    await feed(harness, message_update("/invite Петров", ADMIN_ID))
    code = next(
        word.strip("<code>/ ")
        for text in session.sent_texts
        for word in text.split()
        if word.strip("<code>/ ").startswith("TNVED-")
    )

    session.calls.clear()
    await feed(harness, message_update(f"/start {code}", GUEST_ID, update_id=2))
    assert "Доступ открыт" in " ".join(session.sent_texts)
    assert await runtime.users.is_allowed(GUEST_ID)


async def test_invite_cannot_be_reused(harness: tuple[Any, ...]) -> None:
    runtime, session, _ = harness
    code = await runtime.users.create_invite(ADMIN_ID, None, ttl_hours=24)
    await feed(harness, message_update(f"/start {code}", GUEST_ID, update_id=2))

    session.calls.clear()
    await feed(harness, message_update(f"/start {code}", 12345, update_id=3))
    assert "уже активирован" in " ".join(session.sent_texts)


# ---------------------------------------------------------------- классификация


async def test_text_classification_answers(harness: tuple[Any, ...]) -> None:
    runtime, session, _ = harness
    runtime.dispatcher.workflow_data["service"]._classifier._llm = StubLlm(  # noqa: SLF001
        {"keywords": "кофе приборы", "chapters": ["85"]},
        {"code": "8516710000", "confidence": 0.91, "reasoning": ["бытовой прибор"]},
    )
    await feed(harness, message_update("кофеварка капельная бытовая", ADMIN_ID))

    joined = " ".join(session.sent_texts)
    assert "8516 71 000 0" in joined
    assert "справочный характер" in joined, "дисклеймер обязателен в каждом ответе"
    assert "Достоверность" in joined


async def test_clarification_shows_buttons(harness: tuple[Any, ...]) -> None:
    runtime, session, _ = harness
    runtime.dispatcher.workflow_data["service"]._classifier._llm = StubLlm(  # noqa: SLF001
        {"keywords": "трубы", "chapters": []},
        {
            "code": None,
            "confidence": 0.2,
            "clarifying_question": "Из какого материала труба?",
            "options": ["Сталь", "Пластик"],
        },
    )
    await feed(harness, message_update("труба", ADMIN_ID))

    assert "Из какого материала" in " ".join(session.sent_texts)
    markup = session.last_markup
    assert markup is not None
    labels = [button["text"] for row in markup["inline_keyboard"] for button in row]
    assert "Сталь" in labels
    assert any("Свой вариант" in label for label in labels)
    assert any("Отмена" in label for label in labels)


async def test_too_short_input_rejected(harness: tuple[Any, ...]) -> None:
    session = await feed(harness, message_update("ab", ADMIN_ID))
    assert "подробнее" in " ".join(session.sent_texts)


async def test_html_from_model_is_escaped(harness: tuple[Any, ...]) -> None:
    """Незакрытый тег из ответа модели уронил бы отправку целиком."""
    runtime, session, _ = harness
    runtime.dispatcher.workflow_data["service"]._classifier._llm = StubLlm(  # noqa: SLF001
        {"keywords": "кофе", "chapters": ["85"]},
        {
            "code": "8516710000",
            "confidence": 0.9,
            "reasoning": ["<b>незакрытый тег и <script>alert(1)</script>"],
        },
    )
    await feed(harness, message_update("кофеварка бытовая", ADMIN_ID))
    joined = " ".join(session.sent_texts)
    # Угловые скобки срезаются ещё схемой ответа, до формирования сообщения: до Telegram
    # не доходит ни исполняемого тега, ни незакрытого — парсер HTML не сломается.
    assert "<script>" not in joined
    assert "<b>незакрытый" not in joined
    assert "приготовления кофе" in joined, "полезная часть ответа должна сохраниться"


# ---------------------------------------------------------------- лимиты


async def test_rate_limit_stops_after_quota(harness: tuple[Any, ...]) -> None:
    runtime, session, _ = harness
    runtime.dispatcher.workflow_data["service"]._classifier._llm = StubLlm()  # noqa: SLF001

    limit = 20
    for i in range(limit + 2):
        await feed(harness, message_update("кофеварка бытовая", ADMIN_ID, update_id=100 + i))

    assert any("Лимит запросов исчерпан" in text for text in session.sent_texts)


# ---------------------------------------------------------------- кнопки


def callback_update(data: str, user_id: int, update_id: int = 50) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=User(id=user_id, is_bot=False, first_name="Тест"),
            chat_instance="ci",
            data=data,
            message=TgMessage(
                message_id=update_id,
                date=datetime.now(UTC),
                chat=Chat(id=user_id, type="private"),
                text="вопрос",
            ),
        ),
    )


async def _ask_clarification(harness: tuple[Any, ...]) -> str:
    runtime, _, _ = harness
    runtime.dispatcher.workflow_data["service"]._classifier._llm = StubLlm(  # noqa: SLF001
        {"keywords": "трубы", "chapters": []},
        {
            "code": None,
            "confidence": 0.2,
            "clarifying_question": "Материал?",
            "options": ["Сталь", "Пластик"],
        },
    )
    await feed(harness, message_update("труба", ADMIN_ID))
    session = await runtime.sessions.find_open(ADMIN_ID)
    assert session is not None
    return session.id


async def test_option_button_continues_dialog(harness: tuple[Any, ...]) -> None:
    runtime, session, _ = harness
    session_id = await _ask_clarification(harness)

    runtime.dispatcher.workflow_data["service"]._classifier._llm = StubLlm(  # noqa: SLF001
        {"keywords": "трубы пластик", "chapters": ["39"]},
        {"code": "3923210000", "confidence": 0.88},
    )
    session.calls.clear()
    await feed(harness, callback_update(f"opt:{session_id}:1", ADMIN_ID))

    joined = " ".join(session.sent_texts)
    assert "Код ТН ВЭД" in joined, "нажатие кнопки должно довести диалог до ответа"
    assert "3923 21 000 0" in joined
    assert any(name == "AnswerCallbackQuery" for name, _ in session.calls), (
        "без answer() у пользователя навсегда останутся «часики» на кнопке"
    )

    # Ответ пользователя попал в запрос: диалог продолжился, а не начался заново.
    closed = await runtime.sessions.get(session_id)
    assert closed is not None
    assert closed.answers == ["Пластик"]


async def test_option_index_out_of_range_is_refused(harness: tuple[Any, ...]) -> None:
    """`callback_data` приходит от клиента — индекс обязан сверяться с сохранёнными вариантами."""
    _, session, _ = harness
    session_id = await _ask_clarification(harness)
    session.calls.clear()

    await feed(harness, callback_update(f"opt:{session_id}:99", ADMIN_ID))
    assert "уже закрыт" in " ".join(session.sent_texts)


async def test_foreign_session_callback_refused(harness: tuple[Any, ...]) -> None:
    runtime, session, _ = harness
    session_id = await _ask_clarification(harness)
    await runtime.users.add(GUEST_ID, added_by=ADMIN_ID)
    session.calls.clear()

    await feed(harness, callback_update(f"opt:{session_id}:0", GUEST_ID))
    assert not any("Ищу код" in text for text in session.sent_texts)
    assert any(name == "AnswerCallbackQuery" for name, _ in session.calls)


async def test_cancel_button_closes_session(harness: tuple[Any, ...]) -> None:
    runtime, session, _ = harness
    session_id = await _ask_clarification(harness)
    session.calls.clear()

    await feed(harness, callback_update(f"cancel:{session_id}:0", ADMIN_ID))
    assert "прерван" in " ".join(session.sent_texts)
    assert await runtime.sessions.find_open(ADMIN_ID) is None


async def test_garbage_callback_data_does_not_crash(harness: tuple[Any, ...]) -> None:
    for payload in ("мусор", "opt:", "opt:abc:xyz", ":::", "opt:x:1:2:3"):
        await feed(harness, callback_update(payload, ADMIN_ID))
    _, session, _ = harness
    assert any(name == "AnswerCallbackQuery" for name, _ in session.calls)


# ---------------------------------------------------------------- ошибки


async def test_handler_failure_is_reported_with_error_id(harness: tuple[Any, ...]) -> None:
    """Обработчик ошибок должен ловить сбои команд, а не только собственного роутера."""
    runtime, session, _ = harness

    async def boom(*args: Any, **kwargs: Any) -> None:
        msg = "внутренняя поломка"
        raise RuntimeError(msg)

    runtime.dispatcher.workflow_data["service"].handle_text = boom
    await feed(harness, message_update("кофеварка бытовая", ADMIN_ID))

    joined = " ".join(session.sent_texts)
    assert "Произошла ошибка" in joined
    assert "внутренняя поломка" not in joined, "внутренние подробности наружу не уходят"
    assert "Traceback" not in joined


async def test_service_commands_are_free(harness: tuple[Any, ...]) -> None:
    """`/help` не обращается к ИИ и не должен расходовать лимит."""
    _, session, db = harness
    for i in range(30):
        await feed(harness, message_update("/help", ADMIN_ID, update_id=200 + i))
    assert not any("Лимит" in text for text in session.sent_texts)

    row = await db.fetch_one("SELECT COUNT(*) AS n FROM usage_counters")
    assert row is not None and row["n"] == 0
