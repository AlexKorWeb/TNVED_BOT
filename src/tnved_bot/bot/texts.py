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
from tnved_bot.db.users import AllowedUser, Invite

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

# ---------------------------------------------------------------- админ-панель

ADMIN_ONLY = "Панель доступна только администратору."

ADMIN_HINT = (
    "Вы администратор. Управление доступом — кнопкой ниже или командой /admin.\n\n"
    "Там же выдача приглашений: код можно создать в один клик, не спрашивая у человека "
    "его Telegram ID."
)

ADMIN_FROM_ENV = (
    "Это администратор из <code>.env</code>.\n\n"
    "Его доступ отзывается только правкой файла и перезапуском — так задумано: это "
    "аварийный вход на случай, если список в базе окажется испорчен."
)

MIN_FREE_MB = 500


def admin_menu_text(users: int, invites: int, requests: int) -> str:
    return (
        "🛠 <b>Панель администратора</b>\n\n"
        f"Пользователей с доступом: <b>{users}</b>\n"
        f"Невостребованных приглашений: <b>{invites}</b>\n"
        f"Запросов за сутки: <b>{requests}</b>"
    )


def admin_users_text(people: list[AllowedUser], total: int, admins: frozenset[int]) -> str:
    lines = [f"👥 <b>Пользователи</b> ({total})", ""]
    if not people:
        lines.append("Список пуст. Создайте приглашение кнопкой ниже.")
    for person in people:
        note = f" — {esc(person.note)}" if person.note else ""
        seen = person.last_seen_at[:10] if person.last_seen_at else "не заходил"
        source = "по коду" if person.added_by == 0 else f"выдал {person.added_by}"
        lines.append(
            f"<code>{person.user_id}</code>{note}\n"
            f"    с {person.added_at[:10]}, был {seen}, {source}"
        )
    if total > len(people):
        lines.append(f"\n…и ещё {total - len(people)}. Полный список: /users")

    lines += [
        "",
        f"Администраторы из <code>.env</code>: {', '.join(str(a) for a in sorted(admins))}",
        "Их через панель отозвать нельзя.",
        "",
        "Кнопка ниже отзывает доступ.",
    ]
    return "\n".join(lines)


def admin_confirm_user_text(user_id: int, label: str) -> str:
    return (
        "Отозвать доступ?\n\n"
        f"<code>{user_id}</code> — {esc(label)}\n\n"
        "Активные диалоги этого пользователя будут закрыты. "
        "История о том, что доступ был, сохранится."
    )


def admin_user_removed(user_id: int, closed: int, removed: bool) -> str:
    if not removed:
        return f"Пользователя <code>{user_id}</code> в списке не было."
    return f"Доступ отозван: <code>{user_id}</code>. Закрыто диалогов: {closed}."


def admin_invites_text(invites: list[Invite], total: int, note: str | None = None) -> str:
    lines = []
    if note:
        lines += [note, ""]
    lines += [f"🎟 <b>Невостребованные приглашения</b> ({total})", ""]

    if not invites:
        lines.append("Активных кодов нет.")
    for invite in invites:
        label = f" — {esc(invite.note)}" if invite.note else ""
        lines.append(
            f"<code>{invite.code}</code>{label}\n    действует до {invite.expires_at[:16]}"
        )

    if total > len(invites):
        lines.append(f"\n…и ещё {total - len(invites)}.")

    lines += [
        "",
        "Невостребованный код — открытая дверь до истечения срока. "
        "Лишние лучше отозвать кнопкой ниже.",
    ]
    return "\n".join(lines)


def invite_created(code: str, ttl_hours: int, bot_username: str | None = None) -> str:
    handle = f"@{bot_username} " if bot_username else ""
    return (
        f"🎟 <b>Код приглашения</b> (действует {ttl_hours} ч)\n\n"
        f"<code>{code}</code>\n\n"
        "Перешлите человеку сообщение ниже — нажатие на него копирует текст целиком:\n\n"
        f"<pre>Открой бота {handle}и отправь ему:\n/start {code}</pre>\n\n"
        "Код одноразовый. После активации человек появится в списке пользователей."
    )


def admin_stats_text(values: list[tuple[str, int]]) -> str:
    lines = ["📊 <b>За последние сутки</b>", ""]
    lines += [f"{name}: <b>{value}</b>" for name, value in values]
    return "\n".join(lines)


async def health_text(nomenclature: object, llm: object, db_path_parent: str) -> str:
    """Состояние системы. Собирается здесь, чтобы одинаково выглядеть в /health и в панели."""
    import shutil

    version = await nomenclature.active_version()  # type: ignore[attr-defined]
    binary = llm.check_binary()  # type: ignore[attr-defined]
    breaker_open = llm.breaker_open  # type: ignore[attr-defined]

    # Проверка состояния не имеет права падать сама: недоступный каталог — это как раз то,
    # о чём она должна сообщить, а не то, из-за чего она молчит.
    try:
        free_mb = shutil.disk_usage(db_path_parent).free // (1024 * 1024)
        disk = f"{'🟢' if free_mb >= MIN_FREE_MB else '🔴'} Диск: {free_mb} МБ свободно"
    except OSError as exc:
        disk = f"🔴 Диск: путь недоступен ({exc.strerror or exc})"

    return "\n".join(
        [
            "🩺 <b>Состояние</b>",
            "",
            f"{'🟢' if version else '🔴'} Справочник: "
            + (f"{version.rows} кодов, {version.source_date or '—'}" if version else "не загружен"),
            f"{'🟢' if binary else '🔴'} claude: " + (binary or "не найден в PATH"),
            f"{'🔴' if breaker_open else '🟢'} ИИ: "
            + (
                f"недоступен ещё {llm.retry_after_seconds} с"  # type: ignore[attr-defined]
                if breaker_open
                else "в норме"
            ),
            disk,
            f"⏳ В очереди к ИИ: {llm.queue_depth}",  # type: ignore[attr-defined]
        ]
    )
