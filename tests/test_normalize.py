"""Нормализация денег, дат и валют."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from freelance_bot.models import BudgetKind, RawJob
from freelance_bot.normalize import (
    normalise,
    parse_absolute_time,
    parse_deadline,
    parse_money,
    parse_relative_time,
    to_rub,
)

FX = {"RUB": 1.0, "UAH": 2.3, "USD": 95.0, "EUR": 102.0, "BYN": 29.0}


@pytest.mark.parametrize(
    "text,amount,currency,kind",
    [
        ("10 000 ₽", 10000, "RUB", BudgetKind.FIXED),
        ("10\u00a0000 ₽", 10000, "RUB", BudgetKind.FIXED),
        ("$11", 11, "USD", BudgetKind.FIXED),
        ("70 $", 70, "USD", BudgetKind.FIXED),
        ("1 500 - 3 000 руб", 2250, "RUB", BudgetKind.RANGE),
        ("от 5000 руб", 5000, "RUB", BudgetKind.RANGE),
        ("800 руб/час", 800, "RUB", BudgetKind.HOURLY),
        ("10 000 UAH", 10000, "UAH", BudgetKind.FIXED),
        ("Договорная", None, "RUB", BudgetKind.UNKNOWN),
        ("", None, "RUB", BudgetKind.UNKNOWN),
        (None, None, "RUB", BudgetKind.UNKNOWN),
    ],
)
def test_parse_money(text, amount, currency, kind) -> None:
    got_amount, got_currency, got_kind = parse_money(text)
    assert got_amount == amount
    assert got_currency == currency
    assert got_kind == kind


def test_parse_relative_two_components() -> None:
    now = datetime(2026, 11, 3, 1, 0, tzinfo=UTC)
    got = parse_relative_time("18 часов 4 минуты назад", now)
    assert got == now - timedelta(hours=18, minutes=4)


def test_parse_relative_days() -> None:
    now = datetime(2026, 11, 3, 1, 0, tzinfo=UTC)
    assert parse_relative_time("3 дня назад", now) == now - timedelta(days=3)
    assert parse_relative_time("2 недели назад", now) == now - timedelta(weeks=2)


def test_parse_relative_rejects_noise() -> None:
    assert parse_relative_time("Заказ", datetime.now(UTC)) is None


def test_parse_absolute_moscow() -> None:
    got = parse_absolute_time("02.11.2026 18:59")
    assert got is not None
    assert got.hour == 15  # 18:59 МСК == 15:59 UTC
    assert got.day == 2


def test_parse_deadline_srok() -> None:
    now = datetime(2026, 11, 3, tzinfo=UTC)
    assert parse_deadline("Срок: 2 дня", now) == now + timedelta(days=2)
    assert parse_deadline("Осталось: 22 ч. 39 мин.", now) == now + timedelta(hours=22, minutes=39)


def test_to_rub() -> None:
    assert to_rub(100, "RUB", FX) == 100
    assert to_rub(100, "UAH", FX) == 230
    assert to_rub(10, "USD", FX) == 950
    assert to_rub(10, "XXX", FX) is None
    assert to_rub(None, "RUB", FX) is None


def _raw(**kwargs) -> RawJob:
    base = {
        "platform": "fl_ru",
        "external_id": "1",
        "url": "https://example.test/1",
        "title": "Заказ",
        "description": "Описание",
    }
    base.update(kwargs)
    return RawJob(**base)


def test_normalise_converts_currency() -> None:
    job = normalise(_raw(budget_text="10 000 UAH"), fx=FX)
    assert job.currency == "UAH"
    assert job.budget_rub_estimate == 23000
    assert job.budget_kind == BudgetKind.FIXED


def test_normalise_non_numeric_budget_is_unknown() -> None:
    job = normalise(_raw(budget_text="Договорная"), fx=FX)
    assert job.budget_amount is None
    assert job.budget_kind == BudgetKind.UNKNOWN
    assert job.budget_rub_estimate is None


def test_normalise_hourly_estimates_day_rate() -> None:
    job = normalise(_raw(budget_text="1 000 руб/час"), fx=FX)
    assert job.budget_kind == BudgetKind.HOURLY
    assert job.budget_rub_estimate == 8000


def test_normalise_computes_published_from_text() -> None:
    job = normalise(_raw(published_text="5 часов назад"), fx=FX)
    assert job.published_at is not None
    age = datetime.now(UTC) - job.published_at
    assert 4.5 < age.total_seconds() / 3600 < 5.5


def test_normalise_unescapes_html_entities() -> None:
    """HTML-сущности из площадки не должны доходить до карточки и письма.

    kwork отдаёт «&mdash;» прямо в тексте: в письме это читалось как
    «Бюджет проекта клиента &mdash; от 50 000 ₽».
    """
    job = normalise(
        _raw(
            title="Разработка сайта &laquo;под ключ&raquo;",
            description="Бюджет проекта &mdash; от 50 000 &nbsp;₽",
        ),
        fx=FX,
    )
    assert "&mdash;" not in job.description
    assert "&nbsp;" not in job.description
    assert "—" in job.description
    assert "«под ключ»" in job.title


def test_split_sentences_keeps_abbreviations_together() -> None:
    """Точка после «см.» не рвёт предложение.

    Заказ «Вывести в ТОП-50 (см. прикрепленный файл) эту страницу» делился
    на два «предложения», и в письмо попадал обрывок «прикрепленный файл)
    эту страницу».
    """
    from freelance_bot.text import split_sentences

    parts = split_sentences(
        "Вывести в ТОП-50 (см. прикрепленный файл) эту страницу. Нужны цена и сроки."
    )
    assert parts == [
        "Вывести в ТОП-50 (см. прикрепленный файл) эту страницу.",
        "Нужны цена и сроки.",
    ]


def test_lowercase_sentence_starts_ignores_abbreviations() -> None:
    """«т. к.» и «см.» — не начало нового предложения со строчной буквы."""
    from freelance_bot.text import lowercase_sentence_starts

    assert lowercase_sentence_starts("Срочно. Т.к. дедлайн близко.") == []
    assert lowercase_sentence_starts("Нормально. Всё с заглавной.") == []
    assert lowercase_sentence_starts("Клиенты. готов взяться") == ["готов взяться"]
