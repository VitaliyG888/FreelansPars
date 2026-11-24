"""Запасной stealth-браузер для JavaScript-challenge и SPA-листингов.

Только цель для эскалации: сначала пробуется HTTP-слой. Браузер запускается
лениво и переиспользуется, с отдельным изолированным контекстом на домен.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from ..config import NetworkConfig, RuntimeConfig
from ..models import Outcome
from .captcha import CaptchaChallenge, CaptchaError, CaptchaSolver
from .proxies import ProxyPool, playwright_proxy
from .stealth import FetchResult, classify_response

STEALTH_JS = """
// Remove the most common automation tells. Kept minimal on purpose:
// exotic patches are themselves detectable.
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU','ru','en-US','en']});
Object.defineProperty(navigator, 'plugins', {
  get: () => [
    {name: 'Chrome PDF Plugin'}, {name: 'Chrome PDF Viewer'}, {name: 'Native Client'}
  ]
});
Object.defineProperty(navigator, 'mimeTypes', {get: () => [{type: 'application/pdf'}]});
window.chrome = window.chrome || {runtime: {}, app: {isInstalled: false}};
const originalQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
window.navigator.permissions.query = (parameters) =>
  parameters.name === 'notifications'
    ? Promise.resolve({state: Notification.permission, onchange: null})
    : originalQuery(parameters);

