# MIT License
# Copyright (c) 2026 Heinrich Munz
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Per-instrument search for the objective of the judged week.

The contest is scored over ONE week, so the target is not the same as in
tools_sweep.py. There the objective was a straight equity curve over months
(REGEL 9.-1 [EVEN-EQUITY]); here it is the number of WINNING clusters per
week, because a curve that needs months to straighten has no time to do so
in four trading days.

Headline number: `wins_per_week`. Guard: `net > 0`. Without that guard the
ranking would crown the martingale failure mode - many small take-profits
funded by one cluster that never closed. `worst` (the deepest single
cluster) and `open_at_end` (clusters still open when the window stops) are
reported next to it, because a run that ends holding an underwater grid has
not earned the wins it shows.

Every number is a MEASUREMENT of the simulator over real option closes; a
ten-day window is far too short for a stable estimate and that caveat
belongs to every figure printed here.

Usage:
    python tools_week.py SPY            # one symbol, the whole grid
    python tools_week.py SPY QQQ IWM    # several
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date

import backtest_options as bt

HERE = os.path.dirname(os.path.abspath(__file__))

# The last two full trading weeks before the contest. Fri 2026-08-28 is the
# most recent session with settled option closes; the judged window starts
# Mon 2026-08-31.
WINDOW = ("2026-08-14", "2026-08-28")
# A second, shorter window of the same data: the single most recent week.
# A parameter set that wins on both is less likely to be a fit to one week.
WINDOW_LAST = ("2026-08-24", "2026-08-28")

TRADING_DAYS_PER_WEEK = 5.0

# Every grid parameter a run depends on. A generated config names all of
# them, so a config file is a complete description of the measurement
# behind it and cannot be re-run against a different default.
CORE_KEYS = ("rebuy_1st_pct", "rebuy_pct", "tp_pct", "initial_qty",
             "max_invest_count", "max_adverse_pct")


def bars(symbol: str, window: tuple[str, str]) -> list[dict]:
    """RTH hourly bars of one symbol inside a date window."""
    path = os.path.join(HERE, "data", f"{symbol.lower()}_1h.json")
    with open(path, "r", encoding="utf-8") as fh:
        rows = json.load(fh)["bars"]
    lo, hi = window
    rows = [b for b in rows if lo <= b["t"][:10] <= hi]
    if not rows:
        raise RuntimeError(f"no hourly bars for {symbol} in {lo}..{hi}")
    return rows


def trend_pct(symbol: str, window: tuple[str, str]) -> float:
    """Move of the underlying across the window, in percent.

    "gerne auch im aktuellen trend": a rising underlying favours the long
    side (put credit spreads), a falling one the short side (call credit
    spreads). This is the measured move, not a forecast.
    """
    rows = bars(symbol, window)
    return (rows[-1]["c"] / rows[0]["c"] - 1.0) * 100.0


def run_cell(symbol: str, start_long: bool, window: tuple[str, str],
             cost_usd: float, **overrides) -> dict:
    """One symbol, one direction, one parameter set.

    cost_usd is positional and has no default: this function chooses the
    configs that go live, and it once chose 25 of them in a world where
    execution was free.
    """
    params = dict(bt.DEFAULT_PARAMS)
    params["start_long"] = start_long
    grid = {k: v for k, v in overrides.items() if k in params}
    run_kw = {k: v for k, v in overrides.items() if k not in params}
    params.update(grid)
    rows = bars(symbol, window)
    run_kw_dte_min = run_kw.pop("dte_min", 4)
    run_kw_dte_max = run_kw.pop("dte_max", 10)
    res = bt.run(rows, params,
                 dte_min=run_kw_dte_min,
                 dte_max=run_kw_dte_max,
                 cost_usd=cost_usd,
                 underlying=symbol, style="short_premium_spreads",
                 regime=run_kw.pop("regime", "same"),
                 timeframe="1Hour", **run_kw)
    clusters = res["clusters"]
    wins = [c for c in clusters if c["result_usd"] > 0]
    days = len({b["t"][:10] for b in rows})
    weeks = days / TRADING_DAYS_PER_WEEK
    eq = [r + o for (_, r, o) in res["equity"]]
    peak, dd = float("-inf"), 0.0
    for v in eq:
        peak = max(peak, v)
        dd = min(dd, v - peak)
    # Unrealized value still on the books when the window stops. A run that
    # banks its wins and leaves the losses open would otherwise look clean.
    open_at_end = res["equity"][-1][2] if res["equity"] else 0.0
    return {
        "symbol": symbol,
        "side": "long" if start_long else "short",
        # The grid parameters this cell actually ran with. They travel into
        # the generated config so no later loader can reinterpret them.
        "params": {k: params[k] for k in CORE_KEYS},
        "dte_min": run_kw_dte_min, "dte_max": run_kw_dte_max,
        # Travels into the generated config so the next reader knows what
        # this measurement was allowed to ignore.
        "cost_usd": cost_usd,
        "trades": len(clusters),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(clusters), 3) if clusters else None,
        "wins_per_week": round(len(wins) / weeks, 2),
        "trades_per_week": round(len(clusters) / weeks, 2),
        "net": round(res["realized_total"], 0),
        "open_at_end": round(open_at_end, 0),
        "worst": round(min((c["result_usd"] for c in clusters), default=0.0)),
        "dd": round(dd, 0),
        "margin": round(res["stats"]["max_margin"], 0),
        "days": days,
    }


