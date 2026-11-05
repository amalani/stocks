#!/usr/bin/env python3
"""
CSP screener.

For every ticker on the watchlist (a published Google Sheet CSV via WATCHLIST_URL
in .env, else watchlist.txt), finds the short put closest to (but not above)
TARGET delta in each expiration inside the DTE window, computes premium / IV /
liquidity / earnings metrics, keeps a daily IV history in SQLite (for IV Rank),
and renders a static page: public/index.html (+ latest.json, latest.csv).
Tickers with an "own price" also get a wheel view: the strike at/below the price
you would be happy to own the stock at.

    python3 screener.py              # live; needs TRADIER_TOKEN env var or token.txt
    python3 screener.py --mock       # synthetic data, no API calls, separate DB

Stdlib only (Python 3.9+).
"""
import argparse
import csv
import hashlib
import io
import json
import math
import os
import random
import re
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
    def __init__(self, today=None):
        self.today = today or date.today()

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


LADDER_MIN_DELTA, LADDER_MAX_DELTA, LADDER_SIZE = 0.05, 0.40, 12


def build_ladder(chain, spot, dte, a_iv, target, center=None):
    """Puts around the target delta (or, with `center`, around a strike) for one expiry, so the page can show
    neighbouring strikes."""
    em = spot * a_iv * math.sqrt(dte / 365) if a_iv else None
    lo, hi = (0.02, 0.60) if center is not None else (LADDER_MIN_DELTA, LADDER_MAX_DELTA)
    out = []
    for o in chain:
        if o.get("option_type") != "put" or not o.get("bid"):
            continue
        d = (o.get("greeks") or {}).get("delta")
        if d is None or not (lo <= abs(float(d)) <= hi):
            continue
        bid, ask = float(o["bid"]), float(o.get("ask") or 0)
        mid = (bid + ask) / 2 if ask else bid
        K = float(o["strike"])
        if mid <= 0 or K <= 0:
            continue
        out.append(dict(
            strike=K, delta=round(abs(float(d)), 4), bid=bid, ask=ask, mid=round(mid, 3),
            roc=round(mid / K * 365 / dte * 100, 2), cushion_pct=round((spot - K) / spot * 100, 2),
            em_mult=round((spot - K) / em, 3) if em else None,
            spread_pct=round((ask - bid) / mid * 100, 2) if ask else None,
            oi=int(o.get("open_interest") or 0),
            iv=norm_iv((o.get("greeks") or {}).get("mid_iv"))))
    if center is not None:
        out = sorted(out, key=lambda x: abs(x["strike"] - center))[:LADDER_SIZE]   # nearest to the strike
    else:
        out = sorted(out, key=lambda x: abs(x["delta"] - target))[:LADDER_SIZE]    # nearest to target
    return sorted(out, key=lambda x: x["delta"])


# ---- watchlist: sheet (published CSV) or text file --------------------------
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def parse_num(s):
    """'0.30', '30%', '$1,200' -> float; blank -> None. Raises ValueError on junk."""
    s = str(s).strip().replace("$", "").replace(",", "")
    if not s:
        return None
    return float(s[:-1]) / 100 if s.endswith("%") else float(s)


def _check(sym, delta, own, cost=None):
    if delta is not None and not (0 < delta < 1):
        raise ValueError(f"{sym}: max delta must be between 0 and 1 (got {delta})")
    if own is not None and own <= 0:
        raise ValueError(f"{sym}: own price must be positive (got {own})")
    if cost is not None and cost <= 0:
        raise ValueError(f"{sym}: cost basis must be positive (got {cost})")


