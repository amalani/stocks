#!/usr/bin/env python3
"""
CSP screener.

For every ticker in watchlist.txt, finds the short put closest to (but not
above) TARGET delta in each expiration inside the DTE window, computes
premium / IV / liquidity / earnings metrics, keeps a daily IV history in
SQLite (for IV Rank), and renders a static page: public/index.html
(+ latest.json, latest.csv).

    python3 screener.py              # live; needs TRADIER_TOKEN env var or token.txt
    python3 screener.py --mock       # synthetic data, no API calls, separate DB

Stdlib only (Python 3.9+).
"""
import argparse
import csv
import json
import math
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------
# Tradier client (same endpoints your Apps Script uses, plus quotes/history)
# --------------------------------------------------------------------------
def aslist(x):
    """Tradier returns a bare object instead of a list when there is one item."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


class Tradier:
    def __init__(self, token, base):
        self.token, self.base = token, base.rstrip("/")

    def _get(self, path, **params):
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.load(r)
                time.sleep(0.15)  # stay well under rate limits
                return data
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise

    def quotes(self, symbols):
        d = self._get("/markets/quotes", symbols=",".join(symbols))
        return {q["symbol"]: q for q in aslist((d.get("quotes") or {}).get("quote"))}

    def expirations(self, sym):
        d = self._get("/markets/options/expirations", symbol=sym, includeAllRoots="true")
        return aslist((d.get("expirations") or {}).get("date"))

    def chain(self, sym, exp):
        d = self._get("/markets/options/chains", symbol=sym, expiration=exp, greeks="true")
        return aslist((d.get("options") or {}).get("option"))

    def closes(self, sym, days=75):
        start = (date.today() - timedelta(days=days)).isoformat()
        d = self._get("/markets/history", symbol=sym, interval="daily", start=start)
        return [float(x["close"]) for x in aslist((d.get("history") or {}).get("day"))]


# --------------------------------------------------------------------------
# Mock client: returns Tradier-shaped data so the real parsing code runs
# --------------------------------------------------------------------------
def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs(S, K, T, sigma, kind, r=0.04):
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "call":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2), _ncdf(d1)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1), _ncdf(d1) - 1


class Mock:
    def __init__(self):
        self.today = date.today()

    def _p(self, sym):
        rng = random.Random(sym)
        return dict(spot=round(rng.uniform(30, 600), 2), iv=rng.uniform(0.22, 0.75),
                    rv=rng.uniform(0.18, 0.6), oi=rng.choice([40, 300, 1500, 6000]))

    def quotes(self, symbols):
        return {s: {"symbol": s, "last": self._p(s)["spot"]} for s in symbols}

    def expirations(self, sym):
        d, out = self.today, []
        while len(out) < 8:
            d += timedelta(days=1)
            if d.weekday() == 4:
                out.append(d.isoformat())
        return out

    def chain(self, sym, exp):
        p = self._p(sym)
        S, T = p["spot"], max((date.fromisoformat(exp) - self.today).days, 1) / 365
        step = 1 if S < 100 else 2.5 if S < 250 else 5
        rng = random.Random(f"{sym}{exp}")
        out, K = [], math.floor(S * 0.7 / step) * step
        while K <= S * 1.3:
            for kind in ("put", "call"):
                sigma = max(0.08, p["iv"] * (1 + 0.6 * (1 - K / S)) * rng.uniform(0.97, 1.03))
                price, delta = _bs(S, K, T, sigma, kind)
                half = max(0.01, price * (0.02 if p["oi"] > 1000 else 0.12) / 2)
                out.append({"symbol": f"{sym}{exp}{kind[0]}{K}", "strike": K, "option_type": kind,
                            "bid": round(max(price - half, 0), 2), "ask": round(price + half, 2),
                            "volume": int(p["oi"] * rng.uniform(0, 0.3)),
                            "open_interest": int(p["oi"] * rng.uniform(0.2, 1.2)),
                            "greeks": {"delta": round(delta, 4), "mid_iv": round(sigma, 4)}})
            K += step
        return out

    def closes(self, sym, days=75):
        p = self._p(sym)
        rng = random.Random(f"{sym}c")
        c, out = p["spot"], []
        for _ in range(45):
            c *= math.exp(rng.gauss(0, p["rv"] / math.sqrt(252)))
            out.append(c)
        return out


def seed_mock_history(con, symbols, today):
    """Fake 120 days of IV history so IV Rank / charts have something to show."""
    for s in symbols:
        rng = random.Random(f"{s}hist")
        base = Mock()._p(s)
        iv = base["iv"]
        for i in range(120, 0, -1):
            d = today - timedelta(days=i)
            if d.weekday() >= 5:
                continue
            iv = min(max(iv + rng.gauss(0, 0.02), 0.12), 1.2)
            con.execute("INSERT OR REPLACE INTO iv_daily VALUES (?,?,?,?,?)",
                        (d.isoformat(), s, iv, base["rv"] * rng.uniform(0.85, 1.15), base["spot"]))
            con.execute("INSERT OR REPLACE INTO picks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (d.isoformat(), s, (d + timedelta(days=14)).isoformat(), 14, base["spot"] * 0.9,
                         0.2, 1.0, iv, iv * 55 * rng.uniform(0.8, 1.2), 10.0, base["spot"]))
    con.commit()


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
def norm_iv(v):
    """Tradier mid_iv is a decimal (0.35); tolerate percent-style values."""
    if v is None:
        return None
    v = float(v)
    if v <= 0:
        return None
    return v / 100 if v > 3 else v


def atm_iv(chain, spot):
    """Average of put/call mid_iv at the strike nearest spot."""
    if not chain:
        return None
    k = min({float(o["strike"]) for o in chain}, key=lambda s: abs(s - spot))
    ivs = [norm_iv((o.get("greeks") or {}).get("mid_iv")) for o in chain if float(o["strike"]) == k]
    ivs = [v for v in ivs if v]
    return sum(ivs) / len(ivs) if ivs else None


def realized_vol(closes, n=30):
    c = closes[-(n + 1):]
    if len(c) < 21:
        return None
    r = [math.log(b / a) for a, b in zip(c, c[1:]) if a > 0 and b > 0]
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1) * 252)


def iv_rank(hist, current, min_days):
    vals = [v for v in hist if v] + [current]
    if len(vals) < min_days:
        return None, len(vals)
    lo, hi = min(vals), max(vals)
    return (100 * (current - lo) / (hi - lo) if hi > lo else None), len(vals)


def finnhub_earnings(key, sym, start, end):
    """List of earnings dates in [start, end], or None if unknown."""
    url = ("https://finnhub.io/api/v1/calendar/earnings?" +
           urllib.parse.urlencode({"from": start, "to": end, "symbol": sym, "token": key}))
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            d = json.load(r)
        time.sleep(1.1)  # free tier: 60/min
        return [x["date"] for x in d.get("earningsCalendar", [])]
    except Exception:
        return None


def screen_symbol(api, sym, spot, cfg, today, earnings, hist_iv):
    exps = []
    for e in api.expirations(sym):
        dte = (date.fromisoformat(e) - today).days
        if cfg.dte_min <= dte <= cfg.dte_max:
            exps.append((e, dte))
    if not exps:
        return [], None, None

    rv = realized_vol(api.closes(sym))
    ref_iv, ref_gap, rows = None, 10 ** 9, []

    for e, dte in exps:
        chain = api.chain(sym, e)
        a_iv = atm_iv(chain, spot)
        if a_iv and abs(dte - 21) < ref_gap:
            ref_iv, ref_gap = a_iv, abs(dte - 21)

        cands = []
        for o in chain:
            if o.get("option_type") != "put" or not o.get("bid"):
                continue
            d = (o.get("greeks") or {}).get("delta")
            if d is None or not (-cfg.delta <= float(d) < 0):
                continue
            cands.append((abs(float(d)), o))
        if not cands:
            continue
        delta, o = max(cands, key=lambda t: t[0])  # highest delta that is still <= target

        bid, ask = float(o["bid"]), float(o.get("ask") or 0)
        mid = (bid + ask) / 2 if ask else bid
        K = float(o["strike"])
        em = spot * a_iv * math.sqrt(dte / 365) if a_iv else None
        earn = [d for d in (earnings or []) if today.isoformat() <= d <= e]
        rows.append(dict(
            symbol=sym, spot=spot, expiry=e, dte=dte, strike=K, delta=delta,
            bid=bid, ask=ask, mid=mid,
            spread_pct=(ask - bid) / mid * 100 if ask and mid > 0 else None,
            oi=int(o.get("open_interest") or 0), volume=int(o.get("volume") or 0),
            iv=norm_iv((o.get("greeks") or {}).get("mid_iv")), atm_iv=a_iv,
            roc=mid / K * 365 / dte * 100,                   # annualized, on mid
            cushion_pct=(spot - K) / spot * 100,
            em_pct=em / spot * 100 if em else None,
            em_mult=(spot - K) / em if em else None,         # cushion measured in expected moves
            premium=mid * 100, collateral=K * 100,
            earnings=min(earn) if earn else None,
            earn_known=earnings is not None,
        ))

    ivr, n = (iv_rank(hist_iv, ref_iv, cfg.min_history) if ref_iv else (None, 0))
    for r in rows:
        r["ref_iv"], r["ivr"], r["ivr_n"] = ref_iv, ivr, n
        r["rv30"] = rv
        r["iv_rv"] = (ref_iv / rv) if (ref_iv and rv) else None
        liquid = (r["spread_pct"] is not None and r["spread_pct"] <= cfg.max_spread
                  and r["oi"] >= cfg.min_oi)
        # Rank when we have enough history, otherwise fall back to IV vs realized.
        rich = (ivr >= cfg.ivr_min) if ivr is not None else (r["iv_rv"] is not None and r["iv_rv"] >= cfg.ivrv_min)
        r["liquid"], r["rich"] = liquid, rich
        r["passes"] = bool(liquid and rich and not r["earnings"])
    return rows, ref_iv, rv


# --------------------------------------------------------------------------
# Storage + output
# --------------------------------------------------------------------------
def open_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS iv_daily(date TEXT, symbol TEXT, atm_iv REAL, rv30 REAL, "
                "spot REAL, PRIMARY KEY(date, symbol))")
    con.execute("CREATE TABLE IF NOT EXISTS picks(date TEXT, symbol TEXT, expiry TEXT, dte INT, strike REAL, "
                "delta REAL, mid REAL, iv REAL, roc REAL, cushion_pct REAL, spot REAL, "
                "PRIMARY KEY(date, symbol, expiry))")
    return con


CSV_COLS = ["symbol", "spot", "expiry", "dte", "strike", "delta", "bid", "ask", "mid", "premium", "collateral",
            "roc", "cushion_pct", "em_mult", "atm_iv", "iv", "ivr", "ivr_n", "rv30", "iv_rv", "spread_pct",
            "oi", "volume", "earnings", "passes"]


def load_env(path):
    """Minimal .env loader: KEY=VALUE lines; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def render_page(payload):
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return (HERE / "template.html").read_text().replace("/*__DATA__*/null", blob)


