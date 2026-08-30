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

import backtest_options as bt

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = ("C:/Users/HMz/AppData/Local/Temp/claude/"
           "C--Users-HMz-Documents-Source/"
           "83cbb501-aa6b-40fa-86f6-dbbcce01c313/scratchpad")

WINDOWS = {
    "2025": os.path.join(SCRATCH, "spy_1h_2025.json"),
    "2026": os.path.join(HERE, "data", "spy_1h.json"),
}

_BARS: dict[str, list] = {}


def bars(window: str) -> list:
    if window not in _BARS:
        with open(WINDOWS[window], "r", encoding="utf-8") as fh:
            _BARS[window] = json.load(fh)["bars"]
    return _BARS[window]


def run_cell(window: str, start_long: bool, **overrides) -> dict:
    """One window, one direction. Returns the four numbers that matter."""
    params = dict(bt.DEFAULT_PARAMS)
    params["start_long"] = start_long
    grid = {k: v for k, v in overrides.items() if k in params}
    run_kw = {k: v for k, v in overrides.items() if k not in params}
    params.update(grid)
    res = bt.run(bars(window), params,
                 dte_min=run_kw.pop("dte_min", 4),
                 dte_max=run_kw.pop("dte_max", 10),
                 underlying="SPY", style="short_premium_spreads",
                 regime=run_kw.pop("regime", "same"),
                 timeframe="1Hour", **run_kw)
    eq = [r + o for (_, r, o) in res["equity"]]
    peak, dd = float("-inf"), 0.0
    for v in eq:
        peak = max(peak, v)
        dd = min(dd, v - peak)
    return {
        "net": round(res["realized_total"], 0),
        "dd": round(dd, 0),
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
    for window in ("2025", "2026"):
        for side, start_long in (("long", True), ("short", False)):
            cells[f"{window}_{side}"] = run_cell(window, start_long,
                                                 **overrides)
    nets = {k: v["net"] for k, v in cells.items()}
    asym = (abs(nets["2025_long"] - nets["2025_short"])
            + abs(nets["2026_long"] - nets["2026_short"]))
    return {
        "params": overrides,
        "cells": cells,
        "net_total": sum(nets.values()),
        "asymmetry": asym,
        "worst_dd": min(v["dd"] for v in cells.values()),
        "worst_cluster": min(v["worst"] for v in cells.values()),
        "max_margin": max(v["margin"] for v in cells.values()),
        "all_sides_positive": all(v > 0 for v in nets.values()),
    }


def show(label: str, r: dict) -> None:
    c = r["cells"]
    print(f"{label:34s} net {r['net_total']:+8,.0f} | schlechteste DD "
          f"{r['worst_dd']:+8,.0f} | Asymmetrie {r['asymmetry']:8,.0f} | "
          f"Margin {r['max_margin']:6,.0f} | "
          f"25L {c['2025_long']['net']:+7,.0f} 25S {c['2025_short']['net']:+7,.0f} "
          f"26L {c['2026_long']['net']:+7,.0f} 26S {c['2026_short']['net']:+7,.0f}",
          flush=True)


if __name__ == "__main__":
    show("Basis", evaluate())
