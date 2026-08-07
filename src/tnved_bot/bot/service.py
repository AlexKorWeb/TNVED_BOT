"""Связка бота с ядром: ведёт диалог классификации от сообщения до ответа.

Хендлеры остаются тонкими, а логика диалога лежит здесь — иначе она разъехалась бы между
обработчиком текста, обработчиком кнопок и обработчиком фотографий, где её пришлось бы
поддерживать в трёх местах.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from aiogram.types import Message

from tnved_bot.bot import keyboards, texts
from tnved_bot.core.classifier import Classifier
from tnved_bot.core.models import Answer, Clarification, NoResult, Outcome
from tnved_bot.core.sanitize import Rejected, Sanitized, clean_user_text
from tnved_bot.db.audit import AuditLog, text_digest
from tnved_bot.db.corrections import CorrectionRepository
from tnved_bot.db.nomenclature import NomenclatureRepository
from tnved_bot.db.sessions import Session, SessionRepository
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

CODE_LENGTH = 10


@dataclass(frozen=True, slots=True)
class DialogSettings:
    timeout_minutes: int = 30
    max_rounds: int = 3
    accept: float = 0.80
    clarify: float = 0.45


class DialogService:
    def __init__(
        self,
        classifier: Classifier,
        sessions: SessionRepository,
        audit: AuditLog,
        settings: DialogSettings | None = None,
        corrections: CorrectionRepository | None = None,
        nomenclature: NomenclatureRepository | None = None,
    ) -> None:
        self._classifier = classifier
        self._sessions = sessions
        self._audit = audit
        self._settings = settings or DialogSettings()
        self._corrections = corrections
        self._nomenclature = nomenclature

    # ------------------------------------------------------------------ вход

    async def handle_text(self, message: Message, user_id: int) -> None:
        """Текстовое сообщение: либо ответ на заданный вопрос, либо новый запрос."""
        cleaned = clean_user_text(message.text)
        if isinstance(cleaned, Rejected):
            if cleaned.reason == "garbage":
                await self._audit.record("injection_suspected", user_id=user_id)
            await message.answer(cleaned.user_message)
            return

        open_session = await self._sessions.find_open(user_id)
        if open_session is not None and open_session.awaiting_correction:
            if await self._apply_correction(message, open_session, cleaned.text, user_id):
                return
            # Не код — значит человек передумал исправлять и пишет новый запрос.
            open_session.context["awaiting_correction"] = False
            await self._sessions.save(open_session, self._settings.timeout_minutes)

        if open_session is not None and open_session.awaiting_custom:
            await self._continue_with_answer(message, open_session, cleaned.text)
            return

        # Новый запрос закрывает предыдущий незавершённый диалог: держать два
        # параллельных было бы неотличимо для пользователя.
        if open_session is not None:
            await self._sessions.close(open_session.id, "expired")

        await self._start_session(message, user_id, cleaned)

    async def _start_session(self, message: Message, user_id: int, cleaned: Sanitized) -> None:
        session = await self._sessions.create(
            session_id=uuid.uuid4().hex[:16],
            user_id=user_id,
            chat_id=message.chat.id,
            timeout_minutes=self._settings.timeout_minutes,
            description=cleaned.text,
        )
        await self._audit.record(
            "classify_started",
            user_id=user_id,
            payload={"digest": text_digest(cleaned.text), "suspicious": cleaned.suspicious},
        )
        await self._run(message, session)

    async def _continue_with_answer(self, message: Message, session: Session, answer: str) -> None:
        session.answers.append(answer)
        session.context["awaiting_custom"] = False
        session.context["options"] = []
        # Дополнение описания с фотографии — не раунд уточнения: пользователь добавил
        # данные по своей инициативе, а не в ответ на вопрос бота.
        if session.context.get("question"):
            session.round += 1
        await self._sessions.save(session, self._settings.timeout_minutes)
        await self._run(message, session)

    async def continue_with_option(self, message: Message, session: Session, index: int) -> bool:
        """Ответ кнопкой. Индекс сверяется с сохранёнными вариантами.

        Возвращает `False`, если индекс не соответствует ничему из предложенного:
        `callback_data` приходит от клиента и является недоверенными данными.
        """
        options = session.pending_options
        if not 0 <= index < len(options):
            log.warning("callback_option_out_of_range", session=session.id, index=index)
            return False

        session.answers.append(options[index])
        session.context["options"] = []
        session.round += 1
        await self._sessions.save(session, self._settings.timeout_minutes)
        await self._run(message, session)
        return True

    async def await_custom_answer(self, session: Session) -> None:
        session.context["awaiting_custom"] = True
        await self._sessions.save(session, self._settings.timeout_minutes)

    # ------------------------------------------------------------------ исправления

    async def await_correction(self, session: Session) -> None:
        """Пользователь нажал 👎 — ждём от него верный код."""
        session.context["awaiting_correction"] = True
        session.context["awaiting_custom"] = False
        session.state = "clarifying"
        await self._sessions.save(session, self._settings.timeout_minutes)

    async def _apply_correction(
        self, message: Message, session: Session, text: str, user_id: int
    ) -> bool:
        """Записывает исправление. `False` — текст не похож на код, это обычный запрос.

        Код проверяется по активной версии справочника **до** записи: исправление с
        несуществующим кодом было бы хуже отсутствия исправления — оно бы подсказывало
        модели заведомо неверный ответ.
        """
        if self._corrections is None or self._nomenclature is None:
            return False

        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits or len(digits) != CODE_LENGTH or len(digits) != len(text.replace(" ", "")):
            # В сообщении есть что-то кроме цифр и пробелов — это не код, а новый запрос.
            return False

        row = await self._nomenclature.verify_code(digits)
        if row is None:
            await message.answer(texts.correction_unknown_code(digits))
            return True

        await self._corrections.add(
            user_id=user_id,
            query=session.description or "",
            correct_code=row.code,
            wrong_code=session.answer_code,
        )
        await self._audit.record(
            "correction_saved", user_id=user_id, payload={"chapter": row.code[:2]}
        )
        session.context["awaiting_correction"] = False
        await self._sessions.save(session, self._settings.timeout_minutes)
        await self._sessions.close(session.id, "done")
        await message.answer(texts.correction_saved(row.code))
        return True

    # ------------------------------------------------------------------ выполнение

    async def run(self, message: Message, session: Session) -> None:
        """Запуск классификации по готовой сессии (подтверждение описания с фотографии)."""
        await self._run(message, session)

    async def _run(self, message: Message, session: Session) -> None:
        placeholder = await message.answer(texts.SEARCHING)
        try:
            outcome = await self._classifier.classify(
                session.description or "", answers=session.answers, rounds_used=session.round
            )
        except Exception:
            log.exception("classify_failed", session=session.id)
            await self._sessions.close(session.id, "expired")
            raise
        await self._render(placeholder, session, outcome)

    async def _render(self, placeholder: Message, session: Session, outcome: Outcome) -> None:
        if isinstance(outcome, Clarification):
            session.state = "clarifying"
            session.context["options"] = outcome.options
            session.context["question"] = outcome.question
            await self._sessions.save(session, self._settings.timeout_minutes)
            await placeholder.edit_text(
                texts.clarification_message(outcome.question),
                reply_markup=keyboards.clarification(session.id, outcome.options),
            )
            await self._audit.record("clarification_asked", user_id=session.user_id)
            return

        if isinstance(outcome, NoResult):
            await self._sessions.close(session.id, "done")
            await placeholder.edit_text(outcome.user_message)
            await self._audit.record(
                "no_result", user_id=session.user_id, ok=False, payload={"reason": outcome.reason}
            )
            return

        await self._finish_with_answer(placeholder, session, outcome)

    async def _finish_with_answer(
        self, placeholder: Message, session: Session, answer: Answer
    ) -> None:
        session.state = "done"
        session.context["answer_code"] = answer.code
        await self._sessions.save(session, self._settings.timeout_minutes)
        await placeholder.edit_text(
            texts.answer_message(answer, self._settings.accept, self._settings.clarify),
            reply_markup=keyboards.feedback(session.id),
        )
        await self._audit.record(
            "classified",
            user_id=session.user_id,
            payload={
                "chapter": answer.code[:2],
                "confidence": answer.confidence,
                "degraded": answer.degraded is not None,
                "rounds": session.round,
            },
        )
