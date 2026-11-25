# Freelance Bot

Russian freelance-marketplace aggregator, explainable scorer, and first-contact message assistant. It collects public project listings, normalises budgets and timestamps, ranks orders against a configurable profile, generates a natural Russian outreach message, and writes a reviewable dry-run outbox by default. The implementation is deliberately conservative: no login or paywall bypass, no credential harvesting, no auto-submit transport, robots.txt and per-domain token buckets are enforced, and live sending requires explicit per-message approval. Seven marketplace adapters are wired to real captured HTML and were exercised end-to-end in a live dry run; the remaining requested sources are documented with the evidence that made them unsuitable for public scraping.

**Документация на русском:** [`MANUAL_RU.md`](MANUAL_RU.md) — полное руководство по архитектуре, конфигурации, CLI, Telegram-боту, скорингу, LLM и разработке. Практические дополнения: [`TELEGRAM.md`](TELEGRAM.md), [`PROFILE.md`](PROFILE.md), [`DEPLOY_VPS.md`](DEPLOY_VPS.md), [`REPORT.md`](REPORT.md), [`tests/README.md`](tests/README.md).

## Status

- **Test suite:** 382 tests passing (`python -m pytest -q`), including the Telegram control panel.
- **Static checks:** `ruff check` clean, `mypy src` clean.
- **Live dry run:** 7/7 enabled platforms returned `ok`; 192 jobs in 51 s for the first cycle, 8 eligible jobs, 8 generated messages, zero duplicate messages on the second cycle.
- **Default mode:** dry-run. Messages go to `var/outbox/` and `messages.jsonl`; nothing is sent.
- **No LLM key required:** a deterministic Russian fallback generator is tested and used when no API key is configured.
- **Telegram control panel:** every job that passes the filter is delivered to Telegram with an LLM analysis, a domain-matched first letter, and send/skip/copy/favorite buttons. Each user tunes personal filters (`/filters`) and bookmarks jobs (`/favorites`).

## Safety and scope

The bot only reads public listing pages. It does not log in, solve paywalls, buy access, or submit messages through undocumented authenticated endpoints. `config.yaml` defaults to `dry_run: true`; `--live` only removes the dry-run flag, and every message still has to pass the human-approval gate in `OutboxSender`. The CLI does not wire a delivery transport, so `--live` blocks by design unless a transport is injected in code.

The requested scope excluded Upwork and Fiverr; neither is included.

## Quick start

Requires Python 3.11+.

```powershell
# From the project root
python -m pip install -e ".[browser,dev]"
python -m playwright install chromium

Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env

$env:PYTHONIOENCODING = "utf-8"
$env:FREELANCE_BOT_CONFIG = "config.yaml"

python -m freelance_bot.cli preflight
python -m freelance_bot.cli run --top 10
```

The first `run` collects all enabled platforms, stores jobs in `var/freelance.db`, and writes reviewable messages to `var/outbox/YYYY-MM-DD/`.

### Telegram mode

```powershell
# 1. Fill in profile.yaml with your skills and pricing strategy
# 2. Put the bot token and your chat ID in .env:
#    TELEGRAM_BOT_TOKEN=123456789:ABCdef...
#    TELEGRAM_ADMIN_CHAT_IDS=123456789

# 3. Verify the token before starting the long-running bot
python -m freelance_bot.cli telegram-check
python -m freelance_bot.cli telegram-check --send-test

# 4. Start the bot
python -m freelance_bot.cli telegram --interval 300
```

Every job that passes the category filter arrives in Telegram. For each one the LLM analyses the brief and drafts a first letter using the skills from `profile.yaml` that match the job's domain. See [TELEGRAM.md](TELEGRAM.md) and [PROFILE.md](PROFILE.md).

All runtime settings are also editable from Telegram without touching the files: platforms, category filter, red-flag penalty and freshness decay, sending mode and limits, LLM model/key/prompt, custom OpenAI-compatible LLM providers, letter style, skills and the freelancer profile. `/settings` shows the effective values, `/set <path> <value>` and `/get <path>` cover any individual field, `/export` dumps everything, and `/reset config|profile|all confirm` restores the file values. Changes are stored in the `var/runtime_settings.yaml` overlay over `config.yaml`/`profile.yaml`, so they survive service restarts while the original files stay untouched. `/menu` lists every command by group and `/help [command]` prints a short description of each command or detailed help with examples for one of them. The `/menu` and `/help` buttons stay under the input field, so they can be tapped instead of typed, and the full command list is registered with Telegram's command menu.

