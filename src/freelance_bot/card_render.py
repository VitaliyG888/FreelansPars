"""Рендер карточки заказа в Telegram (HTML) и её клавиатуры.

Отделён от mixin'ов ради тестируемости: функции чистые, зависят только от
доменной модели. Вёрстка повторяет эталон: иконка отрасли и бейдж «Новый»,
заголовок, сворачиваемое описание, блоки бюджета и срока, карточка заказчика,
строка биржи, время и три ряда кнопок.

Telegram не умеет таблицы и две колонки: «колонки» эталона превращаются в
последовательные блоки, разделённые тонкой линией.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, tzinfo
from html import escape as html_escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .models import BudgetKind, Job

log = logging.getLogger(__name__)

#: Тонкая линия между блоками. Telegram не рисует border, поэтому блоки
#: разделяются символом — визуально ближе всего к эталону.
DIVIDER = "━━━━━━━━━━━━━━━━━━"

#: Человекочитаемые названия бирж (в эталоне «Upwork», а не «upwork_com»).
PLATFORM_NAMES: dict[str, str] = {
    "fl_ru": "FL.ru",
    "freelance_ru": "Freelance.ru",
    "freelancejob_ru": "FreelanceJob.ru",
    "pchel_net": "Pchel.net",
    "kwork_ru": "Kwork",
    "weblancer_net": "Weblancer",
    "freelancehunt_com": "Freelancehunt",
    "avito_ru": "Avito",
}

#: Иконка отрасли по ключу верхней категории таксономии.
CATEGORY_ICONS: dict[str, str] = {
    "design": "🎨",
    "programming": "💻",
    "writing_translations": "✍️",
    "seo": "📈",
    "promotion": "📣",
    "audio_video": "🎬",
    "business": "💼",
}
DEFAULT_CATEGORY_ICON = "🧩"

#: Подписи бюджета по его типу — как «Фиксированная цена» в эталоне.
BUDGET_KIND_LABELS: dict[BudgetKind, str] = {
    BudgetKind.FIXED: "Фиксированная цена",
    BudgetKind.HOURLY: "Оплата за час",
    BudgetKind.RANGE: "Вилка бюджета",
    BudgetKind.UNKNOWN: "По договорённости",
}

MONTHS_RU = (
    "янв", "фев", "мар", "апр", "мая", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
)

#: Сколько символов описания показывать до «Показать полностью». Две-три
#: строки на телефоне — примерно 170 символов, как в эталоне.
DESCRIPTION_PREVIEW_CHARS = 170

#: Потолок описания в карточке. Сворачиваемая цитата — один длинный абзац без
#: переводов строки; если она не влезет в лимит сообщения, ``_chunk`` разрежет
#: её посреди тега и Telegram отклонит разметку. Потолок держит карточку
#: целиком в одном сообщении.
DESCRIPTION_CARD_CHARS = 1200

#: Подписи кнопки избранного. Одна константа на состояние: карточка и
#: callback-переключатель должны показывать одинаковый текст.
FAV_OFF_LABEL = "♥ Выделить как понравившееся"
FAV_ON_LABEL = "💔 Убрать из избранного"


def platform_label(platform: str) -> str:
    """Название биржи для человека; неизвестная платформа остаётся как есть."""
    return PLATFORM_NAMES.get(platform, platform)


def category_icon(category_key: str | None) -> str:
    """Иконка отрасли; для неизвестной категории — нейтральная."""
    if not category_key:
        return DEFAULT_CATEGORY_ICON
    return CATEGORY_ICONS.get(category_key, DEFAULT_CATEGORY_ICON)


#: Символы валют, которые площадки отдают в ``Job.currency``.
CURRENCY_SYMBOLS: dict[str, str] = {
    "RUB": "₽",
    "RUR": "₽",
    "USD": "$",
    "EUR": "€",
    "UAH": "₴",
    "KZT": "₸",
    "BYN": "Br",
}


def _money(amount: float, currency: str) -> str:
    """Сумма с пробелами между разрядами и символом валюты.

    Рубли пишутся как «1 500 ₽», остальные валюты — как «€ 1 500»: так сумма
    читается одинаково привычно и в русской, и в международной записи.
    """
    formatted = f"{amount:,.0f}".replace(",", " ")
    symbol = CURRENCY_SYMBOLS.get(currency.upper(), currency.upper())
    if symbol == "₽":
        return f"{formatted} ₽"
    return f"{symbol} {formatted}"


def format_budget(job: Job) -> tuple[str, str]:
    """Возвращает (сумма, подпись типа бюджета) для блока «Бюджет».

    Приоритет — та сумма, которую назвала площадка: она в исходной валюте
    заказчика. Оценка в рублях используется только когда суммы нет вовсе.
    """
    if job.budget_amount:
        value = _money(job.budget_amount, job.currency or "RUB")
    elif job.budget_rub_estimate:
        value = _money(job.budget_rub_estimate, "RUB")
    elif job.budget_min and job.budget_max and job.budget_min != job.budget_max:
        low = f"{job.budget_min:,.0f}".replace(",", " ")
        high = f"{job.budget_max:,.0f}".replace(",", " ")
        value = f"{low} – {high} ₽"
        return value, BUDGET_KIND_LABELS[BudgetKind.RANGE]
    else:
        # Суммы нет вовсе: что бы ни сообщила площадка, обсуждать нечего —
        # «Фиксированная цена» без числа вводила бы в заблуждение.
        return "Не указан", BUDGET_KIND_LABELS[BudgetKind.UNKNOWN]

    label = BUDGET_KIND_LABELS.get(job.budget_kind, BUDGET_KIND_LABELS[BudgetKind.UNKNOWN])
    if job.budget_kind is BudgetKind.UNKNOWN:
        # Сумма есть, тип площадка не сообщила — не утверждаем, что она фиксированная.
        label = "По бюджету заказа"
    return value, label


def format_deadline(job: Job) -> tuple[str, str]:
    """Возвращает (срок, подпись) для блока «Срок выполнения».

    Площадки присылают срок по-разному: точную дату (``deadline_at``) или
    свободный текст вроде «3–4 недели» (``deadline_text``). Текст точнее
    передаёт замысел заказчика, поэтому он в приоритете — как в эталоне.
    """
    if job.deadline_text:
        value = job.deadline_text.strip()
        # Кворк присылает «Осталось: 2 дня» — оставляем только срок.
        for prefix in ("Осталось:", "осталось:"):
            if value.startswith(prefix):
                value = value[len(prefix) :].strip()
        if value:
            return value, "По договорённости"
    if job.deadline_at is not None:
        day = job.deadline_at.day
        month = MONTHS_RU[job.deadline_at.month - 1]
        return f"до {day} {month}", "Дедлайн из задания"
    return "Не указан", "По договорённости"


def description_preview(description: str) -> tuple[str, bool]:
    """Сокращает описание до 2–3 строк.

    Возвращает (текст, обрезано ли). Обрезка идёт по границе слова, чтобы
    не показывать половину слова в карточке. Используется для текстовых
    отчётов (например, /jobs); в карточке работает сворачиваемая цитата.
    """
    text = " ".join((description or "").split())
    if len(text) <= DESCRIPTION_PREVIEW_CHARS:
        return text, False
    cut = text[:DESCRIPTION_PREVIEW_CHARS]
    space = cut.rfind(" ")
    if space > DESCRIPTION_PREVIEW_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…", True


def description_html(description: str) -> str:
    """Описание для карточки: короткое — как есть, длинное — сворачиваемым.

    Telegram не даёт кликабельный текст внутри сообщения, зато умеет
    ``<blockquote expandable>``: клиент сам показывает первые строки и
    «Показать полностью» — ровно как в эталоне.

    Длинный текст обрезается до :data:`DESCRIPTION_CARD_CHARS`: цитата —
    один абзац без переводов строки, и разрезать её между сообщениями значит
    порвать HTML-тег.
    """
    text = " ".join((description or "").split())
    if not text:
        return ""
    if len(text) > DESCRIPTION_CARD_CHARS:
        cut = text[:DESCRIPTION_CARD_CHARS]
        space = cut.rfind(" ")
        if space > DESCRIPTION_CARD_CHARS // 2:
            cut = cut[:space]
        text = cut.rstrip(" ,.;:—-") + "…"
    escaped = html_escape(text)
    if len(text) <= DESCRIPTION_PREVIEW_CHARS:
        return escaped
    return f"<blockquote expandable>{escaped}</blockquote>"


def format_time(moment: datetime | None, tz: str | None = None) -> str | None:
    """Время в формате HH:MM (штамп под карточкой, как в эталоне).

    ``tz`` — имя зоны из ``runtime.timezone``; без него берётся локальная зона
    процесса, что удобно в тестах и в консольных отчётах.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    zone: tzinfo | None = None
    if tz:
        try:
            zone = ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("unknown timezone %r, using local time", tz)
    return moment.astimezone(zone).strftime("%H:%M")


