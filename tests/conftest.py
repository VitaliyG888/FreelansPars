"""Общие тестовые фикстуры: реальный сохранённый HTML, фабрики заказов, офлайн-стенд конвейера.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from freelance_bot.config import (  # noqa: E402
    AppConfig,
    PlatformConfig,
    RedFlagRule,
    RedFlagsConfig,
    ScoringConfig,
)
from freelance_bot.models import (  # noqa: E402
    BudgetKind,
    ClientInfo,
    Job,
    PlatformRunResult,
    RawJob,
    TransportKind,
)

FIXTURES = ROOT / "fixtures" / "live"

FIXTURE_MAP = {
    "fl_ru": "fl_ru.html",
    "freelance_ru": "freelance_ru.html",
    "freelancejob_ru": "freelancejob.html",
    "pchel_net": "pchel_projects.html",
    "kwork_ru": "browser_kwork.ru_projects.html",
    "weblancer_net": "browser_www.weblancer.net_jobs_.html",
    "freelancehunt_com": "browser_freelancehunt.com_projects.html",
}


def load_html(platform: str) -> str:
    return (FIXTURES / FIXTURE_MAP[platform]).read_text(encoding="utf-8", errors="replace")


@pytest.fixture(params=sorted(FIXTURE_MAP))
def platform_fixture(request) -> tuple[str, str]:
    return request.param, load_html(request.param)


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(
        platforms={
            "fl_ru": PlatformConfig(listing_url="https://www.fl.ru/projects/", transport="http"),
        },
        scoring=ScoringConfig(
            skill_match={
                "skills": ["python", "парсинг", "api", "автоматизация", "телеграм"],
                "exclude_keywords": ["1с", "бухгалтер", "копирайт"],
            },
            market_median_rub={"default": 10000, "Разработка и IT": 15000},
        ),
        red_flags=RedFlagsConfig(
            rules={
                "unpaid_test": RedFlagRule(severity=0.8, patterns=[r"бесплатн\w* (тестов|пробн)"]),
                "vague_brief": RedFlagRule(severity=0.3, max_description_chars=90),
            }
        ),
    )


def make_job(
    *,
    platform: str = "fl_ru",
    external_id: str = "1",
    title: str = "Разработка парсера на Python",
    description: str = (
        "Нужен парсер каталога товаров на Python с выгрузкой в PostgreSQL и "
        "ежедневным запуском по расписанию. Доступ к API заказчик предоставит."
    ),
    budget: float = 20000.0,
    proposals: int | None = 3,
    hours_ago: float = 1.0,
    deadline_hours: float | None = 48.0,
    client: ClientInfo | None = None,
    kind: BudgetKind = BudgetKind.FIXED,
) -> Job:
    now = datetime.now(UTC)
    return Job(
        platform=platform,
        external_id=external_id,
        url=f"https://example.test/{external_id}",
        title=title,
        description=description,
        budget_amount=budget,
        budget_rub_estimate=budget,
        budget_kind=kind,
        published_at=now - timedelta(hours=hours_ago),
        deadline_at=now + timedelta(hours=deadline_hours) if deadline_hours else None,
        proposals_count=proposals,
        client=client or ClientInfo(),
    )


class FixtureAdapter:
    """Адаптер, отдающий сохранённый HTML вместо обращения к сети."""

    transport = TransportKind.HTTP

    def __init__(self, platform: str, html: str) -> None:
        from freelance_bot.adapters.registry import ADAPTERS

        self.platform = platform
        cls = ADAPTERS[platform]
        self._inner = cls(PlatformConfig(listing_url=f"https://{cls.domain}/", transport="http"))
        self._html = html

    async def fetch(self, ctx) -> tuple[list[RawJob], PlatformRunResult]:
        jobs = self._inner.parse(self._html, self._inner.listing_url)
        return jobs, PlatformRunResult(
            platform=self.platform,
            transport=TransportKind.HTTP,
            outcome="ok",  # type: ignore[arg-type]
            jobs_found=len(jobs),
            duration_ms=1,
        )
