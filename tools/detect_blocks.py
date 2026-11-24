"""Находит повторяющиеся DOM-блоки (кандидаты в карточки листинга) на странице.

Эвристика: группируем элементы по (тег, отсортированный список классов, глубина)
и возвращаем группы с >= min_repeats элементами, содержащими ссылку и достаточно текста.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

from bs4 import BeautifulSoup

NOISE = {
    "li", "a", "span", "div", "p", "option", "label", "input", "br", "svg", "use",
    "path", "g", "i", "b", "strong", "em", "td", "tr", "th", "button",
}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print("usage: detect_blocks.py <file.html> [min_repeats]")
        return 2
    path = Path(sys.argv[1])
    min_repeats = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "lxml")

    groups: dict[tuple, list] = defaultdict(list)
    for el in soup.find_all(True):
        classes = el.get("class") or []
        if not classes:
            continue
        # пропускаем совсем общие обёртки
        sig = (el.name, tuple(sorted(classes)))
        groups[sig].append(el)

    scored = []
    for sig, els in groups.items():
        if len(els) < min_repeats:
            continue
        sample = els[0]
        text = " ".join(sample.get_text(" ", strip=True).split())
        links = sample.find_all("a", href=True)
        if len(text) < 40 or not links:
            continue
        # предпочитаем блоки, чьи дети тоже повторяются => контейнеры карточек
        child_sigs = {(c.name, tuple(sorted(c.get("class") or []))) for c in sample.find_all(True)}
        score = len(els) * min(len(text), 400) + (2000 if len(child_sigs) > 5 else 0)
        scored.append((score, sig, len(els), text[:110], links[0]["href"][:70]))

    scored.sort(reverse=True)
    print(f"# {path.name}: {len(scored)} candidate groups\n")
    for score, sig, n, text, href in scored[:15]:
        print(f"{n:>4}x  <{sig[0]} class=\"{' '.join(sig[1])[:95]}\">")
        print(f"      score={score} text={text!r}")
        print(f"      href={href}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