### Roles and personal filters

- **Super admin** (chats from `.env`) owns the shared pipeline settings: `/interval` (900–3600 s between passes), `/score` (red-flag penalty, freshness decay), `/set`, `/reset` and everything else that changes config files.
- **Every approved user** gets personal settings that affect only their own feed: `/filters` (platforms, rubrics, subcategories, minimum price — empty filter shows everything), `/jobs [5|10|30]` (latest jobs ordered by their personal filters), `/favorites` (bookmarked jobs via the ♥ button on each card).

Scoring never hides jobs: it only decides the order. Flagged and stale jobs are shown last.

### Useful commands

```powershell
python -m freelance_bot.cli platforms
python -m freelance_bot.cli trial --minutes 15 --interval 300 -p fl_ru -p kwork_ru
python -m freelance_bot.cli outbox --limit 30
python -m freelance_bot.cli approve 12
python -m freelance_bot.cli parse-fixture fl_ru fixtures/live/fl_ru.html
python -m freelance_bot.cli score jobs.json --top 10
python -m freelance_bot.cli categories
python -m freelance_bot.cli classify jobs.json --verbose
python -m freelance_bot.cli profile
python -m freelance_bot.cli telegram
python -m freelance_bot.cli telegram-check --send-test
python -m freelance_bot.cli version
```

`trial` is always dry-run, even if `config.yaml` says otherwise. It is intended for measuring parse success, challenge frequency, and latency over a bounded window.

## How it works

```text
CLI / scheduler
      |
      v
pipeline.collect
  -> per-platform adapter
       -> robots.txt cache
       -> per-domain token bucket
       -> proxy pool (optional)
       -> HTTP (curl_cffi TLS impersonation)
       -> browser escalation (stealth Playwright + CAPTCHA hook)
  -> normalise (money, currency, relative dates, deadlines)
      |
      v
SQLite dedupe
      |
      v
ScoringEngine (6 weighted factors + red-flag penalty; no thresholds,
orders the feed: clean and fresh jobs first)
      |
      v
+-----------------------------------------------------+
|  EVERY job that passed the filters                 |
|    -> domain detection (web / telegram / parsing /  |
|       backend / data / devops / ai / design / text) |
|    -> profile skills matched to that domain         |
|    -> price resolved by the chosen strategy         |
|                                                     |
|    -> MessageGenerator                              |
|       -> LLM JSON analysis + draft (if key set)     |
|       -> guard check and one repair attempt         |
|       -> deterministic Russian fallback             |
|    -> Telegram card (industry icon, budget,        |
|       deadline, client block, 3 buttons: open /    |
|       AI letter / favourite)                       |
+-----------------------------------------------------+
      |
      v
message guard (length, links, AI tells, specificity, Cyrillic, emoji)
      |
      v
OutboxSender
  -> dry-run: Markdown file + JSONL record
  -> live: approval + transport + hourly/cooldown gate
     (no transport configured -> blocked, nothing written)
      |
      v
SQLite jobs, messages, cycles
```

### Adapters

| Platform | Transport | Why | Live trial |
|---|---|---|---|
| `fl_ru` | HTTP + relay | Direct TCP to fl.ru is blocked from the VPS; reader-relay returns the listing | 30 jobs |
| `freelance_ru` | HTTP + relay | Same network block; relay returns server-rendered cards | 25 jobs |
| `freelancejob_ru` | HTTP | Plain server-rendered `div.x17` cards | 40 jobs |
| `pchel_net` | HTTP | Server-rendered, 50 cards/page, Cloudflare edge without challenge | 50 jobs |
| `kwork_ru` | HTTP | Qrator passes; inline `wantsListData` JSON holds 12 wants per page (DOM parse is the fallback) | 12 jobs |
| `weblancer_net` | HTTP | Cloudflare passes with the `chrome131` TLS profile; `/jobs/` redirects to `/freelance/` | 20 jobs |
| `freelancehunt_com` | HTTP | `chrome131` passes Cloudflare; Ukrainian exchange, UAH budgets | 15 jobs |
| `avito_ru` | Browser | Enabled, but self-disables without RU residential proxies (Qrator 439/403) | disabled |

