"""Разовая разведка: загружает публичное дерево категорий Kwork и выгружает его.

Результат используется для жёсткого задания таксономии в `freelance_bot/categories.py`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from curl_cffi import requests  # noqa: E402

URLS = [
    "https://kwork.ru/categories",
    "https://kwork.ru/projects?c=11",
]

out = Path("fixtures/probes")
out.mkdir(parents=True, exist_ok=True)

for url in URLS:
    try:
        resp = requests.get(url, impersonate="chrome", timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(url, "ERROR", type(exc).__name__, exc)
        continue
    html = resp.text
    print(url, "status", resp.status_code, "len", len(html))
    slug = re.sub(r"\W+", "_", url)[-40:]
    (out / f"kwork_categories_{slug}.html").write_text(html, encoding="utf-8")
