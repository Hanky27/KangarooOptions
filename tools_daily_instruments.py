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

"""Compare instruments at DAILY resolution.

The hourly option-bar coverage of the calmer ETFs is too thin to run the
grid (measured: DIA, XLP and TLT-long abort with "no tradable credit
spread"). Daily option bars exist for far more contracts, so this runs the
same grid, the same parameters and the same linearity metric on the daily
store instead.

Usage: python tools_daily_instruments.py SPY TLT DIA XLP
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "C:/Users/HMz/Documents/Source/QuantroTrader")

import backtest_options as bt
from fetch_alpaca_bars import fetch
from SignalEngine.optimizer.equity_shape import equity_linearity

HERE = os.path.dirname(os.path.abspath(__file__))
START, END = "2024-02-01", "2026-08-27"


def daily_bars(symbol: str) -> list[dict]:
    """Daily store for one symbol, fetched and cached like the SPY one."""
    path = os.path.join(HERE, "data", f"{symbol.lower()}_daily.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            rows = json.load(fh)["bars"]
    else:
        rows = fetch(symbol, "1Day", START, END)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"bars": rows}, fh)
        os.replace(tmp, path)
        print(f"  {symbol}: daily store written, {len(rows)} bars")
    return [b for b in rows if b["t"][:10] >= START]


def measure(symbol: str, start_long: bool, **overrides) -> dict:
    params = dict(bt.DEFAULT_PARAMS)
    params["start_long"] = start_long
    params.update({k: v for k, v in overrides.items() if k in params})
    run_kw = {k: v for k, v in overrides.items() if k not in params}
    res = bt.run(daily_bars(symbol), params, dte_min=4, dte_max=10,
                 cost_usd=run_kw.pop("cost_usd"),
                 underlying=symbol, style="short_premium_spreads",
                 regime="same", timeframe="1Day", **run_kw)
    eq = [r + o for (_, r, o) in res["equity"]]
    peak, dd = float("-inf"), 0.0
    for v in eq:
        peak = max(peak, v)
        dd = min(dd, v - peak)
    cl = res["clusters"]
    r = equity_linearity([c["end"] for c in cl],
                         [c["result_usd"] for c in cl],
                         start_time=cl[0]["start"]) if cl else None
    return {"net": round(res["realized_total"]), "dd": round(dd),
            "r": None if r is None else round(r, 4), "clusters": len(cl),
            "margin": round(res["stats"]["max_margin"]),
            "worst": round(min((c["result_usd"] for c in cl), default=0))}


def _cost_from_argv(argv: list[str], usage: str) -> float:
    """--cost-usd, required, removed from argv in place.

    No default: this tool's output is read as evidence, and it was read as
    evidence for two and a half years of runs in which execution was free.
    """
    if "--cost-usd" not in argv:
        raise SystemExit(usage)
    i = argv.index("--cost-usd")
    try:
        value = float(argv[i + 1])
    except (IndexError, ValueError):
        raise SystemExit(usage)
    del argv[i:i + 2]
    return value


USAGE = ("usage: tools_daily_instruments.py --cost-usd <USD> [SYMBOL ...]"
         "\n\nMeasured on the live book 2026-09-01: half the bid/ask is "
         "34.95 USD per\nspread per crossing. Pass 0 explicitly for a "
         "frictionless run.")


if __name__ == "__main__":
    _argv = sys.argv[1:]
    COST_USD = _cost_from_argv(_argv, USAGE)
    syms = _argv or ["SPY", "TLT", "DIA", "XLP"]
    print(f"Daily grid, {START}..{END} (2,5 Jahre), max_adverse_pct=5, "
          f"spread_width_pct=0.71, cost {COST_USD:.2f} USD/spread/execution")
    for sym in syms:
        for side, sl in (("long", True), ("short", False)):
            t = time.time()
            try:
                m = measure(sym, sl, max_adverse_pct=5.0,
                            spread_width_pct=0.71, cost_usd=COST_USD)
                print(f"  {sym:4s} {side:5s}: net {m['net']:+7,.0f} | "
                      f"DD {m['dd']:+8,.0f} | r {m['r']} | "
                      f"{m['clusters']:3d} Cluster | Margin {m['margin']:6,.0f} | "
                      f"schlimmster {m['worst']:+7,.0f}  [{time.time()-t:.0f}s]",
                      flush=True)
            except Exception as exc:
                print(f"  {sym:4s} {side:5s}: {str(exc)[:100]}  "
                      f"[{time.time()-t:.0f}s]", flush=True)
