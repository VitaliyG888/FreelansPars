"""Полный цикл: здесь точка входа планировщика.

scheduler -> adapters -> normaliser -> dedupe -> category classifier ->
             category filter -> scorer (приоритет, без порогов) -> LLM ->
             guard -> personal delivery per user -> sender
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from .adapters.base import AdapterContext
from .adapters.registry import build_adapters, build_context
from .categories import CategoryFilter, Classification, classify_job
from .config import AppConfig, Secrets
from .llm.message import MessageGenerator
from .models import (
    CycleReport,
    Job,
    Outcome,
    PlatformRunResult,
    ScoredJob,
    TransportKind,
)
from .normalize import normalise
from .profile import FreelancerProfile
from .scoring.engine import ScoringEngine
from .sender import OutboxSender
from .storage import JobStore
from .user_prefs import UserFilterPrefs

log = logging.getLogger(__name__)


class PollSchedule:
    """Пер-платформенные интервалы опроса для автоматических циклов.

    Площадке можно назначить собственный интервал (например, kwork 1800 с,
    чтобы не дёргать Qrator каждые пять минут); 0 или отсутствие ключа —
    опрашивать в каждом цикле. Время последнего опроса живёт в памяти
    процесса: после рестарта сервиса площадка опрашивается сразу, что
    допустимо — это один дополнительный визит.
    """

    def __init__(self) -> None:
        self._last_polled: dict[str, float] = {}

    def should_poll(self, name: str, interval_seconds: int) -> tuple[bool, int]:
        """Решает, пора ли опрашивать площадку.

        Возвращает ``(нужно_ли, секунд_до_следующего_опроса)``; второе
        значение осмысленно только при отказе.
        """
        if interval_seconds <= 0:
            return True, 0
        now = time.monotonic()
        last = self._last_polled.get(name)
        if last is None or now - last >= interval_seconds:
            return True, 0
        return False, int(round(interval_seconds - (now - last)))

    def mark_polled(self, name: str) -> None:
        self._last_polled[name] = time.monotonic()


# Единый планировщик на процесс: живёт между циклами сервиса.
_POLL_SCHEDULE = PollSchedule()


async def collect(
    config: AppConfig,
    secrets: Secrets,
    *,
    platform_names: list[str] | None = None,
    with_browser: bool = True,
) -> tuple[list[Job], list[PlatformRunResult], AdapterContext]:
    adapters = build_adapters(config)
    if platform_names:
        wanted = set(platform_names)
        adapters = [a for a in adapters if a.platform in wanted]

    ctx = build_context(config, secrets, with_browser=with_browser)
    jobs: list[Job] = []
    results: list[PlatformRunResult] = []

    # Ручной запуск с явным перечнем площадок (CLI -p kwork_ru) минует
    # расписание: пользователь попросил именно эти площадки прямо сейчас.
    manual_run = bool(platform_names)

    # Последовательно намеренно: лимиты по доменам уже сериализуют большую часть
    # работу, и один упавший адаптер не должен отравлять остальные.
    for adapter in adapters:
        cfg = getattr(adapter, "cfg", None)
        interval = cfg.poll_interval_seconds if cfg is not None else 0
        if not manual_run:
            ok, wait_s = _POLL_SCHEDULE.should_poll(adapter.platform, interval)
            if not ok:
                log.info(
                    "%s: пропущен по расписанию, следующий опрос через ~%d с",
                    adapter.platform,
                    wait_s,
                )
                continue
        # Любая попытка (даже упавшая) считается опросом: иначе площадка,
        # на которой всё падает, дёргалась бы в каждом цикле подряд.
        _POLL_SCHEDULE.mark_polled(adapter.platform)
        try:
            raw_jobs, result = await adapter.fetch(ctx)
        except Exception as e:  # noqa: BLE001
            log.exception("adapter %s crashed", adapter.platform)
            results.append(
                PlatformRunResult(
                    platform=adapter.platform,
                    transport=getattr(adapter, "transport", TransportKind.HTTP),
                    outcome=Outcome.PARSE_ERROR,
                    error=f"{type(e).__name__}: {e}"[:300],
                )
            )
            continue

        normalised: list[Job] = []
        for raw in raw_jobs:
            try:
                normalised.append(normalise(raw, fx=config.fx_to_rub))
            except Exception as e:  # noqa: BLE001
                log.warning("normalise failed for %s/%s: %s", raw.platform, raw.external_id, e)
        result.jobs_found = len(normalised)
        jobs.extend(normalised)
        results.append(result)
        log.info(
            "%s: %s, %d jobs in %d ms%s",
            adapter.platform,
            result.outcome.value,
            len(normalised),
            result.duration_ms,
            f" ({result.challenge})" if result.challenge else "",
        )

    return jobs, results, ctx


async def run_cycle(
    config: AppConfig,
    secrets: Secrets,
    *,
    platform_names: list[str] | None = None,
    with_browser: bool = True,
    generate_messages: bool = True,
    only_new: bool = True,
    store: JobStore | None = None,
    telegram_bot=None,
    profile: FreelancerProfile | None = None,
) -> CycleReport:
    """Один проход сбора и персональной доставки заказов.

    Порогов балла и совпадения навыков нет: показывается каждый заказ,
    прошедший фильтр категорий/подкатегорий и минимальной цены. Скоринг
    влияет только на приоритет: заказы со штрафом красных флагов и старые
    уходят после свежих без штрафов. Каждому пользователю заказ доставляется
    по его личным фильтрам (категории, подкатегории, биржи, мин. цена) один раз.
    """
    report = CycleReport(dry_run=config.runtime.dry_run)
    owns_store = store is None
    store = store or JobStore(secrets.freelance_bot_db)

    try:
        jobs, platform_results, ctx = await collect(
            config, secrets, platform_names=platform_names, with_browser=with_browser
        )
        report.platforms = platform_results

        # Дедупликация внутри цикла с сохранением самого полного описания.
        by_uid: dict[str, Job] = {}
        for job in jobs:
            existing = by_uid.get(job.uid)
            if existing is None or len(job.description) > len(existing.description):
                by_uid[job.uid] = job
        unique = list(by_uid.values())

        # Общий (админский) фильтр категорий ограничивает, что попадает в базу.
        # Личные фильтры пользователей применяются позже, при доставке.
        cat_filter = CategoryFilter(
            enabled=config.category_filter.enabled,
            categories=tuple(config.category_filter.categories),
            subcategories={
                k: tuple(v) for k, v in (config.category_filter.subcategories or {}).items()
            }
            if config.category_filter.subcategories
            else None,
            min_confidence=config.category_filter.min_confidence,
            unknown_policy=config.category_filter.unknown_policy,
            min_price_rub=config.category_filter.min_price_rub,
        )
        # Минимальная цена работает независимо от выбора категорий: заказы
        # дешевле порога не показываются, но без цены (по договорённости) —
        # проходят всегда.
        if cat_filter.min_price_rub > 0:
            before_price = len(unique)
            unique = [job for job in unique if cat_filter.allows_price(job)]
            log.info(
                "min price filter (>= %.0f RUB): %d/%d jobs passed",
                cat_filter.min_price_rub,
                len(unique),
                before_price,
            )

        # Классификация нужна и общему фильтру, и личным фильтрам пользователей.
        classifications: dict[str, Classification] = {}
        for job in unique:
            classifications[job.uid] = classify_job(job)

        # Аудитория доставки нужна уже на этапе сбора. Админский фильтр задаёт,
        # что видит сам админ, но он не должен отменять выбор подписчика: если
        # человек явно выбрал «Тексты и переводы», заказ из этой рубрики обязан
        # дойти, даже когда админ собирает только IT. Раньше общий фильтр резал
        # целые рубрики до личных фильтров, и выбор подписчика молча не работал.
        recipients: dict[int, UserFilterPrefs] = {}
        if telegram_bot is not None:
            for chat_id in telegram_bot.audience_chat_ids():
                recipients[chat_id] = UserFilterPrefs.default_for(chat_id)
        for chat_id, prefs in store.all_user_filters().items():
            recipients[chat_id] = prefs

        # Расширяют сбор только явные выборы рубрик: фильтр без ограничений
        # означает «всё, что прошло общий фильтр» и не должен его отменять.
        subscriber_choices = [prefs for prefs in recipients.values() if prefs.categories]

        def selected_by_subscriber(job: Job, classification: Classification) -> bool:
            """Заказ явно выбран хотя бы одним подписчиком.

            Порог уверенности общий для всех: выбор подписчика расширяет
            сбор рубриками, но не отменяет минимальную уверенность. Иначе
            заказ со слабым совпадением («Настройка VPS» → «сервер», балл
            0.23) проходил только потому, что рубрика выбрана, и подписчик
            получал мусор вперемешку с заказами.
            """
            if classification.confidence < cat_filter.min_confidence:
                return False
            return any(
                prefs.allows_platform(job.platform)
                and prefs.allows_price(job.budget_rub_estimate)
                and prefs.allows_category(
                    classification.category_key,
                    classification.subcategory,
                    classification.group,
                )
                for prefs in subscriber_choices
            )

        if cat_filter.active:
            filtered: list[Job] = []
            for job in unique:
                classification = classifications[job.uid]
                if cat_filter.allows(classification) or selected_by_subscriber(
                    job, classification
                ):
                    filtered.append(job)
            log.info(
                "category filter: %d/%d jobs passed (%s; выбор подписчиков: %d)",
                len(filtered),
                len(unique),
                cat_filter.describe(),
                len(subscriber_choices),
            )
            unique = filtered

        # Скоринг без порогов: балл и разбор остаются на карточке, штраф
        # красных флагов понижает приоритет доставки.
        engine = ScoringEngine(config.scoring, config.red_flags)
        ranked_all = engine.rank(unique, top_n=len(unique))
        report.jobs_scored = len(unique)

        previously_seen = {job.uid for job in unique if store.seen(job.uid)}
        report.jobs_new = len(unique) - len(previously_seen)

        # Каждый заказ, прошедший фильтры, сохраняется как подходящий.
        for scored in ranked_all:
            store.save_scored(
                scored, eligible=True, classification=classifications[scored.job.uid]
            )
        report.jobs_eligible = len(ranked_all)

        # Кому именно нужен заказ: одинаковый заказ может быть нужен одному
        # подписчику и не нужен другому, поэтому отметка «показан» глобальная
        # лишь для админской ленты, а получатели считаются отдельно.
        wanted_by_uid: dict[str, list[int]] = {}
        if telegram_bot is not None:
            for scored in ranked_all:
                job = scored.job
                classification = classifications[job.uid]
                wanted_by_uid[job.uid] = [
                    chat_id
                    for chat_id, prefs in recipients.items()
                    if prefs.matches(
                        platform=job.platform,
                        category_key=classification.category_key,
                        subcategory=classification.subcategory,
                        group=classification.group,
                        budget_rub_estimate=job.budget_rub_estimate,
                    )
                    and not store.delivered_to(chat_id, job.uid)
                ]

        def has_recipient(scored: ScoredJob) -> bool:
            """Заказ ещё кому-то нужен (или лента админа его не показывала)."""
            if telegram_bot is None:
                return not store.notified(scored.job.uid)
            return bool(wanted_by_uid.get(scored.job.uid))

        # Показываем только те заказы, которые ещё не доставлены всем, кому
        # подходят. Признак хранится в SQLite, поэтому рестарт сервиса не
        # приводит к повторной рассылке.
        candidates = [s for s in ranked_all if not only_new or has_recipient(s)]
        # Приоритет выгрузки: сначала заказы без штрафов, внутри группы —
        # свежие; заказы со штрафами и старые уходят последними.
        candidates.sort(
            key=lambda s: (
                s.score.penalty > 0,
                -(s.job.published_at or s.job.fetched_at).timestamp(),
            )
        )
        report.top = candidates

        generator = MessageGenerator(config, secrets, profile=profile)
        sender = OutboxSender(
            secrets.freelance_bot_outbox,
            config.messaging,
            dry_run=config.runtime.dry_run,
        )
        sem = asyncio.Semaphore(3)
        announced = 0

        async def handle(scored: ScoredJob) -> None:
            """Анализ, первое письмо и одноразовая доставка заинтересованным."""
            nonlocal announced
            async with sem:
                message = None
                analysis = None
                if generate_messages:
                    try:
                        analysis, message = await generator.generate(scored.job)
                    except Exception as e:  # noqa: BLE001
                        # Без письма заказ не показываем: попробуем снова в
                        # следующем проходе, а не пришлём его без черновика.
                        log.warning(
                            "message generation failed for %s: %s", scored.job.uid, e
                        )
                        return

                price, strategy = generator.resolve_price(scored.job, analysis)
                if message is not None and not generator.price_is_acceptable(price):
                    log.info("skip %s: price %.0f below floor", scored.job.uid, price or 0)
                    message = None

                msg_id: int | None = None
                if message is not None:
                    report.messages_generated += 1
                    # Синхронизируем черновик с карточкой: он может быть уже в БД
                    # (повторный проход), иначе сохраняется впервые.
                    store.set_draft(scored.job.uid, message.text)
                    msg_id = store.save_message(message)
                    result = sender.send(message, scored.job, message_id=msg_id)
                    if result.ok and (config.runtime.dry_run or result.delivered):
                        report.messages_sent += 1
                    else:
                        report.messages_rejected += 1
                        log.warning("send blocked for %s: %s", scored.job.uid, result.reason)
                        # Отклонённый черновик не должен предлагаться к одобрению.
                        message = None
                    if analysis and analysis.need_summary_ru:
                        log.info("  %s | %s", scored.job.platform, analysis.need_summary_ru[:120])

                delivered_any = False
                if telegram_bot is not None:
                    job = scored.job
                    wanted = wanted_by_uid.get(job.uid, [])
                    if wanted:
                        delivered_ids = await telegram_bot.deliver_job(
                            wanted,
                            scored,
                            message_text=message.text if message is not None else None,
                            price=price,
                            strategy=strategy,
                            analysis=analysis,
                            message_id=msg_id if message is not None else None,
                        )
                        for chat_id in delivered_ids:
                            store.mark_delivered_to(chat_id, job.uid)
                        delivered_any = bool(delivered_ids)
                    if not delivered_any and wanted:
                        # Бот не работает: не помечаем показанным, повторим.
                        return

                store.mark_notified(scored.job.uid)
                announced += 1

        if candidates:
            # Ограничение из /send drafts защищает от лавины LLM-вызовов, но
            # не теряет заказы: не показанные остаются без отметки и попадут
            # в следующий проход уже с готовым письмом.
            limit = (
                max(1, config.messaging.max_messages_per_cycle)
                if generate_messages
                else len(candidates)
            )
            await asyncio.gather(*(handle(scored) for scored in candidates[:limit]))

        report.jobs_announced = announced

        # Сводка нужна только когда в проходе реально появился новый показанный
        # заказ. Пустые проходы обновляют статистику молча.
        if telegram_bot and announced:
            await telegram_bot.notify_cycle_complete(
                jobs_collected=len(unique),
                jobs_new=report.jobs_new,
                jobs_eligible=announced,
                messages_generated=report.messages_generated,
            )

        report.finished_at = datetime.now(UTC)
        store.save_cycle(
            report.model_dump_json(),
            report.started_at.isoformat(),
            report.finished_at.isoformat(),
            report.dry_run,
        )
        await ctx.http.close()
        if ctx.browser is not None:
            await ctx.browser.close()
        return report
    finally:
        if owns_store:
            store.close()


async def run_trial(
    config: AppConfig,
    secrets: Secrets,
    *,
    minutes: int = 30,
    interval_seconds: int = 600,
    platform_names: list[str] | None = None,
) -> list[CycleReport]:
    """Ограниченный наблюдательный запуск.

    Никогда не отправляет: на всё время принудительный dry-run.
    """
    original = config.runtime.dry_run
    config.runtime.dry_run = True
    reports: list[CycleReport] = []
    deadline = datetime.now(UTC).timestamp() + minutes * 60
    try:
        while datetime.now(UTC).timestamp() < deadline:
            report = await run_cycle(config, secrets, platform_names=platform_names)
            reports.append(report)
            remaining = deadline - datetime.now(UTC).timestamp()
            if remaining <= 0:
                break
            await asyncio.sleep(min(interval_seconds, remaining))
    finally:
        config.runtime.dry_run = original
    return reports
