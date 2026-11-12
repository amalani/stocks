# CSP screener: notes for Claude Code

## What this is
A daily/on-demand screener for cash-secured puts. For each ticker in `watchlist.txt` it finds the short put
closest to (but not above) 0.20 delta in every weekly (Friday) expiration 1-39 DTE out (this week through ~5.5 weeks), computes premium / IV / liquidity /
earnings metrics, stores daily IV history in SQLite, and renders a static page for a human to eyeball.
Owner sells CSPs for premium and wants to avoid assignment; the page exists to spot where IV is rich.

The reasoning behind the rules, thresholds, income tiers and put-management levels is in `STRATEGY.md`.

Owner's trading rules the screener encodes (change via CLI flags, not by editing logic):
- Delta ≤ 0.20, same week through ~3 weeks out. Mid price (not bid) is the premium input; limit orders.
- Annualized ROC = mid / strike × 365 / DTE is the primary comparison metric.
- IV Rank > 50 preferred. Avoid new entries into earnings windows.

## Layout
- `screener.py`: everything: Tradier client, `Mock` client, metrics, SQLite, output. Stdlib only, Python 3.9+.
- Watchlist source: `load_watchlist(path, url)` reads a published Google Sheet CSV (`WATCHLIST_URL`, never echoed or logged) with fallback to `watchlist.txt`; `watchlist.txt` lines accept `TICKER [delta] [@own_price]`.
- `template.html`: the page. `screener.py` replaces the literal `/*__DATA__*/null` with the JSON payload and writes
  `public/index.html`. Vanilla JS, no CDN (except the optional TradingView embeds, see below), so it works offline on a locked-down server.
- `watchlist.txt`: tickers, `#` comments. Optional per-ticker max put delta after the ticker (`AMD 0.30`); default is `--delta`.
- Covered calls (`cc_leg` / `screen_cc`, payload `cc`, tab 3 in the page): watchlist `$465` / sheet "Cost basis" column = shares held.
  Expiries = one per week (Friday, Thursday if holiday) in `--cc-dte-min..max`; pick = highest call delta ≤ `--cc-delta` with strike ≥ cost
  basis (`floored` + `target_strike` when the cost lifted it). Rows reuse the key `roc` for annualized yield on *spot* (not strike) so
  filters/sort work; `ccCols`, `ccReason`, `ccLadderHTML`, `ccTakeHTML` in the template. `Memo` caches chains so puts and calls share API calls.
  Spot < cost means strikes get floored far OTM and pay little by design; the ladder shows the cost of selling lower (loss if called).
- `--only puts|cc`: partial run. Skips the other leg, carries its rows/errors over from the same session's latest.json (or runs snapshot;
  none = empty), records `payload.only` + `leg_times` (page banner when they differ). `cc` fetches only tickers with a cost basis. IV history/picks
  are only written by puts runs. `deploy.sh --run-puts|--run-cc` wrap it for the NAS.
- `--new`: sets `--symbols` to watchlist tickers not yet in latest.json (rows/wheel/cc/errors/`exch`); exits with no API calls if none; when it does run it also drops removed tickers from carried rows. Changed delta/own/cost on an existing ticker still needs `--symbols`.
- Tickers are screened in parallel (`work()` in `main`, `ThreadPoolExecutor`, `--workers`, default 4; `Tradier._throttle` caps all threads at 100 calls/min, the quota is 120/min and a violation is HTTP 400 "Quota Violation", not 429); sqlite reads happen before and writes after, on the main thread. Run time is `payload.elapsed_s`, shown in the page header.
- `--symbols AMD,NVDA`: refresh only those watchlist tickers (combine with `--only`); their rows replace the carried ones, other tickers' rows/errors stay; `history` still covers the whole watchlist; payload `subset` drives a page banner. (`--tickers` = watchlist FILE, unrelated.)
- `pwa/`: installable-app files (manifest, `sw.js`, icons), copied into the output dir by `write_pwa()` on every run and
  `--rerender`. The service worker is network-first (fresh data, cached fallback offline) and registers only over https.