def _client_metrics(job: Job) -> list[str]:
    """Метрики заказчика, которые есть в данных. Пустые не показываются.

    Telegram не поддерживает таблицы и колонки, поэтому каждая метрика — своя
    строка вида «⭐ Рейтинг: 4.9/5», а не «4.9/5 | 87% | 24».
    """
    client = job.client
    metrics: list[str] = []
    if client.rating is not None:
        metrics.append(f"⭐ Рейтинг: <b>{client.rating:g}/5</b>")
    if client.hire_rate is not None:
        share = client.hire_rate * 100 if client.hire_rate <= 1 else client.hire_rate
        metrics.append(f"🤝 Нанято исполнителей: <b>{share:.0f}%</b>")
    if client.projects_posted is not None:
        metrics.append(f"📋 Заказов опубликовано: <b>{client.projects_posted}</b>")
    elif client.reviews_count is not None:
        metrics.append(f"💬 Отзывов: <b>{client.reviews_count}</b>")
    if client.total_spend_rub:
        spend = f"{client.total_spend_rub:,.0f}".replace(",", " ")
        metrics.append(f"💳 Потратил: <b>{spend} ₽</b>")
    return metrics


def _client_block(job: Job) -> list[str]:
    """Блок «О заказчике»; пустой список, если данных нет вовсе."""
    client = job.client
    metrics = _client_metrics(job)
    has_identity = bool(client.name) or client.verified or client.registered_at is not None
    if not has_identity and not metrics:
        return []

    lines = ["👤 <b>О заказчике</b>"]
    if client.name:
        lines.append(f"<b>{html_escape(client.name)}</b>")

    # Строка-подпись повторяет эталон: роль, стаж на платформе и зелёная
    # галочка проверки. Telegram не даёт выровнять бейдж справа, поэтому он
    # идёт тем же предложением — смысл сохраняется.
    subtitle: list[str] = ["Заказчик"]
    if client.registered_at is not None:
        subtitle.append(f"На платформе с {client.registered_at.year} года")
    elif client.country:
        subtitle.append(html_escape(client.country))
    if client.verified:
        subtitle.append("✅ Проверен")
    lines.append(f"<i>{' · '.join(subtitle)}</i>")

    if metrics:
        lines.extend([""] + metrics)
    return lines


