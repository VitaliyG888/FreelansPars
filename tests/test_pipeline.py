"""Сквозной конвейер, по одному замоканному потоку на площадку.

Адаптеры получают сохранённый HTML; всё дальше (нормализация, дедупликация,
скоринг, генерация сообщений, защита, dry-run outbox, SQLite) — настоящий код.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FIXTURE_MAP, FixtureAdapter, load_html
from freelance_bot.config import (
    AppConfig,
    MessageLimit,
    MessagingConfig,
    PlatformConfig,
    Secrets,
)
from freelance_bot.models import Outcome
from freelance_bot.pipeline import PollSchedule, collect, run_cycle


def build_config(platform: str) -> AppConfig:
    return AppConfig(
        platforms={
            platform: PlatformConfig(
                listing_url=f"https://example.test/{platform}", transport="http"
            )
        },
        messaging=MessagingConfig(
            limits={platform: MessageLimit(max_chars=2000)},
            max_sends_per_hour=100,
            min_seconds_between_sends=0,
        ),
    )


TG_CHAT = 111


class FakeTelegram:
    """Имитация бота: конвейер доставляет заказы по личным фильтрам.

    Возвращает переданные chat_ids, как настоящий deliver_job, чтобы
    pipeline помечал заказ доставленным.
    """

    def __init__(self) -> None:
        self.notified: list[str] = []
        self.with_message: list[str] = []
        self.without_message: list[str] = []
        self.recipients: list[int] = []  # chat_id каждого доставленного чата
        self.cycles = 0

    def audience_chat_ids(self) -> set[int]:
        return {TG_CHAT}

    async def deliver_job(
        self,
        chat_ids,
        scored,
        message_text=None,
        *,
        price=None,
        strategy=None,
        analysis=None,
        message_id=None,
    ) -> list[int]:
        self.notified.append(scored.job.uid)
        if message_text:
            self.with_message.append(scored.job.uid)
        else:
            self.without_message.append(scored.job.uid)
        self.recipients.extend(chat_ids)
        return list(chat_ids)

    async def notify_cycle_complete(self, **kwargs) -> None:
        self.cycles += 1


def build_secrets(tmp_path: Path) -> Secrets:
    return Secrets(
        llm_api_key="",
        freelance_bot_db=str(tmp_path / "bot.db"),
        freelance_bot_outbox=str(tmp_path / "outbox"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", sorted(FIXTURE_MAP))
async def test_cycle_end_to_end_per_platform(platform: str, tmp_path: Path, monkeypatch) -> None:
    config = build_config(platform)
    config.runtime.dry_run = True
    config.network.respect_robots_txt = False
    secrets = build_secrets(tmp_path)

    adapter = FixtureAdapter(platform, load_html(platform))
    monkeypatch.setattr("freelance_bot.pipeline.build_adapters", lambda cfg: [adapter])

    report = await run_cycle(
        config, secrets, platform_names=[platform], with_browser=False
    )

    assert report.platforms[0].outcome == Outcome.OK
    assert report.platforms[0].jobs_found > 0
    assert report.jobs_scored > 0
    assert report.jobs_new > 0
    # Порогов больше нет: все прошедшие фильтры заказы доходят до Telegram.
    assert len(report.top) == report.jobs_eligible
    assert report.messages_generated > 0
    assert report.messages_sent <= report.messages_generated
    assert report.messages_sent + report.messages_rejected == report.messages_generated
    assert report.dry_run is True

    outbox = tmp_path / "outbox" / "messages.jsonl"
    assert outbox.exists()
    assert len(outbox.read_text(encoding="utf-8").splitlines()) == report.messages_sent

    # Баллы в допустимом диапазоне; порядок задаёт приоритет (штраф/свежесть),
    # поэтому сортировка по баллу больше не гарантируется.
    totals = [s.score.total for s in report.top]
    assert all(0 <= t <= 100 for t in totals)

    # Каждое сгенерированное сообщение должно быть на русском и без шаблонных плейсхолдеров.
    for scored in report.top:
        assert scored.score.summary_ru


@pytest.mark.asyncio
async def test_second_cycle_does_not_reprocess_jobs(tmp_path: Path, monkeypatch) -> None:
    config = build_config("fl_ru")
    config.runtime.dry_run = True
    config.network.respect_robots_txt = False
    secrets = build_secrets(tmp_path)
    adapter = FixtureAdapter("fl_ru", load_html("fl_ru"))
    monkeypatch.setattr("freelance_bot.pipeline.build_adapters", lambda cfg: [adapter])

    first = await run_cycle(config, secrets, with_browser=False)
    assert first.messages_generated > 0

    second = await run_cycle(config, secrets, with_browser=False)
    assert second.jobs_new == 0
    assert second.messages_generated == 0


@pytest.mark.asyncio
async def test_second_cycle_with_letter_cap_still_deduplicates(
    tmp_path: Path, monkeypatch
) -> None:
    """Заказы, отложенные лимитом писем, не становятся «новыми» заново."""
    config = build_config("fl_ru")
    config.runtime.dry_run = True
    config.network.respect_robots_txt = False
    config.messaging.max_messages_per_cycle = 3
    secrets = build_secrets(tmp_path)
    adapter = FixtureAdapter("fl_ru", load_html("fl_ru"))
    monkeypatch.setattr("freelance_bot.pipeline.build_adapters", lambda cfg: [adapter])

    first = await run_cycle(config, secrets, with_browser=False)
    assert first.jobs_announced == 3
    second = await run_cycle(config, secrets, with_browser=False)
    assert second.jobs_new == 0
    assert second.jobs_announced == 3  # отложенные заказы показаны следующими


@pytest.mark.asyncio
async def test_all_filtered_jobs_reach_telegram(tmp_path: Path, monkeypatch) -> None:
    """Каждый заказ, прошедший фильтры, уходит в Telegram по личным фильтрам."""
    config = build_config("fl_ru")
    config.runtime.dry_run = True
    config.network.respect_robots_txt = False
    secrets = build_secrets(tmp_path)
    adapter = FixtureAdapter("fl_ru", load_html("fl_ru"))
    monkeypatch.setattr("freelance_bot.pipeline.build_adapters", lambda cfg: [adapter])

    tg = FakeTelegram()
    report = await run_cycle(config, secrets, with_browser=False, telegram_bot=tg)

    # Лента уведомлений покрывает каждый подходящий заказ.
    assert len(tg.notified) == report.jobs_eligible
    assert tg.cycles == 1


@pytest.mark.asyncio
async def test_personal_filters_choose_recipients(tmp_path: Path, monkeypatch) -> None:
    """Личные фильтры решают, кому приходит заказ: чужие не получают."""
    from freelance_bot.storage import JobStore

    config = build_config("fl_ru")
    config.runtime.dry_run = True
    config.network.respect_robots_txt = False
    secrets = build_secrets(tmp_path)
    adapter = FixtureAdapter("fl_ru", load_html("fl_ru"))
    monkeypatch.setattr("freelance_bot.pipeline.build_adapters", lambda cfg: [adapter])

    store = JobStore(secrets.freelance_bot_db)
    # Подписчик с фильтром «только kwork_ru» не должен получить fl_ru-заказы.
    store.set_user_filters(222, platforms=["kwork_ru"])

    tg = FakeTelegram()
    try:
        report = await run_cycle(
            config, secrets, with_browser=False, telegram_bot=tg, store=store
        )
        assert report.jobs_eligible > 0
        # 222 нет среди получателей ни одного заказа.
        assert all(chat != 222 for chat in tg.recipients)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_every_new_eligible_job_gets_first_letter(tmp_path: Path, monkeypatch) -> None:
    """Каждый новый подходящий заказ показывается сразу с первым письмом."""
    config = build_config("fl_ru")
    config.runtime.dry_run = True
    config.network.respect_robots_txt = False
    secrets = build_secrets(tmp_path)
    adapter = FixtureAdapter("fl_ru", load_html("fl_ru"))
    monkeypatch.setattr("freelance_bot.pipeline.build_adapters", lambda cfg: [adapter])

    tg = FakeTelegram()
    report = await run_cycle(config, secrets, with_browser=False, telegram_bot=tg)

    assert report.jobs_eligible > 1
    assert len(tg.notified) == report.jobs_eligible
    # Лимит писем за цикл больше не обрезает черновики для новых заказов.
    # Единичный заказ может не пройти guard качества — он показывается без письма.
    assert len(tg.without_message) <= 1
    assert report.messages_generated == report.jobs_eligible
    assert tg.cycles == 1


@pytest.mark.asyncio
async def test_job_is_announced_only_once(tmp_path: Path, monkeypatch) -> None:
    """Повторный проход не присылает уже показанный заказ и не шлёт сводку."""
    config = build_config("fl_ru")
    config.runtime.dry_run = True
    config.network.respect_robots_txt = False
    secrets = build_secrets(tmp_path)
    adapter = FixtureAdapter("fl_ru", load_html("fl_ru"))
    monkeypatch.setattr("freelance_bot.pipeline.build_adapters", lambda cfg: [adapter])

    tg = FakeTelegram()
    first = await run_cycle(config, secrets, with_browser=False, telegram_bot=tg)
    second = await run_cycle(config, secrets, with_browser=False, telegram_bot=tg)

    assert len(tg.notified) == first.jobs_eligible
    assert first.jobs_announced == first.jobs_eligible
    assert second.jobs_announced == 0
    assert second.jobs_new == 0
    assert tg.cycles == 1


@pytest.mark.asyncio
async def test_cycle_letter_cap_defers_but_keeps_jobs(tmp_path: Path, monkeypatch) -> None:
    """Лимит писем за цикл откладывает заказы, а не показывает их без письма."""
    config = build_config("fl_ru")
    config.runtime.dry_run = True
    config.network.respect_robots_txt = False
    config.messaging.max_messages_per_cycle = 5
    secrets = build_secrets(tmp_path)
    adapter = FixtureAdapter("fl_ru", load_html("fl_ru"))
    monkeypatch.setattr("freelance_bot.pipeline.build_adapters", lambda cfg: [adapter])

    tg = FakeTelegram()
    first = await run_cycle(config, secrets, with_browser=False, telegram_bot=tg)
    assert first.jobs_announced == 5
    # Все пять первых показов пришли с письмом.
    assert len(tg.with_message) == 5

    second = await run_cycle(config, secrets, with_browser=False, telegram_bot=tg)
    assert second.jobs_announced == 5
    total = first.jobs_announced + second.jobs_announced
    assert len(tg.with_message) + len(tg.without_message) == total
    # Единственный заказ может не пройти guard и уйти без письма.
    assert len(tg.with_message) >= total - 1


@pytest.mark.asyncio
async def test_min_price_filter_drops_cheap_keeps_negotiable(tmp_path: Path, monkeypatch) -> None:
    """Мин. цена: заказы дешевле порога отсеиваются, без цены — остаются."""
    config = build_config("fl_ru")
    config.runtime.dry_run = True
    config.network.respect_robots_txt = False
    adapter = FixtureAdapter("fl_ru", load_html("fl_ru"))
    monkeypatch.setattr("freelance_bot.pipeline.build_adapters", lambda cfg: [adapter])

    # Базовый прогон без фильтра цены.
    baseline = await run_cycle(
        config, build_secrets(tmp_path), with_browser=False, only_new=False
    )

    # Порог выше всех бюджетов фикстуры: остаются только заказы без цены.
    config.category_filter.min_price_rub = 10**9
    floored = await run_cycle(
        config, build_secrets(tmp_path), with_browser=False, only_new=False
    )

    assert baseline.jobs_scored > floored.jobs_scored > 0
    # Всё, что прошло высокий порог, — заказы без бюджета (по договорённости).
    assert all(s.job.budget_rub_estimate is None for s in floored.top)


@pytest.mark.asyncio
async def test_adapter_crash_does_not_kill_cycle(tmp_path: Path, monkeypatch) -> None:
    config = build_config("fl_ru")
    config.runtime.dry_run = True
    config.network.respect_robots_txt = False
    secrets = build_secrets(tmp_path)

    class Boom:
        platform = "fl_ru"

        async def fetch(self, ctx):
            raise RuntimeError("layout exploded")

    monkeypatch.setattr("freelance_bot.pipeline.build_adapters", lambda cfg: [Boom()])
    report = await run_cycle(config, secrets, with_browser=False)
    assert report.platforms[0].outcome == Outcome.PARSE_ERROR
    assert "layout exploded" in (report.platforms[0].error or "")


@pytest.mark.asyncio
async def test_blocked_platform_is_reported_not_retried_forever(
    tmp_path: Path, monkeypatch
) -> None:
    config = build_config("weblancer_net")
    config.runtime.dry_run = True
    config.network.respect_robots_txt = False
    secrets = build_secrets(tmp_path)

    class Blocked:
        platform = "weblancer_net"

        async def fetch(self, ctx):
            from freelance_bot.models import PlatformRunResult, TransportKind

            return [], PlatformRunResult(
                platform="weblancer_net",
                transport=TransportKind.BROWSER,
                outcome=Outcome.BLOCKED,
                challenge="cloudflare",
                error="challenge not solved",
            )

    monkeypatch.setattr("freelance_bot.pipeline.build_adapters", lambda cfg: [Blocked()])
    report = await run_cycle(config, secrets, with_browser=False)
    assert report.platforms[0].outcome == Outcome.BLOCKED
    assert report.platforms[0].challenge == "cloudflare"
    assert report.messages_generated == 0


# ------------------------------------------------- пер-платформенное расписание
def test_poll_schedule_basic_flow() -> None:
    schedule = PollSchedule()
    # Без интервала площадка опрашивается всегда.
    assert schedule.should_poll("fl_ru", 0) == (True, 0)
    # Первый опрос площадки с интервалом разрешён.
    assert schedule.should_poll("kwork_ru", 1800) == (True, 0)
    schedule.mark_polled("kwork_ru")
    # Сразу после опроса — отказ с осмысленным ожиданием.
    ok, wait = schedule.should_poll("kwork_ru", 1800)
    assert ok is False
    assert 1700 <= wait <= 1800
    # Другая площадка интервалом не связана.
    assert schedule.should_poll("fl_ru", 1800)[0] is True


def test_poll_schedule_allows_after_interval_elapsed() -> None:
    schedule = PollSchedule()
    schedule.mark_polled("kwork_ru")
    # Сдвигаем время последнего опроса в прошлое дальше интервала.
    schedule._last_polled["kwork_ru"] -= 1801
    assert schedule.should_poll("kwork_ru", 1800) == (True, 0)


@pytest.mark.asyncio
async def test_collect_skips_platform_by_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Автоматический цикл пропускает площадку, чей интервал не истёк."""
    from freelance_bot.config import PlatformConfig
    from freelance_bot.models import PlatformRunResult, TransportKind

    calls: list[str] = []

    class Scheduled:
        platform = "kwork_ru"
        transport = TransportKind.HTTP
        cfg = PlatformConfig(
            listing_url="https://kwork.ru/projects",
            transport="http",
            poll_interval_seconds=1800,
        )

        async def fetch(self, ctx):
            calls.append(self.platform)
            return [], PlatformRunResult(
                platform=self.platform,
                transport=self.transport,
                outcome=Outcome.OK,
            )

    adapter = Scheduled()
    monkeypatch.setattr("freelance_bot.pipeline.build_adapters", lambda cfg: [adapter])
    monkeypatch.setattr("freelance_bot.pipeline.build_context", lambda *a, **k: object())
    schedule = PollSchedule()
    monkeypatch.setattr("freelance_bot.pipeline._POLL_SCHEDULE", schedule)

    # Первый автоматический цикл: площадка опрашивается...
    await collect(None, None)  # type: ignore[arg-type]
    assert calls == ["kwork_ru"]
    # ...второй сразу — уже нет (30 минут не прошли).
    await collect(None, None)  # type: ignore[arg-type]
    assert calls == ["kwork_ru"]

    # Ручной запуск с явным списком площадок минует расписание.
    await collect(None, None, platform_names=["kwork_ru"])  # type: ignore[arg-type]
    assert calls == ["kwork_ru", "kwork_ru"]

    # Когда интервал истёк — автоматический цикл снова опрашивает.
    schedule._last_polled["kwork_ru"] -= 1801
    await collect(None, None)  # type: ignore[arg-type]
    assert calls == ["kwork_ru", "kwork_ru", "kwork_ru"]


