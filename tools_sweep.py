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

"""One measuring device for every grid variant, so numbers stay comparable.

Evaluates a parameter set on BOTH hourly windows and BOTH directions and
returns net, equity drawdown, worst cluster and the peak bound margin per
cell. The score deliberately carries three separate numbers instead of one
blend - a single figure hides which side paid for the other.

Windows: 2025 (whole calendar year) and 2026-01-01..2026-08-27. Both are
built from the same RTH hourly store with the exchange-calendar filter, so
early-close days carry no untradable bars.

Usage:
    from tools_sweep import evaluate, WINDOWS
    print(evaluate(rebuy_pct=0.6, max_invest_count=8))
"""

from __future__ import annotations

import json
import os
import sys

import backtest_options as bt

# The house has ONE implementation of the balance-curve shape metric
# (QuantroTrader CLAUDE.md REGEL 9.-1 [EVEN-EQUITY]); reimplementing a
# second Pearson here would be exactly the drift that rule exists to stop.
_SIGNAL_ENGINE = "C:/Users/HMz/Documents/Source/QuantroTrader"
if _SIGNAL_ENGINE not in sys.path:
    sys.path.insert(0, _SIGNAL_ENGINE)
from SignalEngine.optimizer.equity_shape import (equity_linearity,
                                                 linearity_factor)

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = ("C:/Users/HMz/AppData/Local/Temp/claude/"
           "C--Users-HMz-Documents-Source/"
           "83cbb501-aa6b-40fa-86f6-dbbcce01c313/scratchpad")

WINDOWS = {
    "2025": os.path.join(SCRATCH, "spy_1h_2025.json"),
    "2026": os.path.join(HERE, "data", "spy_1h.json"),
    "2w": os.path.join(HERE, "data", "spy_1h.json"),
}

# Other underlyings are sliced out of their own hourly store, which
# run_engine._ensure_hour_bars builds with the exchange-calendar filter.
WINDOW_RANGE = {"2025": ("2025-01-01", "2025-12-31"),
                "2026": ("2026-01-01", "2026-08-27"),
                # The last two trading weeks before the contest, i.e. the
                # closest thing to the judged window (Mon 31.08 - Thu 03.09).
                # Ten trading days is far too short for a stable estimate -
                # every number from it carries that caveat.
                "2w": ("2026-08-14", "2026-08-27")}

# Weight of the curve-shape multiplier in the combined objective. 1.0 is
# the value the spec documents as the plain product (OPTIMIZE_SPEC.md:433
# "fitness_shaped = fitness_ctrader * max(r, 0) ** equityLinearityExponent").
LINEARITY_EXPONENT = 1.0

_BARS: dict[tuple[str, str], list] = {}


def bars(window: str, symbol: str = "SPY") -> list:
    key = (window, symbol.upper())
    if key not in _BARS:
        if symbol.upper() == "SPY":
            path = WINDOWS[window]
            with open(path, "r", encoding="utf-8") as fh:
                rows = json.load(fh)["bars"]
            # Date-bound, not file-bound: run_engine refetches this very
            # store whenever the GUI is pointed at another range, so a
            # window defined by "whatever the file holds" would silently
            # change under a GUI run.
            lo, hi = WINDOW_RANGE[window]
            rows = [b for b in rows if lo <= b["t"][:10] <= hi]
        else:
            path = os.path.join(HERE, "data", f"{symbol.lower()}_1h.json")
            with open(path, "r", encoding="utf-8") as fh:
                rows = json.load(fh)["bars"]
            lo, hi = WINDOW_RANGE[window]
            rows = [b for b in rows if lo <= b["t"][:10] <= hi]
        if not rows:
            raise RuntimeError(f"no hourly bars for {symbol} in {window}")
        _BARS[key] = rows
    return _BARS[key]


