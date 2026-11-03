# CSP screener: notes for Claude Code

## What this is
A daily/on-demand screener for cash-secured puts. For each ticker in `watchlist.txt` it finds the short put
closest to (but not above) 0.20 delta in every expiration 14-25 DTE out, computes premium / IV / liquidity /
earnings metrics, stores daily IV history in SQLite, and renders a static page for a human to eyeball.
Owner sells CSPs for premium and wants to avoid assignment; the page exists to spot where IV is rich.

Owner's trading rules the screener encodes (change via CLI flags, not by editing logic):
- Delta ≤ 0.20, ~2-3 weeks out. Mid price (not bid) is the premium input; limit orders.
- Annualized ROC = mid / strike × 365 / DTE is the primary comparison metric.
- IV Rank > 50 preferred. Avoid new entries into earnings windows.

## Layout
- `screener.py`: everything: Tradier client, `Mock` client, metrics, SQLite, output. Stdlib only, Python 3.9+.
- `template.html`: the page. `screener.py` replaces the literal `/*__DATA__*/null` with the JSON payload and writes
  `public/index.html`. Vanilla JS, no CDN, so it works offline on a locked-down server.
- `watchlist.txt`: tickers, `#` comments.
- Generated and gitignored: `public/` (index.html, latest.json, latest.csv), `data/screener.db` (live), `data/mock.db`.
- Secrets: `.env` (gitignored; see `.env.example`). Never print, log, or commit the token.

## Run / test
```
python3 screener.py --mock        # no network; seeds 120 days of fake history into data/mock.db
python3 screener.py               # live; needs TRADIER_TOKEN in .env
python3 screener.py --help        # thresholds
```
After any change to the page, run `--mock` and open `public/index.html` (a headless-browser screenshot is a good check).
Mock returns Tradier-shaped responses, so it exercises the real parsing code, but it cannot prove the live API matches.

## Data flow
`Tradier.quotes` → per ticker: `expirations` (filter to DTE window) → `closes` (30d realized vol) → `chain` per expiry
(`greeks=true`) → pick put → `screen_symbol` returns row dicts → SQLite upsert (`iv_daily`, `picks`) → history query →
payload JSON → `template.html`.

## Gotchas / decisions (don't undo without reason)
- Tradier returns a bare object, not a list, when there is one item; always go through `aslist()`.
- Put deltas are negative; rows store `abs(delta)`. Selection = highest |delta| still ≤ target.
- `mid_iv` is a decimal (0.35). `norm_iv` tolerates percent-style values and treats 0 as missing.
- IV Rank reference = ATM IV of the in-window expiry nearest 21 DTE, ranked against that ticker's own stored history.
  Tradier has no IV history, so rank shows "–" until `--min-history` (20) days exist; the ● pass test falls back to IV/RV.
- History is only written on weekdays (ET). Same-day re-runs overwrite (primary key date+symbol). Mock uses a separate DB.
- Earnings come from Finnhub only if `FINNHUB_KEY` is set; otherwise `earn_known` is false and the column shows "?"
  (unknown earnings does NOT block the ● mark; the page banner says so).
- `json.dumps(...).replace("</", "<\\/")` is needed when embedding JSON in the HTML. Keep it.
- One bad ticker must not kill the run; errors are collected and shown at the bottom of the page.

## Status
Built and verified only against `--mock` (page renders, filters/sort/history charts work, re-runs are idempotent).
**Not yet run against live Tradier.** First live run checklist:
1. `.env` has a valid token (sandbox needs `TRADIER_BASE_URL`; sandbox data is delayed and may lack greeks).
2. Rows appear for each ticker; delta/IV values look sane vs the Tradier/broker chain; no ticker errors.
3. Spot used is `last` (falls back to close/prevclose), so check it after hours.
4. Finnhub earnings call (`finnhub_earnings`) is untested; verify the response shape and rate limiting.

## Ideas, roughly by value
- Backfill `iv_daily` from Barchart / Market Chameleon history so IV Rank works from day one.
- Mark tickers already held / with open CSPs (collateral stacking is a real constraint for the owner).
- Cron on the home server after the close (see README), serve `public/` from a static dir.
- Optional Google Sheet push (service account); the page's CSV download covers it manually for now.
- Alerts when a ticker newly passes.

## Conventions
- Keep it stdlib-only and a single script unless it clearly outgrows that.
- Thresholds are CLI flags with defaults in `argparse`; surface any new threshold in the page legend too.
- The sibling folder `../options-tracker-sheet/` is Google Apps Script for the owner's position-tracker spreadsheet.
  Leave it alone unless asked.
