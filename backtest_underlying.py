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

"""Stage-1 edge check: Kangaroo grid mechanics on the UNDERLYING itself.

This deliberately does NOT price options. Each leg is modeled as a
directional block of `contract_multiplier` (100) underlying units with
delta = +/-1, zero theta, zero spread and zero commission - the UPPER BOUND
of what any long-option implementation of the same grid can achieve. If this
upper bound shows no edge, the options variant cannot have one either.

Reuses KangarooCore unchanged for all decisions (rebuy trigger, take-profit
threshold, Mode1 toggle). Daily bars are replayed as four pseudo-ticks in
cTrader bar-backtest order (bullish: O H L C, bearish: O L H C), the same
scheme the FX twin kangaroo.py uses on M1 bars.

No end-of-simulation liquidation: a cluster still open on the last bar is
reported as an OPEN book (marked at the last close), never booked as trades.

Usage:
  python backtest_underlying.py [--bars data/spy_daily.json] [--out data]
"""

from __future__ import annotations

import argparse
import csv
import json
import os

from kangaroo_core import KangarooCore

DEFAULT_PARAMS = dict(rebuy_1st_pct=1.2, rebuy_pct=0.10, tp_pct=0.10,
                      initial_qty=1, max_invest_count=20, start_long=True)

# Data-quality gate. Largest REAL daily range in this dataset is 11.27 %
# (2025-04-09); a bar beyond RANGE_BLOWOUT_PCT is a data defect, not a move.
# Unknown defective bars abort the run (fail loud). Known ones are skipped
# LOUDLY and documented here with the evidence.
RANGE_BLOWOUT_PCT = 30.0
KNOWN_DEFECT_DAYS = {
    # L=69.00 against O=689.58/H=696.93/C=695.41 -> 910 % range, a
    # decimal-shift artifact in the Alpaca daily bar. Second source
    # (Tiingo EOD) shows a normal day with the same close 695.41.
    "2026-02-02": "low 69.00 is a decimal-shift artifact; Tiingo confirms close 695.41",
}


def pseudo_ticks(bar: dict) -> tuple:
    o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
    return (o, h, l, c) if c >= o else (o, l, h, c)


def leg_profit(is_long: bool, entry: float, px: float, qty: int, mult: int) -> float:
    d = (px - entry) if is_long else (entry - px)
    return d * mult * qty


def run(bars: list[dict], params: dict) -> dict:
    core = KangarooCore(**params)
    mult = core.contract_multiplier

    clusters = []          # closed clusters
    equity_curve = []      # (date, realized, open_mark, notional)
    realized_total = 0.0
    cluster_start_ts = None
    cluster_min_unreal = 0.0

    def cluster_state_profit(px: float) -> float:
        return sum(leg_profit(core.is_long, leg.entry_underlying, px, leg.qty, mult)
                   for leg in core.legs)

    for bar in bars:
        ts = bar["t"][:10]
        range_pct = (bar["h"] - bar["l"]) / bar["l"] * 100.0
        if range_pct > RANGE_BLOWOUT_PCT:
            if ts in KNOWN_DEFECT_DAYS:
                print(f"DATA-DEFECT skip {ts}: range {range_pct:.1f} % - "
                      f"{KNOWN_DEFECT_DAYS[ts]}")
                continue
            raise RuntimeError(
                f"range blowout on {ts}: {range_pct:.1f} % > "
                f"{RANGE_BLOWOUT_PCT} % - unverified data defect, refusing to "
                f"simulate (add to KNOWN_DEFECT_DAYS only with evidence)")
        for px in pseudo_ticks(bar):
            if core.legs:
                profit = cluster_state_profit(px)
                cluster_min_unreal = min(cluster_min_unreal, profit)
                if core.check_close(profit, px, px):
                    realized_total += profit
                    clusters.append({
                        "cluster_id": core.cluster_id,
                        "direction": "long" if core.is_long else "short",
                        "start": cluster_start_ts,
                        "end": ts,
                        "legs": core.invest_count,
                        "qty_total": sum(l.qty for l in core.legs),
                        "profit_usd": round(profit, 2),
                        "min_unrealized_usd": round(cluster_min_unreal, 2),
                    })
                    core.on_cluster_closed()
                    cluster_start_ts = None
                    cluster_min_unreal = 0.0
                    continue  # never open on the tick that closed (original rule)
            qty = core.check_rebuy(px, px)
            if qty:
                if not core.legs:
                    cluster_start_ts = ts
                core.add_leg(f"UND_c{core.cluster_id}_l{core.invest_count}",
                             qty, core.entry_reference(px, px), px)

        close = bar["c"]
        open_mark = cluster_state_profit(close) if core.legs else 0.0
        notional = sum(l.qty for l in core.legs) * mult * close if core.legs else 0.0
        equity_curve.append((ts, realized_total, open_mark, notional))

    open_book = None
    if core.legs:
        last = bars[-1]
        open_book = {
            "direction": "long" if core.is_long else "short",
            "start": cluster_start_ts,
            "legs": core.invest_count,
            "qty_total": sum(l.qty for l in core.legs),
            "mark_usd": round(cluster_state_profit(last["c"]), 2),
            "min_unrealized_usd": round(cluster_min_unreal, 2),
        }
    return {"clusters": clusters, "equity_curve": equity_curve,
            "realized_total": realized_total, "open_book": open_book}