def parse_watchlist_lines(lines):
    """Text format, one ticker per line: AMD | AMD 0.30 | AMD @480 | AMD 0.30 @480 $465   ('#' = comment).
    0.30 is the max put delta for that ticker; @480 is the price you would be happy to own it at;
    $465 is the cost basis of shares you already hold (turns on the covered-call view)."""
    wl = {}
    for line in lines:
        parts = line.split("#")[0].upper().split()
        if not parts:
            continue
        sym = parts[0]
        if not TICKER_RE.match(sym):
            raise ValueError(f"bad ticker {parts[0]!r}")
        if sym in wl:
            continue
        delta = own = cost = None
        for p in parts[1:]:
            try:
                if p.startswith("@"):
                    own = parse_num(p[1:])
                elif p.startswith("$"):
                    cost = parse_num(p[1:])
                else:
                    delta = parse_num(p)
            except ValueError:
                raise ValueError(f"{sym}: can't read {p!r} (use e.g. 'AMD 0.30 @480 $465')")
        _check(sym, delta, own, cost)
        wl[sym] = {"delta": delta, "own": own, "cost": cost}
    return wl


def parse_watchlist_csv(text):
    """Published Google Sheet CSV. Header row names the columns: Ticker, Max delta, Own price
    (matched loosely: 'ticker'/'symbol', 'delta', 'own'/'buy'/'hold'/'price', 'cost'/'basis'). Without a header the
    order is ticker, delta, own price, cost basis. Bad rows are skipped with a note rather than failing the run."""
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("sheet is empty")
    head = [c.strip().lower() for c in rows[0]]

    def col(*names):
        return next((i for i, h in enumerate(head) if any(n in h for n in names)), None)

    i_t = col("ticker", "symbol")
    if i_t is None:
        if not TICKER_RE.match(rows[0][0].strip().upper()):
            raise ValueError("no Ticker column found in the sheet")
        i_t, i_d, i_o, i_c, body = 0, 1, 2, 3, rows
    else:
        i_c = col("cost", "basis")
        i_o = next((i for i, h in enumerate(head) if i != i_c and any(n in h for n in ("own", "buy", "hold", "price"))), None)
        i_d, body = col("delta"), rows[1:]
    wl, notes = {}, []
    for r in body:
        cell = lambda i: r[i].strip() if i is not None and i < len(r) else ""
        sym = cell(i_t).upper()
        if not sym or sym in wl:
            continue
        if not TICKER_RE.match(sym):
            notes.append(f"sheet: skipped {sym!r} (not a ticker)")
            continue
        try:
            delta, own, cost = parse_num(cell(i_d)), parse_num(cell(i_o)), parse_num(cell(i_c))
            _check(sym, delta, own, cost)
        except ValueError as ex:
            notes.append(f"sheet: {ex}; using defaults for {sym}")
            delta = own = cost = None
        wl[sym] = {"delta": delta, "own": own, "cost": cost}
    return wl, notes


def load_watchlist(path, url):
    """-> (watchlist dict, source label, notes). The sheet wins when a URL is given; if it can't be read we fall
    back to the text file and say so on the page."""
    notes, source = [], "file"
    if url:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "csp-screener"})
            with urllib.request.urlopen(req, timeout=20) as r:
                wl, notes = parse_watchlist_csv(r.read().decode("utf-8-sig"))
            if wl:
                return wl, "sheet", notes
            notes.append("sheet watchlist had no tickers; using watchlist.txt")
        except Exception as ex:   # never echo the URL itself
            notes.append(f"couldn't read the sheet ({type(ex).__name__}: {ex}); using watchlist.txt")
        source = "file (sheet unavailable)"
    return parse_watchlist_lines(Path(path).read_text().splitlines()), source, notes


