"""Справка по коду ТН ВЭД со страницы ifcg.ru.

Почему именно этот источник. Проверены оба сайта, которые просил посмотреть пользователь:
alta.ru отдаёт в HTML только пошлину и НДС, а разрешительные документы подгружает скриптами —
из статической страницы их не достать. ifcg.ru отдаёт всё сразу и разметкой, к которой можно
привязаться: `table#duties`, `h2#docs`, `div.tnv-document`, `div.tnv-samples`.

Разбор намеренно привязан к этим меткам и **никогда не бросает исключение**: если вёрстка
изменится, справка станет частичной или пустой, но классификация не сломается — она и так
работает без интернета.

Безопасность. Ответ чужого сайта — недоверенные данные наравне с текстом пользователя:

* код в URL проверяется на «ровно десять цифр» — иначе значение, пришедшее от модели,
  могло бы увести запрос на произвольный путь или хост;
* хост зафиксирован, схема только https, конечный адрес после редиректов сверяется;
* размер ответа ограничен, время ожидания ограничено;
* всё, что извлечено, обрезается по длине (`CodeReference.build`) и экранируется при выводе.
"""

from __future__ import annotations

import asyncio
import re
from html import unescape
from types import TracebackType
from urllib.parse import urlsplit

import aiohttp

from tnved_bot.customs.reference import CodeReference, DocGroup
from tnved_bot.logging_setup import get_logger

log = get_logger(__name__)

HOST = "www.ifcg.ru"
BASE_URL = f"https://{HOST}/kb/tnved"
# Ссылка «подробнее» для пользователя: альтернативный взгляд на тот же код.
ALTA_URL = "https://www.alta.ru/tnved/code"

MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 12
MAX_CONCURRENCY = 3
USER_AGENT = "TNVED_BOT/1.0 (личный справочный бот; +https://github.com/)"

_CODE_RE = re.compile(r"^\d{10}$")
_DUTIES = re.compile(r'<table[^>]*id="duties"[^>]*>(.*?)</table>', re.S)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
# Заголовок раздела документов бывает с id и без него: у «необходимых» это `<h2 id="docs">`,
# у «могут потребоваться» — просто `<h2>`. Ищем по смыслу текста, а не по атрибуту.
_H2 = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
_DOC_BLOCK = re.compile(
    r'<div class="tnv-document[^"]*">(.*?)(?=<div class="tnv-document|\Z)', re.S
)
_DOC_TITLE = re.compile(r'<div class="h3">(.*?)</div>', re.S)
_PARAGRAPH = re.compile(r"<p>(.*?)</p>", re.S)
_LINK = re.compile(r"<a[^>]*>(.*?)</a>", re.S)
_REG_ROW = re.compile(r"<tr>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>\s*</tr>", re.S)
_SAMPLE = re.compile(r'<div class="row row-in tnv-samples">(.*?)</div>\s*</div>', re.S)
# Закрывающий `</div>` последней ячейки съедает якорь блока — поэтому его тут не требуем.
_LAST_CELL = re.compile(r'<div class="col-xs-12 col-md-8[^"]*">(.*?)(?:</div>|\Z)', re.S)
_TAGS = re.compile(r"<[^>]+>")

# Заголовок блока документов различает обязательные и условные: «необходимые» против
# «могут потребоваться … требуются редко или только при определённых условиях».
_REQUIRED_MARKER = "необходим"
_KINDS_MARKER = "Виды документов"


def _text(chunk: str) -> str:
    return " ".join(unescape(_TAGS.sub(" ", chunk)).split())


def page_url(code: str) -> str:
    return f"{BASE_URL}/{code}/"


def alta_url(code: str) -> str:
    return f"{ALTA_URL}/{code}/"


def parse(code: str, html_text: str) -> CodeReference:
    """HTML страницы → справка. Любой сбой разбора даёт неполную справку, а не исключение."""
    duty, vat = _parse_duties(html_text)
    header = _docs_header(html_text)

    return CodeReference.build(
        code=code,
        duty=duty,
        vat=vat,
        docs=_parse_docs(html_text, required=_REQUIRED_MARKER in header) if header else [],
        tech_regs=_parse_regs(html_text),
        samples=_parse_samples(html_text),
        source_url=page_url(code),
    )