Two network facts drive this table. First, `fl.ru` and `freelance.ru` accept no TCP
connection from the VPS at all, so `network.relay` (`https://r.jina.ai/{url}` with
`X-Return-Format: html`) is used as a fallback after a short direct timeout. Second,
`kwork.ru`, `weblancer.net` and `freelancehunt.com` all answer plain HTTP with the
`chrome131` fingerprint, so they no longer need a browser (Kwork's CDN is itself
unreachable from the VPS). Do not send `X-No-Cache` to the relay: it makes FL.ru
return 403.

`avito_ru` sets `requires_proxy: true`; if `RU_PROXY_URLS` is empty the adapter
returns `disabled` in under a millisecond instead of stalling the cycle.

### Notifications

- Every eligible job is announced **once**. The `jobs.notified_at` column is
  written after the Telegram message is accepted, so restarts and repeated
  passes never re-send an old job. A failed send leaves the marker empty and is
  retried on the next pass.
- The first letter is generated *before* the notification. A job whose letter
  could not be generated is not marked as shown, so the next pass retries it.
  The per-cycle letter cap defers extra jobs to the next pass instead of showing
  them without a letter.
- A cycle summary is sent only when at least one new eligible job was actually
  shown. Passes with no changes run in the background and only update stats.
- On upgrade, existing rows are backfilled as already shown so the whole old
  backlog is not dumped into Telegram at once.
- `/jobs [5|10|30]` lists the latest jobs from SQLite that pass the user's
  personal filters (platform, rubric, subcategory, minimum price), each with a
  ♥ favorite button. Order: jobs without red flags first, then by freshness —
  scoring no longer hides anything, it only sets the order.

`freelancehunt.ru` is a hijacked casino domain. The adapter uses `freelancehunt.com`.

### Scoring

The score is `100 * weighted_mean * (1 - penalty)`. Weights are normalised at runtime, so changing one weight does not require the others to sum to 1.

| Factor | Default weight | Signal |
|---|---:|---|
| `budget` | 0.28 | RUB estimate vs. category median |
| `skill_match` | 0.22 | Profile skills in title/declared skills, then description |
| `client_quality` | 0.18 | Hire rate, spend, verification, reviews when exposed |
| `competition` | 0.14 | Proposal count, diminishing via `1 / (1 + n/k)` |
| `freshness` | 0.10 | Exponential decay with an 18-hour half-life |
| `urgency_fit` | 0.08 | Deadline proximity and urgency flags |

Red flags (unpaid test, off-platform payment, contact-before-hire, scope mismatch, vague brief, urgency pressure) are combined with diminishing returns and cap the total penalty at 0.8.

There are no eligibility thresholds (`min_total_score`, `min_skill_match`) anymore: every job that passes the category/subcategory and minimum-price filter is surfaced. Scoring only orders the feed — jobs without red flags and fresher jobs come first; flagged and stale orders are shown last. The red-flag penalty and freshness decay are configured by the admin only (`/score`).

### Message generation and guard

The LLM prompt asks for strict JSON with a need summary, requirements, deliverables, risks, questions, estimate, and a first message. If the provider fails, returns invalid JSON, or the message fails the guard, the generator repairs once and then falls back to a deterministic Russian message. The fallback selects an approach from the job's own signals (parser, bot, API integration, backend, frontend, data, automation, LLM) instead of emitting a generic template.

Every message is checked for:

- platform-specific character limit;
- links and external contact handles;
- banned AI phrases and generic openers;
- Cyrillic ratio;
- emoji and exclamation caps;
- specificity overlap with the title and first 400 characters of the brief;
- excessive questions or bullet lists.

Messages that fail the guard are still stored with their violations and are never sent.

### Sending

`OutboxSender` has three modes:

