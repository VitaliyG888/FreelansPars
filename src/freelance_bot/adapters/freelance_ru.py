"""Адаптер листинга Freelance.ru.

Источник: https://freelance.ru/project/search — серверные карточки
`article.task-card`. Страница — приложение в стиле Inertia, но первая страница
ленты присутствует в HTML.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..models import BudgetKind, RawJob, TransportKind
from .base import BaseAdapter, attr_of, parse_int, soup, text_of


class FreelanceRuAdapter(BaseAdapter):
    platform = "freelance_ru"
    domain = "freelance.ru"
    transport = TransportKind.HTTP
    prewarm_path = "/"

    def parse(self, html: str, base_url: str) -> list[RawJob]:
        doc = soup(html)
        jobs: list[RawJob] = []
        for card in doc.select("article.task-card"):
            link = card.select_one(".task-card__title-link")
            if link is None:
                continue
            href = attr_of(link, "href") or ""
            if "/task/view/" not in href:
                continue
            m = re.search(r"/task/view/(\d+)", href)
            external_id = m.group(1) if m else urlsplit(href).path.rsplit("/", 1)[-1]

            foot_items = card.select(".task-card__foot-item")
            published_text = None
            proposals = None
            views = None
            experience = None
            for item in foot_items:
                title_attr = attr_of(item, "title")
                icon = item.select_one("i")
                icon_classes = " ".join(icon.get_attribute_list("class")) if icon else ""
                if "fa-comments" in icon_classes:
                    proposals = parse_int(text_of(item))
                elif "fa-eye" in icon_classes:
                    views = parse_int(text_of(item))
                elif title_attr:
                    published_text = title_attr
                elif text_of(item).lower().startswith("опыт"):
                    experience = text_of(item)
                elif published_text is None and "назад" in text_of(item):
                    published_text = text_of(item)

            budget_text = text_of(card.select_one(".task-card__budget"))
            jobs.append(
                RawJob(
                    platform=self.platform,
                    external_id=external_id,
                    url=self.abs_url(href, base_url),
                    title=(attr_of(link, "title") or text_of(link)).strip(),
                    description=text_of(card.select_one(".task-card__desc")),
                    budget_text=budget_text or None,
                    budget_kind=BudgetKind.FIXED if budget_text else BudgetKind.UNKNOWN,
                    category=text_of(card.select_one(".task-chip--cat")) or None,
                    published_text=published_text,
                    deadline_text=text_of(card.select_one(".task-card__meta-rows")) or None,
                    proposals_count=proposals,
                    views_count=views,
                    raw={"experience": experience},
                )
            )
        return jobs
