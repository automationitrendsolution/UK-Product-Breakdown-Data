# sellerboard-uk-breakdown-cron

Downloads Sellerboard's **Dashboard by product** export for the last 4 full days, stages it in a local
MySQL database (`sellerboard`), then inserts the rows Power BI does not have yet into
`powerbidb.Uk_breakdown_data`. Existing Power BI rows are never changed or deleted.

```
Sellerboard (Playwright, saved session)
  └─ data/incoming/*.xlsx ── validate ──> sellerboard.uk_breakdown_staging (one batch per file)
       └─ data/processed/ | data/failed/          └─ insert-only, verified ──> powerbidb.Uk_breakdown_data
```

## Layout

| Path | What it is |
| --- | --- |
| `run.py` | The only command (see `python run.py --help`) |
| `app/` | `importer.py` (one run), `sellerboard.py` (browser), `excel.py` (parse + validate), `staging.py` (local DB), `sync.py` (Power BI), `schema.py` (column mapping), `config.py`, `exceptions.py`, `logging_config.py`, `schedule.py`, `db.py` |
| `config/selectors.json` | The clicks that export the report from Sellerboard |
| `data/incoming/` | Exports waiting to be imported (downloaded, or placed by hand) |
| `data/processed/`, `data/failed/` | Imported files; files that were unusable or had rejected rows |
| `data/screenshots/` | Page screenshots when Sellerboard's page changed, and `--inspect-page` output |
| `data/browser/` | The saved Sellerboard session (cookies = account access; keep private) |
| `deploy/crontab.example` | The cron line |
| `sql/` | Read-only checks of the staging database and of Power BI |
| `logs/` | `importer.log` (details, rotated daily) and `cron.log` (one line per run) |
| `tests/` | `uv run python -B -m pytest`; integration tests use a throwaway database and skip if MySQL is down |

## Modes

```bash
uv run python run.py                  # download the last 4 full days, import, sync (cron)
uv run python run.py --login          # one-time Sellerboard sign-in by a person (opens Chrome)
uv run python run.py --fetch-only     # download into data/incoming/ only
uv run python run.py --no-fetch       # import data/incoming/, then sync
uv run python run.py --no-sync        # download and import; do not touch Power BI
uv run python run.py --preview        # what the next sync would insert (Power BI read-only)
uv run python run.py --inspect-page   # screenshot + control list of the Sellerboard dashboard
uv run python run.py --dry-run        # parse and validate only (no Sellerboard, MySQL or moves)
uv run python run.py --file x.xlsx    # import one file in place (not moved), then sync
uv run python run.py --init-db        # create the staging tables and exit
```

`--start-date` / `--end-date` (YYYY-MM-DD) override the window. Default window: the last 4 full days
(today is excluded, its numbers are still moving); after a missed or failed run it starts the day after the
newest day already in Power BI (at most 31 days back).

| Exit | Meaning |
| --- | --- |
| 0 | success, including "no files waiting" |
| 1 | unexpected error (see `logs/importer.log`) |
| 2 | configuration error |
| 3 | Sellerboard sign-in needed: run `--login` (a person must do this) |
| 4 | Sellerboard page not as expected (see `data/screenshots/`, update `config/selectors.json`) |
| 6 | download failed or timed out |
| 7 | a report was malformed or had rejected rows (moved to `data/failed/`, never synced) |
| 9 | database error (rolled back; retried next run) |
| 10 | Power BI sync refused or not verified |
| 11 | another run holds the lock |

## Rules

* **Import** — one batch per file, one transaction per batch. Rows are keyed on Date + Marketplace + ASIN + SKU.
  Identical repeats are kept once; differing repeats, bad dates or numbers, and blank ASIN/SKU are rejected,
  with the reason, and a file with any rejected row is never synced. Only marketplaces `Uk_breakdown_data` holds
  are kept. The same file imported twice is recognised and not staged again.
* **Power BI** — every validated batch not yet synced, newest first. A key Power BI lacks is inserted; a key it
  already has is left exactly as it is (counted as unchanged or conflict). Nothing is updated or deleted. Success is
  reported only after a fresh read confirms every inserted row and every pre-existing row.

## Setup on the cron server

```bash
cd /data/cron/sellerboard-uk-breakdown-cron
uv sync                                   # .venv from uv.lock
uv run playwright install --with-deps chromium
cp .env.example .env && chmod 600 .env    # fill in SELLERBOARD_DB_* and POWERBI_DB_*
uv run python run.py --init-db
```

The staging MySQL must be reachable with `SELLERBOARD_DB_*`. The Power BI user needs only
`SELECT, INSERT` on `powerbidb.Uk_breakdown_data`.

**Sellerboard sign-in.** Put `SELLERBOARD_LOGIN_EMAIL` / `SELLERBOARD_LOGIN_PASSWORD` in `.env`, then on the server run
`uv run python run.py --login`: it signs in headless in the terminal (one attempt, *Keep me logged in*) and saves
`data/browser/session.json`. Later runs reuse that session; if it has expired they sign in once more by themselves.
A refused sign-in is reported with a screenshot, and scheduled runs then wait 24 hours before another attempt, so
the account is not locked. On a PC, `--login` opens a Chrome window instead (`--login --terminal` forces the terminal).

**Export clicks.** Run `uv run python run.py --inspect-page`, then fill `export_steps` in
`config/selectors.json` from the screenshot and control list it writes to `data/screenshots/`, and set
`"verified": true`. Until then a scheduled run exits with 4 and imports nothing.

**Cron** (`deploy/crontab.example`), daily at 13:00, last 4 full days (`SELLERBOARD_WINDOW_DAYS=4`):

```cron
0 13 * * * cd /data/cron/UK-Product-Breakdown-Data && /root/.local/bin/uv run --no-sync python run.py --quiet >> logs/cron-output.log 2>&1
```

Check runs with `tail logs/cron.log`; details are in `logs/importer.log`.
