# Freelance Bot — Engineering Report

## Executive summary

A complete Python 3.11+ project was built for public Russian freelance-marketplace collection, explainable scoring, and Russian first-contact message generation. Seven sources (`fl.ru`, `freelance.ru`, `freelancejob.ru`, `pchel.net`, `kwork.ru`, `weblancer.net`, `freelancehunt.com`) are implemented against real captured HTML and passed a live dry run: 192 jobs in 51 s, 8 profile-eligible orders, 8 guard-clean messages, and zero duplicates on the next cycle, with all seven platforms returning `ok`. Eleven further requested sources are either implemented but disabled (`avito.ru`, Qrator 439 on datacenter IPs) or explicitly excluded because they are login-gated, have no public order feed, are mobile-only, or do not serve a working site. The bot respects robots.txt, throttles per domain with token buckets and jittered exponential backoff, never stores marketplace passwords, defaults to dry-run, and ships no auto-submit transport for auth-gated outreach (live mode blocks unless the operator injects a `DeliveryTransport`). Validation is green: 162 tests, `ruff check` clean, `mypy` clean, project-scoped `pip-audit` clean.

## 1. Scope and non-goals

### In scope

- Public listing collection from the requested Russian freelance marketplaces.
- Normalisation of budgets, currencies, relative timestamps, deadlines, and client data.
- Explainable ranking against a configurable skill/budget/client/competition/freshness/urgency model.
- Russian message generation with a deterministic fallback when no LLM is available.
- A quality guard that blocks links, AI tells, generic openers, over-length messages, and low-specificity text.
- A reviewable outbox and a human-approval gate.
- Tests, fixtures, README, trial evidence, and maintenance guidance.

### Explicit non-goals

- Login, paywall, or CAPTCHA-wall bypass.
- Credential harvesting or plaintext password storage.
- Automated submission through authenticated marketplace endpoints.
- Upwork/Fiverr integration.
- Scraping sources whose order feed is not public.

## 2. Research summary

Reconnaissance used `tools/probe.py`, `tools/capture.py`, `tools/browser_probe.py`, and `tools/field_check.py`. Raw pages are preserved in `fixtures/live/`; probe metadata is in `fixtures/probes/`.

| Source | Observed protection / structure | Decision | Evidence |
|---|---|---|---|
| `fl.ru` | DDoS-Guard edge; listing HTML is server-rendered and parseable | HTTP | `fixtures/live/fl_ru.html` |
| `freelance.ru` | Inertia-style markup; `article.task-card` cards in initial HTML | HTTP | `fixtures/live/freelance_ru.html` |
| `freelancejob.ru` | Plain server-rendered `div.x17` cards; budget unit in sibling node | HTTP | `fixtures/live/freelancejob.html` |
| `pchel.net` | Server-rendered, 50 cards/page; Cloudflare edge without challenge on listing | HTTP | `fixtures/live/pchel_projects.html` |
| `kwork.ru` | SPA behind Qrator; Yandex SmartCaptcha markers seen intermittently | Browser | `fixtures/live/browser_kwork.ru_projects.html` |
| `weblancer.net` | Cloudflare managed challenge; passed only with stealth browser | Browser | `fixtures/live/browser_www.weblancer.net_jobs_.html` |
| `freelancehunt.com` | Cloudflare challenge; Ukrainian exchange, UAH budgets | Browser | `fixtures/live/browser_freelancehunt.com_projects.html` |
| `avito.ru` | Qrator HTTP 439 to datacenter IPs, including browser | Implemented, disabled | `fixtures/live/browser_www.avito.ru_*.html` |
| `workzilla` | Task feed behind login; public categories only | Not scraped | `fixtures/live/workzilla*.html` |
| `advego` | Order list requires account; `/job/` redirects to login | Not scraped | `fixtures/live/advego*.html` |
| `profi` | No public order feed; matching happens after registration | Not scraped | `fixtures/live/profi.html` |
| `youdo` | Order feed requires account; public page is a JS shell | Not scraped | `fixtures/live/youdo.html` |
| `yandex_services` | No public order feed; routing in cabinet | Not scraped | `fixtures/live/yandex_services.html` |
| `illustrators` | Artwork marketplace, not an order board | Not scraped | `fixtures/live/illustrators*.html` |
| `frilanseri` | Project section 404; landing page only | Not scraped | `fixtures/live/frilanseri*.html` |
| `headhub` | TLS handshake fails; no working site | Not scraped | probe log |
| `fiwork` | RuStore mobile app only; no web listing | Not scraped | probe log |
| `freeworker` | RuStore mobile app only; no web listing | Not scraped | probe log |

