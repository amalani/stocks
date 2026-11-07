# CSP strategy notes and reasoning

Reference for *why* the screener's rules are what they are. Written from a design discussion, Nov 2026. Personal notes, not
financial advice; thresholds are starting points, not validated rules. Code defaults are CLI flags in `screener.py`.

## Where each rule came from

| Rule | Source | Flag / default |
|---|---|---|
| Delta ≤ 0.20, ~1-3 weeks, mid price, limit orders | Owner's rule | `--delta 0.20`, per-ticker override in the watchlist |
| Annualized ROC as the main comparison | Owner's rule | `mid / strike × 365 / DTE` |
| IV Rank > 50 preferred | Owner's rule | `--ivr-min 50` |
| Avoid earnings before expiry | Owner's rule | Finnhub dates |
| IV/RV ≥ 1.2 | **Claude's stand-in** while IV Rank has no history. Not backed by data; too strict for mega-caps | `--ivrv-min 1.2` |
| 25% annualized hurdle | Owner's "hurdle" request | `--min-roc 25` |
| 15% hurdle for INCOME names | Reasoned below | `--income-min-roc 15` |

## Terms

- **Annualized ROC** = premium ÷ strike × 365 ÷ DTE. A simple yearly scaling for comparing trades of different length. It
  assumes you could repeat the trade every period and ignores assignment, so it is not a promised return.
- **IV Rank** = where today's IV sits between that stock's own low and high in stored history (0-100). Tradier has no IV
  history, so the screener stores a daily ATM IV in SQLite and shows "–" until 20 days exist. Backfilling history (Barchart /
  Market Chameleon) would make it work from day one.
- **IV/RV** = ATM IV ÷ 30-day realized vol. Used only while IV Rank is unavailable. RV over 30 days is noisy: a quiet month
  makes IV/RV look high, a month with an earnings jump makes it look low. Mega-caps usually sit near or below 1.0.
- **Expected move (EM)** = spot × IV × √(DTE/365). "vs EM" is the cushion in units of EM.

## Why the IV gate does not suit steady names

The gate asks "is the market overpaying for risk right now?" That suits opportunistic selling. Regular income on names like
GOOG/MSFT/COST is a different goal: sell on a schedule and accept the premium on offer. Low IV there means smaller premium,
which the yield hurdle already measures, so a separate IV test double-counts. Where richness still matters is relative to the
stock's own history (IV Rank with a loose floor, ~20-30), not a fixed IV/RV ratio.

## Choosing the yield hurdle

A short put is equity exposure: same downside below the strike, upside capped at the premium. So compare it with owning the
stock (8-14%/yr expected), plus T-bills on the collateral (~4%), not with "cash vs market".

Worked example (GOOG $265 put, 14 days, $26,500 collateral, $227 premium): 0.86% = 22% annualized. T-bills on the cash ≈ $40.
Stock drift at 8-14%/yr ≈ $80-140 over two weeks. The put beats average drift but loses to the stock in strong rallies, and a
10% drop costs ~$2,650 ≈ twelve trades' premium.

Working numbers: ~15% annualized for stable names (above the top of the equity range), 12% floor, 20%+ good. Volatile names
(AMD, MU, SNDK) keep 25-30%+ and the IV gate, since the swings are what pay.

## Tiers

| Tier | Names | Delta | Hurdle | IV gate |
|---|---|---|---|---|
| Income (`INCOME` tag) | GOOG, MSFT, COST | up to 0.25 | 15% | off |
| Volatile | AMD, MU, SNDK | 0.20 or lower | 25-30%+ | on |
| Wheel (`@own price`) | names you want to own | strike by price | global | information only |

Wheel picks the strike at or just under a price you would happily own at; Income picks by delta for premium. For stable names
that you are fine holding they overlap, but they are separate views. Watchlist syntax: `GOOG 0.25 INCOME`, `AMD 0.30 @480`,
`AMD $465` (cost basis for covered calls). Sheet columns: Ticker, Max delta, Own price, Cost basis, Income/Style/Profile.

## Managing a short put

Conventions, not guarantees. The screener shows them per row ("Manage it"); the tracker has the live version.

- **Take profit** at 50% of the premium captured (buy back at mid × 0.5). Tracker already flags 80% / 90%.
- **Stop** when the put costs 2× the premium you took (loss ≈ 1× premium). `--stop-mult`.
- **Tested** when the stock is within ~2% of the strike (or put delta ≥ ~0.35-0.40): decide to roll or close. `--tested-pct`.
- **Roll only for a net credit.** A roll that costs money, or just pushes a loss out, avoids taking the loss. On a stable name
  a credit roll is reasonable; on a volatile name that is down hard it keeps you in the position that is hurting.
- **Gaps skip every rule.** An overnight -30% on news blows through a 2× stop. For volatile names the protection is size
  (contracts sold), e.g. a 50% drop on a $200 strike is $10,000 per contract. Check total collateral and correlated names too.

## Experience that motivated this

A few bad trades on very volatile names (SNDK -50%, not caught in time) erased 60-70% of several months' profit; steady
range-bound names went well. Short premium pays small frequent wins and occasional large losses, and a few months in a rising
market is too short to show an edge. Hence: income profile for steady names, stricter gates and sizing for volatile ones, and
explicit buy-back levels.

## Tracker (Google Sheet) notes

Chain Summary already has live price, live delta, ITM/OTM, Premium Captured % and a Recommendation column (`AR`). It lacked a
loss stop; the formula with a `STOP` branch is in `options-tracker-sheet/README.md`. Paste it into the sheet by hand.

## Pick logic

The screened pick is the highest-delta put at or under the target **among strikes that pass the liquidity bar**
(OI ≥ `--min-oi`, spread ≤ `--max-spread`). Odd strikes (e.g. NVDA 172.5) often have no open interest; the neighbours are the
real market. If nothing is liquid the top strike is kept and fails the Liquid check; `skipped_strike` records a passed-over one.
The Wheel view does not apply this liquidity preference yet.