def _docs_header(html_text: str) -> str:
    """Заголовок раздела документов в нижнем регистре или пустая строка, если раздела нет.

    У кодов без ограничений раздела нет вовсе (проверено на говядине 0201 10 000 8) —
    тогда и групп документов не будет.
    """
    for heading in _H2.findall(html_text):
        text = _text(heading).lower()
        if "документ" in text and "ввоз" in text:
            return text
    return ""


def _parse_duties(html_text: str) -> tuple[str | None, str | None]:
    table = _DUTIES.search(html_text)
    if table is None:
        return None, None

    duty = vat = None
    for row in _ROW.findall(table.group(1)):
        cells = [_text(cell) for cell in _CELL.findall(row)]
        if len(cells) < 2 or not cells[1]:
            continue
        label = cells[0].lower()
        # Третья ячейка — оговорка вида «Но не менее 1 Евро/кг». Без неё ставка неверна.
        value = " ".join(part for part in (cells[1], *cells[2:]) if part)
        if "пошлин" in label:
            duty = value
        elif "ндс" in label:
            vat = value
    return duty, vat


def _parse_docs(html_text: str, *, required: bool) -> list[DocGroup]:
    groups: list[DocGroup] = []
    for block in _DOC_BLOCK.findall(html_text):
        title_match = _DOC_TITLE.search(block)
        if title_match is None:
            continue
        title = _text(title_match.group(1))
        if not title:
            continue
        groups.append(DocGroup(title=title, kinds=_parse_kinds(block), required=required))
    return groups


def _parse_kinds(block: str) -> list[str]:
    """Виды документов берутся только из абзаца со словами «Виды документов».

    Собирать все ссылки блока нельзя: там же лежат ссылки на нормативные акты и на услуги
    сайта, и в перечень документов попало бы «раздел 2.12» и «Свяжитесь с нами».
    """
    for paragraph in _PARAGRAPH.findall(block):
        if _KINDS_MARKER not in paragraph:
            continue
        kinds = [_text(link) for link in _LINK.findall(paragraph)]
        return [kind for kind in kinds if kind]
    return []


def _parse_regs(html_text: str) -> list[str]:
    regs: list[str] = []
    for number, name in _REG_ROW.findall(html_text):
        label, title = _text(number), _text(name)
        if "регламент" not in label.lower():
            continue
        regs.append(f"{label} — {title}" if title else label)
    return regs


def _parse_samples(html_text: str) -> list[str]:
    samples: list[str] = []
    for block in _SAMPLE.findall(html_text):
        cell = _LAST_CELL.search(block)
        if cell is None:
            continue
        value = _text(cell.group(1))
        if value:
            samples.append(value)
    return samples


class IfcgClient:
    """Загрузчик справок. Одна сессия на процесс, ограничение одновременных запросов."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT, concurrency: int = MAX_CONCURRENCY) -> None:
        self._timeout = timeout
        self._gate = asyncio.Semaphore(concurrency)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> IfcgClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def fetch(self, code: str) -> CodeReference | None:
        """Справка по коду или `None`, если получить её не удалось.

        `None` — рабочий, а не исключительный исход: справка дополняет ответ, а не образует
        его. Интернета может не быть вовсе.
        """
        if not _CODE_RE.match(code):
            # Код приходит из ответа модели. В URL он попадает только после этой проверки.
            log.warning("reference_bad_code", code=code[:20])
            return None

        url = page_url(code)
        try:
            async with self._gate:
                html_text = await self._get(url)
        except TimeoutError:
            log.warning("reference_timeout", code=code)
            return None
        except Exception as exc:  # noqa: BLE001 — сеть недоступна, это не повод падать
            log.warning("reference_failed", code=code, error=str(exc)[:200])
            return None

        if html_text is None:
            return None
        return parse(code, html_text)

    async def _get(self, url: str) -> str | None:
        session = await self._ensure_session()
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with session.get(url, timeout=timeout) as response:
            if urlsplit(str(response.url)).hostname != HOST:
                # Редирект увёл на чужой хост — дальше не читаем.
                log.warning("reference_offsite_redirect", url=str(response.url)[:120])
                return None
            if response.status != 200:  # noqa: PLR2004 — иных успешных статусов тут не бывает
                log.info("reference_not_found", url=url, status=response.status)
                return None
            body = await response.content.read(MAX_BYTES)
            return body.decode("utf-8", "replace")

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
        return self._session