def run_cell(window: str, start_long: bool, **overrides) -> dict:
    """One window, one direction. Returns the four numbers that matter."""
    params = dict(bt.DEFAULT_PARAMS)
    params["start_long"] = start_long
    grid = {k: v for k, v in overrides.items() if k in params}
    run_kw = {k: v for k, v in overrides.items() if k not in params}
    params.update(grid)
    symbol = str(run_kw.pop("underlying", "SPY")).upper()
    res = bt.run(bars(window, symbol), params,
                 dte_min=run_kw.pop("dte_min", 4),
                 dte_max=run_kw.pop("dte_max", 10),
                 # KeyError rather than a default: a sweep that forgot to
                 # price execution is the exact mistake this is guarding.
                 cost_usd=run_kw.pop("cost_usd"),
                 underlying=symbol, style="short_premium_spreads",
                 regime=run_kw.pop("regime", "same"),
                 timeframe="1Hour", **run_kw)
    eq = [r + o for (_, r, o) in res["equity"]]
    peak, dd = float("-inf"), 0.0
    for v in eq:
        peak = max(peak, v)
        dd = min(dd, v - peak)
    # Pearson r of the REALIZED balance against time, one point per closed
    # cluster, anchored at the first cluster's start so the run-up counts as
    # elapsed time. The window length belongs to every r that is reported:
    # 2025 = one calendar year, 2026 = 2026-01-01..08-27.
    clusters = res["clusters"]
    r_lin = None
    if clusters:
        r_lin = equity_linearity([c["end"] for c in clusters],
                                 [c["result_usd"] for c in clusters],
                                 start_time=clusters[0]["start"])
    return {
        "net": round(res["realized_total"], 0),
        "dd": round(dd, 0),
        "r": None if r_lin is None else round(r_lin, 4),
        "worst": round(min((c["result_usd"] for c in res["clusters"]),
                           default=0.0), 0),
        "clusters": len(res["clusters"]),
        "margin": round(res["stats"]["max_margin"], 0),
    }


def evaluate(**overrides) -> dict:
    """All four cells plus the symmetry and risk summary.

    asymmetry = |net(long) - net(short)| summed over the windows: how far
    apart the two sides land. worst_dd = the deepest equity drawdown of any
    cell - the number that decides whether the account survives.
    """
    cells = {}
    for window in overrides.pop("windows", ("2025", "2026")):
        for side, start_long in (("long", True), ("short", False)):
            cells[f"{window}_{side}"] = run_cell(window, start_long,
                                                 **overrides)
    nets = {k: v["net"] for k, v in cells.items()}
    asym = sum(abs(nets[f"{w}_long"] - nets[f"{w}_short"])
               for w in {k.rsplit("_", 1)[0] for k in nets})
    rs = [v["r"] for v in cells.values() if v["r"] is not None]
    # "Straight AND profitable" is the house objective from REGEL 9.-1
    # [EVEN-EQUITY]: fitness = profit * max(r, 0) ** exponent, with the
    # multiplier taken from the same module as r itself. A cell whose curve
    # falls contributes zero instead of flipping the sign of the sum, and a
    # cell whose r is not measurable (too few clusters) is left out of the
    # score and counted separately, never silently as zero.
    shaped, unscored = 0.0, 0
    for v in cells.values():
        if v["r"] is None:
            unscored += 1
            continue
        shaped += v["net"] * linearity_factor(v["r"], LINEARITY_EXPONENT)
    return {
        "params": overrides,
        "cells": cells,
        "r_min": round(min(rs), 4) if rs else None,
        "r_mean": round(sum(rs) / len(rs), 4) if rs else None,
        "net_total": sum(nets.values()),
        "shaped": round(shaped),
        "cells_without_r": unscored,
        "asymmetry": asym,
        "worst_dd": min(v["dd"] for v in cells.values()),
        "worst_cluster": min(v["worst"] for v in cells.values()),
        "max_margin": max(v["margin"] for v in cells.values()),
        "all_sides_positive": all(v > 0 for v in nets.values()),
    }


def show(label: str, r: dict) -> None:
    """One line per parameter set, with every measured cell spelled out."""
    cells = " ".join(
        f"{k.replace('_long', 'L').replace('_short', 'S')} {v['net']:+7,.0f}"
        f"(r{'  n/a' if v['r'] is None else f'{v[chr(114)]:+.2f}'})"
        for k, v in r["cells"].items())
    print(f"{label:34s} SCORE {r['shaped']:+8,.0f} | r_min {str(r['r_min']):>7s} | net {r['net_total']:+8,.0f} "
          f"| schlechteste DD {r['worst_dd']:+8,.0f} | Asym {r['asymmetry']:7,.0f} "
          f"| Margin {r['max_margin']:6,.0f} | {cells}", flush=True)


if __name__ == "__main__":
    show("Basis", evaluate())
