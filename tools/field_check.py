"""Дамп проверки полей для каждого адаптера на сохранённых фикстурах."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "src")

from freelance_bot.adapters import ADAPTERS  # noqa: E402
from freelance_bot.config import PlatformConfig  # noqa: E402
from freelance_bot.normalize import normalise  # noqa: E402

FIXTURES = {
    "fl_ru": "fixtures/live/fl_ru.html",
    "freelance_ru": "fixtures/live/freelance_ru.html",
    "freelancejob_ru": "fixtures/live/freelancejob.html",
    "pchel_net": "fixtures/live/pchel_projects.html",
    "kwork_ru": "fixtures/live/browser_kwork.ru_projects.html",
    "weblancer_net": "fixtures/live/browser_www.weblancer.net_jobs_.html",
    "freelancehunt_com": "fixtures/live/browser_freelancehunt.com_projects.html",
}
FX = {"RUB": 1.0, "UAH": 2.3, "USD": 95.0, "EUR": 102.0}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for platform, path in FIXTURES.items():
        cls = ADAPTERS[platform]
        cfg = PlatformConfig(listing_url=f"https://{cls.domain}/")
        adapter = cls(cfg)
        jobs = adapter.parse(Path(path).read_text(encoding="utf-8"), cfg.listing_url)
        print(f"\n=== {platform}: {len(jobs)} jobs")
        for raw in jobs[:3]:
            job = normalise(raw, fx=FX)
            print(f"  id={job.external_id}")
            print(f"  title={job.title[:70]}")
            print(f"  budget={job.budget_amount} {job.currency} kind={job.budget_kind.value}"
                  f" -> {job.budget_rub_estimate} RUB")
            print(f"  published={job.published_at}  deadline={job.deadline_at}")
            print(f"  proposals={job.proposals_count}  views={raw.views_count}"
                  f"  urgent={job.is_urgent}")
            print(f"  client={job.client.name} hire={job.client.hire_rate}")
            print(f"  desc[{len(job.description)}]={job.description[:90]}")
            print(f"  url={job.url[:80]}")
        # статистика полноты
        with_budget = sum(1 for j in jobs if j.budget_text)
        with_props = sum(1 for j in jobs if j.proposals_count is not None)
        with_time = sum(1 for j in jobs if j.published_text)
        print(f"  -- budget {with_budget}/{len(jobs)}, proposals {with_props}/{len(jobs)},"
              f" time {with_time}/{len(jobs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
