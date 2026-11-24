"""Перегенерирует `src/freelance_bot/kwork_taxonomy.py` из сохранённых страниц Kwork.

Входные данные
--------------
* ``fixtures/probes/kwork_categories.json``   – дерево меню-заголовка (имена + URL)
* ``fixtures/probes/kwork_category_map.json`` – корневые категории с числовыми id

Использование::

    python tools/build_taxonomy.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

TREE = Path("fixtures/probes/kwork_categories.json")
MAP = Path("fixtures/probes/kwork_category_map.json")
OUT = Path("src/freelance_bot/kwork_taxonomy.py")

tree = json.loads(TREE.read_text(encoding="utf-8"))
root_map = json.loads(MAP.read_text(encoding="utf-8"))

slug_to_id: dict[str, int] = {}
for value in root_map.values():
    slug = str(value.get("seo") or "").strip()
    if slug:
        slug_to_id[slug] = int(value["CATID"])

lines: list[str] = []
for category in tree:
    url = category["url"]
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    key = slug.replace("-", "_")
    lines.append(f'    "{key}": {{')
    lines.append(f'        "name": {json.dumps(category["name"], ensure_ascii=False)},')
    lines.append(f'        "kwork_slug": {json.dumps(slug, ensure_ascii=False)},')
    lines.append(f'        "kwork_id": {slug_to_id.get(slug, 0)},')
    lines.append(f'        "url": {json.dumps(url, ensure_ascii=False)},')
    lines.append('        "groups": {')
    for group in category["groups"]:
        lines.append(
            f'            {json.dumps(group["name"], ensure_ascii=False)}: ['
        )
        for leaf in group["items"]:
            lines.append(
                f'                ({json.dumps(leaf["name"], ensure_ascii=False)}, '
                f'{json.dumps(leaf["url"], ensure_ascii=False)}),'
            )
        lines.append("            ],")
    lines.append("        },")
    lines.append("    },")

header = '''"""Kwork category taxonomy, generated from live public pages.

Do not edit by hand: run ``python tools/build_taxonomy.py`` after re-capturing
``fixtures/probes/kwork_categories.json`` and
``fixtures/probes/kwork_category_map.json``.

The structure mirrors Kwork's own menu: 7 top-level categories, each with
groups of subcategories. The keys are stable slugs (``design``, ``programming``,
...) and are used in ``config.yaml`` (``filters.categories``) and on the CLI
(``--category design``).
"""

from __future__ import annotations

# key -> {name, kwork_id, url, groups: {group name: [(subcategory, url), ...]}}
TAXONOMY: dict[str, dict[str, object]] = {
'''

footer = "}\n"

OUT.write_text(header + "\n".join(lines) + "\n" + footer, encoding="utf-8")
print(f"written {OUT} ({OUT.stat().st_size} bytes)")