def order_id(job: Job) -> str | None:
    """Чистый ID заказа для строки «ID заказа: #…».

    Часть адаптеров вместо номера отдаёт ссылку (у Pchel.net это запасной
    путь, когда скрытого поля нет). Такой хвост в карточке выглядел бы как
    битый ID, поэтому из ссылки берётся последний сегмент пути.
    """
    raw = (job.external_id or "").strip()
    if not raw:
        return None
    if raw.startswith(("http://", "https://")):
        # Сначала снимаем query/hash: иначе последним «сегментом» окажется
        # сам «?from=list» и ID потеряется.
        clean = raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        return clean.rsplit("/", 1)[-1] or None
    return raw


def render_hint(summary: str | None, *, limit: int = 260) -> str:
    """Второе сообщение эталона — «💡 Полезно знать» под карточкой.

    Причина показа живёт вне карточки: в самой карточке её быть не должно,
    но подписчику полезно знать, почему заказ ему прислали. Пустая причина
    превращается в пустую строку — тогда сообщение не отправляется.
    """
    text = " ".join((summary or "").split())
    if not text or text == "—":
        return ""
    if len(text) > limit:
        cut = text[:limit]
        space = cut.rfind(" ")
        if space > limit // 2:
            cut = cut[:space]
        text = cut.rstrip(" ,.;:—-") + "…"
    return f"💡 <b>Полезно знать:</b>\n{html_escape(text)}"


