"""Живая проверка антиблокировки kwork.ru: прогрев сессии + листинг.

Запускается вручную: python tools/check_kwork_live.py
Делает ровно два вежливых запроса (главная → /projects) с паузами лимитера.
"""

from __future__ import annotations

import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8")


async def main() -> None:
    from freelance_bot.config import Secrets, load_config
    from freelance_bot.net.proxies import ProxyPool
    from freelance_bot.net.stealth import StealthHttpClient

    config = load_config()
    # Пул собирается как в проде (registry.build_context): с VPS проверка
    # идёт через прокси площадки, без него проверка отражает только прямой IP.
    secrets = Secrets()
    proxies = ProxyPool(
        urls=secrets.proxies,
        strategy=config.network.proxy_strategy,
        cooldown_seconds=config.network.proxy_cooldown_seconds,
    )
    # Стратегия площадки важнее глобальной (kwork: sticky) — как в адаптере.
    proxy_strategy = config.platforms["kwork_ru"].proxy_strategy or None
    client = StealthHttpClient(config.runtime, config.network, proxies)
    try:
        print("1) Прогрев: GET https://kwork.ru/ ...")
        warm = await client.warmup("https://kwork.ru/", domain="kwork.ru", proxy_strategy=proxy_strategy)
        assert warm is not None
        print(
            f"   outcome={warm.outcome.value} status={warm.status} "
            f"challenge={warm.challenge or '-'} bytes={len(warm.text)} "
            f"proxy={warm.proxy or 'напрямую'}"
        )

        print("2) Листинг: GET https://kwork.ru/projects ...")
        res = await client.get(
            "https://kwork.ru/projects", domain="kwork.ru", proxy_strategy=proxy_strategy
        )
        print(
            f"   outcome={res.outcome.value} status={res.status} "
            f"challenge={res.challenge or '-'} bytes={len(res.text)} attempts={res.attempts} "
            f"proxy={res.proxy or 'напрямую'}"
        )
        has_state = '"wantsListData"' in res.text
        print(f"   initial state (wantsListData): {'✅ найден' if has_state else '❌ нет'}")

        print("3) Разбор живого HTML адаптером Kwork ...")
        from freelance_bot.adapters.kwork_ru import KworkRuAdapter
        from freelance_bot.config import PlatformConfig

        adapter = KworkRuAdapter(
            PlatformConfig(listing_url="https://kwork.ru/projects", transport="http")
        )
        jobs = adapter.parse(res.text, res.url or "https://kwork.ru/projects")
        print(f"   заказов извлечено: {len(jobs)}")
        for raw in jobs[:3]:
            title = (raw.title or "")[:70]
            print(f"   • [{raw.external_id}] {title}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