- **dry-run** (default): writes a Markdown review file and a JSONL record.
- **assist**: queues the message for a human to copy/paste; live gate still applies.
- **live**: requires `approved=True`, a configured transport, and the hourly/cooldown rate gate.

Delivery goes through an optional `DeliveryTransport` callback — `Callable[[GeneratedMessage, Job], None]` — passed to `OutboxSender(..., transport=...)`. The project ships no implementation because every marketplace outreach endpoint is auth-gated and automating login/session handling is out of scope. If `runtime.dry_run` is `false` and no transport is configured, `send()` returns `ok=False` with `mode="blocked"` and writes nothing; it does not silently fall back to the outbox. The `delivered` flag on `SendResult` and in `messages.jsonl` records whether the transport actually ran. The bundled operational path is therefore still dry-run/assist: review the Markdown artifact, copy the message, and send it yourself.

## Platform coverage

### Enabled and parsed

`fl_ru`, `freelance_ru`, `freelancejob_ru`, `pchel_net`, `kwork_ru`, `weblancer_net`, `freelancehunt_com`, `avito_ru`.

`avito_ru` is enabled in the shipped config but reports `disabled` until
`RU_PROXY_URLS` contains Russian residential proxies: Qrator returns HTTP
439/403 to datacenter IPs even in a stealth browser and through the relay. The
adapter checks this before any network call, so the cycle is not delayed.

### Not scraped, with reasons

| Source | Reason |
|---|---|
| `workzilla` | Task feed is behind a login; public pages expose categories only. |
| `advego` | Order list requires an account; `/job/` redirects to login. |
| `profi` | No public order feed; clients are matched to specialists after registration. |
| `youdo` | Order feed requires an account; public pages are a JS shell. |
| `yandex_services` | No public order feed; request routing happens in the cabinet. |
| `illustrators` | Marketplace of artworks, not an order board. |
| `frilanseri` | No public project section (404); site is a landing page. |
| `headhub` | TLS handshake fails; domain not serving a working site. |
| `fiwork` | RuStore mobile app only; no web listing. |
| `freeworker` | RuStore mobile app only; no web listing. |

These are configuration-level exclusions, not parser stubs. `python -m freelance_bot.cli platforms` prints the same matrix at runtime.

## Configuration

`config.yaml` is the base runtime configuration file. Copy `config.example.yaml` and edit it. Secrets live in `.env`, never in `config.yaml`. Values changed from Telegram are written to the `var/runtime_settings.yaml` overlay (path override: `FREELANCE_BOT_OVERRIDES`) and merged on top of the base files at load time; delete the overlay or run `/reset ... confirm` to return to the file values.

Important sections:

- `runtime.dry_run`: must stay `true` until the outbox has been reviewed.
- `network.default_rate` and `network.domains`: per-domain requests/minute and burst.
- `network.backoff`: retry count, base delay, maximum delay, jitter.
- `platforms.*`: enable/disable, transport, listing URL, detail enrichment.
- `scoring.weights`: factor weights.
- `scoring.skill_match`: profile skills and excluded topics.
- `red_flags.rules`: regex rules and severities.
- `llm.style`: banned phrases and guard limits.
- `messaging.limits`: per-platform character limits.

Per-user filters (platforms, rubrics, subcategories, minimum price) live in SQLite, not in `config.yaml`: each approved Telegram user manages them with `/filters`. An empty filter means "show everything the bot collects".

Example profile tuning:

```yaml
scoring:
  skill_match:
    skills: [python, парсинг, api, автоматизация, телеграм, бот, django, fastapi, sql]
    exclude_keywords: [1с, бухгалтер, копирайт, таргет, дизайн]
```

Broaden `skills` for a wider funnel — the skill list feeds the `skill_match` factor, which only affects the order of the feed, never hides a job. The score remains comparable because all factors are normalised.

## Outputs and data

```text
var/
  freelance.db                 SQLite: jobs, messages, cycles
  outbox/
    messages.jsonl             append-only delivery log
    2026-11-03/
      fl_ru__fl_ru_5525021.md  review artifact per message
      ...
```

Each review file contains the platform, source URL, budget, proposal count, mode, generator source, guard result, and the message text.

