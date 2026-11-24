"""Stealth HTTP-слой: имитация TLS, ротация прокси, классификация challenge.

Устройство: каждый запрос идёт через :class:`StealthHttpClient`. Клиент никогда
не падает на антибот-ответе; он классифицирует его, чтобы конвейер мог осознанно
эскалировать в браузер или решатель CAPTCHA вместо слепых повторов.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from curl_cffi.requests import AsyncSession

from ..config import NetworkConfig, RuntimeConfig
from ..models import Outcome
from .proxies import ProxyPool
from .ratelimit import DomainLimiter, backoff_delay

# Актуальные отпечатки Chromium/Safari, поддерживаемые curl_cffi.
# chrome131 идёт первым: он проходит Cloudflare на части площадок, где
# chrome120/124 получают challenge.
IMPERSONATION_PROFILES = ("chrome131", "chrome124", "chrome120", "safari17_0", "edge101")

# Заголовки, согласованные с TLS-отпечатком. curl_cffi `impersonate=` задаёт
# TLS + HTTP/2 и дефолтные заголовки, но явный `headers=` ПЕРЕЗАПИСЫВАЕТ их:
# Chrome 124 с client-hint'ами версии 131 — мгновенная детекция. Поэтому у
# каждого профиля свой User-Agent и свой набор Sec-CH-UA. Safari client
# hints не отправляет вовсе, поэтому в его наборе их нет.
PROFILE_HEADERS: dict[str, dict[str, str]] = {
    "chrome131": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Sec-CH-UA": '"Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
    },
    "chrome124": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
    },
    "chrome120": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Sec-CH-UA": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
    },
    "safari17_0": {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        ),
    },
    "edge101": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/101.0.4951.64 Safari/537.36 Edg/101.0.1210.53"
        ),
        "Sec-CH-UA": '" Not A;Brand";v="99", "Chromium";v="101", "Microsoft Edge";v="101"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
    },
}

CHALLENGE_SIGNATURES: dict[str, tuple[str, ...]] = {
    "cloudflare": (
        "just a moment",
        "cf-mitigated",
        "cf-browser-verification",
        "__cf_chl",
        "challenge-platform",
        "attention required! | cloudflare",
    ),
    "ddos-guard": ("ddos-guard", "__ddg", "ddos guard"),
    "qrator": ("qrator", "__qrator", "доступ ограничен"),
    "yandex-smartcaptcha": ("smartcaptcha", "smart-captcha", "captcha.yandexcloud"),
    "datadome": ("datadome", "dd_cookie", "captcha-delivery.com"),
    "akamai": ("_abck", "akamai bot manager", "reference #18"),
    "variti": ("variti",),
}

LOGIN_SIGNATURES = (
    'name="password"',
    "войдите в аккаунт",
    "вход в аккаунт",
    "sign in to continue",
)

# Страница-заглушка антибота всегда маленькая (Cloudflare ~10-20 КБ, Qrator и
# «Доступ ограничен» — 2-5 КБ). Полноценные страницы листинга в разы больше и
# могут легально упоминать капчу в подключаемых скриптах (kwork грузит
# SmartCaptcha для своих форм): сигнатуры в большом теле — не challenge.
CHALLENGE_BODY_MAX_BYTES = 50_000


@dataclass
class FetchResult:
    outcome: Outcome
    status: int | None
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""
    challenge: str | None = None
    elapsed_ms: int = 0
    attempts: int = 0
    proxy: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in (Outcome.OK, Outcome.EMPTY)


def classify_response(
    status: int, headers: dict[str, str], text: str
) -> tuple[Outcome, str | None]:
    """Определяет защитный слой по статусу, заголовкам и маркерам в теле."""
    lowered = text[:200_000].lower()
    hdrs = {k.lower(): v.lower() for k, v in headers.items()}

    if status in (429, 439, 403, 503) or status >= 500:
        for name, sigs in CHALLENGE_SIGNATURES.items():
            if any(s in lowered for s in sigs) or any(s in hdrs.get("server", "") for s in sigs):
                return Outcome.BLOCKED, name
        if status == 429:
            return Outcome.BLOCKED, "rate-limited"
        if status >= 500:
            return Outcome.NETWORK_ERROR, "upstream-error"
        return Outcome.BLOCKED, "http-forbidden"

    # Для 200/3xx: сигнатуры в теле ищем только у страниц-заглушек —
    # полноценная страница с контентом капчу лишь упоминает.
    if len(lowered) < CHALLENGE_BODY_MAX_BYTES:
        for name, sigs in CHALLENGE_SIGNATURES.items():
            if any(s in lowered for s in sigs):
                return Outcome.CHALLENGE, name
    if hdrs.get("cf-mitigated") == "challenge":
        return Outcome.CHALLENGE, "cloudflare"

    if len(lowered) < 1200 and any(s in lowered for s in LOGIN_SIGNATURES):
        return Outcome.LOGIN_WALL, "login-required"

    if len(text.strip()) < 400:
        return Outcome.EMPTY, None

    return Outcome.OK, None


class StealthHttpClient:
    """Асинхронный HTTP-клиент с имитацией TLS и вежливостью по доменам."""

    def __init__(
        self,
        runtime: RuntimeConfig,
        network: NetworkConfig,
        proxies: ProxyPool | None = None,
        *,
        relay_api_key: str = "",
    ) -> None:
        self.runtime = runtime
        self.network = network
        self.proxies = proxies or ProxyPool(strategy=network.proxy_strategy)
        self.relay_api_key = relay_api_key
        self.limiter = DomainLimiter(
            network.default_rate,
            network.domains,
            jitter=network.backoff.jitter,
        )
        self._sessions: dict[str, AsyncSession] = {}
        self._session_lock = asyncio.Lock()
        # Домены, для которых «прогрев» сессии уже выполнялся (один раз на домен).
        self._prewarmed: set[str] = set()
        # Последний прокси, через который ходил домен: смена IP обнуляет прогрев.
        self._domain_proxy: dict[str, str] = {}

    async def _session(self, impersonate: str) -> AsyncSession:
        async with self._session_lock:
            if impersonate not in self._sessions:
                self._sessions[impersonate] = AsyncSession(
                    impersonate=impersonate,
                    timeout=self.runtime.request_timeout_seconds,
                )
            return self._sessions[impersonate]

    @staticmethod
    def _profile_order(preferred: str | None) -> tuple[str, ...]:
        ordered: list[str] = []
        if preferred:
            ordered.append(preferred)
        for profile in IMPERSONATION_PROFILES:
            if profile not in ordered:
                ordered.append(profile)
        return tuple(ordered)

    def _headers(self, profile: str, referer: str | None = None) -> dict[str, str]:
        # Навигационные заголовки одинаковы для всех браузеров; UA и client
        # hints берутся из таблицы профиля, чтобы совпадать с TLS-отпечатком.
        headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": self.runtime.accept_language,
            "Accept-Encoding": "gzip, deflate, br",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin" if referer else "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }
        headers.update(PROFILE_HEADERS.get(profile, PROFILE_HEADERS["chrome131"]))
        if profile == IMPERSONATION_PROFILES[0]:
            # Явный UA из конфига применяется только к основному профилю.
            # Для запасных TLS-отпечатков UA берётся из таблицы: если
            # подставить пользовательский Chrome 131 в chrome124, отпечаток
            # рассинхронизируется и антибот это заметит.
            headers["User-Agent"] = self.runtime.user_agent
        if referer:
            headers["Referer"] = referer
        return headers

    def relay_target(self, url: str) -> str:
        """Подставляет целевой URL в шаблон reader-сервиса."""
        template = self.network.relay.url_template
        if "{url_encoded}" in template:
            template = template.replace("{url_encoded}", quote(url, safe=""))
        return template.replace("{url}", url)

    def needs_warmup(self, domain: str) -> bool:
        """True, если сессию этого домена ещё не прогревали."""
        return domain not in self._prewarmed

    async def warmup(
        self,
        url: str,
        *,
        domain: str,
        impersonate: str | None = None,
        proxy_strategy: str | None = None,
    ) -> FetchResult | None:
        """«Прогрев» HTTP-сессии: первый визит на нейтральную страницу домена.

        Заход на главную страницу до запроса к целевому разделу оставляет в
        сессии настоящие cookies — антибот-системы (Qrator у kwork) охотнее
        пропускают запрос с историей визита. Куки живут в той же
        ``AsyncSession``, что и основной запрос: профиль impersonate у обоих
        одинаковый, поэтому сессия ровно одна. Прогрев выполняется один раз
        на домен; его неудача не ломает цикл — ретраит уже основной запрос.
        Стратегия прокси передаётся дальше: прогрев и листинг должны пойти
        через один и тот же IP.
        """
        if domain in self._prewarmed:
            return None
        self._prewarmed.add(domain)
        # allow_retry=False: прогрев — дешёвая одноразовая попытка; экспоненциальные
        # ретраи остаются достоянием основного запроса.
        return await self.get(
            url,
            domain=domain,
            impersonate=impersonate,
            proxy_strategy=proxy_strategy,
            allow_retry=False,
            allow_relay=False,
        )

    async def get(
        self,
        url: str,
        *,
        domain: str,
        referer: str | None = None,
        allow_retry: bool = True,
        allow_relay: bool = False,
        impersonate: str | None = None,
        timeout_seconds: float | None = None,
        extra_headers: dict[str, str] | None = None,
        proxy_strategy: str | None = None,
    ) -> FetchResult:
        started = time.monotonic()
        await self.limiter.wait(domain)
        attempts = 0
        last: FetchResult | None = None
        relay_enabled = allow_relay and self.network.relay.enabled
        # Если включён relay, прямой запрос делаем один раз: повторные
        # таймауты только растягивают цикл, а решение всё равно за relay.
        max_attempts = 1 if (relay_enabled or not allow_retry) else self.network.backoff.max_retries
        profiles = self._profile_order(impersonate)
        timeout = timeout_seconds or self.runtime.request_timeout_seconds
        proxy = self.proxies.acquire(domain, strategy=proxy_strategy)
        if proxy is not None:
            prev = self._domain_proxy.get(domain)
            if prev is not None and prev != proxy.url:
                # IP домена сменился (прежний прокси остыл после ошибки):
                # куки прогрева получены с другого адреса и больше не
                # согласованы — адаптер прогреет домен заново.
                self._prewarmed.discard(domain)
            self._domain_proxy[domain] = proxy.url

        while attempts < max_attempts:
            attempts += 1
            profile = profiles[min(attempts - 1, len(profiles) - 1)]
            session = await self._session(profile)
            headers = self._headers(profile, referer)
            if extra_headers:
                headers.update(extra_headers)
            proxy_kwargs: dict[str, Any] = {}
            if proxy is not None:
                proxy_kwargs["proxy"] = proxy.url
            try:
                resp = await session.get(
                    url,
                    headers=headers,
                    allow_redirects=True,
                    impersonate=profile,
                    timeout=timeout,
                    **proxy_kwargs,
                )
                text = resp.text or ""
                hdrs = dict(resp.headers.items())
                outcome, challenge = classify_response(resp.status_code, hdrs, text)
                last = FetchResult(
                    outcome=outcome,
                    status=resp.status_code,
                    text=text,
                    headers=hdrs,
                    url=str(resp.url),
                    challenge=challenge,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    attempts=attempts,
                    proxy=proxy.redact() if proxy else None,
                )
                if proxy is not None:
                    self.proxies.report_success(proxy.url)
                if outcome in (Outcome.OK, Outcome.EMPTY, Outcome.LOGIN_WALL):
                    return last
                if (
                    outcome == Outcome.BLOCKED
                    and challenge == "rate-limited"
                    and attempts < max_attempts
                ):
                    await asyncio.sleep(backoff_delay(attempts, self.network.backoff))
                    proxy = self.proxies.acquire(domain, strategy=proxy_strategy)
                    continue
                if (
                    outcome == Outcome.NETWORK_ERROR
                    and challenge == "upstream-error"
                    and attempts < max_attempts
                ):
                    # 5xx — временная ошибка сервера: повторяем с экспоненциальной
                    # задержкой, как и 429. Отсутствие попыток ретрая на 5xx
                    # теряло страницы из-за минутных сбоев upstream.
                    await asyncio.sleep(backoff_delay(attempts, self.network.backoff))
                    proxy = self.proxies.acquire(domain, strategy=proxy_strategy)
                    continue
                if (
                    outcome in (Outcome.CHALLENGE, Outcome.BLOCKED)
                    and attempts < min(max_attempts, len(profiles))
                ):
                    # Другой TLS-отпечаток иногда проходит challenge (например,
                    # Cloudflare у Freelancehunt). Это дешевле полной паузы.
                    await asyncio.sleep(0.35)
                    continue
                break
            except Exception as e:  # noqa: BLE001 - network errors are expected
                if proxy is not None:
                    self.proxies.report_failure(proxy.url)
                last = FetchResult(
                    outcome=Outcome.NETWORK_ERROR,
                    status=None,
                    url=url,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    attempts=attempts,
                    proxy=proxy.redact() if proxy else None,
                    error=f"{type(e).__name__}: {e}"[:300],
                )
                if attempts < max_attempts:
                    await asyncio.sleep(backoff_delay(attempts, self.network.backoff))
                    proxy = self.proxies.acquire(domain, strategy=proxy_strategy)
                    continue
                break

        if last is not None and last.outcome == Outcome.NETWORK_ERROR and relay_enabled:
            relayed = await self._relay_get(url, referer, extra_headers)
            if relayed is not None:
                return relayed

        return last or FetchResult(outcome=Outcome.NETWORK_ERROR, status=None, url=url)

    async def _relay_get(
        self,
        url: str,
        referer: str | None,
        extra_headers: dict[str, str] | None,
    ) -> FetchResult | None:
        """Загружает страницу через внешний reader-сервис.

        Нужен только для площадок, которые блокируют IP сервера на уровне TCP
        (FL.ru, Freelance.ru). Ответ всё равно проходит классификацию: если
        relay вернул заглушку антибота, адаптер увидит CHALLENGE/BLOCKED.
        """
        started = time.monotonic()
        relay = self.network.relay
        target = self.relay_target(url)
        headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": self.runtime.accept_language,
            "X-Return-Format": relay.return_format,
        }
        if referer:
            headers["Referer"] = referer
        if extra_headers:
            headers.update(extra_headers)
        api_key = self.relay_api_key or os.getenv("JINA_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            session = await self._session("chrome131")
            resp = await session.get(
                target,
                headers=headers,
                impersonate="chrome131",
                allow_redirects=True,
                timeout=relay.timeout_seconds,
            )
            text = resp.text or ""
            hdrs = dict(resp.headers.items())
            outcome, challenge = classify_response(resp.status_code, hdrs, text)
            return FetchResult(
                outcome=outcome,
                status=resp.status_code,
                text=text,
                headers=hdrs,
                url=url,
                challenge=challenge,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                attempts=1,
            )
        except Exception as e:  # noqa: BLE001
            return FetchResult(
                outcome=Outcome.NETWORK_ERROR,
                status=None,
                url=url,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                attempts=1,
                error=f"relay {type(e).__name__}: {e}"[:300],
            )

    async def close(self) -> None:
        for session in self._sessions.values():
            await session.close()
        self._sessions.clear()

    async def __aenter__(self) -> StealthHttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
