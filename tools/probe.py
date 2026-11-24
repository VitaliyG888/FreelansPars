"""Живой разведочный зонд: фиксирует HTTP-статус, edge/CDN и антибот-маркеры.

Запуск: python tools/probe.py [url ...]
Не входит в рантайм бота; используется для проверки предположений о разборе.
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

TARGETS = [
    ("fl.ru", "https://www.fl.ru/projects/"),
    ("kwork", "https://kwork.ru/projects"),
    ("freelance.ru", "https://freelance.ru/project/search"),
    ("workzilla", "https://work-zilla.com/freelancer"),
    ("weblancer", "https://www.weblancer.net/jobs/"),
    ("freelancejob", "https://freelancejob.ru/projects/"),
    ("pchel", "https://pchel.net/"),
    ("advego", "https://advego.com/"),
    ("profi", "https://profi.ru/"),
    ("youdo", "https://youdo.com/"),
    ("yandex-services", "https://services.yandex.ru/"),
    ("avito", "https://www.avito.ru/all/predlozheniya_uslug"),
    ("freelancehunt", "https://freelancehunt.ru/"),
    ("headhub", "https://headhub.ru/"),
    ("illustrators", "https://illustrators.ru/"),
    ("frilanseri", "https://frilanseri.ru/"),
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MARKERS = {
    "ddos-guard": [r"ddos-guard", r"__ddg", r"DDoS-Guard"],
    "cloudflare": [r"cf-browser-verification", r"cf_chl_opt", r"__cf_chl", r"challenge-platform"],
    "yandex-smartcaptcha": [r"smartcaptcha", r"captcha.yandexcloud", r"smart-captcha"],
    "qrator": [r"qrator", r"__qrator"],
    "variti": [r"variti"],
    "data-dome": [r"datadome", r"dd_cookie"],
    "akamai": [r"_abck", r"akamai"],
    "sberbank-js": [r" sbr_", r"sber"],
}


def classify(body: str, headers: dict[str, str]) -> dict:
    lowered = body.lower()
    found = {}
    for name, pats in MARKERS.items():
        hits = [m for p in pats if (m := re.search(p, lowered, re.I))]
        if hits:
            found[name] = len(hits)
    meta = {
        "title": (re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I) or [None, ""])[1]
        if re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        else "",
    }
    text = re.sub(r"<[^>]+>", " ", body)
    text = re.sub(r"\s+", " ", text).strip()
    return {
        "markers": found,
        "title": meta["title"].strip()[:120],
        "text_len": len(text),
        "keyword_hits": {
            k: text.lower().count(k)
            for k in ("проект", "заказ", "бюджет", "фриланс", "руб", "отклик")
            if text.lower().count(k)
        },
        "server": headers.get("server", ""),
        "powered_by": headers.get("x-powered-by", ""),
        "content_type": headers.get("content-type", ""),
    }


def fetch(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read(600_000)
            body = raw.decode("utf-8", "replace")
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            out = {"status": r.status, "bytes": len(raw)}
            out.update(classify(body, hdrs))
            return out
    except urllib.error.HTTPError as e:
        body = e.read(200_000).decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in e.headers.items()}
        out = {"status": e.code, "bytes": len(body), "error": "http"}
        out.update(classify(body, hdrs))
        return out
    except Exception as e:  # noqa: BLE001 - probe reports raw failure
        return {"status": None, "error": f"{type(e).__name__}: {e}"[:220]}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    targets = TARGETS
    if len(sys.argv) > 1:
        targets = [(u, u) for u in sys.argv[1:]]
    results = {}
    for name, url in targets:
        res = fetch(url)
        res["url"] = url
        results[name] = res
        print(
            f"{name:16} {str(res.get('status')):>4} "
            f"{res.get('server','')[:22]:22} "
            f"markers={','.join(res.get('markers',{})) or '-':32} "
            f"text={res.get('text_len',0):>7} "
            f"{res.get('title','')[:45]}"
        )
        if res.get("error") and not res.get("status"):
            print(f"{'':16}   ! {res['error']}")
    out = f"probe_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
