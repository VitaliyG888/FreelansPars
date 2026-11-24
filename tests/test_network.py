"""Сетевой слой: ротация прокси, троттлинг, задержки, классификация challenge, robots.
"""

from __future__ import annotations

import asyncio

import pytest

from freelance_bot.config import BackoffConfig, NetworkConfig, RateConfig, RuntimeConfig
from freelance_bot.models import Outcome
from freelance_bot.net.proxies import ProxyPool, playwright_proxy
from freelance_bot.net.ratelimit import AsyncTokenBucket, DomainLimiter, backoff_delay
from freelance_bot.net.robots import RobotsCache
from freelance_bot.net.stealth import StealthHttpClient, classify_response


# --------------------------------------------------------------------- прокси
def test_proxy_round_robin() -> None:
    pool = ProxyPool(urls=["http://a:1", "http://b:2", "http://c:3"])
    seen = [pool.acquire().url for _ in range(6)]
    assert seen == [
        "http://a:1",
        "http://b:2",
        "http://c:3",
        "http://a:1",
        "http://b:2",
        "http://c:3",
    ]


def test_empty_pool_returns_none() -> None:
    assert ProxyPool().acquire() is None
    assert ProxyPool().enabled is False


def test_failed_proxy_is_skipped_until_cooldown() -> None:
    pool = ProxyPool(urls=["http://a:1", "http://b:2"], cooldown_seconds=60)
    first = pool.acquire()
    pool.report_failure(first.url)
    nxt = pool.acquire()
    assert nxt.url == "http://b:2"
    # 'a' остывает; доступен только 'b'.
    again = pool.acquire()
    assert again.url == "http://b:2"


def test_all_proxies_cooling_returns_none() -> None:
    pool = ProxyPool(urls=["http://a:1"], cooldown_seconds=600)
    p = pool.acquire()
    pool.report_failure(p.url)
    assert pool.acquire() is None


def test_proxy_credentials_are_redacted() -> None:
    pool = ProxyPool(urls=["http://user:secret@1.2.3.4:8080"])
    assert "secret" not in pool.acquire().redact()
    assert "1.2.3.4:8080" in pool.acquire().redact()


# --------------------------------------------------------- sticky-привязка
def test_sticky_keeps_domain_assignment() -> None:
    """Домен закрепляется за одним прокси: куки и TLS согласованы с IP."""
    pool = ProxyPool(urls=["http://a:1", "http://b:2"], strategy="sticky")
    first = pool.acquire("kwork.ru", strategy="sticky")
    # Три вызова подряд для одного домена — один и тот же IP.
    for _ in range(3):
        assert pool.acquire("kwork.ru", strategy="sticky") is first
    # Другой домен получает другой прокси.
    other = pool.acquire("fl.ru", strategy="sticky")
    assert other is not None
    assert other.url != first.url
    assert pool.acquire("fl.ru", strategy="sticky") is other


def test_sticky_reassigns_when_assigned_cools() -> None:
    """Остывший после ошибки прокси заменяется следующим доступным."""
    pool = ProxyPool(urls=["http://a:1", "http://b:2"], strategy="sticky", cooldown_seconds=600)
    assigned = pool.acquire("kwork.ru", strategy="sticky")
    assert assigned is not None
    pool.report_failure(assigned.url)
    fresh = pool.acquire("kwork.ru", strategy="sticky")
    assert fresh is not None
    assert fresh.url != assigned.url
    # Новая привязка держится стабильно.
    assert pool.acquire("kwork.ru", strategy="sticky") is fresh


def test_sticky_all_cooling_falls_back_to_assigned() -> None:
    """Когда остыли все, sticky всё равно пытается через закреплённый."""
    pool = ProxyPool(urls=["http://a:1"], strategy="sticky", cooldown_seconds=600)
    assigned = pool.acquire("kwork.ru", strategy="sticky")
    assert assigned is not None
    pool.report_failure(assigned.url)
    fallback = pool.acquire("kwork.ru", strategy="sticky")
    assert fallback is not None
    assert fallback.url == assigned.url


