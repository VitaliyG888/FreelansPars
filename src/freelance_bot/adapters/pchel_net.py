"""Адаптер листинга Pchel.net.

Источник: https://pchel.net/projects/ — серверный рендеринг, 50 карточек на страницу,
Cloudflare на краю без challenge на листинге (проверено 2026-11).
"""

from __future__ import annotations

import re

from ..models import BudgetKind, ClientInfo, RawJob, TransportKind
from .base import BaseAdapter, attr_of, parse_int, soup, text_of


class PchelNetAdapter(BaseAdapter):
    platform = "pchel_net"
    domain = "pchel.net"
    transport = TransportKind.HTTP
    prewarm_path = "/"

    def parse(self, html: str, base_url: str) -> list[RawJob]:
        doc = soup(html)
        jobs: list[RawJob] = []
        for card in doc.select("div.project-block"):
            link = card.select_one(".project-title a.b-link")
            if link is None:
                continue
            href = attr_of(link, "href") or ""
            external_id = ""
            hidden = card.select_one("input[name=project_id]")
            if hidden is not None:
                external_id = attr_of(hidden, "value") or ""
            if not external_id:
                m = re.search(r"-(\d+)/?$", href)
                external_id = m.group(1) if m else href
            if not external_id:
                continue

            price_text = text_of(card.select_one(".project-block-cont .price"))
            proposals = None
            p_user = card.select_one(".p-user")
            if p_user is not None:
                proposals = parse_int(attr_of(p_user, "title")) or parse_int(text_of(p_user))
            views = None
            p_view = card.select_one(".p-view")
            if p_view is not None:
                views = parse_int(attr_of(p_view, "title")) or parse_int(text_of(p_view))

            tags = [text_of(a) for a in card.select(".project-tags a")]
            client_name = None
            author = card.select_one(".athor-name")
            if author is not None:
                client_name = text_of(author).removeprefix("Работодатель:").strip(" :") or None

            jobs.append(
                RawJob(
                    platform=self.platform,
                    external_id=external_id,
                    url=self.abs_url(href, base_url),
                    title=text_of(link),
                    description=text_of(card.select_one(".project-text")),
                    budget_text=price_text or None,
                    budget_kind=BudgetKind.FIXED if price_text else BudgetKind.UNKNOWN,
                    category=tags[0] if tags else None,
                    skills=tags[1:],
                    proposals_count=proposals,
                    views_count=views,
                    is_urgent=bool(card.select_one("img[alt=urgent]")),
                    client=ClientInfo(name=client_name),
                    raw={"tags": tags, "price_raw": price_text},
                )
            )
        return jobs
