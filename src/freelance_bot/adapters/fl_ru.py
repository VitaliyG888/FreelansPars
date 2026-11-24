"""Адаптер листинга FL.ru.

Источник: https://www.fl.ru/projects/ — серверный рендеринг, на краю DDoS-Guard,
но без JS-challenge на листинге (проверено 2026-11). Карточки: div.b-page__lenta_item.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..models import BudgetKind, RawJob, TransportKind
from .base import BaseAdapter, attr_of, soup, text_of


class FlRuAdapter(BaseAdapter):
    platform = "fl_ru"
    domain = "www.fl.ru"
    transport = TransportKind.HTTP
    prewarm_path = "/"

    def parse(self, html: str, base_url: str) -> list[RawJob]:
        doc = soup(html)
        jobs: list[RawJob] = []
        for card in doc.select("div.b-page__lenta_item.b-post"):
            link = card.select_one("h2.b-post__title a")
            if link is None:
                continue
            href = attr_of(link, "href") or ""
            if not href.startswith("/projects/"):
                continue

            external_id = ""
            card_id = attr_of(card, "id") or ""
            if card_id.startswith("project-item"):
                external_id = card_id.removeprefix("project-item")
            if not external_id:
                external_id = attr_of(link, "data-disposable-project-id") or ""
            if not external_id:
                m = re.search(r"/projects/(\d+)", href)
                external_id = m.group(1) if m else urlsplit(href).path
            if not external_id:
                continue

            foot = text_of(card.select_one(".b-post__foot"))
            proposals = None
            m = re.search(r"(\d+)\s*ответ", foot)
            if m:
                proposals = int(m.group(1))
            views = None
            mv = re.search(r"больше\s+(\d+)|(\d+)\s*просмотр", foot)
            if mv:
                views = int(mv.group(1) or mv.group(2))

            price_text = text_of(card.select_one(".b-post__price"))
            jobs.append(
                RawJob(
                    platform=self.platform,
                    external_id=external_id,
                    url=self.abs_url(href, base_url),
                    title=text_of(link),
                    description=text_of(card.select_one(".b-post__body .b-post__txt")),
                    budget_text=price_text or None,
                    budget_kind=BudgetKind.FIXED if price_text else BudgetKind.UNKNOWN,
                    proposals_count=proposals,
                    views_count=views,
                    published_text=foot or None,
                    raw={"price_raw": price_text, "foot": foot},
                )
            )
        return jobs