def wheel_leg(chain, spot, dte, a_iv, own):
    """The put you would sell to (maybe) buy at `own`: highest listed strike at or below min(own, spot) with a bid."""
    cap = min(own, spot)
    cands = [o for o in chain if o.get("option_type") == "put" and o.get("bid") and float(o["strike"]) <= cap]
    if not cands:
        return None
    o = max(cands, key=lambda o: float(o["strike"]))
    d = (o.get("greeks") or {}).get("delta")
    bid, ask = float(o["bid"]), float(o.get("ask") or 0)
    mid = (bid + ask) / 2 if ask else bid
    K = float(o["strike"])
    em = spot * a_iv * math.sqrt(dte / 365) if a_iv else None
    return dict(
        strike=K, delta=abs(float(d)) if d is not None else None, bid=bid, ask=ask, mid=mid,
        spread_pct=(ask - bid) / mid * 100 if ask and mid > 0 else None,
        oi=int(o.get("open_interest") or 0), volume=int(o.get("volume") or 0),
        iv=norm_iv((o.get("greeks") or {}).get("mid_iv")),
        roc=mid / K * 365 / dte * 100, cushion_pct=(spot - K) / spot * 100,
        em_mult=(spot - K) / em if em else None, premium=mid * 100, collateral=K * 100,
        net_cost=K - mid, net_below_spot_pct=(spot - (K - mid)) / spot * 100)


class Memo:
    """Caches expirations / chains / closes so the put and call screens share API calls."""
    def __init__(self, api):
        self.api, self._c = api, {}

    def __getattr__(self, name):
        fn = getattr(self.api, name)
        if name not in ("expirations", "chain", "closes"):
            return fn

        def cached(*a):
            if (name, a) not in self._c:
                self._c[(name, a)] = fn(*a)
            return self._c[(name, a)]
        return cached


# ---- covered calls: shares you hold, sold above spot AND above your cost basis ----------------------------
def cc_expiries(dates, today, dmin, dmax):
    """Weekly (Friday) and monthly expiries in the window, no dailies. A week with no Friday expiry (holiday)
    falls back to its Thursday; Mon/Wed-only weeks are skipped."""
    last = {}
    for e in dates:
        d = date.fromisoformat(e)
        k = d.isocalendar()[:2]
        if k not in last or d > last[k]:
            last[k] = d
    return [(d.isoformat(), (d - today).days) for d in sorted(last.values())
            if d.weekday() >= 3 and dmin <= (d - today).days <= dmax]


def is_monthly(d):
    """Third Friday (or the Thursday before it when that Friday is a market holiday)."""
    return (d.weekday() == 4 and 15 <= d.day <= 21) or (d.weekday() == 3 and 15 <= (d + timedelta(1)).day <= 21)


def _call_row(o, spot, dte, em, cost):
    d = abs(float((o.get("greeks") or {}).get("delta")))
    bid, ask = float(o["bid"]), float(o.get("ask") or 0)
    mid = (bid + ask) / 2 if ask else bid
    K = float(o["strike"])
    return dict(
        strike=K, delta=round(d, 4), bid=bid, ask=ask, mid=round(mid, 3), premium=round(mid * 100, 2),
        roc=round(mid / spot * 365 / dte * 100, 2),                 # annualized premium yield on the share price
        upside_pct=round((K - spot) / spot * 100, 2),               # room before the shares would be called
        called_pct=round((K + mid - cost) / cost * 100, 2),         # return vs your cost basis if called away
        em_mult=round((K - spot) / em, 3) if em else None,
        spread_pct=round((ask - bid) / mid * 100, 2) if ask and mid > 0 else None,
        oi=int(o.get("open_interest") or 0), volume=int(o.get("volume") or 0),
        iv=norm_iv((o.get("greeks") or {}).get("mid_iv")))


def cc_leg(chain, spot, dte, a_iv, cost, target):
    """The call to sell: highest delta still <= target, but never a strike under cost basis. When the cost basis
    pushes the strike up (spot under cost, or a high-cost name) the row is flagged `floored`."""
    em = spot * a_iv * math.sqrt(dte / 365) if a_iv else None
    cands = []
    for o in chain:
        if o.get("option_type") != "call" or not o.get("bid"):
            continue
        d = (o.get("greeks") or {}).get("delta")
        if d is None or not (0.02 <= abs(float(d)) <= 0.60) or float(o["strike"]) <= 0:
            continue
        cands.append(_call_row(o, spot, dte, em, cost))
    cands.sort(key=lambda c: c["strike"])
    within = [c for c in cands if c["delta"] <= target]      # lowest strike here = highest delta <= target
    if not within:
        return None
    ok = [c for c in within if c["strike"] >= cost]
    if not ok:
        return None
    pick = dict(ok[0])
    pick["floored"] = ok[0] is not within[0]
    pick["target_strike"] = within[0]["strike"]             # where the delta rule alone would have put it
    near = sorted(cands, key=lambda c: abs(c["strike"] - pick["strike"]))[:LADDER_SIZE + 2]
    pick["ladder"] = sorted(near, key=lambda c: c["strike"])
    return pick


