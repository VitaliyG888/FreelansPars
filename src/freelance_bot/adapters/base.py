"""Контракт адаптера и общие помощники разбора/загрузки."""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from ..config import NetworkConfig, PlatformConfig, RuntimeConfig
from ..models import Outcome, PlatformRunResult, RawJob, TransportKind
from ..net.browser import BrowserFetcher
from ..net.robots import RobotsCache
from ..net.stealth import FetchResult, StealthHttpClient
from ..order_links import resolve_order_url


@dataclass
class AdapterContext:
    http: StealthHttpClient
    browser: BrowserFetcher | None
    robots: RobotsCache
    platform: PlatformConfig
    runtime: RuntimeConfig
    network: NetworkConfig


def text_of(node: Tag | None, default: str = "") -> str:
    if node is None:
        return default
    return " ".join(node.get_text(" ", strip=True).split())


def attr_of(node: Tag | None, name: str, default: str | None = None) -> str | None:
    if node is None:
        return default
    value = node.get(name)
    if value is None:
        return default
    return value[0] if isinstance(value, list) else str(value)


def parse_int(text: str | None) -> int | None:
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").replace(" ", "")
    m = re.search(r"\d+", cleaned)
    return int(m.group(0)) if m else None


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


class BaseAdapter(ABC):
    platform: str = ""
    domain: str = ""
    transport: TransportKind = TransportKind.HTTP
    # Необязательный CSS-селектор, который должен появиться, чтобы страница считалась отрисованной.
    wait_selector: str | None = None
    # Необязательная страница для первого визита, чтобы браузер получил обычные cookies.
    prewarm_path: str | None = None

    def __init__(self, cfg: PlatformConfig) -> None:
        self.cfg = cfg
        self.listing_url = cfg.listing_url
        self.transport = TransportKind(cfg.transport)
        if not self.domain:
            self.domain = urlsplit(self.listing_url).netloc

    # ----------------------------------------------------------------- разбор
    @abstractmethod
    def parse(self, html: str, base_url: str) -> list[RawJob]:
        """Извлекает сырые заказы со страницы листинга. Никогда не падает на пустом вводе.
        """

    def page_url(self, page: int) -> str:
        """Переопределение для источников с пагинацией."""
        return self.listing_url

    def build_order_url(
        self,
        external_id: str,
        *,
        fallback: str | None = None,
    ) -> str | None:
        """Ссылка на карточку заказа: URL со страницы → шаблон из order_links.

        Метод намеренно не ходит в сеть и не знает деталей конкретной биржи:
        площадочные шаблоны лежат в :mod:`freelance_bot.order_links`.
        """
        return resolve_order_url(self.platform, external_id, fallback)

    def _attach_order_urls(self, jobs: list[RawJob]) -> None:
        """Заполняет ``order_url`` после разбора, не меняя логику адаптеров."""
        for job in jobs:
            if job.order_url is None:
                job.order_url = self.build_order_url(job.external_id, fallback=job.url)

    # ----------------------------------------------------------------- загрузка
    async def fetch(self, ctx: AdapterContext) -> tuple[list[RawJob], PlatformRunResult]:
        started = time.monotonic()
        result = PlatformRunResult(
            platform=self.platform, transport=self.transport, outcome=Outcome.OK
        )

        if self.cfg.requires_proxy and not ctx.http.proxies.enabled:
            result.outcome = Outcome.DISABLED
            result.error = (
                "requires RU residential proxy: set RU_PROXY_URLS and restart the service"
            )
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return [], result

        allowed, reason = await ctx.robots.allowed(self.listing_url)
        if not allowed:
            result.outcome = Outcome.DISABLED
            result.error = reason
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return [], result

        collected: list[RawJob] = []
        pages = max(1, getattr(self.cfg, "max_pages", 1))
        last: FetchResult | None = None

        for page in range(1, pages + 1):
            url = self.page_url(page)
            fetched = await self._fetch_page(ctx, url)
            last = fetched
            result.attempts += fetched.attempts
            result.http_status = fetched.status
            result.used_proxy = fetched.proxy

            if fetched.outcome in (Outcome.OK, Outcome.EMPTY):
                try:
                    jobs = self.parse(fetched.text, fetched.url or url)
                except Exception as e:  # noqa: BLE001 - a layout change must not kill the cycle
                    result.outcome = Outcome.PARSE_ERROR
                    result.error = f"{type(e).__name__}: {e}"[:300]
                    break
                self._attach_order_urls(jobs)
                collected.extend(jobs)
                if not jobs:
                    break
                if len(collected) >= ctx.runtime.max_jobs_per_platform:
                    break
                continue

            # Challenge/блокировка: одна попытка через браузер, если её ещё не было.
            if (
                fetched.outcome in (Outcome.CHALLENGE, Outcome.BLOCKED)
                and self.transport == TransportKind.HTTP
            ):
                escalated = await self._escalate(ctx, url)
                if escalated is not None:
                    result.attempts += escalated.attempts
                    result.http_status = escalated.status
                    result.challenge = escalated.challenge
                    last = escalated
                    if escalated.outcome in (Outcome.OK, Outcome.EMPTY):
                        try:
                            jobs = self.parse(escalated.text, escalated.url or url)
                        except Exception as e:  # noqa: BLE001
                            result.outcome = Outcome.PARSE_ERROR
                            result.error = f"{type(e).__name__}: {e}"[:300]
                            break
                        self._attach_order_urls(jobs)
                        collected.extend(jobs)
                        continue
            break

        if last is not None:
            result.challenge = result.challenge or last.challenge
            if collected:
                result.outcome = Outcome.OK
            elif last.outcome in (Outcome.OK, Outcome.EMPTY):
                result.outcome = Outcome.EMPTY
            else:
                result.outcome = last.outcome
                result.error = result.error or last.error
            result.error = result.error or last.error

        result.jobs_found = len(collected)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return collected, result

    async def _fetch_page(self, ctx: AdapterContext, url: str) -> FetchResult:
        if self.transport == TransportKind.BROWSER:
            if ctx.browser is None:
                return FetchResult(
                    outcome=Outcome.DISABLED,
                    status=None,
                    url=url,
                    error="browser transport requires playwright",
                )
            prewarm = None
            if self.prewarm_path:
                prewarm = urljoin(url, self.prewarm_path)
            return await ctx.browser.fetch(
                url,
                domain=self.domain,
                wait_selector=self.wait_selector,
                prewarm_url=prewarm,
            )
        # «Прогрев» HTTP-сессии: первый запрос цикла идёт на prewarm-страницу
        # (обычно главную), чтобы сессия получила cookies домена до запроса
        # к листингу — как человек, открывающий сайт с главной. Referer
        # листинга указывает на неё: запрос выглядит переходом по ссылке.
        # Стратегия прокси площадки передаётся в оба запроса: прогрев и
        # листинг должны пойти через один IP.
        proxy_strategy = self.cfg.proxy_strategy or None
        referer = urljoin(url, "/")
        if self.prewarm_path and ctx.http.needs_warmup(self.domain):
            prewarm_url = urljoin(url, self.prewarm_path)
            await ctx.http.warmup(
                prewarm_url,
                domain=self.domain,
                impersonate=self.cfg.impersonate or None,
                proxy_strategy=proxy_strategy,
            )
            referer = prewarm_url
        return await ctx.http.get(
            url,
            domain=self.domain,
            referer=referer,
            allow_relay=self.cfg.relay_fallback,
            impersonate=self.cfg.impersonate or None,
            timeout_seconds=self.cfg.direct_timeout_seconds or None,
            proxy_strategy=proxy_strategy,
        )

    async def _escalate(self, ctx: AdapterContext, url: str) -> FetchResult | None:
        if ctx.browser is None:
            return None
        prewarm = urljoin(url, self.prewarm_path) if self.prewarm_path else None
        return await ctx.browser.fetch(
            url,
            domain=self.domain,
            wait_selector=self.wait_selector,
            prewarm_url=prewarm,
        )

    def abs_url(self, href: str, base: str) -> str:
        return urljoin(base, href)
