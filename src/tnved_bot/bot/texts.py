"""Все тексты, которые видит пользователь. Одно место — чтобы их можно было вычитать.

Всё, что приходит от модели или от пользователя, экранируется `esc()` перед вставкой:
Telegram работает в режиме HTML, и незакрытый тег из ответа модели уронил бы отправку
целиком.
"""

from __future__ import annotations

from html import escape

from tnved_bot.core.confidence import ConfidenceLevel
from tnved_bot.core.models import Answer, Candidate, CodeSuggestion, Saving
from tnved_bot.customs import marking
from tnved_bot.customs.ifcg import alta_url
from tnved_bot.customs.reference import CodeReference
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


MARKING_HINT = (
    "Перечни маркировки утверждаются постановлениями и меняются несколько раз в год — "
    f'сверьтесь с <a href="{marking.OFFICIAL_URL}">перечнем «Честного знака»</a>.'
)
MARKING_NOT_FOUND = "🏷 Маркировка: в известных перечнях «Честного знака» не нашёл."
DOCS_HINT = "Перечень ориентировочный: он строится по коду, а не по характеристикам товара."
SAVINGS_WARNING = (
    "Это не совет «декларировать подешевле»: код определяется свойствами товара, "
    "а подгонка под ставку — недостоверное декларирование. Смысл блока в другом — "
    "перепроверьте, не описан ли ваш товар точнее соседней позицией."
)


def duty_of(tariff: str | None, reference: CodeReference | None) -> str | None:
    """Ставка пошлины: локальный справочник вперёд внешнего источника.

    Порядок именно такой, и это не мелочь. Сравнение «рядом есть ставка ниже» считается
    по локальному справочнику, и если бы в тексте стояло значение с чужого сайта, разница
    в процентных пунктах не сходилась бы с показанными числами при малейшем расхождении
    источников. Один источник для показа и для арифметики — внешний остаётся запасным.
    """
    return tariff or (reference.duty if reference and reference.duty else None)


def _payments_block(tariff: str | None, reference: CodeReference | None) -> list[str]:
    """Пошлина и НДС. Ставка из локального справочника показывается и без интернета."""
    duty = duty_of(tariff, reference)
    if not duty and not (reference and reference.vat):
        return []

    parts = []
    if duty:
        parts.append(f"пошлина <b>{esc(duty)}</b>")
    if reference and reference.vat:
        parts.append(f"НДС {esc(reference.vat)}")
    return ["", "💰 " + ", ".join(parts)]


def _docs_block(reference: CodeReference | None, *, with_regs: bool = True) -> list[str]:
    if reference is None:
        return ["", "📄 Разрешительные документы: справка недоступна (нет связи с источником)."]
    if not reference.docs:
        return ["", "📄 Разрешительных документов по этому коду в справке не значится."]

    required = reference.required_docs
    header = "📄 <b>Разрешительные документы</b>" if required else "📄 <b>Могут потребоваться</b>"
    lines = ["", header]
    lines += [f"• {esc(group.as_line())}" for group in reference.docs]
    if reference.tech_regs and with_regs:
        lines.append("Техрегламенты: " + esc(", ".join(reference.tech_regs)))
    lines.append(f"<i>{DOCS_HINT}</i>")
    return lines


def _marking_block(answer: Answer) -> list[str]:
    """Ответ про «Честный знак» даётся всегда — в том числе отрицательный.

    Молчание пользователь прочитает как «маркировка не нужна», а это утверждение,
    которого таблица префиксов не позволяет сделать.
    """
    if not answer.marking:
        return ["", MARKING_NOT_FOUND]
    listed = ", ".join(rule.as_line() for rule in answer.marking[:2])
    return ["", f"🏷 <b>Маркировка «Честный знак»</b>: вероятно, {esc(listed)}.", MARKING_HINT]


MAX_SUGGESTION_NAME = 110


def _suggestion_line(item: CodeSuggestion) -> str:
    """Строка альтернативы: код, наименование и то, что важно для решения, — платежи.

    Наименование урезается: в справочнике встречаются позиции длиной в абзац, и такая
    строка в списке альтернатив читается хуже, чем её отсутствие.
    """
    name = (
        item.name
        if len(item.name) <= MAX_SUGGESTION_NAME
        else item.name[:MAX_SUGGESTION_NAME].rstrip() + "…"
    )
    parts = [f"• <code>{format_code(item.code)}</code> {esc(name)}"]
    if item.why:
        parts.append(f" — {esc(item.why)}")

    duty = duty_of(item.tariff, item.reference)
    facts = []
    if duty:
        facts.append(f"пошлина {esc(duty)}")
    if item.reference is not None:
        required = item.reference.required_docs
        facts.append(
            "документы: " + esc(", ".join(group.title for group in required))
            if required
            else "обязательных документов не значится"
        )
    if item.marking:
        facts.append(f"маркировка: {esc(item.marking[0].category)}")
    if facts:
        parts.append("\n    " + "; ".join(facts))
    return "".join(parts)


