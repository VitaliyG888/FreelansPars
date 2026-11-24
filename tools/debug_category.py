"""Разовая отладка: почему заказ попал не в ту категорию?"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")

from freelance_bot.categories import (  # noqa: E402
    CATEGORIES,
    EXTRA_KEYWORDS,
    WEIGHT_EXTRA,
    WEIGHT_GROUP,
    WEIGHT_LEAF,
    _fields,
    _term_score,
)
from freelance_bot.models import Job  # noqa: E402
from freelance_bot.text import contains_term  # noqa: E402

job = Job(
    platform="test",
    external_id="1",
    url="https://example.com",
    title="Разработчик с навыками Python, Telegram-боты, AI-инструменты",
    description=(
        "Ищем разработчика в команду, который умеет превращать повторяющиеся "
        "бизнес-процессы в работающие инструменты: ботов, интеграции и внутренние сервисы."
    ),
    category=None,
    skills=[],
)

fields = _fields(job)
for category in CATEGORIES:
    total = 0.0
    hits = []
    for group in category.groups:
        gs = _term_score(fields, group.name)
        if gs:
            total += gs * WEIGHT_GROUP
            hits.append(f"group {group.name!r} {gs}")
        for leaf in group.items:
            ls = _term_score(fields, leaf.name)
            if ls:
                total += ls * WEIGHT_LEAF
                hits.append(f"leaf {leaf.name!r} {ls}")
    for term in EXTRA_KEYWORDS.get(category.key, ()):
        ts = _term_score(fields, term)
        if ts:
            total += ts * WEIGHT_EXTRA
            hits.append(f"extra {term!r} {ts}")
    if hits:
        print(f"{category.key}: total={total:.2f}")
        for hit in hits:
            print("   ", hit)

print()
print("contains 'бот' in title:", contains_term(job.title, "бот"))
print("contains 'Telegram-боты' term 'телеграм бот':", contains_term(job.title, "телеграм бот"))