def summarize(result: dict) -> None:
    clusters = result["clusters"]
    curve = result["equity_curve"]

    # Equity = realized + open mark, daily close granularity.
    eq = [r + o for (_, r, o, _) in curve]
    peak, max_dd = float("-inf"), 0.0
    for v in eq:
        peak = max(peak, v)
        max_dd = min(max_dd, v - peak)

    print(f"bars: {len(curve)}  ({curve[0][0]} .. {curve[-1][0]})")
    print(f"closed clusters: {len(clusters)}   "
          f"(long {sum(1 for c in clusters if c['direction']=='long')}, "
          f"short {sum(1 for c in clusters if c['direction']=='short')})")
    print(f"realized total: {result['realized_total']:.2f} USD "
          f"(every grid TP close is a win by construction)")
    if clusters:
        durations = [(c["start"], c["end"]) for c in clusters]
        legs = [c["legs"] for c in clusters]
        worst = min(c["min_unrealized_usd"] for c in clusters)
        print(f"legs per closed cluster: max {max(legs)}, "
              f"avg {sum(legs)/len(legs):.2f}")
        print(f"worst intra-cluster drawdown (closed): {worst:.2f} USD")
    print(f"max notional exposure: {max(n for (_, _, _, n) in curve):,.0f} USD")
    print(f"equity max drawdown (realized + open mark, daily): {max_dd:.2f} USD")
    per_year: dict[str, float] = {}
    for c in clusters:
        per_year[c["end"][:4]] = per_year.get(c["end"][:4], 0.0) + c["profit_usd"]
    print("realized per year:", {y: round(v, 2) for y, v in sorted(per_year.items())})
    if result["open_book"]:
        print(f"OPEN cluster at end (not booked): {result['open_book']}")
    else:
        print("no open cluster at end")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", default="data/spy_daily.json")
    parser.add_argument("--out", default="data")
    for key, val in DEFAULT_PARAMS.items():
        parser.add_argument(f"--{key}", type=type(val), default=val)
    args = parser.parse_args()

    with open(args.bars, "r", encoding="utf-8") as fh:
        body = json.load(fh)
    bars = body["bars"]
    params = {k: getattr(args, k) for k in DEFAULT_PARAMS}
    print(f"params: {params}")

    result = run(bars, params)
    summarize(result)

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "clusters_underlying.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "cluster_id", "direction", "start", "end", "legs",
            "qty_total", "profit_usd", "min_unrealized_usd"])
        writer.writeheader()
        writer.writerows(result["clusters"])
    print(f"clusters written: {path}")


if __name__ == "__main__":
    main()