## Tests

```powershell
$env:FREELANCE_BOT_CONFIG = "config.yaml"
python -m pytest -q
python -m ruff check src tests
python -m mypy src
```

The suite covers all seven parsers against captured HTML, normalisation, scoring, red flags, guard, message fallback, network classification, storage, sender rate gates, config validation, and full pipeline cycles including second-cycle dedupe. See [`tests/README.md`](tests/README.md) for the file-by-file map.

## Live trial

Run on 2026-11-03, Windows, datacenter IP, no LLM key, no CAPTCHA solver key, no proxy:

| Cycle | Platforms | Jobs | Eligible | Messages | Duration |
|---|---:|---:|---:|---:|---:|
| 1 | 7/7 `ok` | 192 | 8 | 8 | 51.1 s |
| 2 | 7/7 `ok` | 192 | 8 | 0 | 52.4 s |

Per-platform first-cycle latency: `fl_ru` 3.3 s, `freelance_ru` 2.8 s, `freelancejob_ru` 3.9 s, `pchel_net` 2.9 s, `kwork_ru` 14.8 s, `weblancer_net` 9.7 s, `freelancehunt_com` 11.4 s. No challenge was raised in this window. All eight messages passed the guard with zero violations; the second cycle generated no duplicates.

The full engineering report, including research evidence, architecture, and maintenance plan, is in [`REPORT.md`](REPORT.md).

### VPS trial (2026-11-10)

Full cycle on the production VPS with all eight platforms enabled:

| Platform | Outcome | Jobs | Time |
|---|---|---:|---:|
| `fl_ru` | ok (via relay) | 30 | 23 s |
| `freelance_ru` | ok (via relay) | 25 | 22 s |
| `freelancejob_ru` | ok | 40 | 3 s |
| `pchel_net` | ok | 50 | 2 s |
| `kwork_ru` | ok (JSON state) | 12 | 21 s |
| `weblancer_net` | ok (HTTP) | 20 | 2 s |
| `freelancehunt_com` | ok (HTTP) | 15 | 32 s |
| `avito_ru` | disabled (no RU proxy) | 0 | 0 ms |

Cycle total: 192 jobs, 1 new (the rest were already stored), 13 eligible.

## Maintenance

1. **Re-capture fixtures monthly.** Run `tools/capture.py` / `tools/browser_probe.py`, save new HTML into `fixtures/live/`, then update selectors and rerun `python -m pytest -q`.
2. **Watch anti-bot layers.** `kwork_ru` (Qrator + inline JSON), `weblancer_net` and `freelancehunt_com` (Cloudflare + TLS fingerprint) and `fl_ru`/`freelance_ru` (network block + relay) are the first to break. Re-run one platform with `freelance-bot run -p <name> --no-browser --no-messages` after any failure.
3. **Refresh FX and medians quarterly.** `fx_to_rub` and `scoring.market_median_rub` are deliberately explicit, not hidden magic.
4. **Re-check robots.txt and terms.** The bot respects robots.txt at runtime, but robots policy can change. Review before enabling a new source.
5. **Review the outbox before any live mode.** Approve individual messages; keep `max_sends_per_hour` low.
6. **Rotate proxies and solver keys in `.env`.** Do not commit `.env`.
7. **Run `pip-audit . --skip-editable` in a clean virtualenv before deployment.** The project-scoped audit is clean; a global interpreter may contain unrelated vulnerable packages.

## Limitations

- Public listing markup changes without notice. The fixture-based tests make breakage visible quickly, but they cannot prevent it.
- Browser-only platforms can require a residential IP and a CAPTCHA solver in some regions. The code reports challenges rather than pretending they did not happen.
- Client-quality fields are sparse on most boards; missing data is scored neutrally, not guessed.
- The deterministic fallback is grammatical and task-aware, but an LLM key produces more varied messages. The fallback exists so the pipeline never silently stops.
- Live auto-submit is intentionally not implemented. The deliverable stops at a reviewed outbox; `--live` blocks without an injected `DeliveryTransport`.
- `freelancehunt_com` is a Ukrainian exchange with UAH budgets; RUB conversion is indicative and should be revisited before bidding decisions.
