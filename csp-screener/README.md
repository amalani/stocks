# CSP screener

Scans a watchlist for cash-secured-put candidates at ≤ 0.20 delta, 1–25 DTE, and renders a
sortable/filterable page (`public/index.html`) with per-ticker IV history.

Stdlib-only Python 3.9+. Uses the same Tradier endpoints as `options-tracker-sheet/`.

## Setup

1. `cp .env.example .env` and fill in `TRADIER_TOKEN`. `.env` is gitignored (real environment
   variables also work and take priority). Sandbox users: also set
   `TRADIER_BASE_URL=https://sandbox.tradier.com/v1`.
2. Edit `watchlist.txt` (10–20 tickers). Optionally give a ticker its own max put delta, e.g. `AMD 0.30` for a name
   you are happy to be assigned on (default 0.20). Clicking a row on the page shows the neighbouring strikes at that expiry.
   Add `@price` for the price you would be happy to own it at (`AMD 0.30 @480`); those tickers get the **Wheel** tab.
3. Optional: keep the watchlist in a Google Sheet instead. Make a tab "Screener Watchlist" with columns
   `Ticker | Max delta | Own price` (blanks use defaults; only tickers and prices in that tab). Import
   `screener-watchlist-import.csv` (File, Import, Upload, "Insert new sheet") to start from the current list, then File, Share,
   Publish to web, pick that tab, format CSV. Put the URL in `.env` as `WATCHLIST_URL`. The URL is unauthenticated,
   so keep it private (unpublish if it leaks). Publishing caches ~5 min. If the sheet is unreachable the run falls back
   to `watchlist.txt` and the page says so. `--tickers FILE` bypasses the sheet.
4. Optional, for the Earnings column: free key from finnhub.io as `FINNHUB_KEY` in `.env`.
   Without it, earnings show "?" and are not used to filter.

```
python3 screener.py            # live run, writes public/
python3 screener.py --mock     # fake data, for trying the page (uses data/mock.db)
python3 screener.py --help     # delta, DTE window, liquidity and IV thresholds
```

## How the Google Sheet fits in

Two separate things can live in your spreadsheet. Only the first one talks to the screener.

| | Screener Watchlist tab | Position tracker (`../options-tracker-sheet/`) |
|---|---|---|
| Purpose | What to scan, and your own prices | What you have open (positions, P&L) |
| Direction | Sheet to screener (read-only, via published CSV) | Apps Script pulls Tradier quotes into the sheet |
| Contents | `Ticker`, `Max delta`, `Own price` | Your trades; never published |

Flow: edit the Screener Watchlist tab, then the next screener run (cron, NAS schedule or on demand) fetches the
published CSV, scans those tickers and rebuilds the page. The screener never writes back to the sheet and never sees
your positions, so open puts and collateral are not reflected on the page yet (see Ideas in `CLAUDE.md`).

How the columns drive the tabs:

- **Max delta** (blank = 0.20): the target delta for that ticker on the **Income** tab. Strikes are chosen at or below it.
- **Own price** (blank = no wheel row): the price you would be happy to own the stock at. Tickers with a value also appear
  on the **Wheel** tab, where the strike is chosen from that price instead of delta. A ticker can have both.
- Typical use: pick candidates on the page, place the trade, then record it in your position tracker as before.
  When you decide you are happy owning a name at a new level, change its Own price and re-run.

Tips: keep only tickers and prices in the published tab (the URL is unauthenticated); changes take up to ~5 minutes to
reach the published CSV; if the sheet cannot be read the run uses `watchlist.txt` and a banner on the page says so.
Header names are matched loosely (`Ticker`/`Symbol`, `Delta`, `Own`/`Buy`/`Hold`/`Price`).

## Changing the page (UX-only edits)

Every run also saves `public/runs/<date>_<time>.json` (the data) and a matching `.html`. After editing
`template.html`, apply it to the latest page and all saved ones without touching the API or the DB:

```
python3 screener.py --rerender
```

`--mock` writes to `public-mock/` (separate from `public/`), so it never touches the live page. Add `--mock` to
`--rerender` to rebuild the mock pages.

## Daily run + serving

```
# crontab -e   (after the close; weekdays only)
CRON_TZ=America/New_York
30 16 * * 1-5  cd /path/to/csp-screener && python3 screener.py >> data/run.log 2>&1
```

Serve `public/` from your home server (nginx/Caddy static dir, or `python3 -m http.server 8080 -d public`).
On-demand refresh = run `python3 screener.py` again. Re-runs on the same day overwrite that day's history row.

## Docker on a NAS + Cloudflare tunnel

`docker-compose.yml` runs nginx (serves `public/` only) and the screener as a one-shot job. Secrets stay in `.env`
on the NAS; nginx never sees them.

1. Copy this folder to the NAS and create `.env` (see Setup).
2. First run, then start the web server:
   ```
   docker compose run --rm screener
   docker compose up -d web
   ```
3. Cloudflare Zero Trust, existing tunnel: add a public hostname (e.g. `screener.yourdomain.com`) pointing to
   `http://<nas-ip>:9700` (or `http://web:80` if `cloudflared` is on the same compose network).
4. **Put Cloudflare Access in front of it** (Access, Applications, Self-hosted, allow only your email). Otherwise
   anyone with the URL can see your watchlist and strikes.
5. Schedule `docker compose run --rm screener` in the NAS task scheduler: weekdays 1:30pm Pacific (= 4:30pm ET;
   Pacific and Eastern are always 3 hours apart). Assumes the NAS clock is on Pacific time.
6. On-demand refresh: run the same command over SSH. The page is static, so there is no refresh button.

Notes: the job `pip install`s `tzdata` each run (slim Python images may lack timezone data); bake it into a small
Dockerfile if the extra seconds bother you. Saved per-run pages in `public/runs/` are served too.

## What the columns mean

- **Ann. ROC**: mid / strike × 365 / DTE. One row per expiry in the window, using the highest-delta put still ≤ target.
- **vs EM**: cushion (spot − strike) in units of the 1σ expected move, from ATM IV. ≥ 1× = strike is outside the expected move.
- **IV Rank**: where today's ATM IV (expiry nearest 21 DTE) sits in its own min–max range from `data/screener.db`.
  Tradier has no IV history, so this fills in over time and shows "–" until 20 days exist.
  Until then the ● pass test uses IV/RV (ATM IV ÷ 30-day realized vol) ≥ 1.2.
- **●** = spread ≤ 10% and OI ≥ 100, IV rich (above), ann. ROC ≥ `--min-roc` (default 25%), and no earnings before expiry.
  Every row gets a one-line reason (hover its ● or open it): Skip (earnings / thin market), Weak (IV not elevated),
  Thin (pays under the hurdle) or Worth a look. The "Top opportunities" strip shows the best ● expiry per ticker
  (nearest 14 DTE, so very short expiries don't win on inflated annualized ROC).
  Thresholds are CLI flags.

- **Wheel tab**: for tickers with an own price, the highest put strike at or below min(own price, spot) at each expiry.
  **Net cost** = strike minus premium (your per-share cost if assigned); **Assign Δ** is roughly the assignment chance.
  ● needs liquidity, the ROC hurdle and no earnings; IV richness is informational only.

## Notes

- Weekend runs don't write history.
- IV history is only as good as how consistently it runs; a gap just means fewer points.
- First live Tradier run was on 2026-11-03 (income view only). The Wheel tab and the sheet watchlist have only been tested on mock data and a local CSV, so check the first live run with own prices.