def test_strategy_none_ignores_pool() -> None:
    """Стратегия none — прямой запрос, даже если пул заполнен."""
    pool = ProxyPool(urls=["http://a:1", "http://b:2"], strategy="none")
    assert pool.acquire("kwork.ru", strategy="none") is None
    assert pool.acquire() is None


@pytest.mark.asyncio
async def test_get_proxy_change_resets_prewarm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Смена IP домена обнуляет прогрев: куки от старого IP не отправляются."""
    from types import SimpleNamespace

    from freelance_bot.config import BackoffConfig, NetworkConfig, RateConfig

    network = NetworkConfig(
        default_rate=RateConfig(requests_per_minute=6000, burst=5),
        backoff=BackoffConfig(jitter=False),
    )
    pool = ProxyPool(urls=["http://a:1", "http://b:2"], strategy="sticky")
    client = StealthHttpClient(RuntimeConfig(), network, pool)

    class OkSession:
        async def get(self, url: str, **kwargs: object):  # noqa: ANN003
            return SimpleNamespace(
                status_code=200,
                text="<html><body>" + "page " * 40 + "</body></html>",
                headers={},
                url=url,
            )

    async def fake_session(profile: str) -> OkSession:
        return OkSession()

    monkeypatch.setattr(client, "_session", fake_session)

    # Домен уже прогрет; первый sticky-запрос закрепляет a:1 — прогрев живёт.
    client._prewarmed.add("k.test")
    await client.get("https://k.test/", domain="k.test", proxy_strategy="sticky")
    assert client._domain_proxy["k.test"] == "http://a:1"
    assert client.needs_warmup("k.test") is False

    # a:1 остыл → закрепляется b:2 → куки от старого IP недействительны:
    # домен будет прогрет заново.
    client.proxies.report_failure("http://a:1")
    await client.get("https://k.test/", domain="k.test", proxy_strategy="sticky")
    assert client._domain_proxy["k.test"] == "http://b:2"
    assert client.needs_warmup("k.test") is True


def test_success_resets_failure_count() -> None:
    pool = ProxyPool(urls=["http://a:1"], cooldown_seconds=0)
    p = pool.acquire()
    pool.report_failure(p.url, cooldown=False)
    pool.report_success(p.url)
    assert pool.healthy_snapshot()[0]["failures"] == 0


def test_playwright_proxy_splits_credentials() -> None:
    proxy = playwright_proxy("http://user:pa%40ss@proxy.test:8080")
    assert proxy == {
        "server": "http://proxy.test:8080",
        "username": "user",
        "password": "pa@ss",
    }
    assert playwright_proxy("socks5://127.0.0.1:1080") == {"server": "socks5://127.0.0.1:1080"}


# --------------------------------------------------------------------- лимитер
@pytest.mark.asyncio
async def test_token_bucket_enforces_rate() -> None:
    bucket = AsyncTokenBucket(rate_per_minute=600, burst=1)  # 10/сек
    loop = asyncio.get_event_loop()
    start = loop.time()
    for _ in range(3):
        await bucket.acquire()
    elapsed = loop.time() - start
    assert elapsed >= 0.15  # 2 дополнительных токена при 10/сек


@pytest.mark.asyncio
async def test_bucket_allows_burst_immediately() -> None:
    bucket = AsyncTokenBucket(rate_per_minute=1, burst=3)
    loop = asyncio.get_event_loop()
    start = loop.time()
    for _ in range(3):
        await bucket.acquire()
    assert loop.time() - start < 0.2


@pytest.mark.asyncio
async def test_domain_limiter_uses_per_domain_config() -> None:
    limiter = DomainLimiter(
        RateConfig(requests_per_minute=6000, burst=5),
        {"slow.test": RateConfig(requests_per_minute=600, burst=1)},
        jitter=False,
    )
    loop = asyncio.get_event_loop()
    start = loop.time()
    await limiter.wait("slow.test")
    await limiter.wait("slow.test")
    assert loop.time() - start >= 0.09


def test_backoff_is_capped_and_jittered() -> None:
    cfg = BackoffConfig(base_seconds=3, max_seconds=20, jitter=False, max_retries=5)
    assert backoff_delay(0, cfg) == 3
    assert backoff_delay(1, cfg) == 6
    assert backoff_delay(10, cfg) == 20  # с потолком

    cfg_jitter = BackoffConfig(base_seconds=3, max_seconds=20, jitter=True)
    samples = [backoff_delay(2, cfg_jitter) for _ in range(50)]
    assert all(0 <= s <= 12 for s in samples)
    assert len(set(samples)) > 1


# ---------------------------------------------------------------- классификация
@pytest.mark.parametrize(
    "status,headers,body,outcome,challenge",
    [
        (
            200,
            {},
            "<html><body>" + "нормальная страница " * 60 + "</body></html>",
            Outcome.OK,
            None,
        ),
        (
            200,
            {},
            "<html><title>Just a moment...</title><body>cf_chl_opt</body></html>",
            Outcome.CHALLENGE,
            "cloudflare",
        ),
        (
            403,
            {"server": "cloudflare"},
            "<html>Just a moment...</html>",
            Outcome.BLOCKED,
            "cloudflare",
        ),
        (403, {"server": "ddos-guard"}, "<html>DDoS-Guard</html>", Outcome.BLOCKED, "ddos-guard"),
        (439, {"server": "QRATOR"}, "<html>Доступ ограничен</html>", Outcome.BLOCKED, "qrator"),
        (200, {}, "<html>smartcaptcha widget</html>", Outcome.CHALLENGE, "yandex-smartcaptcha"),
        (429, {}, "<html>slow down</html>", Outcome.BLOCKED, "rate-limited"),
        (
            503,
            {"server": "nginx"},
            "<html>upstream down</html>",
            Outcome.NETWORK_ERROR,
            "upstream-error",
        ),
        (200, {}, "<html><body><500 chars>", Outcome.EMPTY, None),
    ],
)
def test_classify_response(status, headers, body, outcome, challenge) -> None:
    got_outcome, got_challenge = classify_response(status, headers, body)
    assert got_outcome == outcome
    assert got_challenge == challenge


def test_login_wall_detected() -> None:
    html = (
        '<html><body><form><input name="password">'
        "<button>Войти в аккаунт</button></form></body></html>"
    )
    outcome, challenge = classify_response(200, {}, html)
    assert outcome == Outcome.LOGIN_WALL
    assert challenge == "login-required"


def test_large_page_with_captcha_mention_is_not_challenge() -> None:
    """Полноценная страница листинга, упоминающая капчу в скриптах, — это OK.

    Живой kwork подключает Yandex SmartCaptcha для своих форм: упоминание
    в теле 270-КБ страницы не делает её challenge-заглушкой.
    """
    filler = "var goods = 1; " * 9000  # ~126 КБ, больше порога 50 КБ
    html = (
        "<html><head><script src='https://yandexcloud.net/smartcaptcha.js'></script></head>"
        f"<body><div class='want-card'>{filler}</div></body></html>"
    )
    outcome, challenge = classify_response(200, {}, html)
    assert outcome == Outcome.OK
    assert challenge is None


def test_small_challenge_stub_is_still_detected() -> None:
    """Маленькая заглушка с сигнатурой капчи по-прежнему CHALLENGE."""
    stub = "<html><body><div id='smartcaptcha'></div>подождите</body></html>"
    outcome, challenge = classify_response(200, {}, stub)
    assert outcome == Outcome.CHALLENGE
    assert challenge == "yandex-smartcaptcha"


def test_real_fixtures_classify_as_ok() -> None:
    from conftest import FIXTURE_MAP, load_html

    for platform in FIXTURE_MAP:
        outcome, _ = classify_response(200, {}, load_html(platform))
        assert outcome == Outcome.OK, platform


# ------------------------------------------------------------------------ robots
@pytest.mark.asyncio
async def test_robots_disallow_is_enforced() -> None:
    async def fetcher(url: str):
        return 200, "User-agent: *\nDisallow: /private\nAllow: /\n"

    cache = RobotsCache(fetcher=fetcher)
    allowed, reason = await cache.allowed("https://example.test/private/list")
    assert allowed is False
    assert "disallows" in reason

    allowed, _ = await cache.allowed("https://example.test/projects/")
    assert allowed is True


@pytest.mark.asyncio
async def test_robots_missing_fails_open() -> None:
    async def fetcher(url: str):
        return 404, "Not found"

    cache = RobotsCache(fetcher=fetcher)
    allowed, reason = await cache.allowed("https://example.test/projects/")
    assert allowed is True
    assert reason == "no robots.txt"


@pytest.mark.asyncio
async def test_robots_cached_across_calls() -> None:
    calls = {"n": 0}

    async def fetcher(url: str):
        calls["n"] += 1
        return 200, "User-agent: *\nAllow: /\n"

    cache = RobotsCache(fetcher=fetcher)
    await cache.allowed("https://example.test/a")
    await cache.allowed("https://example.test/b")
    assert calls["n"] == 1


# ------------------------------------------------------------------------ relay
def test_relay_target_substitutes_raw_url() -> None:
    client = StealthHttpClient(RuntimeConfig(), NetworkConfig())
    assert (
        client.relay_target("https://www.fl.ru/projects/")
        == "https://r.jina.ai/https://www.fl.ru/projects/"
    )


def test_relay_target_supports_encoded_placeholder() -> None:
    network = NetworkConfig()
    network.relay.url_template = "https://reader.test/{url_encoded}"
    client = StealthHttpClient(RuntimeConfig(), network)
    assert (
        client.relay_target("https://www.fl.ru/projects/?a=1&b=2")
        == "https://reader.test/https%3A%2F%2Fwww.fl.ru%2Fprojects%2F%3Fa%3D1%26b%3D2"
    )


@pytest.mark.asyncio
async def test_network_error_falls_back_to_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    from freelance_bot.net.stealth import FetchResult

    client = StealthHttpClient(RuntimeConfig(), NetworkConfig())

    class FailingSession:
        async def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise TimeoutError("direct blocked")

    async def fake_session(profile: str) -> FailingSession:
        return FailingSession()

    sentinel = FetchResult(outcome=Outcome.OK, status=200, text="relay html", url="https://x.test")

    async def fake_relay(url, referer, extra):  # noqa: ANN001
        return sentinel

    monkeypatch.setattr(client, "_session", fake_session)
    monkeypatch.setattr(client, "_relay_get", fake_relay)
    result = await client.get(
        "https://www.fl.ru/projects/",
        domain="www.fl.ru",
        allow_relay=True,
        timeout_seconds=0.01,
    )
    assert result is sentinel


# ------------------------------------------------------ human-пауза по доменам
@pytest.mark.asyncio
async def test_domain_limiter_applies_human_pause() -> None:
    limiter = DomainLimiter(
        RateConfig(requests_per_minute=6000, burst=5),
        {
            "kwork.test": RateConfig(
                requests_per_minute=6000,
                burst=5,
                pause_min_seconds=0.05,
                pause_max_seconds=0.1,
            )
        },
        jitter=False,
    )
    loop = asyncio.get_event_loop()
    start = loop.time()
    await limiter.wait("kwork.test")
    elapsed = loop.time() - start
    # Пауза домена отработала поверх мгновенного вёдра.
    assert 0.04 <= elapsed <= 0.5


@pytest.mark.asyncio
async def test_domain_limiter_without_pause_is_fast() -> None:
    limiter = DomainLimiter(
        RateConfig(requests_per_minute=6000, burst=5),
        {"kwork.test": RateConfig(requests_per_minute=6000, burst=5)},
        jitter=False,
    )
    loop = asyncio.get_event_loop()
    start = loop.time()
    await limiter.wait("kwork.test")
    assert loop.time() - start < 0.04


@pytest.mark.asyncio
async def test_domain_limiter_pause_bounds_are_ordered() -> None:
    """Перепутанные границы (min > max) не ломают лимитер."""
    limiter = DomainLimiter(
        RateConfig(requests_per_minute=6000, burst=5),
        {
            "x.test": RateConfig(
                requests_per_minute=6000, burst=5, pause_min_seconds=0.1, pause_max_seconds=0.05
            )
        },
        jitter=False,
    )
    loop = asyncio.get_event_loop()
    start = loop.time()
    await limiter.wait("x.test")
    assert loop.time() - start >= 0.04


# ------------------------------------------------- заголовки по TLS-профилям
def test_headers_match_impersonation_profile() -> None:
    client = StealthHttpClient(RuntimeConfig(), NetworkConfig())

    chrome124 = client._headers("chrome124")
    assert "Chrome/124.0.0.0" in chrome124["User-Agent"]
    assert "Chrome/131" not in chrome124["User-Agent"]
    assert 'v="124"' in chrome124["Sec-CH-UA"]
    assert chrome124["Sec-CH-UA-Platform"] == '"Windows"'

    safari = client._headers("safari17_0")
    assert "Safari" in safari["User-Agent"]
    assert "Macintosh" in safari["User-Agent"]
    # Safari client hints не отправляет вовсе.
    assert "Sec-CH-UA" not in safari
    assert "Sec-CH-UA-Platform" not in safari

    # Основной профиль уважает пользовательский UA из конфига.
    chrome131 = client._headers("chrome131")
    assert chrome131["User-Agent"] == client.runtime.user_agent
    assert 'v="131"' in chrome131["Sec-CH-UA"]

    # Без referer — переход по адресу; с referer — навигация внутри сайта.
    assert client._headers("chrome131")["Sec-Fetch-Site"] == "none"
    navigated = client._headers("chrome131", referer="https://kwork.ru/")
    assert navigated["Sec-Fetch-Site"] == "same-origin"
    assert navigated["Referer"] == "https://kwork.ru/"


def test_headers_accept_language_from_config() -> None:
    runtime = RuntimeConfig(accept_language="en-US,en;q=0.9")
    client = StealthHttpClient(runtime, NetworkConfig())
    assert client._headers("chrome124")["Accept-Language"] == "en-US,en;q=0.9"


# ----------------------------------------------------------- ретраи 429 / 5xx
def _fast_backoff_client() -> StealthHttpClient:
    network = NetworkConfig()
    network.default_rate = RateConfig(requests_per_minute=6000, burst=5)
    network.backoff = BackoffConfig(
        base_seconds=0.01, max_seconds=0.05, jitter=False, max_retries=3
    )
    return StealthHttpClient(RuntimeConfig(), network)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_status", [429, 500, 503])
async def test_get_retries_bad_status_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, bad_status: int
) -> None:
    from types import SimpleNamespace

    client = _fast_backoff_client()

    class FlakySession:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get(self, url: str, **kwargs: object):  # noqa: ANN003
            self.calls.append(url)
            status = bad_status if len(self.calls) == 1 else 200
            return SimpleNamespace(
                status_code=status,
                text="<html><body>" + "page content " * 40 + "</body></html>",
                headers={},
                url=url,
            )

    session = FlakySession()

    async def fake_session(profile: str) -> FlakySession:
        return session

    monkeypatch.setattr(client, "_session", fake_session)
    result = await client.get("https://kwork.ru/projects", domain="kwork.ru")
    assert result.ok
    assert result.status == 200
    assert result.attempts == 2
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_get_exhausts_5xx_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    client = _fast_backoff_client()

    class DownSession:
        async def get(self, url: str, **kwargs: object):
            return SimpleNamespace(status_code=503, text="upstream down", headers={}, url=url)

    async def fake_session(profile: str) -> DownSession:
        return DownSession()

    monkeypatch.setattr(client, "_session", fake_session)
    result = await client.get("https://kwork.ru/projects", domain="kwork.ru")
    assert result.attempts == client.network.backoff.max_retries
    assert result.outcome == Outcome.NETWORK_ERROR
    assert result.challenge == "upstream-error"


# ------------------------------------------------------------------- прогрев
@pytest.mark.asyncio
async def test_warmup_runs_once_per_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    from freelance_bot.net.stealth import FetchResult

    client = StealthHttpClient(RuntimeConfig(), NetworkConfig())
    calls: list[tuple[str, dict]] = []

    async def fake_get(url: str, **kwargs):
        calls.append((url, kwargs))
        return FetchResult(outcome=Outcome.OK, status=200, text="main page", url=url)

    monkeypatch.setattr(client, "get", fake_get)
    assert client.needs_warmup("kwork.ru") is True

    first = await client.warmup("https://kwork.ru/", domain="kwork.ru")
    second = await client.warmup("https://kwork.ru/", domain="kwork.ru")

    assert first is not None and first.ok
    assert second is None  # второй прогрев — no-op
    assert [url for url, _ in calls] == ["https://kwork.ru/"]
    # Прогрев — дешёвая одноразовая попытка без ретраев и relay.
    assert calls[0][1]["allow_retry"] is False
    assert calls[0][1]["allow_relay"] is False
    assert client.needs_warmup("kwork.ru") is False
    assert client.needs_warmup("other.test") is True


@pytest.mark.asyncio
async def test_http_fetch_prewarms_session_then_lists() -> None:
    """Перед листингом HTTP-адаптер заходит на prewarm-страницу (главную)."""
    from types import SimpleNamespace

    from freelance_bot.adapters.kwork_ru import KworkRuAdapter
    from freelance_bot.config import PlatformConfig
    from freelance_bot.net.robots import RobotsCache
    from freelance_bot.net.stealth import FetchResult, StealthHttpClient

    class RecordingHTTP(StealthHttpClient):
        def __init__(self) -> None:
            network = NetworkConfig(
                default_rate=RateConfig(requests_per_minute=6000, burst=5),
                backoff=BackoffConfig(jitter=False),
            )
            super().__init__(RuntimeConfig(), network)
            self.calls: list[tuple[str, dict]] = []

        async def get(self, url: str, **kwargs: object):
            self.calls.append((url, kwargs))
            return FetchResult(outcome=Outcome.OK, status=200, text="<html>kwork</html>", url=url)

    async def allow_all(url: str):
        return 200, "User-agent: *\nAllow: /\n"

    adapter = KworkRuAdapter(
        PlatformConfig(listing_url="https://kwork.ru/projects", transport="http")
    )
    http = RecordingHTTP()
    ctx = SimpleNamespace(
        http=http,
        browser=None,
        robots=RobotsCache(fetcher=allow_all, enabled=False),
        platform=adapter.cfg,
        runtime=http.runtime,
        network=http.network,
    )
    jobs, result = await adapter.fetch(ctx)  # type: ignore[arg-type]
    assert jobs == []
    # Сначала прогрев главной, затем листинг с referer на главную.
    assert [url for url, _ in http.calls] == ["https://kwork.ru/", "https://kwork.ru/projects"]
    assert http.calls[1][1]["referer"] == "https://kwork.ru/"
    assert result.outcome == Outcome.EMPTY

    # Повторный цикл: сессия уже прогрета, повторного захода на главную нет.
    await adapter.fetch(ctx)  # type: ignore[arg-type]
    assert sum(1 for url, _ in http.calls if url == "https://kwork.ru/") == 1
