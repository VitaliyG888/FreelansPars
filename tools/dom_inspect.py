"""Выводит outerHTML первых N подходящих элементов, чтобы писать и проверять селекторы.
"""
from __future__ import annotations

import sys
from pathlib import Path

from bs4 import BeautifulSoup


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 4:
        print("usage: inspect.py <file.html> <css-selector> [count]")
        return 2
    path, selector = sys.argv[1], sys.argv[2]
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    html = Path(path).read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    nodes = soup.select(selector)
    print(f"# {path} :: {selector} -> {len(nodes)} matches\n")
    for node in nodes[:count]:
        text = " ".join(node.get_text(" ", strip=True).split())
        print(node.prettify()[:3000])
        print("---TEXT---", text[:500])
        print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