def rerender(out):
    n = 0
    for f in sorted((out / "runs").glob("*.json")):
        f.with_suffix(".html").write_text(render_page(json.loads(f.read_text())))
        n += 1
    if (out / "latest.json").exists():
        (out / "index.html").write_text(render_page(json.loads((out / "latest.json").read_text())))
        n += 1
    print(f"re-rendered {n} pages in {out}", file=sys.stderr)


def main():
    load_env(HERE / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", default=str(HERE / "watchlist.txt"))
    ap.add_argument("--delta", type=float, default=0.20, help="max |delta| of the short put (default 0.20)")
    ap.add_argument("--dte-min", type=int, default=14)
    ap.add_argument("--dte-max", type=int, default=25)
    ap.add_argument("--min-oi", type=int, default=100)
    ap.add_argument("--max-spread", type=float, default=10.0, help="max bid-ask spread as %% of mid")
    ap.add_argument("--ivr-min", type=float, default=50.0)
    ap.add_argument("--ivrv-min", type=float, default=1.2)
    ap.add_argument("--min-history", type=int, default=20, help="days of IV history before IV Rank is shown")
    ap.add_argument("--out", default=str(HERE / "public"))
    ap.add_argument("--db", default=None)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--rerender", action="store_true",
                    help="rebuild index.html and every runs/*.html from saved JSON with the current template "
                         "(no API calls, no DB writes); use after UX-only changes to template.html")
    cfg = ap.parse_args()

    if cfg.rerender:
        return rerender(Path(cfg.out))

    now = datetime.now(ET)
    today = now.date()
    symbols = []
    for line in Path(cfg.tickers).read_text().splitlines():
        line = line.split("#")[0].strip().upper()
        if line and line not in symbols:
            symbols.append(line)
    if not symbols:
        sys.exit("watchlist is empty")

    db_path = Path(cfg.db) if cfg.db else HERE / "data" / ("mock.db" if cfg.mock else "screener.db")
    con = open_db(db_path)

    fh_key = os.environ.get("FINNHUB_KEY") or ((HERE / "finnhub.txt").read_text().strip()
                                              if (HERE / "finnhub.txt").exists() else None)
    if cfg.mock:
        api, fh_key = Mock(), None
        if not con.execute("SELECT 1 FROM iv_daily LIMIT 1").fetchone():
            seed_mock_history(con, symbols, today)
    else:
        token = os.environ.get("TRADIER_TOKEN") or ((HERE / "token.txt").read_text().strip()
                                                    if (HERE / "token.txt").exists() else None)
        if not token:
            sys.exit("No Tradier token: set TRADIER_TOKEN in csp-screener/.env (see .env.example)")
        api = Tradier(token, os.environ.get("TRADIER_BASE_URL", "https://api.tradier.com/v1"))

    quotes = api.quotes(symbols)
    rows, errors, ref = [], [], {}
    for sym in symbols:
        try:
            q = quotes.get(sym)
            spot = float(q.get("last") or q.get("close") or q.get("prevclose")) if q else None
            if not spot:
                raise ValueError("no quote")
            earnings = (finnhub_earnings(fh_key, sym, today.isoformat(),
                                         (today + timedelta(days=cfg.dte_max + 2)).isoformat())
                        if fh_key else None)
            hist = [r[0] for r in con.execute(
                "SELECT atm_iv FROM iv_daily WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 252",
                (sym, today.isoformat()))]
            r, ref_iv, rv = screen_symbol(api, sym, spot, cfg, today, earnings, hist)
            if not r:
                errors.append(f"{sym}: no put at |delta| <= {cfg.delta} in {cfg.dte_min}-{cfg.dte_max} DTE")
            rows += r
            ref[sym] = (ref_iv, rv, spot)
            print(f"{sym:6} {len(r)} expiries", file=sys.stderr)
        except Exception as ex:  # one bad ticker shouldn't kill the run
            errors.append(f"{sym}: {ex}")
            print(f"{sym:6} ERROR {ex}", file=sys.stderr)

    # Only record history on weekdays so weekend re-runs don't add duplicate points.
    recorded = today.weekday() < 5
    if recorded:
        for sym, (iv, rv, spot) in ref.items():
            if iv:
                con.execute("INSERT OR REPLACE INTO iv_daily VALUES (?,?,?,?,?)",
                            (today.isoformat(), sym, iv, rv, spot))
        for r in rows:
            con.execute("INSERT OR REPLACE INTO picks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (today.isoformat(), r["symbol"], r["expiry"], r["dte"], r["strike"], r["delta"],
                         r["mid"], r["iv"], r["roc"], r["cushion_pct"], r["spot"]))
        con.commit()

    history = {}
    for sym in symbols:
        history[sym] = [
            {"d": d, "iv": iv, "rv": rv, "spot": sp, "roc": roc}
            for d, iv, rv, sp, roc in con.execute(
                "SELECT d.date, d.atm_iv, d.rv30, d.spot, "
                "(SELECT MAX(roc) FROM picks p WHERE p.date=d.date AND p.symbol=d.symbol) "
                "FROM iv_daily d WHERE d.symbol=? ORDER BY d.date DESC LIMIT 365", (sym,))][::-1]

    payload = {
        "generated": now.strftime("%Y-%m-%d %H:%M ET"), "mock": cfg.mock, "recorded": recorded,
        "earnings_source": bool(fh_key),
        "config": {"delta": cfg.delta, "dte_min": cfg.dte_min, "dte_max": cfg.dte_max, "min_oi": cfg.min_oi,
                   "max_spread": cfg.max_spread, "ivr_min": cfg.ivr_min, "ivrv_min": cfg.ivrv_min,
                   "min_history": cfg.min_history},
        "rows": rows, "history": history, "errors": errors,
    }
    out = Path(cfg.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.json").write_text(json.dumps(payload, indent=1))
    (out / "index.html").write_text(render_page(payload))
    # Keep a snapshot of every run so UX changes can be re-applied to old pages (--rerender).
    runs = out / "runs"
    runs.mkdir(exist_ok=True)
    stamp = now.strftime("%Y-%m-%d_%H%M") + ("-mock" if cfg.mock else "")
    (runs / f"{stamp}.json").write_text(json.dumps(payload, separators=(",", ":")))
    (runs / f"{stamp}.html").write_text(render_page(payload))
    with open(out / "latest.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        for r in sorted(rows, key=lambda r: -r["roc"]):
            w.writerow([("" if r.get(c) is None else (round(r[c], 4) if isinstance(r[c], float) else r[c]))
                        for c in CSV_COLS])
    print(f"{len(rows)} rows, {sum(r['passes'] for r in rows)} passing -> {out / 'index.html'}", file=sys.stderr)


if __name__ == "__main__":
    main()
