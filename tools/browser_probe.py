"""Проверяет страницы headless Chromium со stealth-настройками и сообщает, что отрисовалось.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU','ru','en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || {runtime: {}};
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => (
  p.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : origQuery(p)
);
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (p) {
  if (p === 37445) return 'Intel Inc.';
  if (p === 37446) return 'Intel Iris OpenGL Engine';
  return getParameter.call(this, p);
};
"""


async def probe(url: str, wait_ms: int = 6000) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--lang=ru-RU",
            ],
        )
        ctx = await browser.new_context(
            user_agent=UA,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8"},
        )
        await ctx.add_init_script(STEALTH)
        page = await ctx.new_page()
        resp = None
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(wait_ms)
        except Exception as e:  # noqa: BLE001
            print(f"  nav error: {type(e).__name__}: {str(e)[:160]}")
        title = await page.title()
        text = await page.evaluate("document.body ? document.body.innerText : ''")
        html = await page.content()
        links = await page.evaluate(
            "Array.from(document.querySelectorAll('a[href]')).length"
        )
        print(f"{url}\n  status={resp.status if resp else None} title={title[:90]!r}")
        print(f"  innerText={len(text)} html={len(html)} links={links}")
        print(f"  sample={text[:260]!r}")
        safe = url.split("//")[-1].replace("/", "_").replace("?", "_")[:60]
        out = Path(f"fixtures/live/browser_{safe}.html")
        out.write_text(html, encoding="utf-8")
        print(f"  saved {out}\n")
        await browser.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for u in sys.argv[1:]:
        asyncio.run(probe(u))