`freelancehunt.ru` is a hijacked casino domain and is not used. The working exchange is `freelancehunt.com`.

## 3. Architecture

```text
┌────────────────────────────────────────────────────────────────────┐
│ CLI: run / trial / platforms / parse-fixture / score / outbox      │
└───────────────────────────────┬────────────────────────────────────┘
                                │
                                v
┌────────────────────────────────────────────────────────────────────┐
│ pipeline.collect                                                   │
│  ├─ build_adapters(config)                                         │
│  ├─ build_context(config, secrets)                                 │
│  │   ├─ ProxyPool (round-robin / sticky / none)                    │
│  │   ├─ DomainLimiter (token bucket per domain)                    │
│  │   ├─ RobotsCache (respect_robots_txt)                           │
│  │   └─ BrowserFetcher (lazy Playwright, one context per domain)   │
│  └─ for each adapter: fetch -> parse -> PlatformRunResult          │
└───────────────────────────────┬────────────────────────────────────┘
                                │ RawJob[]
                                v
┌────────────────────────────────────────────────────────────────────┐
│ normalize: money, currency -> RUB, relative/absolute time, deadline│
└───────────────────────────────┬────────────────────────────────────┘
                                │ Job[]
                                v
┌────────────────────────────────────────────────────────────────────┐
│ dedupe within cycle, SQLite seen-state, save every collected job   │
└───────────────────────────────┬────────────────────────────────────┘
                                │
                                v
┌────────────────────────────────────────────────────────────────────┐
│ ScoringEngine                                                      │
│  budget + skill_match + client_quality + competition + freshness   │
│  + urgency_fit, then red-flag penalty                              │
└───────────────────────────────┬────────────────────────────────────┘
                                │ ScoredJob[]
                                v
┌────────────────────────────────────────────────────────────────────┐
│ eligibility gate: min_skill_match / min_total_score                │
└───────────────────────────────┬────────────────────────────────────┘
                                │ eligible + new
                                v
┌────────────────────────────────────────────────────────────────────┐
│ MessageGenerator                                                   │
│  LLM JSON analysis + draft -> guard -> one repair -> fallback      │
└───────────────────────────────┬────────────────────────────────────┘
                                │ GeneratedMessage
                                v
┌────────────────────────────────────────────────────────────────────┐
│ guard: length, links, AI tells, Cyrillic, emoji, specificity       │
└───────────────────────────────┬────────────────────────────────────┘
                                │ clean message
                                v
┌────────────────────────────────────────────────────────────────────┐
│ OutboxSender                                                       │
│  dry-run: .md + messages.jsonl                                     │
│  live: approval + transport + hourly/cooldown gate                 │
└───────────────────────────────┬────────────────────────────────────┘
                                │
                                v
┌────────────────────────────────────────────────────────────────────┐
│ SQLite: jobs, messages, cycles                                     │
└────────────────────────────────────────────────────────────────────┘
```

## 4. Implementation map

```text
src/freelance_bot/
  cli.py                 Typer CLI and Rich rendering
  cli_support.py         preflight, platform overview, score table
  config.py              pydantic config + Secrets + FX
  models.py              RawJob, Job, ScoredJob, CycleReport
  normalize.py           money/date/deadline normalisation
  pipeline.py            collect, run_cycle, run_trial
  storage.py             SQLite schema and persistence
  sender.py              OutboxSender + RateGate
  text.py                word-boundary/stem matching, contact stripping
  adapters/
    base.py              fetch/escalation contract
    registry.py          adapter map and context construction
    fl_ru.py
    freelance_ru.py
    freelancejob_ru.py
    pchel_net.py
    kwork_ru.py
    weblancer_net.py
    freelancehunt_com.py
    avito_ru.py
  net/
    stealth.py           curl_cffi TLS impersonation + classification
    browser.py           stealth Playwright + CAPTCHA injection
    captcha.py           CapSolver / 2Captcha clients
    proxies.py           proxy pool and cooldown
    ratelimit.py         token bucket + backoff
    robots.py            robots.txt cache
  scoring/
    engine.py            6 factors + eligibility gate
    redflags.py          regex red flags + diminishing penalty
  llm/
    client.py            OpenAI-compatible client + loose JSON parsing
    prompts.py           analysis and repair prompts
    message.py           LLM path, repair, deterministic fallback
    guard.py             message quality gate
```