def _savings_block(savings: list[Saving]) -> list[str]:
    if not savings:
        return []
    lines = ["", "💡 <b>Рядом есть ставка ниже</b>"]
    for saving in savings:
        lines.append(_suggestion_line(saving.suggestion))
        lines.append(f"    ниже на {saving.gap:.1f} п.п.")
    lines.append(f"<i>{SAVINGS_WARNING}</i>")
    return lines


MESSAGE_LIMIT = 4096
# Что выбрасывать, если ответ не влезает в сообщение Telegram. Порядок — от наименее
# ценного к наиболее: код, платежи и дисклеймер не выбрасываются никогда.
_DROP_ORDER = ("savings", "regs", "reasoning", "alternatives")


def answer_message(answer: Answer, accept: float, clarify: float) -> str:
    """Собирает ответ, отбрасывая необязательные блоки, если он не влезает в сообщение.

    Обрезать готовый текст по длине нельзя: обрыв придётся на середину HTML-тега, и Telegram
    откажется отправить сообщение целиком — вместо длинного ответа пользователь получит
    ошибку.
    """
    dropped: set[str] = set()
    for extra in ("", *_DROP_ORDER):
        if extra:
            dropped.add(extra)
        text = _render_answer(answer, accept, clarify, dropped)
        if len(text) <= MESSAGE_LIMIT:
            return text
    # Ничего из необязательного не помогло — источник выдал неправдоподобно длинную
    # справку. Отдаём то, ради чего пользователь пришёл: код, ставку и дисклеймер.
    return _render_minimal(answer)


def _render_minimal(answer: Answer) -> str:
    lines = ["📋 <b>Код ТН ВЭД ЕАЭС</b>", "", f"<code>{format_code(answer.code)}</code>"]
    lines.append(esc(answer.name)[:500])
    lines += _payments_block(answer.tariff, answer.reference)
    lines += ["", "Справка по коду не поместилась в сообщение — смотрите её по ссылке."]
    lines += ["", f'🔎 <a href="{alta_url(answer.code)}">Проверить код на alta.ru</a>']
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def _render_answer(answer: Answer, accept: float, clarify: float, dropped: set[str]) -> str:
    from tnved_bot.core.confidence import level

    lines = ["📋 <b>Код ТН ВЭД ЕАЭС</b>", "", f"<code>{format_code(answer.code)}</code>"]
    lines.append(esc(answer.name))

    if answer.degraded:
        lines += ["", f"⚠️ {esc(answer.degraded)}."]
    else:
        label = LEVEL_LABEL[level(answer.confidence, accept, clarify)]
        lines += ["", f"Достоверность: {label} ({answer.confidence:.2f})"]

    lines += _payments_block(answer.tariff, answer.reference)
    lines += _docs_block(answer.reference, with_regs="regs" not in dropped)
    lines += _marking_block(answer)

    if answer.reasoning and "reasoning" not in dropped:
        lines += ["", "<b>Почему этот код</b>"]
        lines += [f"• {esc(item)}" for item in answer.reasoning]

    if answer.alternatives and "alternatives" not in dropped:
        lines += ["", "<b>Альтернативы</b>"]
        lines += [_suggestion_line(alt) for alt in answer.alternatives]

    if "savings" not in dropped:
        lines += _savings_block(answer.savings)

    lines += ["", f'🔎 <a href="{alta_url(answer.code)}">Проверить код на alta.ru</a>']
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


def code_info(candidate: Candidate, reference: CodeReference | None = None) -> str:
    lines = [f"<code>{format_code(candidate.code)}</code>", "", esc(candidate.name_full)]
    lines += _payments_block(candidate.tariff, reference)
    lines += _docs_block(reference)
    rules = marking.rules_for(candidate.code)
    lines += (
        [
            "",
            f"🏷 <b>Маркировка «Честный знак»</b>: вероятно, {esc(rules[0].as_line())}.",
            MARKING_HINT,
        ]
        if rules
        else ["", MARKING_NOT_FOUND]
    )
    lines += ["", f'🔎 <a href="{alta_url(candidate.code)}">Проверить код на alta.ru</a>']
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


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


def invite_link(code: str, bot_username: str | None) -> str | None:
    """Ссылка вида `https://t.me/бот?start=КОД`.

    Telegram кладёт параметр `start` в аргумент команды, то есть код вводится за человека:
    он нажимает ссылку, потом «Запустить» — и доступ открыт. Алфавит кода (латиница, цифры
    и дефис) укладывается в то, что Telegram разрешает в этом параметре.
    """
    return f"https://t.me/{bot_username}?start={code}" if bot_username else None


