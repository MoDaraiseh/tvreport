# tvreport — US stock market report, Monday and Friday

Builds a rule-based US stock market report from TradingView scanner data twice a week, emails it to you, and saves it in this repo:

- **Monday, pre-market ("Week Ahead")** at 08:00 ET: market overview, then BUY / HOLD / SELL lists.
- **Friday, after the close ("Week in Review")** at 16:45 ET: market overview, how Monday's picks did against the S&P 500, then updated BUY / HOLD / SELL lists.

Each report is emailed (HTML with a plain-text alternative) and saved as `reports/YYYY-MM-DD-{monday|friday}.html` and `.md`. Every pick and its reference price is stored in `data/history.db`. The scoring is deterministic and documented formula by formula in [METHODOLOGY.md](METHODOLOGY.md). It is an automated technical screen, **not financial advice**.

## How it works

```
TradingView scanner (via tradingview-screener) ─► DataProvider ─► data-quality gates
  ─► scores (0–100) ─► BUY / HOLD / SELL ─► overview, performance ─► HTML/text/Markdown ─► SMTP
```

- `src/tvreport/providers/`: data access behind a `DataProvider` interface; only the TradingView provider is implemented.
- `quality.py`: drops bad rows, checks universe size and staleness against the NYSE calendar, and aborts on anything suspicious.
- `scoring.py`, `classify.py`, `portfolio.py`, `weekly.py`, `overview.py`, `performance.py`: the analysis.
- `schedule.py`: America/New_York time windows, NYSE holidays and early closes.
- `history.py`: SQLite history and idempotency.
- `report/`, `notify.py`, `cli.py`: rendering, email and the command line.

## Setup (local)

Requires Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
pip install --no-deps -e .
```

## Running locally

```bash
python -m tvreport run --type monday --dry-run --force    # build and save a report now; no email, no history
python -m tvreport run --type friday --dry-run --force    # (a Friday report needs a closed session: run it after 16:30 ET)
python -m tvreport run --type auto                        # what the scheduler runs: only inside the windows below
```

- `--dry-run` builds and saves the report to `reports/` but doesn't email it or write `history.db`.
- `--force` ignores the time window, but never the "already sent today" check.

Other tools:

```bash
python scripts/discover_fields.py     # live check of every field/unit/index symbol; refreshes tests/fixtures/
python scripts/check_session_bars.py  # which session TradingView's daily/weekly bars belong to right now
pytest                                # offline tests; all network access is mocked with recorded data
ruff check . && ruff format --check . && mypy
```

## Email (Gmail app password)

1. Turn on 2-Step Verification for your Google account. Then go to <https://myaccount.google.com/apppasswords>, create an app password, and copy the 16 characters.
2. The tool reads these environment variables, and nothing else ever holds them:

| Variable | Gmail value |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` (STARTTLS) or `465` (SSL) |
| `SMTP_USER` | your Gmail address |
| `SMTP_PASSWORD` | the app password |
| `REPORT_TO` | where to send the report (comma-separate several addresses) |

Credentials are never logged or printed, and secret values are scrubbed from error messages and failure emails.

## GitHub Actions

1. **Create the repository** (a private one is recommended) and push this code.
2. **Add secrets.** Go to *Settings → Secrets and variables → Actions → New repository secret* and add `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` and `REPORT_TO`.
   - Optional: `HOLDINGS_YAML`, containing the same YAML as [holdings.example.yaml](holdings.example.yaml), enables portfolio mode without committing your holdings.
3. **Allow the workflow to push** `history.db` and the reports: *Settings → Actions → General → Workflow permissions → Read and write*.
4. **Test it.** *Actions → weekly-report → Run workflow* with `run_type = monday` and `dry_run = true` builds a report and attaches it to the run as an artifact. Run it once with `dry_run = false` to receive a real email.

### Check email delivery (`test_email`)

*Actions → weekly-report → Run workflow*, set `run_type` to **`test_email`**, then **Run workflow**. This builds the current Monday ("Week Ahead") dry-run report from live data and emails it through the real SMTP secrets, with **`[TEST]`** at the start of the subject. The `dry_run` box is ignored for this option.

- It never writes `data/history.db`: it reads history through a temporary copy. It records no run and no failure alert, and commits nothing, so it can't make a real Monday or Friday run skip or stay quiet.
- If secrets are missing, it fails with a message naming them, for example `missing SMTP settings: SMTP_PASSWORD, REPORT_TO`.
- Locally: `python -m tvreport test-email` (or `--type friday` after the close), with the five variables set in your environment.