### Entrypoint

```powershell
python -m pip install -e ".[browser,dev]"
python -m playwright install chromium
$env:FREELANCE_BOT_CONFIG = "config.yaml"
$env:PYTHONIOENCODING = "utf-8"
python -m freelance_bot.cli preflight
python -m freelance_bot.cli run --top 10
```

The package also exposes `freelance-bot` as a console script.

### Core cycle

`run_cycle` in `pipeline.py` does the following, in order:

1. `collect` all enabled adapters sequentially.
2. Normalise every `RawJob`; a single bad budget/date does not kill the cycle.
3. Deduplicate within the cycle by `platform:external_id`, keeping the richest description.
4. Score every unique job.
5. Compute `jobs_new` before saving.
6. Persist **all** collected jobs, not only the surfaced top-N. This is what makes second-cycle dedupe correct.
7. Filter by `engine.eligible`.
8. Generate messages only for jobs that are both eligible and newly seen.
9. Save messages; send through the dry-run/approval gate.
10. Persist the cycle report.

A regression test covers the small-top-N case that previously made un-surfaced jobs look new on every cycle.

## 5. Configuration

`config.yaml` and `config.example.yaml` are identical in this delivery. Secrets are environment-only:

```ini
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=
LLM_MODEL=gpt-4o-mini
CAPSOLVER_API_KEY=
TWOCAPTCHA_API_KEY=
RU_PROXY_URLS=
FREELANCE_BOT_DB=var/freelance.db
FREELANCE_BOT_OUTBOX=var/outbox
```

Representative scoring configuration:

```yaml
runtime:
  dry_run: true
  poll_interval_seconds: 900
  max_jobs_per_platform: 40
  top_n: 10

network:
  proxy_strategy: round_robin
  respect_robots_txt: true
  default_rate: { requests_per_minute: 4, burst: 2 }
  backoff: { base_seconds: 3, max_seconds: 180, jitter: true, max_retries: 3 }

scoring:
  weights:
    budget: 0.28
    skill_match: 0.22
    client_quality: 0.18
    competition: 0.14
    freshness: 0.10
    urgency_fit: 0.08
  red_flag_penalty: 0.35
  min_skill_match: 0.45
  min_total_score: 0
  skill_match:
    skills: [python, парсинг, скрапинг, api, интеграция, автоматизация, телеграм, бот]
    exclude_keywords: [1с, бухгалтер, копирайт, статья, таргет, дизайн, книг, комикс]

messaging:
  require_human_approval: true
  max_sends_per_hour: 6
  min_seconds_between_sends: 240
```

The full matrix, red-flag regexes, platform limits, and notes are in `config.yaml`.

## 6. Scoring model

The total is:

```text
score = 100 * (Σ factor.raw * factor.weight / Σ weight) * (1 - penalty)
penalty = min(0.8, severity_sum * red_flag_penalty)
```

`severity_sum` uses `severity * 0.75^i` over sorted flags, so many small flags do not dominate one serious flag.

### Skill matching correctness

An early version used plain substring matching. That made `бот` match `работа` and `обработка`, promoting unrelated orders. The final implementation:

- matches standalone terms or Russian stems;
- weights title/declared skills above prose;
- strips messenger handles, `t.me` links, and emails before body matching;
- treats excluded topics as a hard `0.10` skill score;
- has a regression test for the `бот` / `работа` case.

### Eligibility

`scoring.min_skill_match: 0.45` means:

- a skill in the title/declared skills passes;
- two distinct skills in the description pass;
- a single accidental body mention does not.

This gate is configurable and can be set to `0` to rank the full feed without filtering.

## 7. Message generation and guard

### LLM path

The prompt requests strict JSON:

```json
{
  "need_summary_ru": "...",
  "requirements": ["..."],
  "deliverables": ["..."],
  "risks": ["..."],
  "questions_to_ask": ["..."],
  "estimated_hours": 8,
  "suggested_price_rub": 20000,
  "first_message": "..."
}
```

If the model returns invalid JSON, the client recovers fenced/prefaced JSON. If the draft fails the guard, one repair attempt is made. If repair still fails, the deterministic fallback is used.

### Fallback path

The fallback is not a generic template. It selects an approach from job signals:

- parser/scraper -> data collection, pagination, retries, CSV/DB output;
- Telegram/bot -> commands, state, error handling, deployment;
- API/integration -> contract, timeouts, retries, errors;
- Python/Django/FastAPI -> data model, API, tests, maintainability;
- SQL/PostgreSQL -> schema, queries, indexes, exports;
- React/layout -> responsive frontend, components, browser checks;
- Docker -> packaging and reproducible startup;
- LLM/GPT/NLP -> data preparation, prompts, constraints, quality checks;
- service/broadcast -> API, event handling, retries, logging, monitoring;
- design/text categories -> category-appropriate wording.

It also strips a greeting copied from the client brief, so the outbox never opens with “Здравствуйте! ... Здравствуйте, у меня есть...”.

### Guard

`llm/guard.py` rejects:

- over-limit messages per platform;
- links, `t.me`, emails, and external handles;
- banned phrases and AI tells;
- too many emoji/exclamation marks;
- low Cyrillic ratio;
- low specificity overlap with the title and first 400 characters;
- more than two questions;
- bullet lists in a first message.

A failed message is stored with violations and never sent.

## 8. Anti-bot and safety posture

- **robots.txt** is checked before a listing fetch; a disallowed URL produces `Outcome.DISABLED`.
- **Per-domain token buckets** default to 2–4 requests/minute with burst 1–2.
- **Backoff** is exponential with jitter, capped at 180 s and 3 retries.
- **TLS impersonation** uses `curl_cffi` Chrome/Safari profiles.
- **Browser escalation** is one attempt, with a minimal stealth script, locale/timezone/viewport alignment, and asset blocking.
- **CAPTCHA** is solved only after a confirmed challenge and only if a solver key is configured.
- **No credentials** are read, stored, or transmitted. `.env.example` contains no marketplace login/password fields.
- **No bundled transport.** Live mode requires per-message approval, a configured `DeliveryTransport` callback, and an hourly/cooldown gate. With `dry_run=false` and no transport, `send()` returns `ok=False`/`mode="blocked"` and writes no artifact rather than pretending to deliver.
- **Dry-run default** in both config and `OutboxSender`; `SendResult.delivered` and the `messages.jsonl` record make actual transport execution auditable.

## 9. Validation

### Static and unit validation

```text
python -m ruff check src tests   -> All checks passed!
python -m mypy src               -> Success: no issues found in 36 source files
python -m pytest -q              -> 162 passed
python -m pip_audit . --skip-editable
                                 -> No known vulnerabilities found
```

### Live dry-run trial

Environment: Windows, Python 3.13.5, datacenter IP, no LLM key, no CAPTCHA solver key, no proxy, dry-run forced.

Command:

```powershell
python -m freelance_bot.cli trial --minutes 8 --interval 300
```

| Cycle | Platforms ok | Jobs | Eligible | Messages | Sent (dry-run) | Duration |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 7/7 | 192 | 8 | 8 | 8 | 51.1 s |
| 2 | 7/7 | 192 | 8 | 0 | 0 | 52.4 s |

First-cycle per-platform latency:

| Platform | Jobs | Latency |
|---|---:|---:|
| `fl_ru` | 30 | 3.3 s |
| `freelance_ru` | 25 | 2.8 s |
| `freelancejob_ru` | 40 | 3.9 s |
| `pchel_net` | 50 | 2.9 s |
| `kwork_ru` | 12 | 14.8 s |
| `weblancer_net` | 20 | 9.7 s |
| `freelancehunt_com` | 15 | 11.4 s |

