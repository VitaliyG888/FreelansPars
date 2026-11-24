"""Адаптер листинга Weblancer.net.

https://www.weblancer.net/jobs/ редиректит на /freelance/; Cloudflare пропускает
обычный HTTP-запрос с TLS-отпечатком chrome131 (проверено 2026-11). У карточек нет
стабильного семантического класса — это <article> в стиле Tailwind — поэтому
извлечение опирается на заголовок и span бюджета.
"""

from __future__ import annotations

import re

from ..models import BudgetKind, RawJob, TransportKind
from .base import BaseAdapter, attr_of, soup, text_of

BUDGET_RE = re.compile(r"^\s*(\d[\d\s]{0,12})\s*(₽|руб\.?|\$|€|грн|UAH|USD|EUR)", re.IGNORECASE)


class WeblancerNetAdapter(BaseAdapter):
    platform = "weblancer_net"
    domain = "www.weblancer.net"
    transport = TransportKind.HTTP
    wait_selector = "article h2 a"
    prewarm_path = "/"

    def parse(self, html: str, base_url: str) -> list[RawJob]:
        doc = soup(html)
        jobs: list[RawJob] = []
        for card in doc.select("article"):
            link = card.select_one("h2 a")
            if link is None:
                continue
            href = attr_of(link, "href") or ""
            if "/freelance/" not in href:
                continue
            m = re.search(r"-(\d+)/?$", href) or re.search(r"/(\d+)/?$", href)
            external_id = m.group(1) if m else href.strip("/").rsplit("/", 1)[-1]

            budget_text = text_of(card.select_one("span.text-green-600")) or None
            if budget_text is None:
                body_text = text_of(card)
                title = text_of(link)
                tail = body_text.replace(title, "", 1).strip()
                mb = BUDGET_RE.match(tail)
                if mb:
                    budget_text = f"{mb.group(1)} {mb.group(2)}"

            tags = [text_of(a) for a in card.select("div.flex.flex-wrap.gap-2 a") if text_of(a)]
            footer = text_of(card.select_one("div.flex.flex-wrap.items-center"))
            published_text = None
            md = re.search(r"\d{2}\.\d{2}\.\d{4}", footer)
            if md:
                published_text = md.group(0)
            proposals = None
            mp = re.search(r"(\d+)\s*заяв", footer)
            if mp:
                proposals = int(mp.group(1))
            views = None
            mv = re.search(r"(\d+)\s*просмотр", footer)
            if mv:
                views = int(mv.group(1))

            jobs.append(
                RawJob(
                    platform=self.platform,
                    external_id=external_id,
                    url=self.abs_url(href, base_url),
                    title=text_of(link),
                    description=text_of(card.select_one("p")),
                    budget_text=budget_text,
                    budget_kind=BudgetKind.FIXED if budget_text else BudgetKind.UNKNOWN,
                    category=tags[0] if tags else None,
                    skills=tags[1:],
                    published_text=published_text,
                    proposals_count=proposals,
                    views_count=views,
                    raw={"footer": footer, "tags": tags},
                )
            )
        return jobs