- Generated and gitignored: `public/` (index.html, latest.json, latest.csv, manifest.js, runs/ snapshots), `public-mock/` (same, for `--mock`),
  `data/screener.db` (live), `data/mock.db`.
- Secrets: `.env` (gitignored; see `.env.example`). Never print, log, or commit the token.

## Run / test
```
python3 screener.py --mock        # no network; seeds 120 days of fake history into data/mock.db
python3 screener.py               # live; needs TRADIER_TOKEN in .env
python3 screener.py --help        # thresholds
```
After any change to the page, run `--mock` and open `public-mock/index.html` (a headless-browser screenshot is a good check).
UX-only template edits: `python3 screener.py --rerender` (add `--mock` for the mock pages) rebuilds every saved page.
Mock returns Tradier-shaped responses, so it exercises the real parsing code, but it cannot prove the live API matches.

## Data flow
`Tradier.quotes` → per ticker: `expirations` (filter to DTE window) → `closes` (30d realized vol) → `chain` per expiry
(`greeks=true`) → pick put at the ticker's target delta + `build_ladder` (the 12 puts with 0.05-0.40 |delta| nearest the
target, stored on each row as `ladder`; no extra API calls) → `screen_symbol` returns row dicts → SQLite upsert (`iv_daily`, `picks`) → history query →
payload JSON → `template.html`.

## Gotchas / decisions (don't undo without reason)
- Tradier returns a bare object, not a list, when there is one item; always go through `aslist()`.
- Put expiries default to one per week (Friday, Thursday on holidays) via `cc_expiries`, to cut noise from daily / tri-weekly series; `--expiries all` restores every listed expiry. Mon/Wed-only weeks are skipped.
- Put deltas are negative; rows store `abs(delta)`. Selection = highest |delta| still ≤ target.
- Target delta is per ticker (`row.target_delta`, payload `config.custom_delta`). Rankings stay apples-to-apples only
  among tickers with the same target; the page badges custom ones (`≤0.30Δ`). The `picks` history ("best ROC at ≤ target")
  mixes if a ticker's target is changed later; that is accepted.
- `mid_iv` is a decimal (0.35). `norm_iv` tolerates percent-style values and treats 0 as missing.
- Income profile: a watchlist ticker tagged `INCOME` (text: `GOOG 0.25 INCOME`; sheet: an Income/Style column with y/yes/x)
  skips the IV-richness test (`rich` forced True) and uses `--income-min-roc` (15) instead of `--min-roc`. Liquidity and
  earnings still apply. Delta stays per ticker (`0.25` allowed). Owner reasoning: premium must beat the 8-14% equity
  range plus T-bills on collateral; the IV/RV 1.2 fallback is my own heuristic and rejects mega-caps.
- Pick = highest delta ≤ target among strikes that pass the liquidity bar (OI ≥ `--min-oi`, spread ≤ `--max-spread`); if none do, the top
  one is kept and shows ✗ Liquid. `skipped_strike` records a higher-delta strike passed over (e.g. NVDA 172.5 with 0 OI → 170).
- Put rows carry management levels (`buyback` = mid × `--stop-mult` 2.0, `take_profit` = mid × (1 − `--profit-take` 50%),
  `tested_spot` = strike × (1 + `--tested-pct` 2%)); shown under "Manage it" in the row take. Conventions, not guarantees:
  gaps (SNDK -50%) skip them, so position size is the real protection. Older carried-over rows lack these fields.
- IV Rank reference = ATM IV of the in-window expiry nearest 21 DTE, ranked against that ticker's own stored history.
  Tradier has no IV history, so rank shows "–" until `--min-history` (20) days exist; the ● pass test falls back to IV/RV.
- History and `runs/<session>.{json,html}` are keyed by the *session* (trading day of the quotes' `trade_date`, fallback
  `last_session()`), not the wall-clock date. Last run wins, guarded by phase: close (≥4pm ET) > intraday > off (weekend/holiday/
  pre-market/overnight); a lower phase never overwrites a higher one and is only a preview (`recorded` false: index.html and
  latest.* refresh, no DB rows, snapshot or manifest change). `--asof` fakes the run time for tests. Mock uses a separate DB.
