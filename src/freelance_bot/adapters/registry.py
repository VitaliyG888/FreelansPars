"""Реестр адаптеров и построение контекста."""

from __future__ import annotations

from ..config import AppConfig, PlatformConfig, Secrets
from ..net import BrowserFetcher, ProxyPool, RobotsCache, StealthHttpClient, build_solver
from .avito_ru import AvitoRuAdapter
from .base import AdapterContext, BaseAdapter
from .fl_ru import FlRuAdapter
from .freelance_ru import FreelanceRuAdapter
from .freelancehunt_com import FreelancehuntAdapter
from .freelancejob_ru import FreelanceJobRuAdapter
from .kwork_ru import KworkRuAdapter
from .pchel_net import PchelNetAdapter
from .weblancer_net import WeblancerNetAdapter

ADAPTERS: dict[str, type[BaseAdapter]] = {
    FlRuAdapter.platform: FlRuAdapter,
    FreelanceRuAdapter.platform: FreelanceRuAdapter,
    FreelanceJobRuAdapter.platform: FreelanceJobRuAdapter,
    PchelNetAdapter.platform: PchelNetAdapter,
    KworkRuAdapter.platform: KworkRuAdapter,
    WeblancerNetAdapter.platform: WeblancerNetAdapter,
    FreelancehuntAdapter.platform: FreelancehuntAdapter,
    AvitoRuAdapter.platform: AvitoRuAdapter,
}

#: URL листинга по умолчанию для платформ, добавленных из Telegram-бота.
DEFAULT_LISTING_URLS: dict[str, str] = {
    "fl_ru": "https://www.fl.ru/projects/",
    "freelance_ru": "https://freelance.ru/project/search",
    "freelancejob_ru": "https://freelancejob.ru/projects/",
    "pchel_net": "https://pchel.net/projects/",
    "kwork_ru": "https://kwork.ru/projects",
    "weblancer_net": "https://www.weblancer.net/jobs/",
    "freelancehunt_com": "https://freelancehunt.com/projects",
    "avito_ru": "https://www.avito.ru/all/predlozheniya_uslug",
}


def build_adapters(config: AppConfig) -> list[BaseAdapter]:
    """Создаёт экземпляр каждого включённого адаптера из реестра."""
    built: list[BaseAdapter] = []
    for name, cfg in config.enabled_platforms().items():
        cls = ADAPTERS.get(name)
        if cls is None:
            continue
        built.append(cls(cfg))
    return built


def build_context(
    config: AppConfig,
    secrets: Secrets,
    *,
    with_browser: bool = True,
) -> AdapterContext:
    proxies = ProxyPool(
        urls=secrets.proxies,
        strategy=config.network.proxy_strategy,
        cooldown_seconds=config.network.proxy_cooldown_seconds,
    )
    http = StealthHttpClient(
        config.runtime,
        config.network,
        proxies,
        relay_api_key=secrets.jina_api_key,
    )

    async def _robots_fetch(url: str) -> tuple[int, str]:
        from urllib.parse import urlsplit

        domain = urlsplit(url).netloc
        # robots.txt не стоит ждать полный таймаут страницы: если он
        # недоступен, RobotsCache корректно трактует это как «правил нет».
        res = await http.get(
            url,
            domain=domain,
            allow_retry=False,
            timeout_seconds=min(8, config.runtime.request_timeout_seconds),
        )
        return res.status or 0, res.text

    robots = RobotsCache(
        fetcher=_robots_fetch,
        user_agent=config.runtime.user_agent,
        enabled=config.network.respect_robots_txt,
    )

    solver = None
    if config.captcha.enabled:
        key = (
            secrets.capsolver_api_key
            if config.captcha.provider == "capsolver"
            else secrets.twocaptcha_api_key
        )
        solver = build_solver(
            config.captcha.provider, key, timeout_seconds=config.captcha.timeout_seconds
        )

    browser = None
    if with_browser and config.network.browser.enabled and config.runtime.dry_run is not None:
        browser = BrowserFetcher(
            config.runtime,
            config.network,
            solver=solver,
            proxies=proxies,
        )

    return AdapterContext(
        http=http,
        browser=browser,
        robots=robots,
        platform=PlatformConfig(listing_url=""),
        runtime=config.runtime,
        network=config.network,
    )
