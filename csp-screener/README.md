# CSP screener

Scans a watchlist for cash-secured-put candidates at ≤ 0.20 delta, 1–25 DTE, and renders a
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
- **●** = spread ≤ 10% and OI ≥ 100, IV rich (above), and no earnings before expiry.
  Thresholds are CLI flags.

## Notes

- Weekend runs don't write history.
- IV history is only as good as how consistently it runs; a gap just means fewer points.
- Not yet run against live Tradier. Mock mode exercises all the parsing and the page, but check the first live run.
