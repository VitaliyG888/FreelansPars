"""Помощники нормализации: разбор денег, относительные даты, конвертация валют.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

from .models import BudgetKind, ClientInfo, Job, RawJob
from .order_links import resolve_order_url

CURRENCY_TOKENS = {
    "₽": "RUB",
    "руб": "RUB",
    "руб.": "RUB",
    "р.": "RUB",
    "rub": "RUB",
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "грн": "UAH",
    "uah": "UAH",
    "₴": "UAH",
    "byr": "BYN",
    "byn": "BYN",
}

_NUM_RE = re.compile(r"\d[\d\s\u00a0\u202f]*(?:[.,]\d+)?")

RELATIVE_RE = re.compile(
    r"(?:(?P<n1>\d+)\s*(?P<u1>секунд|минут|час|день|дня|дней|недел|месяц)[а-я]*)?"
    r"\s*(?:(?P<n2>\d+)\s*(?P<u2>секунд|минут|час|день|дня|дней|недел|месяц)[а-я]*)?"
    r"\s*назад",
    re.IGNORECASE,
)

DEADLINE_RE = re.compile(
    r"осталось[:\s]*(?:(?P<d>\d+)\s*(?:дн|день|дня)[а-я]*)?\s*"
    r"(?:(?P<h>\d+)\s*ч[а-я]*\.?)?\s*(?:(?P<m>\d+)\s*мин[а-я]*\.?)?",
    re.IGNORECASE,
)


def parse_money(text: str | None) -> tuple[float | None, str, BudgetKind]:
    """Разбирает денежную строку в (сумма, валюта, вид).

        Обрабатывает "10 000 ₽", "70 $", "1 500 - 3 000 руб", "до 5000", "$11", "10 000 UAH".
    """
    if not text:
        return None, "RUB", BudgetKind.UNKNOWN
    raw = text.replace("\xa0", " ").replace("\u202f", " ").strip()
    low = raw.lower()

    currency = "RUB"
    for token, code in CURRENCY_TOKENS.items():
        if token in low:
            currency = code
            break

    numbers = []
    for m in _NUM_RE.finditer(raw):
        chunk = m.group(0).replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
        chunk = chunk.replace(",", ".")
        try:
            numbers.append(float(chunk))
        except ValueError:
            continue
    numbers = [n for n in numbers if n > 0]
    if not numbers:
        return None, currency, BudgetKind.UNKNOWN

    if re.search(r"\b(в час|/час|час|hour|h)\b", low) or "в час" in low:
        return numbers[0], currency, BudgetKind.HOURLY
    if len(numbers) >= 2 and re.search(r"[-–—]|до|от", low):
        lo, hi = numbers[0], numbers[1]
        return (lo + hi) / 2, currency, BudgetKind.RANGE
    if re.search(r"\b(от|до)\b", low):
        return numbers[0], currency, BudgetKind.RANGE
    return numbers[0], currency, BudgetKind.FIXED


def _unit_delta(unit: str, n: int) -> timedelta:
    unit = unit.lower()
    if unit.startswith("секунд"):
        return timedelta(seconds=n)
    if unit.startswith("минут"):
        return timedelta(minutes=n)
    if unit.startswith("час"):
        return timedelta(hours=n)
    if unit.startswith(("день", "дня", "дней")):
        return timedelta(days=n)
    if unit.startswith("недел"):
        return timedelta(weeks=n)
    return timedelta(days=30 * n)


def parse_relative_time(text: str | None, now: datetime | None = None) -> datetime | None:
    """Разбирает русские относительные метки времени вида '18 часов 4 минуты назад'.
    """
    if not text:
        return None
    now = now or datetime.now(UTC)
    m = RELATIVE_RE.search(text)
    if not m or not (m.group("n1") or m.group("n2")):
        return None
    delta = timedelta(0)
    if m.group("n1") and m.group("u1"):
        delta += _unit_delta(m.group("u1"), int(m.group("n1")))
    if m.group("n2") and m.group("u2"):
        delta += _unit_delta(m.group("u2"), int(m.group("n2")))
    return now - delta


def parse_absolute_time(text: str | None) -> datetime | None:
    """Разбирает '02.11.2026 18:59' или '01.11.2026 в 22:30' как московское время.
    """
    if not text:
        return None
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})(?:\s*(?:в\s*)?(\d{1,2}):(\d{2}))?", text)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour = int(m.group(4) or 0)
    minute = int(m.group(5) or 0)
    try:
        # Europe/Moscow — это UTC+3 круглый год.
        msk = timezone(timedelta(hours=3))
        return datetime(year, month, day, hour, minute, tzinfo=msk).astimezone(UTC)
    except ValueError:
        return None


def parse_deadline(text: str | None, now: datetime | None = None) -> datetime | None:
    if not text:
        return None
    now = now or datetime.now(UTC)
    m = DEADLINE_RE.search(text)
    if m and any(m.group(g) for g in ("d", "h", "m")):
        days = int(m.group("d") or 0)
        hours = int(m.group("h") or 0)
        minutes = int(m.group("m") or 0)
        return now + timedelta(days=days, hours=hours, minutes=minutes)
    m2 = re.search(r"срок[:\s]*(\d+)\s*(дн|день|дня|час|ч)", text, re.IGNORECASE)
    if m2:
        n = int(m2.group(1))
        unit = m2.group(2).lower()
        return now + (timedelta(days=n) if unit.startswith(("дн", "день")) else timedelta(hours=n))
    return None


def to_rub(amount: float | None, currency: str, fx: dict[str, float]) -> float | None:
    if amount is None:
        return None
    rate = fx.get(currency.upper())
    if rate is None:
        return None
    return round(amount * rate, 2)


def normalise(raw: RawJob, *, fx: dict[str, float], now: datetime | None = None) -> Job:
    """Преобразует запись адаптера в готовый к скорингу Job с оценкой в рублях.
    """
    amount = raw.budget_amount
    currency = (raw.currency or "RUB").upper()
    budget_kind = raw.budget_kind
    if amount is None and raw.budget_text:
        parsed, parsed_currency, kind = parse_money(raw.budget_text)
        if parsed is not None:
            amount = parsed
            currency = parsed_currency
            if budget_kind == BudgetKind.UNKNOWN:
                budget_kind = kind
        else:
            # Бюджет указан текстом, но не числом («Договорная», «по договорённости»).
            budget_kind = BudgetKind.UNKNOWN

    estimate = to_rub(amount, currency, fx)
    if estimate is not None and budget_kind == BudgetKind.HOURLY:
        estimate = round(estimate * 8, 2)  # ориентировочная оценка дневной ставки

    published = raw.published_at or parse_absolute_time(raw.published_text)
    if published is None:
        published = parse_relative_time(raw.published_text, now)

    deadline = raw.deadline_at or parse_deadline(raw.deadline_text, now)

    return Job(
        platform=raw.platform,
        external_id=raw.external_id,
        url=raw.url,
        order_url=raw.order_url
        or resolve_order_url(raw.platform, raw.external_id, raw.url),
        title=raw.title,
        description=raw.description,
        budget_amount=amount,
        budget_min=raw.budget_min,
        budget_max=raw.budget_max,
        currency=currency,
        budget_kind=budget_kind,
        budget_rub_estimate=estimate,
        category=raw.category,
        skills=raw.skills,
        published_at=published,
        deadline_at=deadline,
        deadline_text=raw.deadline_text,
        proposals_count=raw.proposals_count,
        is_urgent=raw.is_urgent,
        client=raw.client if isinstance(raw.client, ClientInfo) else ClientInfo(),
    )
