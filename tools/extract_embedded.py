"""Извлекает встроенные JSON-данные (JSON-LD, data-page, __NEXT_DATA__) из сохранённой фикстуры.
"""
from __future__ import annotations

import html as htmllib
import json
import sys
from pathlib import Path

from bs4 import BeautifulSoup


def walk(obj, depth=0, maxdepth=3):
    """Печатает неглубокую структуру JSON-объекта."""
    pad = "  " * depth
    if depth > maxdepth:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                n = len(v)
                print(f"{pad}{k}: {type(v).__name__}[{n}]")
                walk(v, depth + 1, maxdepth)
            else:
                s = str(v)
                print(f"{pad}{k}: {s[:90]}")
    elif isinstance(obj, list) and obj:
        print(f"{pad}[0] of {len(obj)}:")
        walk(obj[0], depth + 1, maxdepth)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print("usage: extract_embedded.py <file.html> [mode]  mode=ld|page|next")
        return 2
    path = Path(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "ld"
    raw = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(raw, "lxml")

    if mode == "ld":
        for i, tag in enumerate(soup.select('script[type="application/ld+json"]')):
            print(f"--- ld+json #{i}")
            try:
                walk(json.loads(tag.string or "{}"), maxdepth=2)
            except Exception as e:  # noqa: BLE001
                print("  parse error", e)
    elif mode == "page":
        for i, tag in enumerate(soup.select("[data-page]")):
            payload = tag.get("data-page")
            print(f"--- data-page #{i} len={len(payload or '')}")
            try:
                walk(json.loads(htmllib.unescape(payload or "{}")), maxdepth=3)
            except Exception as e:  # noqa: BLE001
                print("  parse error", e)
    elif mode == "next":
        tag = soup.select_one("#__NEXT_DATA__")
        if not tag:
            print("no __NEXT_DATA__")
            return 1
        walk(json.loads(tag.string or "{}"), maxdepth=4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
