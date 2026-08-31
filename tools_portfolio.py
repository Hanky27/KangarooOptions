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

"""Run every config in configs/ over one window and add them up per BAR.

The per-instrument table answers "does this instrument have an edge". It
cannot answer the two questions that decide whether the account survives:

  1. How much margin do the instruments need AT THE SAME TIME? A sum of
     per-instrument peaks is an upper bound only - the peaks fall on
     different bars.
  2. How deep is the PORTFOLIO drawdown? Instruments that are individually
     shallow can still dip together, because they are all short premium on
     correlated US equities and one gap day moves all of them the same
     way.

Both need the series aligned by timestamp, which is what this does. Bars
missing for one instrument (a symbol with a shorter history) carry that
instrument's last known value forward, so the sum never silently drops a
position that is still open.

An explicit second window is the OUT-OF-SAMPLE check. The configs were
chosen on WINDOW by tools_make_configs.py, so their result there is
in-sample by construction and says nothing about a week the search never
saw.

Usage:
    python tools_portfolio.py                          # the search window
    python tools_portfolio.py 2026-08-03 2026-08-13    # a holdout
"""

from __future__ import annotations

import glob
import os
import sys

import yaml

import backtest_options as bt
from tools_week import WINDOW, bars

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(HERE, "configs")
ACCOUNT_USD = 100_000.0


def run_one(cfg: dict, window: tuple[str, str]) -> dict:
    params = dict(bt.DEFAULT_PARAMS)
    params["start_long"] = bool(cfg.get("start_long", True))
    for key in ("rebuy_1st_pct", "rebuy_pct", "tp_pct", "initial_qty",
                "max_invest_count", "max_adverse_pct"):
        if key in cfg:
            params[key] = cfg[key]
    sym = cfg["underlying"].upper()
    return bt.run(bars(sym, window), params,
                  dte_min=int(cfg["dte_min"]), dte_max=int(cfg["dte_max"]),
                  underlying=sym, style="short_premium_spreads",
                  regime="same", timeframe="1Hour")


def align(series_by_name: dict[str, list[tuple[str, float]]]) -> \
        tuple[list[str], dict[str, list[float]]]:
    """One row per stamp seen anywhere; a gap carries the last value."""
    stamps = sorted({t for s in series_by_name.values() for t, _ in s})
    out: dict[str, list[float]] = {}
    for name, series in series_by_name.items():
        by_stamp = dict(series)
        last, col = 0.0, []
        for t in stamps:
            last = by_stamp.get(t, last)
            col.append(last)
        out[name] = col
    return stamps, out


def main() -> int:
    window = (tuple(sys.argv[1:3]) if len(sys.argv) >= 3 else WINDOW)
    in_sample = window == WINDOW
    files = sorted(glob.glob(os.path.join(CONFIG_DIR, "*.yaml")))
    if not files:
        raise SystemExit("configs/ is empty - run tools_make_configs.py")
    equity_by, margin_by, rows = {}, {}, []
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        name = os.path.basename(path)[:-5]
        res = run_one(cfg, window)
        equity_by[name] = [(t, r + o) for (t, r, o) in res["equity"]]
        margin_by[name] = res["margin"]
        wins = [c for c in res["clusters"] if c["result_usd"] > 0]
        rows.append((name, len(res["clusters"]), len(wins),
                     res["realized_total"],
                     res["stats"]["max_margin"]))
        print(f"{name:14s} {len(res['clusters']):3d} clusters, "
              f"{len(wins):3d} wins, net {res['realized_total']:+9.0f}, "
              f"peak margin {res['stats']['max_margin']:8.0f}")

    stamps, eq_cols = align(equity_by)
    _, mg_cols = align(margin_by)
    total_eq = [sum(eq_cols[n][i] for n in eq_cols)
                for i in range(len(stamps))]
    total_mg = [sum(mg_cols[n][i] for n in mg_cols)
                for i in range(len(stamps))]
    peak, dd, dd_at = float("-inf"), 0.0, ""
    for t, v in zip(stamps, total_eq):
        peak = max(peak, v)
        if v - peak < dd:
            dd, dd_at = v - peak, t
    mg_peak = max(total_mg)
    mg_at = stamps[total_mg.index(mg_peak)]
    sum_of_peaks = sum(r[4] for r in rows)
    trades = sum(r[1] for r in rows)
    wins = sum(r[2] for r in rows)
    days = len({t[:10] for t in stamps})

    tag = ("IN SAMPLE - the configs were chosen on this window"
           if in_sample else "OUT OF SAMPLE")
    print(f"\n--- portfolio, {len(rows)} instruments, "
          f"{window[0]}..{window[1]} ({days} trading days), {tag} ---")
    print(f"clusters closed        {trades:6d}  "
          f"({trades / (days / 5.0):.1f} per week)")
    print(f"winning clusters       {wins:6d}  "
          f"({wins / (days / 5.0):.1f} per week, "
          f"{wins / trades * 100:.1f} %)")
    print(f"mark-to-market end  {total_eq[-1]:+9.0f} USD  "
          f"({total_eq[-1] / ACCOUNT_USD * 100:+.2f} % of the account)")
    print(f"portfolio drawdown  {dd:+9.0f} USD  at {dd_at}")
    print(f"margin, SIMULTANEOUS {mg_peak:8.0f} USD  at {mg_at}  "
          f"({mg_peak / ACCOUNT_USD * 100:.1f} % of the account)")
    print(f"margin, sum of peaks {sum_of_peaks:8.0f} USD  "
          f"(the upper bound the per-instrument table shows)")
    print("\nTen trading days is a short window and the instruments are all "
          "short premium on correlated US equities - the drawdown above is "
          "what happened, not a bound.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