def invite_created(code: str, ttl_hours: int, bot_username: str | None = None) -> str:
    link = invite_link(code, bot_username)
    lines = [f"🎟 <b>Код приглашения</b> (действует {ttl_hours} ч)", "", f"<code>{code}</code>", ""]
    if link:
        lines.append(f'Ссылка: <a href="{link}">{esc(link)}</a>')
        lines.append("Следующим сообщением — готовое приглашение, его можно просто переслать.")
    else:
        lines.append("Имя бота получить не удалось, поэтому ссылки нет — передайте код.")
    lines.append("")
    lines.append("Код одноразовый. После активации человек появится в списке пользователей.")
    return "\n".join(lines)


def invite_share(code: str, bot_username: str | None) -> str:
    """Самостоятельное сообщение для пересылки — его читает приглашаемый, не админ."""
    link = invite_link(code, bot_username)
    if link:
        return (
            "Приглашение в бота подбора кодов ТН ВЭД ЕАЭС.\n\n"
            f'👉 <a href="{link}">Открыть бота и войти</a>\n\n'
            "Нажмите ссылку, затем кнопку «Запустить» — код подставится сам. "
            "Приглашение одноразовое и действует ограниченное время."
        )
    return (
        "Приглашение в бота подбора кодов ТН ВЭД ЕАЭС.\n\n"
        f"Откройте бота и отправьте ему:\n<code>/start {code}</code>\n\n"
        "Приглашение одноразовое и действует ограниченное время."
    )


# ---------------------------------------------------------------- исправления

CORRECTION_ASK = (
    "Спасибо. Если знаете верный код — пришлите его сообщением "
    "(<code>8516 71 000 0</code> или <code>8516710000</code>).\n\n"
    "Я запомню пару «запрос → код» и учту её, когда встречу похожий товар. "
    "Не знаете — просто напишите новый запрос, ничего делать не нужно."
)

CORRECTION_BAD_FORMAT = (
    "Это не похоже на код ТН ВЭД. Нужны десять цифр — или напишите новый запрос, "
    "чтобы выйти из режима исправления."
)


def correction_unknown_code(code: str) -> str:
    return (
        f"Кода <code>{esc(code)}</code> нет в активном справочнике — запомнить его не могу.\n"
        "Проверьте цифры: исправление с несуществующим кодом хуже, чем его отсутствие."
    )


def correction_saved(code: str) -> str:
    return (
        f"Запомнил: <code>{format_code(code)}</code>.\n"
        "При похожем запросе этот код попадёт в рассмотрение."
    )


def corrections_list(rows: list[tuple[str, str, str, str]]) -> str:
    """`rows` — (дата, запрос, неверный код, верный код)."""
    if not rows:
        return (
            "Исправлений пока нет. Они появляются, когда пользователь нажимает 👎 и присылает код."
        )
    lines = [f"📝 <b>Исправления</b> ({len(rows)})", ""]
    for created, query, wrong, correct in rows:
        was = f"было {format_code(wrong)} → " if wrong else ""
        lines.append(
            f"<code>{esc(created[:10])}</code> {esc(query[:80])}\n"
            f"    {was}<b>{format_code(correct)}</b>"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- рассылка

BROADCAST_PROMPT = (
    "Пришлите текст рассылки одним сообщением. Разметка не поддерживается — "
    "текст уйдёт как есть.\n\nОтмена — /cancel."
)


def broadcast_preview(text: str, recipients: int) -> str:
    return f"📣 <b>Предпросмотр</b> — получателей: {recipients}\n\n{esc(text)}\n\nОтправить всем?"


def broadcast_report(sent: int, failed: int) -> str:
    lines = [f"📣 Рассылка завершена.\n\nДоставлено: <b>{sent}</b>"]
    if failed:
        lines.append(
            f"Не доставлено: <b>{failed}</b> — обычно это те, кто заблокировал бота "
            "или ни разу его не открывал."
        )
    return "\n".join(lines)


RELEASE_NOTE = """🆕 Бот обновился.

Что нового:

• Вместе с кодом бот теперь показывает ставку пошлины и НДС.
• Появился список разрешительных документов и техрегламентов по коду.
• Бот подсказывает, подпадает ли товар под маркировку «Честный знак».
• Если у соседней позиции ставка ниже — бот покажет её, чтобы вы перепроверили классификацию.
• Пошлина и документы теперь видны и у альтернативных кодов, а не только у основного.
• Кнопка 👎 стала полезной: пришлите верный код, и бот учтёт его при похожих запросах.

Ответ по-прежнему носит справочный характер — решение принимает декларант."""


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
