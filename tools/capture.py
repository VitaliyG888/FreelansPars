"""Сохраняет сырые HTML-фикстуры публичных страниц листинга для офлайн-разработки парсеров.

Запуск: python tools/capture.py
Пишет fixtures/live/<platform>.html и <platform>.meta.json.
Это публичные страницы листинга, снятые один раз и используемые только как тестовые фикстуры.
"""
from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "fixtures" / "live"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

TARGETS = {
    "fl_ru": "https://www.fl.ru/projects/",
    "kwork": "https://kwork.ru/projects",
    "freelance_ru": "https://freelance.ru/project/search",
    "workzilla": "https://work-zilla.com/freelancer",
    "weblancer": "https://www.weblancer.net/jobs/",
    "freelancejob": "https://freelancejob.ru/projects/",
    "pchel": "https://pchel.net/",
    "advego": "https://advego.com/",
    "profi": "https://profi.ru/",
    "youdo": "https://youdo.com/",
    "yandex_services": "https://services.yandex.ru/",
    "avito": "https://www.avito.ru/all/predlozheniya_uslug",
    "freelancehunt": "https://freelancehunt.ru/",
    "headhub": "https://headhub.ru/",
    "illustrators": "https://illustrators.ru/",
    "frilanseri": "https://frilanseri.ru/",
}


def grab(url: str, timeout: int = 30) -> tuple[int | None, bytes, dict]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, r.read(2_500_000), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read(1_000_000), {k.lower(): v for k, v in e.headers.items()}
    except Exception as e:  # noqa: BLE001
        return None, str(e).encode(), {}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    OUT.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[1:])
    for name, url in TARGETS.items():
        if only and name not in only:
            continue
        status, body, headers = grab(url)
        (OUT / f"{name}.html").write_bytes(body)
        meta = {
            "platform": name,
            "url": url,
            "status": status,
            "bytes": len(body),
            "server": headers.get("server", ""),
            "content_type": headers.get("content-type", ""),
            "set_cookie": headers.get("set-cookie", "")[:400],
        }
        (OUT / f"{name}.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"{name:18} {status} {len(body):>8} bytes  {headers.get('server','')}")
        time.sleep(2.0 + (hash(name) % 20) / 10.0)  # вежливые интервалы с джиттером
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