def screen_cc(api, sym, spot, cfg, today, earnings, hist_iv, cost):
    exps = cc_expiries(api.expirations(sym), today, cfg.cc_dte_min, cfg.cc_dte_max)
    if not exps:
        return []
    rv = realized_vol(api.closes(sym))
    ref_iv, ref_gap, rows = None, 10 ** 9, []
    for e, dte in exps:
        chain = api.chain(sym, e)
        a_iv = atm_iv(chain, spot)
        if a_iv and abs(dte - 21) < ref_gap:
            ref_iv, ref_gap = a_iv, abs(dte - 21)
        leg = cc_leg(chain, spot, dte, a_iv, cost, cfg.cc_delta)
        if not leg:
            continue
        earn = [d for d in (earnings or []) if today.isoformat() <= d <= e]
        leg.update(symbol=sym, spot=spot, expiry=e, dte=dte, monthly=is_monthly(date.fromisoformat(e)),
                   cost_basis=cost, spot_vs_cost=(spot / cost - 1) * 100, target_delta=cfg.cc_delta, atm_iv=a_iv,
                   collateral=spot * 100, earnings=min(earn) if earn else None, earn_known=earnings is not None)
        rows.append(leg)
    ivr, n = (iv_rank(hist_iv, ref_iv, cfg.min_history) if ref_iv else (None, 0))
    for r in rows:   # IV richness is information only: you hold the shares either way
        r["ref_iv"], r["ivr"], r["ivr_n"], r["rv30"] = ref_iv, ivr, n, rv
        r["iv_rv"] = (ref_iv / rv) if (ref_iv and rv) else None
        r["liquid"] = (r["spread_pct"] is not None and r["spread_pct"] <= cfg.max_spread and r["oi"] >= cfg.min_oi)
        r["pays"] = r["roc"] >= cfg.min_cc_yield
        r["passes"] = bool(r["liquid"] and r["pays"] and not r["earnings"])
    return rows


