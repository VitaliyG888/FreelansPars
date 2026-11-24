# Tests

The suite is fixture-driven. Parsers run against real captured HTML from live reconnaissance; the rest of the pipeline is the production code path. Network calls are mocked or isolated, so the suite does not depend on marketplace availability.

## Run

```powershell
$env:FREELANCE_BOT_CONFIG = "config.yaml"
python -m pytest -q
```

Useful variants:

```powershell
python -m pytest -q -k parser
python -m pytest -q tests/test_pipeline.py -vv
python -m pytest -q --maxfail=1
python -m ruff check src tests
python -m mypy src
```

## Layout

| File | What it proves |
|---|---|
| `test_config.py` | YAML/env loading, defaults, secret isolation, platform config validation |
| `test_normalize.py` | RUB/UAH/USD/EUR conversion, range/hourly budgets, relative/absolute timestamps, deadlines |
| `test_parsers.py` | Every adapter parses its captured fixture; empty/garbage HTML returns `[]` instead of raising |
| `test_scoring.py` | Bounds, factor explanations, budget/skill/client/competition/freshness behaviour, weight changes, red-flag penalty, skill word boundaries, always-open eligibility gate |
| `test_redflags.py` | Unpaid tests, off-platform payment, contact-before-hire, vague brief, urgency pressure, diminishing returns |
| `test_guard.py` | Character limits, link/contact blocking, AI tells, Cyrillic ratio, emoji/exclamation caps, specificity |
| `test_message.py` | Mocked LLM path, repair path, deterministic fallback, task-appropriate approach, platform limits, no placeholders |
| `test_network.py` | Challenge classification (Cloudflare, Qrator, DDoS-Guard, SmartCaptcha, login wall), backoff, token bucket, robots cache |
| `test_sender.py` | Dry-run outbox, approval gate, rate gate, blocked messages never leave |
| `test_storage.py` | Job dedupe, message existence, approval/sent state, cycle history, score persistence |
| `test_pipeline.py` | One end-to-end mocked cycle per marketplace, second-cycle dedupe with small top-N, adapter crash isolation, blocked-platform reporting |
| `test_telegram.py` | Roles (super admin vs approved user), every command and inline callback, personal filters (`/filters`), favorites (`/favorites`, `fav:/unfav:`), `/jobs 5\|10\|30`, `/interval` bounds, `/score` admin-only, SQLite `/stats`, Markdown escaping, message chunking, delivery cards via `deliver_job`, handler registration |

## Fixtures

`fixtures/live/` contains the exact HTML used by the parser tests. `fixtures/probes/` contains reconnaissance JSON from the initial protection-layer survey. Keeping both means a selector change can be diagnosed offline before touching the live network.

When a platform changes markup:

1. Capture a fresh page with `tools/capture.py` (HTTP) or `tools/browser_probe.py` (browser).
2. Save it into `fixtures/live/` under the name used by `tests/conftest.py::FIXTURE_MAP`.
3. Update the adapter selectors.
4. Run `python -m pytest -q tests/test_parsers.py` and then the full suite.

## Notes

- `tests/conftest.py` provides `FixtureAdapter`, `make_job`, and the fixture map.
- Pipeline tests monkeypatch `freelance_bot.pipeline.build_adapters`; `run_cycle` still closes its own HTTP/browser context.
- The suite runs with `asyncio_mode = "auto"` from `pyproject.toml`.
- No test writes to the project's real `var/` directory; pipeline/storage tests use `tmp_path`.
- A missing LLM key is the normal test path; LLM tests monkeypatch the client.