No challenge was raised in this window. All eight messages passed the guard with zero violations. The second cycle produced no duplicate messages.

### Sample generated messages

```text
Здравствуйте! Увидел задачу «Разработчик с навыками Python, Telegram-боты,
AI-инструменты». Ищем разработчика в команду, который умеет превращать
повторяющиеся бизнес-процессы в работающие инструменты: ботов, интеграции
и внутренние сервисы. Могу сделать Telegram-бота: команды, хранение
состояния, обработка ошибок и запуск на вашем сервере. Подскажите, какой
бюджет вы закладываете на эту задачу?

Здравствуйте! Увидел задачу «Спарсить все контакты дизайнеров, риелторов».
Требуется спарсить контакты дизайнеров, риелторов и строителей из
Екатеринбурга. Могу собрать сборщик данных: настрою источники, пагинацию,
обработку ошибок и выгрузку в CSV или базу. Подскажите, какой бюджет вы
закладываете на эту задачу?
```

## 10. Delivery inventory

```text
README.md                       operator quick start and platform matrix
REPORT.md                       this report
config.yaml                     runnable configuration
config.example.yaml             template (identical to config.yaml)
.env.example                    secrets template, no marketplace credentials
pyproject.toml                  dependencies, ruff, mypy, pytest config
src/freelance_bot/              application package
tests/                         162 tests + tests/README.md
fixtures/live/                  real captured HTML
fixtures/probes/                reconnaissance JSON
tools/                          probe/capture/field-check utilities
var/                            trial database + dry-run outbox
```

## 11. Honest limitations

1. **Browser-only sources are fragile.** `kwork_ru`, `weblancer_net`, and `freelancehunt_com` use hashed/Tailwind markup and anti-bot layers. The fixture tests catch selector drift, but they cannot prevent it.
2. **No live send was performed.** The sender stops at the reviewed outbox because every outreach endpoint is auth-gated. This is intentional, not an unfinished transport.
3. **No LLM key was present.** Message quality in the trial is the deterministic fallback. An LLM key would increase variety; the fallback is deliberate so the pipeline does not stop.
4. **Client quality is sparse.** Most boards do not expose hire rate or spend. Missing data is neutral, not invented.
5. **FX and medians are static.** `UAH -> RUB` and category medians are explicit config values; refresh them before commercial use.
6. **Avito needs a Russian residential proxy.** The adapter exists but is disabled by default and must be re-verified with a residential egress.
7. **Some requested sources cannot be scraped publicly.** Login-gated, mobile-only, or non-existent order feeds are documented in `authenticated_only` rather than faked.
8. **The trial is one IP and one time window.** Challenge frequency can change by region, hour, and egress reputation.

## 12. Maintenance plan

### Weekly

- Check `python -m freelance_bot.cli platforms` and `preflight`.
- Inspect the outbox for tone and specificity drift.
- Review `var/freelance.db` cycle reports for new `challenge` or `parse_error` outcomes.

### Monthly

- Re-capture fixtures for the seven enabled sources.
- Run `python -m pytest -q`; update selectors if a parser test fails.
- Re-check robots.txt and marketplace terms.
- Re-run `pip-audit . --skip-editable` in a clean virtualenv.

### Quarterly

- Refresh `fx_to_rub` and `scoring.market_median_rub`.
- Review skill/exclude lists against actual won/lost projects.
- Rotate solver/proxy/LLM keys.
- Re-test browser-only sources with and without a residential proxy.

### When a platform breaks

1. Capture the current page.
2. Diff it against `fixtures/live/`.
3. Update the adapter selector or transport.
4. Run `pytest -q tests/test_parsers.py`, then the full suite.
5. Re-run `trial --minutes 8` before returning it to the regular schedule.

## 13. Bottom line

The deliverable is runnable, tested, and honest about its boundaries. It collects and ranks public listings well, produces guard-clean Russian outreach, and leaves the final send decision with the operator. The places most likely to need future work are the three browser-only marketplaces, the Avito proxy path, and the optional LLM key for message variety.