def screen_symbol(api, sym, spot, cfg, today, earnings, hist_iv, target=None, own=None):
    target = cfg.delta if target is None else target
    exps = []
    for e in api.expirations(sym):
        dte = (date.fromisoformat(e) - today).days
        if cfg.dte_min <= dte <= cfg.dte_max:
            exps.append((e, dte))
    if not exps:
        return [], [], None, None

    rv = realized_vol(api.closes(sym))
    ref_iv, ref_gap, rows, wheel = None, 10 ** 9, [], []

    for e, dte in exps:
        chain = api.chain(sym, e)
        a_iv = atm_iv(chain, spot)
        if a_iv and abs(dte - 21) < ref_gap:
            ref_iv, ref_gap = a_iv, abs(dte - 21)
        earn = [d for d in (earnings or []) if today.isoformat() <= d <= e]

        if own:   # wheel view: the strike at/below the price you'd be happy to own it at
            w = wheel_leg(chain, spot, dte, a_iv, own)
            if w:
                w.update(symbol=sym, spot=spot, expiry=e, dte=dte, own_price=own, own_above_spot=own >= spot,
                         atm_iv=a_iv, earnings=min(earn) if earn else None, earn_known=earnings is not None,
                         ladder=build_ladder(chain, spot, dte, a_iv, 0, center=w["strike"]))
                wheel.append(w)

        cands = []
        for o in chain:
            if o.get("option_type") != "put" or not o.get("bid"):
                continue
            d = (o.get("greeks") or {}).get("delta")
            if d is None or not (-target <= float(d) < 0):
                continue
            cands.append((abs(float(d)), o))
        if not cands:
            continue
        delta, o = max(cands, key=lambda t: t[0])  # highest delta that is still <= target

        bid, ask = float(o["bid"]), float(o.get("ask") or 0)
        mid = (bid + ask) / 2 if ask else bid
        K = float(o["strike"])
        em = spot * a_iv * math.sqrt(dte / 365) if a_iv else None
        rows.append(dict(
            symbol=sym, spot=spot, expiry=e, dte=dte, strike=K, delta=delta, target_delta=target,
            ladder=build_ladder(chain, spot, dte, a_iv, target),
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
        pays = r["roc"] >= cfg.min_roc
        r["liquid"], r["rich"], r["pays"] = liquid, rich, pays
        r["passes"] = bool(liquid and rich and pays and not r["earnings"])

    for w in wheel:   # you are happy to own it, so IV richness is information here, not a requirement
        w["ref_iv"], w["ivr"], w["ivr_n"] = ref_iv, ivr, n
        w["rv30"] = rv
        w["iv_rv"] = (ref_iv / rv) if (ref_iv and rv) else None
        w["liquid"] = (w["spread_pct"] is not None and w["spread_pct"] <= cfg.max_spread and w["oi"] >= cfg.min_oi)
        w["pays"] = w["roc"] >= cfg.min_roc
        w["passes"] = bool(w["liquid"] and w["pays"] and not w["earnings"])
    return rows, wheel, ref_iv, rv


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


def render_page(payload, root=""):
    """root is the path from the page back to the output dir ("" for index.html, "../" for runs/*.html)."""
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return ((HERE / "template.html").read_text()
            .replace("/*__DATA__*/null", blob).replace('/*__ROOT__*/""', json.dumps(root))
            .replace("/*__ROOTPATH__*/", root))


# --------------------------------------------------------------------------
# Per-session snapshots: runs/YYYY-MM-DD.{json,html} (+ manifest.js for the page's prev/next nav)
# --------------------------------------------------------------------------
# A "session" is the trading day the quotes belong to, not the wall-clock date. Same-session runs overwrite each
# other (last run wins) unless the new run is lower quality: a close run beats intraday beats off-hours.
PHASE_RANK = {"off": 1, "intraday": 2, "close": 3}
OPEN_T, CLOSE_T = (9, 30), (16, 0)


def last_session(now):
    """Most recent weekday whose regular session has started (ignores holidays; used when quotes carry no date)."""
    d = now.date()
    if d.weekday() >= 5 or (now.hour, now.minute) < OPEN_T:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def quote_session(quotes, now):
    """Trading day of the quotes: newest Tradier trade_date (epoch ms), else last_session()."""
    days = []
    for q in quotes.values():
        try:
            days.append(datetime.fromtimestamp(int(q["trade_date"]) / 1000, ET).date())
        except (KeyError, TypeError, ValueError, OSError):
            pass
    return max(days) if days else last_session(now)


def run_phase(now, session):
    if now.date() == session:
        if OPEN_T <= (now.hour, now.minute) < CLOSE_T:
            return "intraday"
        if (now.hour, now.minute) >= CLOSE_T:
            return "close"
    return "off"


def saved_rank(out, session):
    f = out / "runs" / f"{session}.json"
    try:
        return PHASE_RANK.get(json.loads(f.read_text()).get("phase"), 0)
    except (OSError, ValueError):
        return 0 if not f.exists() else 1


def write_manifest(out):
    runs = []
    for f in sorted((out / "runs").glob("????-??-??.json"), reverse=True):
        p = json.loads(f.read_text())
        runs.append({"d": f.stem, "gen": p["generated"][11:], "phase": p.get("phase"),
                     "n": len(p["rows"]), "pass": sum(1 for r in p["rows"] if r.get("passes"))})
    (out / "manifest.js").write_text("window.RUNS=" + json.dumps(runs, separators=(",", ":")) + ";\n")
    return runs


LEGACY_RUN = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4})(-mock)?$")


