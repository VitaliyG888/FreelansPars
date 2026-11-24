"""Разовая разведка: контекст вокруг названия листа на странице Kwork."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

html = Path("fixtures/probes/kwork_leaf_parsery.html").read_text(encoding="utf-8")
idx = html.find('"name":"Парсеры"')
print(re.sub(r"\s+", " ", html[max(0, idx - 1200) : idx + 600]))
