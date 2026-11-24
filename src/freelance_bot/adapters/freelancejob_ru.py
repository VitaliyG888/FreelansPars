"""Адаптер листинга FreelanceJob.ru.

Источник: https://freelancejob.ru/projects/ — обычный серверный HTML, карточки
`div.x17`. За маркером бюджета (`div.x19`) идёт метка единицы.
"""

from __future__ import annotations

import re

from bs4 import Tag

from ..models import BudgetKind, RawJob, TransportKind
from .base import BaseAdapter, attr_of, soup, text_of


class FreelanceJobRuAdapter(BaseAdapter):
    platform = "freelancejob_ru"
    domain = "freelancejob.ru"
    transport = TransportKind.HTTP
    prewarm_path = "/"

    def parse(self, html: str, base_url: str) -> list[RawJob]:
        doc = soup(html)
        jobs: list[RawJob] = []
        for card in doc.select("div.x17"):
            link = card.select_one("a.big")
            if link is None:
                continue
            href = attr_of(link, "href") or ""
            m = re.search(r"/vacancy/(\d+)", href)
            if not m:
                continue

            # Описание — первый div без класса после блока заголовка.
            description = ""
            children = list(card.find_all("div", recursive=False))
            for child in children:
                if not isinstance(child, Tag):
                    continue
                classes = attr_of(child, "class") or ""
                if classes:
                    continue
                if child.select_one("a.big") is not None:
                    continue  # обёртка заголовка
                description = text_of(child)
                break

            budget_text = text_of(card.select_one(".x19")) or None
            unit = ""
            budget_div = card.select_one(".x18")
            if budget_div is not None:
                unit = text_of(budget_div).replace(budget_text or "", "").strip()

            meta = text_of(card.select_one(".x20"))
            proposals = None
            mprop = re.search(r"Ответов:\s*(\d+)", meta)
            if mprop:
                proposals = int(mprop.group(1))
            views = None
            mview = re.search(r"Просмотров:\s*(\d+)", meta)
            if mview:
                views = int(mview.group(1))

            kind = BudgetKind.UNKNOWN
            if budget_text:
                kind = BudgetKind.HOURLY if "час" in unit.lower() else BudgetKind.FIXED

            jobs.append(
                RawJob(
                    platform=self.platform,
                    external_id=m.group(1),
                    url=self.abs_url(href, base_url),
                    title=text_of(link),
                    description=description,
                    budget_text=budget_text,
                    budget_kind=kind,
                    published_text=meta or None,
                    proposals_count=proposals,
                    views_count=views,
                    raw={"meta": meta, "unit": unit},
                )
            )
        return jobs
