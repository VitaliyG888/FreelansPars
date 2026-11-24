"""Адаптер листинга Kwork.ru.

https://kwork.ru/projects — SPA за Qrator. Серверный HTML не содержит карточек
`div.want-card`, зато содержит полный initial state в inline-JSON
(`wantsListData.wants`). Поэтому основной путь — HTTP + разбор JSON; DOM-разбор
оставлен запасным для браузерного транспорта и старых сохранённых страниц.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from ..models import BudgetKind, ClientInfo, RawJob, TransportKind
from .base import BaseAdapter, attr_of, soup, text_of

MOSCOW = timezone(timedelta(hours=3))


def _extract_state(html: str) -> dict[str, Any] | None:
    """Находит JSON-объект, содержащий ``wantsListData``.

    Kwork вставляет состояние одной огромной inline-конструкцией. Маркер
    ``wantsListData`` находится глубоко внутри, поэтому идём от него назад до
    ближайшей ``{``, из которой JSON парсится целиком.
    """
    marker = '"wantsListData"'
    idx = html.find(marker)
    if idx < 0:
        return None
    decoder = json.JSONDecoder()
    for pos in range(idx, -1, -1):
        if html[pos] != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(html, pos)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("wantsListData"), dict):
            return obj
    return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _format_price(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        amount = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        text = str(value).strip()
        return text or None
    if amount <= 0:
        return None
    return f"{amount:,.0f} ₽".replace(",", " ")


def _as_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=MOSCOW).astimezone(UTC)
        except ValueError:
            continue
    return None


class KworkRuAdapter(BaseAdapter):
    platform = "kwork_ru"
    domain = "kwork.ru"
    transport = TransportKind.HTTP
    wait_selector = "div.want-card"
    prewarm_path = "/"

    def page_url(self, page: int) -> str:
        if page <= 1:
            return self.listing_url
        separator = "&" if "?" in self.listing_url else "?"
        return f"{self.listing_url}{separator}page={page}"

    def parse(self, html: str, base_url: str) -> list[RawJob]:
        jobs = self._parse_state(html, base_url)
        if jobs:
            return jobs
        return self._parse_dom(html, base_url)

    # ------------------------------------------------------------- JSON state
    def _parse_state(self, html: str, base_url: str) -> list[RawJob]:
        state = _extract_state(html)
        if state is None:
            return []
        data = state.get("wantsListData")
        if not isinstance(data, dict):
            return []
        wants = data.get("wants")
        if not isinstance(wants, list):
            pagination = data.get("pagination")
            wants = pagination.get("data") if isinstance(pagination, dict) else []
        if not isinstance(wants, list):
            return []

        categories = state.get("categories")
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for want in wants:
            if not isinstance(want, dict):
                continue
            external_id = str(want.get("id") or "").strip()
            title = str(want.get("name") or "").strip()
            if not external_id or not title or external_id in seen:
                continue
            seen.add(external_id)

            price_text = _format_price(want.get("priceLimit"))
            raw_user = want.get("user")
            user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
            raw_user_data = user.get("data")
            user_data: dict[str, Any] = raw_user_data if isinstance(raw_user_data, dict) else {}
            hire_percent = user_data.get("wants_hired_percent")
            hire_rate = None
            if hire_percent not in (None, ""):
                try:
                    hire_rate = float(str(hire_percent)) / 100.0
                except ValueError:
                    hire_rate = None

            raw_dates = want.get("wantDates")
            dates: dict[str, Any] = raw_dates if isinstance(raw_dates, dict) else {}
            category_id = want.get("category_id")
            category = None
            if isinstance(categories, dict):
                category_info = categories.get(str(category_id))
                if isinstance(category_info, dict):
                    category = category_info.get("name")

            time_left = str(want.get("timeLeft") or "").strip()
            jobs.append(
                RawJob(
                    platform=self.platform,
                    external_id=external_id,
                    url=self.abs_url(f"/projects/{external_id}", base_url),
                    title=title,
                    description=str(want.get("description") or "").strip(),
                    budget_text=price_text,
                    budget_kind=BudgetKind.FIXED if price_text else BudgetKind.UNKNOWN,
                    category=str(category) if category else None,
                    published_at=_as_datetime(want.get("date_create")),
                    published_text=str(dates.get("dateCreate") or "") or None,
                    deadline_at=_as_datetime(want.get("date_expire")),
                    deadline_text=f"Осталось: {time_left}" if time_left else None,
                    proposals_count=_as_int(want.get("kwork_count")),
                    views_count=_as_int(want.get("views_dirty")),
                    client=ClientInfo(
                        name=str(user.get("username") or "") or None,
                        external_id=str(user.get("USERID") or "") or None,
                        hire_rate=hire_rate,
                        projects_posted=_as_int(user_data.get("wants_count")),
                    ),
                    raw={
                        "source": "initial-state",
                        "category_id": category_id,
                        "kwork_count": want.get("kwork_count"),
                        "views": want.get("views_dirty"),
                    },
                )
            )
        return jobs

    # -------------------------------------------------------------- DOM fallback
    def _parse_dom(self, html: str, base_url: str) -> list[RawJob]:
        doc = soup(html)
        jobs: list[RawJob] = []
        for card in doc.select("div.want-card"):
            link = card.select_one(".wants-card__header-title a")
            if link is None:
                continue
            href = attr_of(link, "href") or ""
            m = re.search(r"/projects/(\d+)", href)
            if not m:
                continue

            description = ""
            hidden = card.select_one(
                '.wants-card__description-text > div[style*="display: none"] div.d-inline'
            )
            if hidden is not None:
                description = text_of(hidden)
            else:
                description = text_of(
                    card.select_one(".wants-card__description-text div.d-inline")
                )

            price_raw = text_of(card.select_one(".wants-card__price"))
            price_raw = re.sub(r"^Цена\s*", "", price_raw, flags=re.IGNORECASE)

            stats = text_of(card.select_one(".want-payer-statistic"))
            hire_rate = None
            mh = re.search(r"Нанято:\s*(\d+)\s*%", stats)
            if mh:
                hire_rate = int(mh.group(1)) / 100.0
            posted = None
            mp = re.search(r"Размещено проектов на бирже:\s*(\d+)", stats)
            if mp:
                posted = int(mp.group(1))

            buyer = card.select_one('a[href*="/user/"]')
            informers = text_of(card.select_one(".want-card__informers"))
            proposals = None
            mo = re.search(r"Предложений:\s*(\d+)", informers)
            if mo:
                proposals = int(mo.group(1))
            deadline_text = None
            md = re.search(r"Осталось:\s*([^П]+?)(?:\s*Предложений|$)", informers)
            if md:
                deadline_text = f"Осталось: {md.group(1).strip()}"

            jobs.append(
                RawJob(
                    platform=self.platform,
                    external_id=m.group(1),
                    url=self.abs_url(href, base_url),
                    title=text_of(link),
                    description=description,
                    budget_text=price_raw or None,
                    budget_kind=BudgetKind.FIXED if price_raw else BudgetKind.UNKNOWN,
                    proposals_count=proposals,
                    deadline_text=deadline_text,
                    client=ClientInfo(
                        name=text_of(buyer) or None,
                        external_id=(attr_of(buyer, "href") or "").rsplit("/", 1)[-1] or None,
                        hire_rate=hire_rate,
                        projects_posted=posted,
                    ),
                    raw={"stats": stats, "informers": informers, "source": "dom"},
                )
            )
        return jobs