const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (parameter) {
  if (parameter === 37445) return 'Intel Inc.';
  if (parameter === 37446) return 'Intel Iris OpenGL Engine';
  return getParameter.call(this, parameter);
};
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
"""

SMARTCAPTCHA_KEYS = (
    ".smart-captcha[data-site-key]",
    "div[data-sitekey]",
    "[data-testid='smart-captcha']",
)


@dataclass
class BrowserSession:
    context: object
    prewarmed: bool = False
    requests: int = 0
    proxy_label: str | None = None


@dataclass
class BrowserFetcher:
    runtime: RuntimeConfig
    network: NetworkConfig
    solver: CaptchaSolver | None = None
    proxies: ProxyPool | None = None
    _playwright: object = field(default=None, init=False, repr=False)
    _browser: object = field(default=None, init=False, repr=False)
    _sessions: dict[str, BrowserSession] = field(default_factory=dict, init=False)
    _start_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def start(self) -> None:
        async with self._start_lock:
            if self._browser is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:  # pragma: no cover - environment dependent
                raise RuntimeError(
                    "playwright is not installed; install with: pip install '.[browser]' "
                    "&& playwright install chromium"
                ) from e
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.network.browser.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--lang={self.network.browser.locale}",
                ],
            )

    async def _context(self, domain: str) -> BrowserSession:
        if domain in self._sessions:
            return self._sessions[domain]
        assert self._browser is not None
        browser_cfg = self.network.browser
        proxy_state = self.proxies.acquire(domain) if self.proxies else None
        context = await self._browser.new_context(  # type: ignore[attr-defined]
            user_agent=self.runtime.user_agent,
            locale=browser_cfg.locale,
            timezone_id=browser_cfg.timezone,
            viewport=browser_cfg.viewport,
            java_script_enabled=True,
            ignore_https_errors=False,
            proxy=playwright_proxy(proxy_state.url) if proxy_state is not None else None,
            extra_http_headers={
                "Accept-Language": self.runtime.accept_language,
                "Sec-CH-UA": '"Chromium";v="131", "Not_A Brand";v="24"',
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"Windows"',
            },
        )
        await context.add_init_script(STEALTH_JS)
        # Блокируем тяжёлые сторонние ресурсы: циклы быстрее, трафик вежливее.
        await context.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in ("image", "media", "font")
                else route.continue_()
            ),
        )
        session = BrowserSession(
            context=context,
            proxy_label=proxy_state.redact() if proxy_state is not None else None,
        )
        self._sessions[domain] = session
        return session

    async def _humanize(self, page: object) -> None:
        """Небольшой правдоподобный след взаимодействия перед извлечением."""
        try:
            for _ in range(random.randint(2, 4)):
                await page.mouse.move(  # type: ignore[attr-defined]
                    random.randint(80, 1200), random.randint(90, 700), steps=random.randint(4, 12)
                )
                await asyncio.sleep(random.uniform(0.05, 0.18))
            await page.mouse.wheel(0, random.randint(180, 520))  # type: ignore[attr-defined]
            await asyncio.sleep(random.uniform(0.25, 0.6))
        except Exception:  # noqa: BLE001 - cosmetic only
            pass

    async def fetch(
        self,
        url: str,
        *,
        domain: str,
        wait_selector: str | None = None,
        prewarm_url: str | None = None,
    ) -> FetchResult:
        await self.start()
        session = await self._context(domain)
        context = session.context
        page = await context.new_page()  # type: ignore[attr-defined]
        started = asyncio.get_event_loop().time()
        status: int | None = None
        browser_cfg = self.network.browser
        try:
            if prewarm_url and not session.prewarmed:
                try:
                    await page.goto(
                        prewarm_url,
                        wait_until="commit",
                        timeout=min(browser_cfg.navigation_timeout_ms, 15_000),
                    )
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    session.prewarmed = True
                except Exception:  # noqa: BLE001
                    pass

            # commit, а не domcontentloaded: часть площадок подвешивает загрузку
            # сторонних CDN, и ожидание DOM-события съедает весь таймаут.
            response = await page.goto(
                url,
                wait_until="commit",
                timeout=browser_cfg.navigation_timeout_ms,
            )
            status = response.status if response else None
            if wait_selector:
                try:
                    await page.wait_for_selector(
                        wait_selector, timeout=15_000
                    )
                except Exception:  # noqa: BLE001 - absence is handled by classification
                    pass
            await page.wait_for_timeout(browser_cfg.settle_ms)
            await self._humanize(page)

            html = await page.content()
            outcome, challenge = classify_response(status or 200, {}, html)

            # Managed challenge (Cloudflare/Qrator) иногда решается сам за
            # несколько секунд. Даём ему шанс, прежде чем считать площадку заблокированной.
            if outcome in (Outcome.CHALLENGE, Outcome.BLOCKED):
                deadline = asyncio.get_event_loop().time() + browser_cfg.challenge_wait_ms / 1000
                while asyncio.get_event_loop().time() < deadline:
                    await page.wait_for_timeout(2_000)
                    html = await page.content()
                    outcome, challenge = classify_response(status or 200, {}, html)
                    if outcome not in (Outcome.CHALLENGE, Outcome.BLOCKED):
                        break

            if outcome in (Outcome.CHALLENGE, Outcome.BLOCKED) and self.solver and challenge:
                solved = await self._try_solve(page, url, challenge, html)
                if solved:
                    await page.wait_for_timeout(browser_cfg.settle_ms)
                    html = await page.content()
                    outcome, challenge = classify_response(status or 200, {}, html)

            session.requests += 1
            elapsed = int((asyncio.get_event_loop().time() - started) * 1000)
            return FetchResult(
                outcome=outcome,
                status=status,
                text=html,
                url=page.url,
                challenge=challenge,
                elapsed_ms=elapsed,
                attempts=1,
                proxy=session.proxy_label,
            )
        except Exception as e:  # noqa: BLE001
            elapsed = int((asyncio.get_event_loop().time() - started) * 1000)
            # Таймаут навигации не означает, что страница пуста: если HTML уже
            # пришёл, отдаём его парсеру.
            try:
                html = await page.content()
                if len(html.strip()) >= 1200:
                    outcome, challenge = classify_response(status or 200, {}, html)
                    return FetchResult(
                        outcome=outcome,
                        status=status,
                        text=html,
                        url=page.url,
                        challenge=challenge,
                        elapsed_ms=elapsed,
                        attempts=1,
                        proxy=session.proxy_label,
                    )
            except Exception:  # noqa: BLE001
                pass
            return FetchResult(
                outcome=Outcome.NETWORK_ERROR,
                status=status,
                url=url,
                elapsed_ms=elapsed,
                error=f"{type(e).__name__}: {e}"[:300],
                proxy=session.proxy_label,
            )
        finally:
            await page.close()

    async def _try_solve(self, page: object, url: str, challenge: str, html: str) -> bool:
        """По мере возможности решаем CAPTCHA на странице. Возвращает True, если токен внедрён.
        """
        if not self.solver:
            return False
        kind = {
            "yandex-smartcaptcha": "yandex_smartcaptcha",
            "cloudflare": "recaptcha_v2",
            "datadome": "recaptcha_v2",
        }.get(challenge)
        if not kind:
            return False
        site_key = None
        for selector in SMARTCAPTCHA_KEYS:
            try:
                el = await page.query_selector(selector)  # type: ignore[attr-defined]
                if el:
                    site_key = await el.get_attribute("data-site-key") or await el.get_attribute(
                        "data-sitekey"
                    )
                    if site_key:
                        break
            except Exception:  # noqa: BLE001
                continue
        if not site_key:
            return False
        try:
            token = await self.solver.solve(
                CaptchaChallenge(kind=kind, site_key=site_key, page_url=url)
            )
        except CaptchaError:
            return False

        # Записываем в задокументированное скрытое поле и вызываем callback виджета.
        try:
            await page.evaluate(  # type: ignore[attr-defined]
                """(token) => {
                    const selector = 'input[name="smart-token"], '
                      + 'input[name="captcha-token"], '
                      + 'textarea[name="g-recaptcha-response"]';
                    const inputs = document.querySelectorAll(selector);
                    inputs.forEach(i => { i.value = token; });
                    if (window.smartCaptcha && window.smartCaptcha.callback) {
                        window.smartCaptcha.callback(token);
                    }
                    document.querySelectorAll('form').forEach(f => {
                        const ev = new Event('submit', {bubbles: true});
                        f.dispatchEvent(ev);
                    });
                }""",
                token,
            )
            await page.wait_for_timeout(3500)  # type: ignore[attr-defined]
            return True
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        for session in self._sessions.values():
            try:
                await session.context.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()
        if self._browser is not None:
            try:
                await self._browser.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None

    async def __aenter__(self) -> BrowserFetcher:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