### Schedule (timezone-critical)

GitHub cron is UTC-only and often delayed or skipped, so [weekly-report.yml](.github/workflows/weekly-report.yml) triggers **every 15 minutes across both the EDT and EST equivalents** of each window. The code then decides in `America/New_York` (zoneinfo, so DST is handled) whether the trigger falls inside the window:

| Report | Runs when (New York time) |
|---|---|
| Monday "Week Ahead" | Mondays 06:30–09:25 ET, i.e. before the 09:30 open. It runs on market holidays too, and says the market is closed. |
| Friday "Week in Review" | Fridays from the session close + 30 min to 18:15 ET. On an early-close day this starts at 13:30. On a holiday it starts at 16:30 and uses the last session's data, labelled as such. |

- **Idempotency:** a report is recorded in `history.db` (one per date and run type) only after its email was sent. Every later trigger that day is skipped, so duplicate crons can never send twice.
- **Cheap skips:** the workflow's first step is a standard-library time-window gate that needs no checkout and no install, so a trigger outside a window ends there. Inside a window, `scripts/already_sent.py` checks `history.db` before anything is installed, so the rest of the window's triggers end in seconds once the report is sent.
- **One run at a time:** the workflow's `concurrency` group queues runs (`queue: max`, up to 100 pending) and never cancels one in progress. Each run checks out the latest commit when it starts, so a queued run sees a report recorded by the run before it.
- **Verified in the tests:** the cron lines cover every Monday and Friday through the DST changes on 1 Nov 2026 and 14 Mar 2027, including the exact 08:00 and 16:45 targets, and never run a Friday report before close + 30.
- **Manual runs:** `workflow_dispatch` takes inputs `run_type` (`monday`, `friday`, or `auto`) and `dry_run`. `monday` and `friday` run immediately and ignore the window; `auto` behaves exactly like a scheduled trigger.

**Missed runs:** GitHub can delay scheduled runs by many minutes and drops some entirely on quiet repos. With 15-minute triggers across a ~2-hour window, a whole window being skipped is unlikely but possible. For extra reliability, have an outside scheduler (for example a free cron service) call the `workflow_dispatch` API every 15 minutes during the windows, with `run_type=auto` and `dry_run=false`. Use a fine-grained token limited to this repository's Actions. The window and idempotency checks make the extra calls harmless.

**Keeping the schedule alive:** GitHub's documented rule is that "in a public repository, scheduled workflows are automatically disabled when no repository activity has occurred in 60 days". Its docs don't say whether private repositories are affected, or what counts as activity. This workflow commits `history.db` and the reports twice a week, and its last step re-enables itself through the API on every run (`gh api -X PUT …/enable`). If it was ever disabled anyway, re-enable it under *Actions → weekly-report → Enable workflow*, or with `gh workflow enable weekly-report.yml`.

**Failures** fail the Actions run (exit code 1) every time and send a short failure email with the error summary (no secrets). You get **at most one failure email per run type per window**: the alert is recorded in `history.db` (committed even after a failure), and later failing triggers in the same window only log. A failed run isn't recorded as sent, so the next trigger in the window still retries the report. Failures outside the report step, such as a failed dependency install, are emailed by `scripts/notify_failure.py` and share the same once-per-window limit.

## Configuration

Everything tunable is in [config.yaml](config.yaml) and validated at startup:
- the universe filters;
- the index symbols;
- the score weights, which must sum to 1.0;
- the list sizes, the per-sector BUY cap and the BUY/SELL/HOLD thresholds;
- the portfolio thresholds;
- the schedule windows;
- the request politeness settings: 1 request/second, 30-second timeout, 3 attempts with backoff on network errors and 5xx, and no retries on 4xx.

## Holdings (portfolio mode)

Copy [holdings.example.yaml](holdings.example.yaml) to `holdings.yaml` (git-ignored), or store its content in the `HOLDINGS_YAML` secret. Each holding gets BUY MORE (score ≥ 65), HOLD (40–65) or SELL (< 40), plus unrealized P/L if you give `avg_cost`. Holdings outside the screened universe are fetched and scored too.

## Known limitations

See [METHODOLOGY.md](METHODOLOGY.md#known-limitations). The main ones: the data is 15-minute delayed, `Perf.W` is unreliable so weekly changes are computed from weekly bars instead, the official Russell 2000 (`FTSE:RUT`) can publish late (a labelled fallback is used), and the Monday pre-market bar behaviour is handled both ways but was not yet observed live.