# Candidate grid. Short DTE and a low take-profit cash target are what make
# a cluster close inside a four-day window; the long-DTE / high-TP corner
# that won the months-long objective cannot close often enough here.
DTE_CANDIDATES = [(1, 4), (2, 6), (4, 10)]
TP_CANDIDATES = [0.02, 0.04, 0.08]
SIDES = [True, False]


def search(symbol: str, cost_usd: float,
           window: tuple[str, str] = WINDOW) -> list[dict]:
    """Every candidate for one symbol, ranked by wins per week."""
    out = []
    for dte_min, dte_max in DTE_CANDIDATES:
        for tp in TP_CANDIDATES:
            for start_long in SIDES:
                try:
                    row = run_cell(symbol, start_long, window,
                                   cost_usd,
                                   dte_min=dte_min, dte_max=dte_max,
                                   tp_pct=tp, max_adverse_pct=5.0)
                except Exception as exc:          # noqa: BLE001
                    out.append({"symbol": symbol,
                                "side": "long" if start_long else "short",
                                "dte": f"{dte_min}-{dte_max}", "tp": tp,
                                "error": f"{type(exc).__name__}: {exc}"})
                    continue
                row["dte"] = f"{dte_min}-{dte_max}"
                row["tp"] = tp
                out.append(row)
    return out


USAGE = """usage: tools_week.py --cost-usd <USD> [SYMBOL ...]

--cost-usd is the execution cost per spread per EXECUTION, and it is
required. Measured on the live book 2026-09-01: half the bid/ask is 34.95
USD per spread per crossing. The 25 configs this project trades were all
chosen with it implicitly at 0. What charging it does has been measured on
three symbols only (SPY, META, QQQ) and it is not uniform - SPY turns from
+2,850 to -6,172.65 over 2.5 years, META stays positive. What is certain is
that none of them was ever measured against it.

Pass 0 for a frictionless run - explicitly."""


def main() -> int:
    argv = sys.argv[1:]
    if "--cost-usd" not in argv:
        print(USAGE)
        return 2
    i = argv.index("--cost-usd")
    try:
        cost_usd = float(argv[i + 1])
    except (IndexError, ValueError):
        print(USAGE)
        return 2
    del argv[i:i + 2]
    symbols = [s.upper() for s in argv] or ["SPY"]
    print(f"execution cost: {cost_usd:.2f} USD per spread per execution")
    for sym in symbols:
        t = trend_pct(sym, WINDOW)
        print(f"\n=== {sym}  (underlying {t:+.2f} % over "
              f"{WINDOW[0]}..{WINDOW[1]}) ===")
        rows = search(sym, cost_usd)
        good = [r for r in rows if "error" not in r]
        bad = [r for r in rows if "error" in r]
        good.sort(key=lambda r: (-r["wins_per_week"], -r["net"]))
        print(f"{'dte':>5} {'tp':>5} {'side':>5} {'w/wk':>6} {'t/wk':>6} "
              f"{'rate':>5} {'net':>8} {'open':>8} {'worst':>8} {'dd':>8} "
              f"{'margin':>8}")
        for r in good:
            print(f"{r['dte']:>5} {r['tp']:>5.2f} {r['side']:>5} "
                  f"{r['wins_per_week']:>6.2f} {r['trades_per_week']:>6.2f} "
                  f"{'-' if r['win_rate'] is None else r['win_rate']:>5} "
                  f"{r['net']:>8.0f} {r['open_at_end']:>8.0f} "
                  f"{r['worst']:>8.0f} {r['dd']:>8.0f} {r['margin']:>8.0f}")
        for r in bad:
            print(f"{r['dte']:>5} {r['tp']:>5.2f} {r['side']:>5}  {r['error']}")
        path = os.path.join(HERE, "data", f"week_{sym.lower()}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