def render_job_card(
    job: Job,
    *,
    category_key: str | None = None,
    category_name: str | None = None,
    buyer: str | None = None,
    timezone: str | None = None,
) -> str:
    """Собирает HTML-текст карточки заказа.

    ``buyer`` — имя подписчика, которому адресована карточка; в эталоне его
    нет, поэтому параметр необязательный и нужен только для приветствия.
    ``timezone`` — зона штампа времени из ``runtime.timezone``.
    """
    icon = category_icon(category_key)
    industry = category_name or (job.category or "Заказ")
    title = html_escape(job.title)
    budget_value, budget_label = format_budget(job)
    deadline_value, deadline_label = format_deadline(job)

    lines: list[str] = [
        f"{icon} <b>{html_escape(industry)}</b>  ·  🟢 Новый",
        "",
        f"<b>{title}</b>",
    ]

    if job.description:
        lines.extend(["", description_html(job.description)])

    lines.extend(
        [
            "",
            DIVIDER,
            "",
            "💰 <b>Бюджет</b>",
            f"<b>{html_escape(budget_value)}</b>",
            f"<i>{html_escape(budget_label)}</i>",
            "",
            "📅 <b>Срок выполнения</b>",
            f"<b>{html_escape(deadline_value)}</b>",
            f"<i>{html_escape(deadline_label)}</i>",
        ]
    )

    client_lines = _client_block(job)
    if client_lines:
        lines.extend(["", DIVIDER, ""] + client_lines)

    footer = f"🌐 Биржа: {html_escape(platform_label(job.platform))}"
    oid = order_id(job)
    if oid:
        footer += f" · ID заказа: #{html_escape(oid)}"
    lines.extend(["", DIVIDER, "", footer])

    stamp = format_time(job.published_at or job.fetched_at, timezone)
    if stamp:
        lines.extend(["", f"<i>{stamp}</i>"])

    return "\n".join(lines)


def card_keyboard(
    job: Job,
    *,
    order_url: str | None,
    is_favorite: bool = False,
    has_draft: bool = False,
) -> InlineKeyboardMarkup:
    """Три ряда кнопок эталона: заказ, AI-письмо, избранное.

    «Показать полностью» рисует сам клиент Telegram внутри сворачиваемой
    цитаты, поэтому клавиатура остаётся ровно из трёх рядов, как на макете.
    """
    url = order_url or job.url
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("🔗 Перейти на страницу заказа", url=url)]
    ]
    # Кнопка AI-письма всегда доступна: если черновик уже есть, квиз открывает
    # его для правки вместо повторной генерации с нуля.
    rows.append(
        [
            InlineKeyboardButton(
                "🪄 Составить письмо-ответ с AI",
                callback_data=f"aq:start:{job.uid}",
            )
        ]
    )
    if is_favorite:
        rows.append(
            [InlineKeyboardButton(FAV_ON_LABEL, callback_data=f"unfav:{job.uid}")]
        )
    else:
        rows.append(
            [InlineKeyboardButton(FAV_OFF_LABEL, callback_data=f"fav:{job.uid}")]
        )
    del has_draft  # сохранено для читаемости вызова; вид кнопки не зависит
    return InlineKeyboardMarkup(rows)
