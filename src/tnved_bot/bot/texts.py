"""Все тексты, которые видит пользователь. Одно место — чтобы их можно было вычитать.

Всё, что приходит от модели или от пользователя, экранируется `esc()` перед вставкой:
Telegram работает в режиме HTML, и незакрытый тег из ответа модели уронил бы отправку
целиком.
"""

from __future__ import annotations

from html import escape

from tnved_bot.core.confidence import ConfidenceLevel
from tnved_bot.core.models import Answer, Candidate
from tnved_bot.db.nomenclature import format_code

DISCLAIMER = (
    "⚠️ Ответ носит справочный характер. Окончательное решение принимает декларант; "
    "для официальной позиции — предварительное решение ФТС."
)

START = (
    "👋 Помогу подобрать код ТН ВЭД ЕАЭС.\n\n"
    "Опишите товар словами или пришлите фотографию. Чем точнее описание — "
    "материал, назначение, конструкция, — тем точнее код.\n\n"
    "Пример: <i>кофеварка капельная бытовая, 900 Вт, пластиковый корпус</i>\n\n"
    f"{DISCLAIMER}"
)

HELP = (
    "<b>Как получить точный код</b>\n\n"
    "Укажите в описании:\n"
    "• что это за товар (родовое название, не бренд);\n"
    "• из чего сделан — основной материал;\n"
    "• для чего используется;\n"
    "• конструкцию: механизм, питание, разборность;\n"
    "• состояние: готовое изделие, части, сырьё.\n\n"
    "На эти признаки опирается номенклатура — бренд и модель в ней не встречаются.\n\n"
    "<b>Команды</b>\n"
    "/code &lt;10 цифр&gt; — наименование кода из справочника\n"
    "/cancel — прервать уточнения\n"
    "/forget — удалить мои фото и сессии\n"
    "/version — версия бота и справочника\n\n"
    f"{DISCLAIMER}"
)

SEARCHING = "🔎 Ищу код…"
THINKING = "🤖 Анализирую описание…"
LOOKING_AT_PHOTO = "🖼 Смотрю фотографию…"

QUEUED = "⏳ Занят другими запросами, вы в очереди. Отвечу, как только освобожусь."
CANCELLED = "Диалог прерван. Отправьте новое описание, когда будете готовы."
NOTHING_TO_CANCEL = "Сейчас нечего прерывать — активного диалога нет."
SESSION_CLOSED = "Этот диалог уже закрыт. Отправьте новый запрос."
CUSTOM_ANSWER_PROMPT = "Напишите свой вариант ответа сообщением."

ERROR_GENERIC = "Произошла ошибка. ID: <code>{error_id}</code>\nПопробуйте ещё раз через минуту."

LEVEL_LABEL = {
    ConfidenceLevel.HIGH: "высокая",
    ConfidenceLevel.MEDIUM: "средняя",
    ConfidenceLevel.LOW: "низкая",
}


def esc(text: str) -> str:
    return escape(str(text), quote=False)


def access_denied(user_id: int) -> str:
    return (
        "🔒 Доступ к боту ограничен.\n\n"
        f"Ваш ID: <code>{user_id}</code> — передайте его владельцу бота.\n"
        "Если у вас есть код приглашения, отправьте: <code>/start ВАШ-КОД</code>"
    )


def rate_limited(minutes: int) -> str:
    return (
        f"Лимит запросов исчерпан. Следующий запрос можно отправить через {minutes} мин.\n"
        "Лимит нужен, чтобы бот не перегружал компьютер, на котором работает."
    )


def answer_message(answer: Answer, accept: float, clarify: float) -> str:
    from tnved_bot.core.confidence import level

    lines = ["📋 <b>Код ТН ВЭД ЕАЭС</b>", "", f"<code>{format_code(answer.code)}</code>"]
    lines.append(esc(answer.name))

    if answer.degraded:
        lines += ["", f"⚠️ {esc(answer.degraded)}."]
    else:
        label = LEVEL_LABEL[level(answer.confidence, accept, clarify)]
        lines += ["", f"Достоверность: {label} ({answer.confidence:.2f})"]

    if answer.tariff:
        lines.append(f"Ставка пошлины: {esc(answer.tariff)}")

    if answer.reasoning:
        lines += ["", "<b>Почему этот код</b>"]
        lines += [f"• {esc(item)}" for item in answer.reasoning]

    if answer.alternatives:
        lines += ["", "<b>Альтернативы</b>"]
        for alt in answer.alternatives:
            why = f" — {esc(alt.why)}" if alt.why else ""
            lines.append(f"• <code>{format_code(alt.code)}</code> {esc(alt.name)}{why}")

    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def clarification_message(question: str) -> str:
    return f"❓ {esc(question)}"


def timeout_answer(answer: Answer) -> str:
    return (
        "⏳ Не дождался уточнения, отвечаю по тому, что есть.\n\n"
        f"<code>{format_code(answer.code)}</code>\n{esc(answer.name)}\n\n"
        "⚠️ Ответ дан <b>без уточнений</b> — достоверность снижена.\n\n"
        f"{DISCLAIMER}"
    )


def code_info(candidate: Candidate) -> str:
    return (
        f"<code>{format_code(candidate.code)}</code>\n\n{esc(candidate.name_full)}\n\n{DISCLAIMER}"
    )


def code_not_found(code: str) -> str:
    return (
        f"Кода <code>{esc(code)}</code> нет в загруженном справочнике.\n"
        "Проверьте, что цифр ровно десять."
    )


CODE_FORMAT_HINT = (
    "Укажите код из 10 цифр: <code>/code 8516710000</code> или <code>/code 8516 71 000 0</code>"
)
