"""Проверка прокси из RU_PROXY_URLS: доступность и согласованность egress.

Запускается вручную: python tools/check_proxies.py [--kwork]

Без --kwork каждый прокси проверяется коротким запросом к нейтральному
эндпоинту (api.ipify.org) — видно только, живой ли прокси и с каких стран
выходит; сами адреса и учётные данные не выводятся. С --kwork выполняется
полный путь kwork (прогрев → листинг) через sticky-привязку, как в боте.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]

# Код страны egress — не секрет и полезен для диагностики (RU-прокси
# предназначены для российских антибот-систем).
GEO_ENDPOINT = "http://ip-api.com/json/?fields=countryCode"
REACH_ENDPOINT = "https://api.ipify.org/"


def read_proxy_urls() -> list[str]:
    values: list[str] = []
    for raw in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^RU_PROXY_URLS=(.*)$", raw.strip())
        if m:
            values = [u.strip() for u in m.group(1).split(",") if u.strip()]
    return values


async def check_proxy(index: int, url: str) -> tuple[int, str, str]:
    """Проверяет один прокси: доступность и код страны egress."""
    from curl_cffi.requests import AsyncSession

    # Без схемы тип туннеля неоднозначен — считаем HTTP-прокси (как в Secrets).
    if "://" not in url:
        url = "http://" + url
    async with AsyncSession(impersonate="chrome131", timeout=15) as session:
        try:
            resp = await session.get(REACH_ENDPOINT, proxy=url)
            reachable = resp.status_code == 200
        except Exception as e:  # noqa: BLE001 - сетевые ошибки ожидаемы
            return index, "FAIL", f"{type(e).__name__}: {e}"[:120]
        try:
            geo = await session.get(GEO_ENDPOINT, proxy=url)
            country = str(geo.json().get("countryCode", "?"))
        except Exception:  # noqa: BLE001 - гео не критично
            country = "?"
        return index, ("OK" if reachable else f"HTTP {resp.status_code}"), country


async def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка прокси из .env")
    parser.add_argument("--kwork", action="store_true",
                        help="дополнительно проверить полный путь kwork через sticky-прокси")
    args = parser.parse_args()

    urls = read_proxy_urls()
    if not urls:
        print("RU_PROXY_URLS пуст: прокси не заданы")
        return
    print(f"Прокси в .env: {len(urls)} шт. Проверка доступности ...")

    results = await asyncio.gather(*(check_proxy(i, u) for i, u in enumerate(urls, 1)))
    for index, status, extra in results:
        print(f"  #{index}: {status} (egress: {extra})")
    alive = sum(1 for _, status, _ in results if status == "OK")
    print(f"Живых: {alive}/{len(urls)}")

    if args.kwork and alive:
        print("\nЖивая проверка kwork через sticky-прокси ...")
        from freelance_bot.config import load_config
        from freelance_bot.net.proxies import ProxyPool
        from freelance_bot.net.stealth import StealthHttpClient

        config = load_config()
        pool = ProxyPool(urls=urls, strategy=config.network.proxy_strategy)
        client = StealthHttpClient(config.runtime, config.network, pool)
        try:
            warm = await client.warmup(
                "https://kwork.ru/", domain="kwork.ru", proxy_strategy="sticky"
            )
            assert warm is not None
            print(f"  прогрев:   outcome={warm.outcome.value} через {warm.proxy or 'напрямую'}")
            res = await client.get(
                "https://kwork.ru/projects", domain="kwork.ru", proxy_strategy="sticky"
            )
            print(
                f"  листинг:   outcome={res.outcome.value} attempts={res.attempts} "
                f"через {res.proxy or 'напрямую'}"
            )
            has_state = '"wantsListData"' in res.text
            print(f"  initial state: {'✅ найден' if has_state else '❌ нет'}")
        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(main())
