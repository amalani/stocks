# CSP screener

Scans a watchlist for cash-secured-put candidates at ≤ 0.20 delta, 14–25 DTE, and renders a
sortable/filterable page (`public/index.html`) with per-ticker IV history.

Stdlib-only Python 3.9+. Uses the same Tradier endpoints as `options-tracker-sheet/`.

## Setup

1. `cp .env.example .env` and fill in `TRADIER_TOKEN`. `.env` is gitignored (real environment
   variables also work and take priority). Sandbox users: also set
   `TRADIER_BASE_URL=https://sandbox.tradier.com/v1`.
2. Edit `watchlist.txt` (10–20 tickers).
3. Optional, for the Earnings column: free key from finnhub.io as `FINNHUB_KEY` in `.env`.
   Without it, earnings show "?" and are not used to filter.

```
python3 screener.py            # live run, writes public/
python3 screener.py --mock     # fake data, for trying the page (uses data/mock.db)
python3 screener.py --help     # delta, DTE window, liquidity and IV thresholds
```

## Changing the page (UX-only edits)

Every run also saves `public/runs/<date>_<time>.json` (the data) and a matching `.html`. After editing
`template.html`, apply it to the latest page and all saved ones without touching the API or the DB:

```
python3 screener.py --rerender
```

Note that a `--mock` run overwrites `public/index.html`; use `--out` to keep mock output separate.

## Daily run + serving

```
# crontab -e   (after the close; weekdays only)
CRON_TZ=America/New_York
30 16 * * 1-5  cd /path/to/csp-screener && python3 screener.py >> data/run.log 2>&1
```

Serve `public/` from your home server (nginx/Caddy static dir, or `python3 -m http.server 8080 -d public`).
On-demand refresh = run `python3 screener.py` again. Re-runs on the same day overwrite that day's history row.

## What the columns mean

- **Ann. ROC**: mid / strike × 365 / DTE. One row per expiry in the window, using the highest-delta put still ≤ target.
- **vs EM**: cushion (spot − strike) in units of the 1σ expected move, from ATM IV. ≥ 1× = strike is outside the expected move.
- **IV Rank**: where today's ATM IV (expiry nearest 21 DTE) sits in its own min–max range from `data/screener.db`.
  Tradier has no IV history, so this fills in over time and shows "–" until 20 days exist.
  Until then the ● pass test uses IV/RV (ATM IV ÷ 30-day realized vol) ≥ 1.2.
- **●** = spread ≤ 10% and OI ≥ 100, IV rich (above), and no earnings before expiry.
  Thresholds are CLI flags.

## Notes

- Weekend runs don't write history.
- IV history is only as good as how consistently it runs; a gap just means fewer points.
- Not yet run against live Tradier. Mock mode exercises all the parsing and the page, but check the first live run.
