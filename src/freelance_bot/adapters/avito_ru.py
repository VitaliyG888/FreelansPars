"""Адаптер Avito (браузерный транспорт, требует российский резидентный прокси).

Слой Qrator у Avito возвращает HTTP 439/403 дата-центрам даже в настоящем
браузере и через reader-relay. Проверено в 2026-11 с не-российского адреса
дата-центра. Поэтому при пустом ``RU_PROXY_URLS`` адаптер сразу возвращает
``DISABLED`` и не тратит таймауты цикла.

Разбор опирается на JSON-LD schema.org ItemList/Product, который Avito отдаёт для
объявлений. Он намеренно обобщён, потому что разметку карточек не удалось
получить без резидентного прокси; считайте её непроверенной, пока не будет
записана фикстура через российский прокси.
"""

from __future__ import annotations

import json
import re

from ..models import BudgetKind, RawJob, TransportKind
from .base import BaseAdapter, attr_of, soup, text_of


class AvitoRuAdapter(BaseAdapter):
    platform = "avito_ru"
    domain = "www.avito.ru"
    transport = TransportKind.BROWSER
    wait_selector = '[data-marker="item"]'
    prewarm_path = "/"

    def parse(self, html: str, base_url: str) -> list[RawJob]:
        doc = soup(html)
        jobs: list[RawJob] = []

        # Основной путь: структурированные данные.
        for tag in doc.select('script[type="application/ld+json"]'):
            try:
                payload = json.loads(tag.string or "{}")
            except json.JSONDecodeError:
                continue
            for item in self._iter_items(payload):
                job = self._from_ld(item, base_url)
                if job:
                    jobs.append(job)
        if jobs:
            return jobs

        # Запасной путь: карточки по data-маркерам, если Avito не отдаёт JSON-LD.
        for card in doc.select('[data-marker="item"]'):
            link = card.select_one("a[data-marker='item-title']") or card.select_one("a[href]")
            if link is None:
                continue
            href = attr_of(link, "href") or ""
            m = re.search(r"_(\d{6,})", href)
            if not m:
                continue
            price = text_of(card.select_one("[data-marker='item-price']"))
            jobs.append(
                RawJob(
                    platform=self.platform,
                    external_id=m.group(1),
                    url=self.abs_url(href, base_url),
                    title=text_of(link),
                    description=text_of(card.select_one("[class*='description']")),
                    budget_text=price or None,
                    budget_kind=BudgetKind.FIXED if price else BudgetKind.UNKNOWN,
                )
            )
        return jobs

    @staticmethod
    def _iter_items(payload: object):
        if isinstance(payload, dict):
            if (
                payload.get("@type") in ("ItemList", "Product", "Service")
                or "itemListElement" in payload
            ):
                yield payload
            for value in payload.values():
                yield from AvitoRuAdapter._iter_items(value)
        elif isinstance(payload, list):
            for item in payload:
                yield from AvitoRuAdapter._iter_items(item)

    def _from_ld(self, payload: dict, base_url: str) -> RawJob | None:
        elements = payload.get("itemListElement")
        if not elements:
            return None
        for element in elements:
            item = element.get("item", element) if isinstance(element, dict) else {}
            url = item.get("url") or element.get("url")
            name = item.get("name") or element.get("name")
            if not url or not name:
                continue
            m = re.search(r"_(\d{6,})", url)
            if not m:
                continue
            offers = item.get("offers") or {}
            price = offers.get("price") if isinstance(offers, dict) else None
            return RawJob(
                platform=self.platform,
                external_id=m.group(1),
                url=self.abs_url(url, base_url),
                title=str(name),
                description=str(item.get("description") or ""),
                budget_text=str(price) if price else None,
                budget_kind=BudgetKind.FIXED if price else BudgetKind.UNKNOWN,
            )
        return None
