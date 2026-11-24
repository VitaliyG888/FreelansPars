"""Загружает заданный список URL в fixtures/live/ для работы над парсером.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture import grab  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "fixtures" / "live"

URLS = {
    "workzilla_cat": "https://work-zilla.com/freelance-jobs/development-and-it",
    "pchel_projects": "https://pchel.net/projects/",
    "advego_job": "https://advego.com/job/",
    "kwork_c41": "https://kwork.ru/projects?c=41",
    "illustrators_orders": "https://illustrators.ru/orders",
    "frilanseri_root": "https://frilanseri.ru/",
}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    only = set(sys.argv[1:])
    for name, url in URLS.items():
        if only and name not in only:
            continue
        status, body, headers = grab(url)
        (OUT / f"{name}.html").write_bytes(body)
        (OUT / f"{name}.meta.json").write_text(
            json.dumps(
                {"url": url, "status": status, "bytes": len(body),
                 "server": headers.get("server", ""),
                 "content_type": headers.get("content-type", "")},
                ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"{name:22} {status} {len(body):>8}  {headers.get('server','')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