def migrate_runs(out):
    """Collapse old per-run files (YYYY-MM-DD_HHMM.json) into one snapshot per session, best phase then latest."""
    runs, best = out / "runs", {}
    for f in sorted(runs.glob("*.json")):
        if not LEGACY_RUN.match(f.stem):
            continue
        p = json.loads(f.read_text())
        gen = datetime.strptime(p["generated"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=ET)
        session = last_session(gen)
        p.setdefault("session", session.isoformat())
        p.setdefault("phase", run_phase(gen, session))
        p["recorded"] = True
        key = (PHASE_RANK[p["phase"]], p["generated"])
        if session.isoformat() not in best or key >= best[session.isoformat()][0]:
            best[session.isoformat()] = (key, p)
    for f in list(runs.glob("*.json")) + list(runs.glob("*.html")):
        if LEGACY_RUN.match(f.stem):
            f.unlink()
    for session, (_, p) in best.items():
        (runs / f"{session}.json").write_text(json.dumps(p, separators=(",", ":")))
    if best:
        print(f"migrated legacy runs into {len(best)} per-session snapshot(s)", file=sys.stderr)


def write_pwa(out):
    """Copy the installable-app files (manifest, service worker, icons) next to the pages."""
    out.mkdir(parents=True, exist_ok=True)
    files = sorted((HERE / "pwa").iterdir())
    # Version = hash of the app files + template, so a changed sw.js byte-wise triggers the browser's update
    # and the new worker drops the previous cache.
    h = hashlib.sha1()
    for f in files + [HERE / "template.html"]:
        h.update(f.read_bytes())
    for f in files:
        data = f.read_bytes()
        if f.name == "sw.js":
            data = data.replace(b"/*__VERSION__*/", h.hexdigest()[:10].encode())
        (out / f.name).write_bytes(data)


def rerender(out):
    n = 0
    write_pwa(out)
    if (out / "runs").is_dir():
        migrate_runs(out)
        for f in sorted((out / "runs").glob("*.json")):
            f.with_suffix(".html").write_text(render_page(json.loads(f.read_text()), "../"))
            n += 1
        write_manifest(out)
    if (out / "latest.json").exists():
        (out / "index.html").write_text(render_page(json.loads((out / "latest.json").read_text())))
        n += 1
    print(f"re-rendered {n} pages in {out}", file=sys.stderr)


def main():
    load_env(HERE / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", default=None,
                    help="watchlist text file (default watchlist.txt). Giving this skips WATCHLIST_URL, the "
                         "published-sheet CSV set in .env")
    ap.add_argument("--delta", type=float, default=0.20, help="default max |delta| of the short put (0.20); override per ticker in watchlist.txt, e.g. 'AMD 0.30'")
    ap.add_argument("--dte-min", type=int, default=1)
    ap.add_argument("--dte-max", type=int, default=25)
    ap.add_argument("--min-oi", type=int, default=100)
    ap.add_argument("--max-spread", type=float, default=10.0, help="max bid-ask spread as %% of mid")
    ap.add_argument("--ivr-min", type=float, default=50.0)
    ap.add_argument("--ivrv-min", type=float, default=1.2)
    ap.add_argument("--min-roc", type=float, default=25.0,
                    help="annualized ROC (%%) a put must pay to count as worth a look (default 25)")
    ap.add_argument("--cc-delta", type=float, default=0.20,
                    help="max |delta| of the covered call, roughly the chance of being called away (0.20)")
    ap.add_argument("--cc-dte-min", type=int, default=5, help="skip covered-call expiries sooner than this (no dailies)")
    ap.add_argument("--cc-dte-max", type=int, default=38, help="covered-call window: Friday/monthly expiries up to this many days out")
    ap.add_argument("--min-cc-yield", type=float, default=10.0,
                    help="annualized premium yield (%% of share price) a call must pay to count as worth a look (default 10)")
    ap.add_argument("--min-history", type=int, default=20, help="days of IV history before IV Rank is shown")
    ap.add_argument("--out", default=None, help="output dir (default public/, or public-mock/ with --mock)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--rerender", action="store_true",
                    help="rebuild index.html and every runs/*.html from saved JSON with the current template "
                         "(no API calls, no DB writes); use after UX-only changes to template.html. Also folds "
                         "old runs/YYYY-MM-DD_HHMM files into one snapshot per session and rebuilds manifest.js")
    ap.add_argument("--asof", default=None, metavar="'YYYY-MM-DD HH:MM'",
                    help="pretend the run happens at this ET time (testing the session/phase logic with --mock)")
    cfg = ap.parse_args()
    if cfg.out is None:
        cfg.out = str(HERE / ("public-mock" if cfg.mock else "public"))

    if cfg.rerender:
        return rerender(Path(cfg.out))

    now = datetime.strptime(cfg.asof, "%Y-%m-%d %H:%M").replace(tzinfo=ET) if cfg.asof else datetime.now(ET)
    today = now.date()
    # Watchlist: published Google Sheet CSV (WATCHLIST_URL) if set, else watchlist.txt (or --tickers FILE).
    # Per ticker: optional max put delta and optional "own price" (what you'd be happy to pay for the stock).
    try:
        wl, wl_source, wl_notes = load_watchlist(cfg.tickers or HERE / "watchlist.txt",
                                                 None if cfg.tickers else os.environ.get("WATCHLIST_URL"))
    except (ValueError, OSError) as ex:
        sys.exit(f"watchlist: {ex}")
    symbols = list(wl)
    deltas = {s: v["delta"] for s, v in wl.items() if v["delta"]}
    owns = {s: v["own"] for s, v in wl.items() if v["own"]}
    costs = {s: v["cost"] for s, v in wl.items() if v["cost"]}
    if not symbols:
        sys.exit("watchlist is empty")
    for n_ in wl_notes:
        print(f"watchlist: {n_}", file=sys.stderr)

    db_path = Path(cfg.db) if cfg.db else HERE / "data" / ("mock.db" if cfg.mock else "screener.db")
    con = open_db(db_path)

    fh_key = os.environ.get("FINNHUB_KEY") or ((HERE / "finnhub.txt").read_text().strip()
                                              if (HERE / "finnhub.txt").exists() else None)
    if cfg.mock:
        api, fh_key = Mock(today), None
        if not con.execute("SELECT 1 FROM iv_daily LIMIT 1").fetchone():
            seed_mock_history(con, symbols, today)
    else:
        token = os.environ.get("TRADIER_TOKEN") or ((HERE / "token.txt").read_text().strip()
                                                    if (HERE / "token.txt").exists() else None)
        if not token:
            sys.exit("No Tradier token: set TRADIER_TOKEN in csp-screener/.env (see .env.example)")
        api = Tradier(token, os.environ.get("TRADIER_BASE_URL", "https://api.tradier.com/v1"))
    quotes = api.quotes(symbols)
    api = Memo(api)

    session = quote_session(quotes, now)
    phase = run_phase(now, session)
    out = Path(cfg.out)
    # Persist (snapshot + IV history) unless this session already has a better run, e.g. an overnight preview
    # must not replace the 4:30pm run. Otherwise it only refreshes index.html / latest.*.
    recorded = PHASE_RANK[phase] >= saved_rank(out, session)
    rows, wheel_rows, cc_rows, errors, ref = [], [], [], [], {}
    for sym in symbols:
        try:
            q = quotes.get(sym)
            spot = float(q.get("last") or q.get("close") or q.get("prevclose")) if q else None
            if not spot:
                raise ValueError("no quote")
            earnings = (finnhub_earnings(fh_key, sym, today.isoformat(),
                                         (today + timedelta(days=max(cfg.dte_max, cfg.cc_dte_max if sym in costs else 0) + 2)).isoformat())
                        if fh_key else None)
            hist = [r[0] for r in con.execute(
                "SELECT atm_iv FROM iv_daily WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 252",
                (sym, session.isoformat()))]
            target = deltas.get(sym, cfg.delta)
            r, w, ref_iv, rv = screen_symbol(api, sym, spot, cfg, today, earnings, hist, target, owns.get(sym))
            if not r:
                errors.append(f"{sym}: no put at |delta| <= {target} in {cfg.dte_min}-{cfg.dte_max} DTE")
            if sym in owns and not w:
                errors.append(f"{sym}: no put with a bid at or below ${owns[sym]:g} in {cfg.dte_min}-{cfg.dte_max} DTE (wheel view)")
            rows += r
            wheel_rows += w
            if sym in costs:
                c = screen_cc(api, sym, spot, cfg, today, earnings, hist, costs[sym])
                if not c:
                    errors.append(f"{sym}: no call with a bid at or above your ${costs[sym]:g} cost basis within "
                                  f"{cfg.cc_delta:g}Δ in {cfg.cc_dte_min}-{cfg.cc_dte_max} DTE (covered calls)")
                cc_rows += c
            ref[sym] = (ref_iv, rv, spot)
            print(f"{sym:6} {len(r)} expiries", file=sys.stderr)
        except Exception as ex:  # one bad ticker shouldn't kill the run
            errors.append(f"{sym}: {ex}")
            print(f"{sym:6} ERROR {ex}", file=sys.stderr)

    if recorded:
        for sym, (iv, rv, spot) in ref.items():
            if iv:
                con.execute("INSERT OR REPLACE INTO iv_daily VALUES (?,?,?,?,?)",
                            (session.isoformat(), sym, iv, rv, spot))
        for r in rows:
            con.execute("INSERT OR REPLACE INTO picks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (session.isoformat(), r["symbol"], r["expiry"], r["dte"], r["strike"], r["delta"],
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
        "generated": now.strftime("%Y-%m-%d %H:%M ET"), "generated_at": now.isoformat(), "mock": cfg.mock, "recorded": recorded,
        "session": session.isoformat(), "phase": phase,
        "earnings_source": bool(fh_key),
        "config": {"delta": cfg.delta, "dte_min": cfg.dte_min, "dte_max": cfg.dte_max, "min_oi": cfg.min_oi,
                   "max_spread": cfg.max_spread, "ivr_min": cfg.ivr_min, "ivrv_min": cfg.ivrv_min,
                   "min_history": cfg.min_history, "min_roc": cfg.min_roc, "custom_delta": deltas,
                   "own_price": owns, "cost_basis": costs, "cc_delta": cfg.cc_delta,
                   "cc_dte_min": cfg.cc_dte_min, "cc_dte_max": cfg.cc_dte_max, "min_cc_yield": cfg.min_cc_yield},
        "watchlist": {"source": wl_source, "notes": wl_notes},
        "rows": rows, "wheel": wheel_rows, "cc": cc_rows, "history": history, "errors": errors,
    }
    write_pwa(out)
    (out / "latest.json").write_text(json.dumps(payload, indent=1))
    (out / "index.html").write_text(render_page(payload))
    # One snapshot per session (last good run wins); the page's prev/next nav reads manifest.js.
    # Kept as JSON too so UX changes can be re-applied to old pages (--rerender).
    runs = out / "runs"
    runs.mkdir(exist_ok=True)
    migrate_runs(out)
    if recorded:
        (runs / f"{session}.json").write_text(json.dumps(payload, separators=(",", ":")))
        (runs / f"{session}.html").write_text(render_page(payload, "../"))
    write_manifest(out)
    with open(out / "latest.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        for r in sorted(rows, key=lambda r: -r["roc"]):
            w.writerow([("" if r.get(c) is None else (round(r[c], 4) if isinstance(r[c], float) else r[c]))
                        for c in CSV_COLS])
    print(f"{len(rows)} rows, {sum(r['passes'] for r in rows)} passing -> {out / 'index.html'}", file=sys.stderr)


if __name__ == "__main__":
    main()