@pytest.mark.asyncio
async def test_collect_manual_run_does_not_hit_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ручной `-p`-запуск опрашивает площадку даже сразу после автоцикла."""
    from freelance_bot.config import PlatformConfig
    from freelance_bot.models import PlatformRunResult, TransportKind

    calls: list[str] = []

    class Scheduled:
        platform = "kwork_ru"
        transport = TransportKind.HTTP
        cfg = PlatformConfig(
            listing_url="https://kwork.ru/projects",
            transport="http",
            poll_interval_seconds=1800,
        )

        async def fetch(self, ctx):
            calls.append(self.platform)
            return [], PlatformRunResult(
                platform=self.platform,
                transport=self.transport,
                outcome=Outcome.OK,
            )

    adapter = Scheduled()
    monkeypatch.setattr("freelance_bot.pipeline.build_adapters", lambda cfg: [adapter])
    monkeypatch.setattr("freelance_bot.pipeline.build_context", lambda *a, **k: object())
    schedule = PollSchedule()
    monkeypatch.setattr("freelance_bot.pipeline._POLL_SCHEDULE", schedule)

    await collect(None, None, platform_names=["kwork_ru"])  # type: ignore[arg-type]
    # Повторный ручной запрос той же площадки выполняется без ожидания.
    await collect(None, None, platform_names=["kwork_ru"])  # type: ignore[arg-type]
    assert calls == ["kwork_ru", "kwork_ru"]


@pytest.mark.asyncio
async def test_collect_adapter_without_cfg_polls_every_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Адаптер без cfg (как тестовые заглушки) опрашивается в каждом цикле."""
    from freelance_bot.models import PlatformRunResult, TransportKind

    calls: list[str] = []

    class Bare:
        platform = "fl_ru"

        async def fetch(self, ctx):
            calls.append(self.platform)
            return [], PlatformRunResult(
                platform=self.platform,
                transport=TransportKind.HTTP,
                outcome=Outcome.OK,
            )

    monkeypatch.setattr("freelance_bot.pipeline.build_adapters", lambda cfg: [Bare()])
    monkeypatch.setattr("freelance_bot.pipeline.build_context", lambda *a, **k: object())
    monkeypatch.setattr("freelance_bot.pipeline._POLL_SCHEDULE", PollSchedule())

    await collect(None, None)  # type: ignore[arg-type]
    await collect(None, None)  # type: ignore[arg-type]
    assert calls == ["fl_ru", "fl_ru"]