- `manifest.js` (`window.RUNS`, newest first) is rebuilt from `runs/*.json` on every run and `--rerender`; pages load it by
  `<script src>` (works on file://) so old pages never need re-rendering for nav. `--rerender` also folds legacy
  `YYYY-MM-DD_HHMM` files into per-session snapshots. Existing DB rows written by old pre-market runs are not migrated.
- Earnings come from Finnhub only if `FINNHUB_KEY` is set; otherwise `earn_known` is false and the column shows "?"
  (unknown earnings does NOT block the ● mark; the page banner says so).
- `json.dumps(...).replace("</", "<\\/")` is needed when embedding JSON in the HTML. Keep it.
- One bad ticker must not kill the run; errors are collected and shown at the bottom of the page.

## Status
Verified against `--mock` (page renders, filters/sort/history charts work, re-runs are idempotent) and with a first
live run on 2026-11-03 (Tradier + Finnhub tokens in `.env`): 7 tickers, 19 rows, no ticker errors, deltas 0.15-0.20,
IVs plausible, Finnhub earnings dates returned (AMD, NVDA). IV Rank is "–" until history accumulates.
Still to check:
1. Delta/IV values against the broker chain (only eyeballed for sanity so far).
2. Spot used is `last` (falls back to close/prevclose), so check it after hours.
3. Tickers with blank earnings (MU, MSFT, ...): confirm that means none in the window, not a missed lookup.
4. Thin non-standard expiries (e.g. Wed 11-18) show OI 0 and 25-40% spreads yet can top the ROC ranking; consider a
   min-OI filter or a flag.

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

## Page UX notes (template.html)
- Two lenses (tabs): Income (by delta, the table above) and Wheel (own price). `lens`, `src()` (rows vs `D.wheel`), `incomeCols`/`wheelCols`.
  Wheel rows come from `wheel_leg()`: strike = highest put ≤ min(own, spot) with a bid; ● = liquid and pays and no earnings (no IV test).
  `wheelReason()` / wheel `checklist()` mirror that; keep in sync with `screen_symbol`.
- Wheel staleness: `init()` computes `r.spot_vs_own` client-side (so old saved runs get it), adds a banner per ticker with spot ≤ own price, and a "page is N hours old" banner on index.html only (`ROOT === ''`) when the run is >26h old (weekend grace). Pages are static; spot is as of the last run.
- Default sort is `passes` desc then ROC: "Worth a look" rows first, the rest dimmed, with group header rows.
- ● = liquid AND rich AND `pays` (ann. ROC ≥ `--min-roc`, default 25) AND no earnings; the flags are computed in `screen_symbol`.
  `reason(r)` turns them into one line (Skip / Weak / Thin / Worth a look) for the ● tooltip, the details header and the
  "Top opportunities" cards (`topPicks()`: best ● expiry per ticker nearest `PREF_DTE`, top 6, independent of filters).
  Saved runs from before `pays` existed have `pays == null`; the page guards for it.
- `cols[].chk(r)` marks the cells that decide the ● (spread, OI, IV Rank or IV/RV, earnings). They light up green/red/amber on
  row hover/selection. **Keep these in sync with the `liquid` / `rich` / `passes` logic in `screener.py`.**
- A selected row is keyed `SYMBOL|expiry`. Its details panel is sticky at the bottom of the window: checklist, the strike
  ladder (`ladderHTML`, greyed rows are above the target delta), then IV history charts. Saved runs from before the ladder
  existed have no `ladder`/`target_delta`; the template guards for that (`tgt()`).
- History nav (`loadNav`/`buildNav`): ← Older / calendar popover (days with a run clickable, dot = close vs intraday/off-hours; custom, no `<select>`) / Newer → / Latest and `[` `]` keys, built from
  `window.RUNS`; `ROOT` is `""` for index.html and `"../"` for runs/*.html (injected by `render_page`). Missing manifest = no nav.
- The help drawer (`? How to read this`, or the `?` key) is built in `helpHTML()` from `config`, so thresholds there never drift.
- "Rows per ticker" (All / Best ROC / Nearest 14 DTE) is client-side only; `PREF_DTE` is a JS constant, not a CLI flag.
- Tab layout: first top-level tab is Markets (`data-lens="markets"`), a group with a sub-tab row `#subtabs`: Markets / Prices / Charts / Detailed chart (lens `market` / `prices` / `charts` / `detail`; `MK`, `lastMarkets` in `switchLens`). Then Income, Wheel, Covered calls. Hashes `#prices|#charts|#market` still work; `#markets` = last-used sub-tab.
- Stored-price table (now on Finder, see above): ticker, last spot, day / since-first change, range, trendline, ticker links to Google Finance (`gfUrl`, `quote/SYM:EXCH` from payload `exch`, which comes from the Tradier quote; fallback Google search). Built client-side by `priceRows()` from `D.history[sym].spot` (+ this run's spot) so it costs no API calls; its own `#prices` container, the filter bar/cards/table are hidden while it is active.
- Finder tab (`lens === 'finder'`, after Covered calls; `renderFinder()`, container `#finder`): the stored-price table (formerly on the Prices sub-tab): ticker, last spot, day / since-first change, range, trendline, Days, plus a `(Find)` link after each ticker (links in this table are ink-coloured, not blue) to TradingView's options Strategy Finder (`finderUrl`: symbol via `tvSym`, expiries today → +`FINDER_DAYS` (45), strikes 5-20% percent; dates computed client-side). The CSV button exports this table here. The Prices sub-tab is now only the live TradingView quotes.
- Markets (lens `market`) is the default tab (no hash); the others use `#income|#wheel|#cc|#markets|#prices|#charts|#detail`. Markets sub-tab: `tv-market-data` (our own symbol sectors; avoid symbols the free embed denies, e.g. TVC:VIX, CBOE:VIX, TVC:US10Y; VIX is shown via the front-month futures CBOE:VX1!, which the embed allows, which show "Permission denied") then the S&P heatmap. Sub-tab order Markets, Prices, Charts, Detailed chart, Screener (`screener` lens: TradingView stock screener embed, overview columns, most_capitalized screen). Left sidebar `#side` (`sideInit`/`sideSet`): market-quotes embed of the watchlist, collapsed by default, open/closed stored per device in localStorage `sideOpen`; widget built lazily on first open; pushes content right at ≥1100px, overlays below. Charts: a `symbol-info` (Symbol details, shows pre-market) per ticker. Detailed chart (`detail`): one `symbol-overview` for the watchlist. (Market Summary was removed as not useful.) Web-component widgets load via `tvComp` (module script from widgets.tradingview-widget.com, no theme attribute); iframe-style ones via `tvWidget`. Both go through `tvLazy` (IntersectionObserver, 400px margin), so off-screen widgets don't load until scrolled near. Some symbols (TVC:US10Y etc.) show "Permission denied" in free embeds, so rates use ETFs (TLT, IEF, UUP). Other TradingView notes: TradingView embed widgets (`tvWidget`, `TV_VIEWS`; market-quotes, mini-symbol-overview, market-overview, stock-heatmap). They load scripts from s3.tradingview.com at runtime (delayed ~15 min, internet needed, https/http only, not file://), so they are the one exception to the no-CDN rule and just stay blank offline. Symbols use `tvSym()` (`EXCH:SYM` from payload `exch`). Widgets bake in the theme, so `tvTheme()` rebuilds them on theme change. The active tab lives in the URL hash.
- `--rerender latest` = index.html only; `--prune-runs N` keeps the newest N snapshots; `--manifest` rebuilds manifest.js only (after hand-deleting runs). None re-render old pages.
- After editing the template run `python3 screener.py --rerender` (or `--mock --rerender`) to apply it to saved pages.
