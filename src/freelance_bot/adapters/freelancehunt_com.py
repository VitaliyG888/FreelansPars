"""Адаптер Freelancehunt.

Настоящий домен — https://freelancehunt.com (украинская биржа, бюджеты в UAH).
freelancehunt.RU ведёт на постороннюю страницу казино, использовать нельзя.
Cloudflare пропускает HTTP с TLS-отпечатком chrome131 (проверено 2026-11);
старые отпечатки получают challenge.
"""

from __future__ import annotations

import re

from ..models import BudgetKind, RawJob, TransportKind
from .base import BaseAdapter, attr_of, parse_int, soup, text_of


class FreelancehuntAdapter(BaseAdapter):
    platform = "freelancehunt_com"
    domain = "freelancehunt.com"
    transport = TransportKind.HTTP
    wait_selector = "article.project-card"
    prewarm_path = "/"

    def parse(self, html: str, base_url: str) -> list[RawJob]:
        doc = soup(html)
        jobs: list[RawJob] = []
        for card in doc.select("article.project-card"):
            link = card.select_one(".list-card-title a") or card.select_one("a.project-link")
            if link is None:
                continue
            href = attr_of(link, "href") or ""
            m = re.search(r"/project/[^/]+/(\d+)", href)
            if not m:
                continue

            budget_text = text_of(card.select_one(".project-stats .budget")) or None
            proposals = None
            for stat in card.select(".project-stats .project-stat"):
                t = text_of(stat)
                if "ставк" in t.lower():
                    proposals = parse_int(t)
            skills = [text_of(a) for a in card.select(".project-skill")]
            body = text_of(card)
            published_text = None
            mp = re.search(r"(\d+)\s*(минут|час|дн|день|дня|недел)[а-я]*\s*назад", body, re.I)
            if mp:
                published_text = mp.group(0)

            jobs.append(
                RawJob(
                    platform=self.platform,
                    external_id=m.group(1),
                    url=self.abs_url(href, base_url),
                    title=text_of(link),
                    description=text_of(card.select_one("p.project-description")),
                    budget_text=budget_text,
                    budget_kind=BudgetKind.FIXED if budget_text else BudgetKind.UNKNOWN,
                    category=skills[0] if skills else None,
                    skills=skills[1:],
                    published_text=published_text,
                    proposals_count=proposals,
                    raw={"body_len": len(body)},
                )
            )
        return jobs
