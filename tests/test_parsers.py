"""Регрессионные тесты адаптеров на реальном сохранённом HTML.

Именно эти тесты ловят изменение вёрстки площадки: они проверяют точное число
карточек и конкретные значения полей, а не просто то, что разбор что-то вернул.
"""

from __future__ import annotations

import pytest

from conftest import FIXTURE_MAP, load_html
from freelance_bot.adapters.registry import ADAPTERS
from freelance_bot.config import PlatformConfig
from freelance_bot.models import BudgetKind
from freelance_bot.normalize import normalise

FX = {"RUB": 1.0, "UAH": 2.3, "USD": 95.0, "EUR": 102.0}

EXPECTED_MIN_JOBS = {
    "fl_ru": 25,
    "freelance_ru": 20,
    "freelancejob_ru": 35,
    "pchel_net": 45,
    "kwork_ru": 10,
    "weblancer_net": 15,
    "freelancehunt_com": 12,
}


def parse(platform: str):
    cls = ADAPTERS[platform]
    cfg = PlatformConfig(listing_url=f"https://{cls.domain}/")
    return cls(cfg).parse(load_html(platform), cfg.listing_url)


def parse_norm(platform: str):
    """Сырой вывод адаптера через настоящий нормализатор (валюта, вид бюджета, даты).
    """
    return [normalise(raw, fx=FX) for raw in parse(platform)]


@pytest.mark.parametrize("platform", sorted(FIXTURE_MAP))
def test_parser_extracts_cards(platform: str) -> None:
    jobs = parse(platform)
    assert len(jobs) >= EXPECTED_MIN_JOBS[platform], f"{platform}: only {len(jobs)} cards"


@pytest.mark.parametrize("platform", sorted(FIXTURE_MAP))
def test_required_fields_populated(platform: str) -> None:
    raw_jobs = parse(platform)
    for job in raw_jobs:
        assert job.external_id, f"{platform}: empty external_id"
        assert job.title.strip(), f"{platform}: empty title"
        assert job.url.startswith("http"), f"{platform}: relative url {job.url}"

    # Заголовки обязательны; тела на некоторых карточках законно отсутствуют.
    with_description = sum(1 for job in raw_jobs if job.description.strip())
    share = with_description / max(len(raw_jobs), 1)
    assert share >= 0.8, f"{platform}: only {share:.0%} of cards have a description"


@pytest.mark.parametrize("platform", sorted(FIXTURE_MAP))
def test_no_duplicate_ids(platform: str) -> None:
    ids = [job.external_id for job in parse(platform)]
    assert len(ids) == len(set(ids)), f"{platform}: duplicate external_id"


def test_fl_ru_specific_job() -> None:
    raw = {j.external_id: j for j in parse("fl_ru")}
    jobs = {j.external_id: j for j in parse_norm("fl_ru")}
    job = jobs["5527671"]
    assert "презентацию" in job.title.lower() or "презентация" in job.title.lower()
    assert job.budget_amount == 10000
    assert job.budget_kind == BudgetKind.FIXED
    assert job.proposals_count == 334
    assert raw["5527671"].published_text and "назад" in raw["5527671"].published_text


def test_freelance_ru_specific_job() -> None:
    jobs = {j.external_id: j for j in parse_norm("freelance_ru")}
    job = jobs["14476"]
    assert "AutoCAD" in job.title
    assert job.budget_amount == 7000
    assert job.category == "Инженерия и Проектирование"
    assert job.deadline_at is not None


def test_freelancejob_ru_specific_job() -> None:
    jobs = {j.external_id: j for j in parse_norm("freelancejob_ru")}
    job = jobs["77196"]
    assert job.budget_amount == 100
    assert job.proposals_count == 1
    # Описание должно быть телом, а не повторённым заголовком.
    assert len(job.description) > len(job.title)


def test_pchel_net_specific_job() -> None:
    jobs = {j.external_id: j for j in parse_norm("pchel_net")}
    job = jobs["1626526"]
    assert job.budget_amount == 11
    assert job.currency == "USD"
    assert job.budget_rub_estimate == 1045
    assert job.proposals_count == 63
    assert job.is_urgent is True
    assert job.client.name == "designdecorspbstudiogmailcom"


def test_kwork_specific_job() -> None:
    jobs = {j.external_id: j for j in parse_norm("kwork_ru")}
    job = jobs["3270699"]
    assert job.budget_amount == 500
    assert job.proposals_count == 0
    assert job.client.name == "gavrushinka"
    assert job.client.hire_rate == 0.0
    # Нужно использовать полное скрытое описание, а не усечённый превью-текст.
    assert len(job.description) > 180


def test_kwork_parses_embedded_initial_state() -> None:
    """Серверный HTML Kwork не содержит want-card, но содержит JSON initial state."""
    raw_jobs = parse("kwork_ru")
    assert len(raw_jobs) == 12
    assert {job.raw.get("source") for job in raw_jobs} == {"initial-state"}
    first = raw_jobs[0]
    assert first.published_at is not None
    assert first.published_at.tzinfo is not None


@pytest.mark.asyncio
async def test_avito_without_proxy_is_disabled_immediately() -> None:
    from types import SimpleNamespace

    from freelance_bot.adapters.avito_ru import AvitoRuAdapter
    from freelance_bot.models import Outcome
    from freelance_bot.net.proxies import ProxyPool

    adapter = AvitoRuAdapter(
        PlatformConfig(
            listing_url="https://www.avito.ru/all/predlozheniya_uslug",
            transport="browser",
            requires_proxy=True,
        )
    )
    ctx = SimpleNamespace(http=SimpleNamespace(proxies=ProxyPool()))
    jobs, result = await adapter.fetch(ctx)  # type: ignore[arg-type]
    assert jobs == []
    assert result.outcome == Outcome.DISABLED
    assert "RU residential proxy" in (result.error or "")
    assert result.duration_ms < 1000


def test_weblancer_specific_job() -> None:
    jobs = {j.external_id: j for j in parse_norm("weblancer_net")}
    job = jobs["1270328"]
    assert job.title == "Tatra Tram for Roblox"
    assert job.budget_amount == 70
    assert job.currency == "USD"


def test_freelancehunt_specific_job() -> None:
    jobs = {j.external_id: j for j in parse_norm("freelancehunt_com")}
    job = jobs["1662432"]
    assert job.budget_amount == 10000
    assert job.currency == "UAH"
    assert job.budget_rub_estimate == 23000
    assert "эвакуаци" in job.title.lower()


def test_parser_handles_empty_and_garbage() -> None:
    for _platform, cls in ADAPTERS.items():
        cfg = PlatformConfig(listing_url=f"https://{cls.domain}/")
        adapter = cls(cfg)
        assert adapter.parse("", "https://example.test") == []
        assert (
            adapter.parse("<html><body>no cards here</body></html>", "https://example.test") == []
        )
